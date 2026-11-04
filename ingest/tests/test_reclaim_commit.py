"""Commit resume: lease arms, adopt or copy."""

from typing import Any

import pytest
from botocore.stub import ANY

from asteroidea_ingest import commit, sessions

NOW = 1000  # Harness.run's clock
CLAIM_AT = NOW - 720  # a delivery redelivered after the 720 s visibility
RESUME_CONDITION = (
    "#state = :expect AND #finalize_token = :token AND "
    "(attribute_not_exists(#lease_until) OR #lease_until < :now)"
)


def _resume_claim(h: Any) -> None:
    h.ddb_stub.add_response(
        "update_item",
        {},
        expected_params={
            "TableName": ANY,
            "Key": ANY,
            "UpdateExpression": ANY,
            "ExpressionAttributeNames": ANY,
            "ExpressionAttributeValues": ANY,
            "ConditionExpression": RESUME_CONDITION,
        },
    )


def _raw_head(h: Any, *, crc: str = "AAAAAAAAAAA=", version: str = "rv-old") -> None:
    h.s3_stub.add_response(
        "head_object",
        {"ContentLength": 100, "ChecksumCRC64NVME": crc, "VersionId": version},
        expected_params={
            "Bucket": "raw-bucket",
            "Key": "raw/ds1/sub-01/anat/sub-01_T1w.nii",
            "ChecksumMode": "ENABLED",
        },
    )
    if crc == "AAAAAAAAAAA=":
        h.lock(version=version)  # an adopted version is locked too


def _finish_commit(h: Any) -> None:
    h.update_ok()  # COMMITTED
    h.extract_sent()


def _accepted(h: Any, **extra: Any) -> None:
    h.get_item(state=sessions.ACCEPTED, landing_version_id="lv1", **extra)


def test_crash_after_copy_adopts_the_raw_version(harness: Any) -> None:
    h = harness
    _accepted(h, lease_until=CLAIM_AT + 150)
    h.head()  # landing
    _resume_claim(h)
    _raw_head(h)  # the earlier run's copy is there: adopt, no copy_object stubbed
    _finish_commit(h)
    assert h.run() == sessions.COMMITTED
    h.finish()
    (row,) = h.rows()
    assert row["s3_version_id"] == "rv-old"


def test_crash_after_claim_copies(harness: Any) -> None:
    h = harness
    _accepted(h, lease_until=CLAIM_AT + 150)
    h.head()
    _resume_claim(h)
    h.s3_stub.add_client_error("head_object", service_error_code="404")  # no raw
    h.copy(version="rv-new")
    _finish_commit(h)
    assert h.run() == sessions.COMMITTED
    h.finish()
    (row,) = h.rows()
    assert row["s3_version_id"] == "rv-new"


def test_a_raw_object_with_another_checksum_is_not_adopted(harness: Any) -> None:
    h = harness
    _accepted(h, lease_until=CLAIM_AT + 150)
    h.head()
    _resume_claim(h)
    _raw_head(h, crc=h.OTHER_CRC)
    h.copy(version="rv-new")
    _finish_commit(h)
    assert h.run() == sessions.COMMITTED
    h.finish()
    assert h.rows()[0]["s3_version_id"] == "rv-new"


def test_a_legacy_shaped_accepted_item_without_a_lease_is_resumed(harness: Any) -> None:
    h = harness
    _accepted(h)  # legacy claims set only landing_version_id: no lease_until
    h.head()
    _resume_claim(h)
    h.s3_stub.add_client_error("head_object", service_error_code="404")
    h.copy()
    _finish_commit(h)
    assert h.run() == sessions.COMMITTED
    h.finish()
    assert len(h.rows()) == 1


