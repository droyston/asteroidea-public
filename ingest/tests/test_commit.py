"""Commit Lambda: claim-once, checksum before copy, check-before-append."""

from typing import Any

import pytest
from botocore.stub import ANY
from pyiceberg.exceptions import CommitStateUnknownException
from pyiceberg.table import Table

from asteroidea_ingest import commit, sessions
from asteroidea_ingest.catalog_writer import CatalogWriter


def test_happy_path_commits_one_row(harness: Any) -> None:
    h = harness
    h.get_item()
    h.head()
    h.update_ok()  # claim ACCEPTED
    h.copy()
    h.update_ok()  # COMMITTED
    h.extract_sent()
    assert h.run() == sessions.COMMITTED
    h.finish()
    (row,) = h.rows()
    assert row["s3_version_id"] == "rv1"
    assert row["s3_key"] == "raw/ds1/sub-01/anat/sub-01_T1w.nii"
    assert (row["subject"], row["datatype"], row["suffix"], row["extension"]) == (
        "sub-01",
        "anat",
        "T1w",
        ".nii",
    )


def test_duplicate_delivery_gives_one_copy_and_one_row(harness: Any) -> None:
    h = harness
    # First delivery runs to the end.
    h.get_item()
    h.head()
    h.update_ok()
    h.copy()
    h.update_ok()
    h.extract_sent()
    assert h.run() == sessions.COMMITTED
    # Second delivery read FINALIZING before the first claimed: the claim is refused,
    # and the stubs allow no copy_object, so any copy would raise.
    h.get_item()
    h.head()
    h.update_refused()
    assert h.run() == commit.LOST_CLAIM
    # A later delivery sees the finished item and stops at the read.
    h.get_item(state=sessions.COMMITTED)
    assert h.run() == commit.SKIPPED
    h.finish()
    assert len(h.rows()) == 1


def test_checksum_mismatch_is_incomplete_with_no_copy_and_no_row(harness: Any) -> None:
    h = harness
    h.get_item()
    h.head(crc=h.OTHER_CRC)
    h.ddb_stub.add_response(
        "update_item",
        {},
        expected_params={
            "TableName": ANY,
            "Key": ANY,
            "UpdateExpression": ANY,
            "ConditionExpression": ANY,
            "ExpressionAttributeNames": ANY,
            "ExpressionAttributeValues": sessions.marshal(
                {
                    ":state": sessions.INCOMPLETE,
                    ":updated_at": 1000,
                    ":error": {"reason": "checksum or size mismatch"},
                    ":expect": sessions.FINALIZING,
                    ":token": "tok-1",
                }
            ),
        },
    )
    assert h.run() == sessions.INCOMPLETE
    h.finish()  # no copy_object was stubbed: a copy would have raised
    assert h.rows() == []


def test_missing_landing_object_is_incomplete(harness: Any) -> None:
    h = harness
    h.get_item()
    h.head_missing()
    h.update_ok()
    assert h.run() == sessions.INCOMPLETE
    h.finish()
    assert h.rows() == []


def test_size_mismatch_is_incomplete(harness: Any) -> None:
    h = harness
    h.get_item()
    h.head(size=99)
    h.update_ok()
    assert h.run() == sessions.INCOMPLETE
    h.finish()
    assert h.rows() == []


def test_destination_checksum_mismatch_deletes_the_version(harness: Any) -> None:
    h = harness
    h.get_item()
    h.head()
    h.update_ok()
    h.copy(crc=h.OTHER_CRC)
    h.s3_stub.add_response(
        "delete_object",
        {},
        expected_params={
            "Bucket": "raw-bucket",
            "Key": "raw/ds1/sub-01/anat/sub-01_T1w.nii",
            "VersionId": "rv1",
        },
    )
    h.update_ok()
    assert h.run() == sessions.INCOMPLETE
    h.finish()
    assert h.rows() == []


@pytest.mark.parametrize(
    ("state", "extra"),
    [
        (sessions.OPEN, {}),
        # ACCEPTED stops only while its lease is live; past it, it resumes.
        (sessions.ACCEPTED, {"lease_until": 1001}),
        (sessions.COMMITTED, {}),
    ],
)
def test_not_claimable_stops_before_any_side_effect(
    harness: Any, state: str, extra: dict[str, Any]
) -> None:
    h = harness
    h.get_item(state=state, **extra)
    assert h.run() == commit.SKIPPED
    h.finish()
    assert h.rows() == []


