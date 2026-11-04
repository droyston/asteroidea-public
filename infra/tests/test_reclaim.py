"""`make reclaim` selection: offline, no AWS."""

from typing import Any

import boto3
import pytest
from botocore.stub import Stubber

from asteroidea_infra import reclaim
from asteroidea_ingest import sessions

NOW = 10_000


def _item(**fields: Any) -> dict[str, Any]:
    return {"session_id": "s1", "finalize_token": "tok", **fields}


def _sends(items: list[dict[str, Any]]) -> list[reclaim.Resend]:
    sent: list[reclaim.Resend] = []
    reclaim.reclaim(items, NOW, send=sent.append)
    return sent


def test_accepted_past_its_lease_is_resent_to_the_commit_queue() -> None:
    (sent,) = _sends([_item(state="ACCEPTED", lease_until=NOW - 1)])
    assert sent == reclaim.Resend(
        "commit", {"session_id": "s1", "finalize_token": "tok"}, f"s1:{NOW - 1}"
    )


def test_a_legacy_shaped_accepted_item_without_a_lease_is_reclaimed() -> None:
    (sent,) = _sends([_item(state="ACCEPTED")])  # legacy claims set no lease_until
    assert sent.queue == "commit"
    assert sent.dedup_id == "s1:0"


def test_finalizing_past_its_lease_is_resent() -> None:
    (sent,) = _sends([_item(state="FINALIZING", lease_until=NOW - 1)])
    assert sent.queue == "commit"


def test_a_lease_not_yet_expired_is_not_resent() -> None:
    assert _sends([_item(state="ACCEPTED", lease_until=NOW + 1)]) == []
    assert _sends([_item(state="ACCEPTED", lease_until=NOW)]) == []  # not "past"


@pytest.mark.parametrize("kind", ["commit", "extract"])
def test_attempts_at_three_are_never_resent(kind: str) -> None:
    items = [
        _item(state="ACCEPTED", commit_attempts=3),
        _item(state="COMMITTED", extract_state="PENDING", extract_attempts=3),
    ]
    picked = [i for i in items if f"{kind}_attempts" in i]
    assert _sends(picked) == []
    below = {**picked[0], f"{kind}_attempts": 2}
    assert len(_sends([below])) == 1


def test_pending_or_stale_extracting_is_resent_to_the_extract_queue() -> None:
    sent = _sends(
        [
            _item(state="COMMITTED", extract_state="PENDING", extract_lease_until=5),
            _item(state="COMMITTED", extract_state="EXTRACTING"),
        ]
    )
    assert [(s.queue, s.body, s.dedup_id) for s in sent] == [
        ("extract", {"session_id": "s1"}, "s1:5"),
        ("extract", {"session_id": "s1"}, "s1:0"),
    ]


def test_a_failed_extraction_is_never_retried() -> None:
    assert _sends([_item(state="COMMITTED", extract_state="FAILED")]) == []
    reason = reclaim.select(_item(state="COMMITTED", extract_state="FAILED"), NOW)
    assert isinstance(reason, str)


@pytest.mark.parametrize(
    "item",
    [
        _item(state="OPEN"),
        _item(state="INCOMPLETE"),
        _item(state="COMMITTED"),  # legacy: no extract_state
        _item(state="COMMITTED", extract_state="EXTRACTED"),
        _item(state="COMMITTED", extract_state="EXTRACTING", extract_lease_until=NOW),
    ],
)
def test_other_items_are_left_alone(item: dict[str, Any]) -> None:
    assert _sends([item]) == []


def test_a_new_lease_gives_a_new_dedup_id() -> None:
    first = reclaim.select(_item(state="ACCEPTED", lease_until=100), NOW)
    second = reclaim.select(_item(state="ACCEPTED", lease_until=200), NOW)
    assert isinstance(first, reclaim.Resend)
    assert isinstance(second, reclaim.Resend)
    assert first.dedup_id != second.dedup_id


def test_dry_run_sends_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    chosen = reclaim.reclaim([_item(state="ACCEPTED")], NOW, send=None)
    assert len(chosen) == 1
    assert "would send commit s1" in capsys.readouterr().out