def test_redelivery_after_a_commit_timeout_reclaims_and_resumes(harness: Any) -> None:
    """Claimed at t, timed out at t + 120, redelivered at t + 720 (the visibility)."""
    h = harness
    _accepted(h, lease_until=CLAIM_AT + sessions.COMMIT_LEASE_SECONDS)
    assert CLAIM_AT + sessions.COMMIT_LEASE_SECONDS < NOW
    h.head()
    _resume_claim(h)
    h.s3_stub.add_client_error("head_object", service_error_code="404")
    h.copy()
    _finish_commit(h)
    assert h.run() == sessions.COMMITTED
    h.finish()


def test_a_lease_not_yet_expired_is_not_reclaimed(harness: Any) -> None:
    h = harness
    _accepted(h, lease_until=NOW + 1)  # another run holds the claim
    assert h.run() == commit.SKIPPED
    h.finish()  # nothing else was called
    assert h.rows() == []


def test_a_resume_that_loses_the_lease_race_stops(harness: Any) -> None:
    h = harness
    _accepted(h, lease_until=CLAIM_AT)
    h.head()
    h.ddb_stub.add_client_error(
        "update_item",
        service_error_code="ConditionalCheckFailedException",
        expected_params={
            "TableName": ANY,
            "Key": ANY,
            "UpdateExpression": ANY,
            "ExpressionAttributeNames": ANY,
            "ExpressionAttributeValues": ANY,
            "ConditionExpression": RESUME_CONDITION,
        },
    )
    assert h.run() == commit.LOST_CLAIM
    h.finish()


def test_every_claim_sets_a_fresh_lease(harness: Any) -> None:
    h = harness
    values: list[dict[str, Any]] = []
    h.ddb.meta.events.register(
        "before-parameter-build.dynamodb.UpdateItem",
        lambda params, **_: values.append(
            sessions.unmarshal(params["ExpressionAttributeValues"])
        ),
    )
    h.get_item()
    h.head()
    h.update_ok()
    h.copy()
    _finish_commit(h)
    assert h.run() == sessions.COMMITTED
    claim, committed = values
    assert claim[":lease_until"] == NOW + 150
    assert committed[":extract_lease_until"] == NOW + 600


@pytest.mark.parametrize(
    ("item", "claimable"),
    [
        ({"extract_state": "PENDING"}, True),
        ({"extract_state": "PENDING", "extract_lease_until": NOW + 99}, True),
        ({"extract_state": "EXTRACTING"}, True),  # no lease: claimable
        ({"extract_state": "EXTRACTING", "extract_lease_until": NOW - 1}, True),
        ({"extract_state": "EXTRACTING", "extract_lease_until": NOW + 1}, False),
        ({"extract_state": "FAILED"}, False),  # never retried
        ({"extract_state": "EXTRACTED"}, False),
        ({}, False),
    ],
)
def test_extract_claimable(item: dict[str, Any], *, claimable: bool) -> None:
    assert sessions.extract_claimable(item, NOW) is claimable


def test_extract_claim_condition_has_both_arms(harness: Any) -> None:
    h = harness
    h.ddb_stub.add_response(
        "update_item",
        {},
        expected_params={
            "TableName": h.TABLE,
            "Key": sessions.marshal({"session_id": "s"}),
            "UpdateExpression": ANY,
            "ConditionExpression": (
                "#extract_state = :pending OR (#extract_state = :extracting AND "
                "(attribute_not_exists(#extract_lease_until) OR "
                "#extract_lease_until < :now))"
            ),
            "ExpressionAttributeNames": ANY,
            "ExpressionAttributeValues": sessions.marshal(
                {
                    ":pending": "PENDING",
                    ":extracting": "EXTRACTING",
                    ":lease": NOW + 150,
                    ":now": NOW,
                }
            ),
        },
    )
    assert sessions.claim_extract(sessions.SessionTable(h.ddb, h.TABLE), "s", NOW)
    h.finish()


def test_a_refused_extract_claim_returns_false(harness: Any) -> None:
    h = harness
    h.ddb_stub.add_client_error(
        "update_item", service_error_code="ConditionalCheckFailedException"
    )
    assert not sessions.claim_extract(sessions.SessionTable(h.ddb, h.TABLE), "s", NOW)
    h.finish()