def test_wrong_token_stops(harness: Any) -> None:
    h = harness
    h.get_item()
    assert h.run(token="other") == commit.SKIPPED
    h.finish()


def test_claim_condition_requires_state_and_token(harness: Any) -> None:
    h = harness
    h.ddb_stub.add_client_error(
        "update_item",
        service_error_code="ConditionalCheckFailedException",
        expected_params={
            "TableName": ANY,
            "Key": ANY,
            "UpdateExpression": ANY,
            "ExpressionAttributeNames": ANY,
            "ConditionExpression": "#state = :expect AND #finalize_token = :token",
            "ExpressionAttributeValues": sessions.marshal(
                {
                    ":state": sessions.ACCEPTED,
                    ":updated_at": 5,
                    ":expect": sessions.FINALIZING,
                    ":token": "t",
                }
            ),
        },
    )
    won = sessions.transition(
        sessions.SessionTable(h.ddb, h.TABLE),
        "s",
        sessions.Transition(
            expect=sessions.FINALIZING,
            to=sessions.ACCEPTED,
            now=5,
            token="t",
        ),
    )
    assert won is False
    h.finish()


def test_existing_row_for_the_session_is_not_appended_again(harness: Any) -> None:
    h = harness
    row = {
        "asset_id": "a",
        "session_id": "11111111-1111-4111-8111-111111111111",
        "dataset": "ds1",
        "s3_key": "k",
        "s3_version_id": "v",
        "size_bytes": 1,
        "checksum_crc64nvme": "c",
        "extension": ".nii",
        "committed_at": commit.utc_now_naive(),
    }
    writer = CatalogWriter(h.catalog)
    key = {"session_id": row["session_id"]}
    first = writer.append_if_absent("assets", key, [row])
    second = writer.append_if_absent("assets", key, [row])
    assert first == second
    assert len(h.rows()) == 1


def test_commit_state_unknown_is_rechecked_not_reappended(
    harness: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = harness
    original = Table.append
    calls: list[int] = []

    def append_then_lose_the_reply(self: Table, df: Any) -> None:
        original(self, df)  # the commit lands, the reply is lost
        calls.append(1)
        if len(calls) == 1:
            raise CommitStateUnknownException

    monkeypatch.setattr(Table, "append", append_then_lose_the_reply)
    row = {
        "asset_id": "a",
        "session_id": "s-1",
        "dataset": "ds1",
        "s3_key": "k",
        "s3_version_id": "v",
        "size_bytes": 1,
        "checksum_crc64nvme": "c",
        "extension": ".nii",
        "committed_at": commit.utc_now_naive(),
    }
    CatalogWriter(h.catalog).append_if_absent("assets", {"session_id": "s-1"}, [row])
    assert len(calls) == 1
    assert len(h.rows()) == 1


def test_retention_is_governance_until_committed_at_plus_the_setting(
    harness: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RAW_RETENTION_DAYS", "7")
    h = harness
    h.get_item()
    h.head()
    h.update_ok()
    h.s3_stub.add_response(
        "copy_object",
        {"VersionId": "rv1", "CopyObjectResult": {"ChecksumCRC64NVME": h.CRC}},
    )
    h.lock(days=7)  # exact Mode, VersionId and date are in the expected params
    h.update_ok()
    h.extract_sent()
    assert h.run() == sessions.COMMITTED
    h.finish()


def test_checksum_mismatch_deletes_the_version_and_never_locks(harness: Any) -> None:
    h = harness
    h.get_item()
    h.head()
    h.update_ok()
    h.copy(crc=h.OTHER_CRC)  # queues no put_object_retention
    h.s3_stub.add_response("delete_object", {})
    h.update_ok()
    assert h.run() == sessions.INCOMPLETE
    h.finish()  # an unexpected put_object_retention would have raised


@pytest.mark.parametrize("value", [None, "", "0", "-3", "x"])
def test_missing_or_bad_retention_raises_before_any_state_transition(
    harness: Any, monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    if value is None:
        monkeypatch.delenv("RAW_RETENTION_DAYS")
    else:
        monkeypatch.setenv("RAW_RETENTION_DAYS", value)
    h = harness
    h.get_item()  # the only call allowed: the read; no claim, no S3
    with pytest.raises(ValueError, match="RAW_RETENTION_DAYS"):
        h.run()
    h.finish()