def _client(service: str) -> Any:
    return boto3.client(
        service,
        region_name="us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )


def test_scan_reads_every_page() -> None:
    ddb = _client("dynamodb")
    stub = Stubber(ddb)
    first = sessions.marshal({"session_id": "a", "state": "OPEN"})
    second = sessions.marshal({"session_id": "b", "state": "OPEN"})
    key = sessions.marshal({"session_id": "a"})
    stub.add_response(
        "scan", {"Items": [first], "LastEvaluatedKey": key}, {"TableName": "t"}
    )
    stub.add_response(
        "scan", {"Items": [second]}, {"TableName": "t", "ExclusiveStartKey": key}
    )
    with stub:
        items = reclaim.scan_items(sessions.SessionTable(ddb, "t"))
    assert [i["session_id"] for i in items] == ["a", "b"]
    stub.assert_no_pending_responses()


def test_dlq_depths_reads_both_dlqs() -> None:
    sqs = _client("sqs")
    stub = Stubber(sqs)
    for url, depth in (("c-dlq", "0"), ("e-dlq", "2")):
        stub.add_response(
            "get_queue_attributes",
            {"Attributes": {"ApproximateNumberOfMessages": depth}},
            {"QueueUrl": url, "AttributeNames": ["ApproximateNumberOfMessages"]},
        )
    with stub:
        depths = reclaim.dlq_depths(
            sqs, {"CommitDlqUrl": "c-dlq", "ExtractDlqUrl": "e-dlq"}
        )
    assert depths == {"commit": 0, "extract": 2}


# -- FORCE=1 -------------------------------------------------------------------


def _forced(
    item: dict[str, Any], *, writes: int
) -> tuple[reclaim.Expiry | str, list[dict[str, Any]]]:
    """Run `force_expire` on a DynamoDB stub that allows `writes` UpdateItem calls.

    With `writes=0` any call raises, so a refusal is proven to write nothing.
    """
    ddb = _client("dynamodb")
    updates: list[dict[str, Any]] = []
    ddb.meta.events.register(
        "before-parameter-build.dynamodb.UpdateItem",
        lambda params, **_: updates.append(dict(params)),
    )
    stub = Stubber(ddb)
    for _ in range(writes):
        stub.add_response("update_item", {})
    with stub:
        result = reclaim.force_expire(sessions.SessionTable(ddb, "t"), item, NOW)
    stub.assert_no_pending_responses()
    return result, updates


@pytest.mark.parametrize(
    ("item", "attribute", "guard"),
    [
        ({"state": "ACCEPTED", "lease_until": NOW + 99}, "lease_until", "ACCEPTED"),
        ({"state": "FINALIZING"}, "lease_until", "FINALIZING"),
        (
            {
                "state": "COMMITTED",
                "extract_state": "PENDING",
                "extract_lease_until": NOW + 500,
            },
            "extract_lease_until",
            "PENDING",
        ),
        (
            {"state": "COMMITTED", "extract_state": "EXTRACTING"},
            "extract_lease_until",
            "EXTRACTING",
        ),
    ],
)
def test_force_expires_only_the_resumable_sessions_lease(
    item: dict[str, Any], attribute: str, guard: str
) -> None:
    result, updates = _forced(_item(**item), writes=1)
    assert isinstance(result, reclaim.Expiry)
    assert result.attribute == attribute
    assert result.new == NOW - 1
    (update,) = updates
    values = sessions.unmarshal(update["ExpressionAttributeValues"])
    assert values[":new"] == NOW - 1
    assert guard in values.values()  # the write is conditional on the state it read
    assert update["UpdateExpression"] == "SET #lease = :new, #updated_at = :now"


@pytest.mark.parametrize(
    ("item", "reason"),
    [
        (
            {"state": "COMMITTED", "extract_state": "EXTRACTED"},
            "COMMITTED with extract_state EXTRACTED: nothing resumable",
        ),
        (
            {"state": "COMMITTED", "extract_state": "SKIPPED"},
            "COMMITTED with extract_state SKIPPED: nothing resumable",
        ),
        ({"state": "ACCEPTED", "commit_attempts": 3}, "commit_attempts reached 3"),
        (
            {"state": "COMMITTED", "extract_state": "PENDING", "extract_attempts": 3},
            "extract_attempts reached 3",
        ),
        ({"state": "OPEN"}, "state OPEN: not a resumable state"),
        ({"state": "INCOMPLETE"}, "state INCOMPLETE: not a resumable state"),
    ],
)
def test_force_refuses_with_the_reason_and_writes_nothing(
    item: dict[str, Any], reason: str
) -> None:
    result, updates = _forced(_item(**item), writes=0)
    assert result == reason
    assert updates == []


def test_force_a_pending_extract_leaves_the_commit_lease_alone() -> None:
    item = _item(
        state="COMMITTED",
        extract_state="PENDING",
        lease_until=7,
        extract_lease_until=NOW + 500,
    )
    result, updates = _forced(item, writes=1)
    assert isinstance(result, reclaim.Expiry)
    assert (result.attribute, result.old, result.new) == (
        "extract_lease_until",
        NOW + 500,
        NOW - 1,
    )
    assert "lease_until" not in sessions.unmarshal(
        updates[0]["ExpressionAttributeValues"]
    )


PENDING_AND_ZERO = {"PENDING", 0}


def test_force_re_runs_a_failed_extraction_on_purpose() -> None:
    """FAILED goes back to PENDING with fresh attempts, lease expired."""
    item = _item(
        state="COMMITTED",
        extract_state="FAILED",
        extract_attempts=3,
        extract_lease_until=NOW + 500,
    )
    result, updates = _forced(item, writes=1)
    assert isinstance(result, reclaim.Expiry)
    (update,) = updates
    values = sessions.unmarshal(update["ExpressionAttributeValues"])
    assert values[":new"] == NOW - 1
    assert "FAILED" in values.values()  # conditional on the state it read
    assert set(values.values()) >= PENDING_AND_ZERO
    assert "extract_state" in update["ExpressionAttributeNames"].values()
    assert "extract_attempts" in update["ExpressionAttributeNames"].values()
    # and it is now eligible for a re-send
    again = {**item, result.attribute: result.new, **result.extra}
    assert not isinstance(reclaim.select(again, NOW), str)
    assert "ReturnValues" not in update


@pytest.mark.parametrize("state", ["QUEUED", "DONE", "FAILED"])
def test_a_reextract_tracking_item_is_ignored_even_with_an_expired_lease(
    state: str,
) -> None:
    # `reextract_state`, never `state` or `extract_state`, so reclaim skips it.
    item = sessions.reextract_item("run1", "asset1", NOW - 10_000)
    item["reextract_state"] = state
    assert item["reextract_lease_until"] < NOW  # the lease is long past
    assert isinstance(reclaim.select(item, NOW), str)
    assert isinstance(reclaim.plan_force(item, NOW), str)
    assert _sends([item]) == []
