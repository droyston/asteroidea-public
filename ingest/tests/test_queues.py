"""Writer queues: dedup ids, events, the never-raise guard."""

import json
from typing import Any

import pytest
from botocore.stub import ANY

from asteroidea_ingest import commit, queues, sessions
from asteroidea_ingest.catalog_writer import CatalogWriter
from asteroidea_ingest.commit import CommitDeps
from asteroidea_ingest.sessions import SessionTable

SID = "11111111-1111-4111-8111-111111111111"


def _guard(h: Any, kind: queues.Kind = "commit") -> queues.Guard:
    return queues.Guard(SessionTable(h.ddb, h.TABLE), kind, SID, 500)


def _attempts(h: Any, n: int, kind: str = "commit") -> None:
    h.ddb_stub.add_response(
        "update_item",
        {"Attributes": {f"{kind}_attempts": {"N": str(n)}}},
        expected_params={
            "TableName": h.TABLE,
            "Key": sessions.marshal({"session_id": SID}),
            "UpdateExpression": "ADD #attempts :one",
            "ConditionExpression": "attribute_exists(session_id)",
            "ExpressionAttributeNames": {"#attempts": f"{kind}_attempts"},
            "ExpressionAttributeValues": sessions.marshal({":one": 1}),
            "ReturnValues": "UPDATED_NEW",
        },
    )


def _set_ok(h: Any) -> None:
    h.ddb_stub.add_response("update_item", {}, expected_params=ANY_UPDATE)


ANY_UPDATE = {
    "TableName": ANY,
    "Key": ANY,
    "UpdateExpression": ANY,
    "ConditionExpression": ANY,
    "ExpressionAttributeNames": ANY,
    "ExpressionAttributeValues": ANY,
}


def _capture(h: Any) -> list[dict[str, Any]]:
    seen: list[dict[str, Any]] = []
    h.ddb.meta.events.register(
        "before-parameter-build.dynamodb.UpdateItem",
        lambda params, **_: seen.append(
            {
                "expr": params["UpdateExpression"],
                "cond": params["ConditionExpression"],
                "values": sessions.unmarshal(params["ExpressionAttributeValues"]),
            }
        ),
    )
    return seen


def _boom() -> str:
    msg = "catalog unreachable"
    raise RuntimeError(msg)


# -- dedup ids and event shape ---------------------------------------------------


def test_dedup_ids_follow_the_plan() -> None:
    assert queues.commit_dedup_id("s", "t") == "s:t"
    assert queues.extract_dedup_id("s") == "s:x"
    assert queues.reclaim_dedup_id("s", 1330) == "s:1330"


def test_send_commit_uses_the_catalog_group_and_the_finalize_dedup_id(
    harness: Any,
) -> None:
    harness.sqs_stub.add_response(
        "send_message",
        {"MessageId": "m"},
        expected_params={
            "QueueUrl": "q",
            "MessageBody": json.dumps(
                {"finalize_token": "t", "session_id": "s"}, sort_keys=True
            ),
            "MessageGroupId": "catalog",
            "MessageDeduplicationId": "s:t",
        },
    )
    queues.send_commit(harness.sqs, "q", "s", "t")
    harness.finish()


def test_bodies_reads_the_sqs_event_source_shape() -> None:
    event = {"Records": [{"messageId": "1", "body": '{"session_id": "s"}'}]}
    assert queues.bodies(event) == [{"session_id": "s"}]
    assert queues.bodies({}) == []


# -- the guard -------------------------------------------------------------------


def test_a_raising_handler_still_returns_and_records_the_error(harness: Any) -> None:
    h = harness
    seen = _capture(h)
    _attempts(h, 1)
    _set_ok(h)  # commit_error
    assert queues.guarded(_guard(h), _boom) == queues.ERROR
    h.finish()
    recorded = seen[-1]["values"]
    assert recorded[":commit_error"] == {
        "class": "RuntimeError",
        "message": "catalog unreachable",
    }
    assert recorded[":updated_at"] == 500


def test_the_third_failing_attempt_records_and_stops_the_session(harness: Any) -> None:
    h = harness
    seen = _capture(h)
    _attempts(h, 3)
    _set_ok(h)  # commit_error
    _set_ok(h)  # INCOMPLETE
    assert queues.guarded(_guard(h), _boom) == queues.ERROR
    h.finish()
    stop = seen[-1]
    assert stop["values"][":state"] == sessions.INCOMPLETE
    assert stop["values"][":error"] == {"reason": "commit attempts exhausted"}
    assert stop["cond"] == "#state IN (:finalizing, :accepted)"


def test_beyond_three_attempts_the_work_never_runs(harness: Any) -> None:
    h = harness
    _attempts(h, 4)
    _set_ok(h)  # INCOMPLETE

    def must_not_run() -> str:
        pytest.fail("work ran after the attempts were exhausted")

    assert queues.guarded(_guard(h), must_not_run) == queues.EXHAUSTED
    h.finish()


def test_extract_exhaustion_sets_extract_state_failed(harness: Any) -> None:
    h = harness
    seen = _capture(h)
    _attempts(h, 4, "extract")
    _set_ok(h)
    assert queues.guarded(_guard(h, "extract"), _boom) == queues.EXHAUSTED
    h.finish()
    assert seen[-1]["values"][":extract_state"] == "FAILED"
    assert seen[-1]["cond"] == "#extract_state <> :done"


def test_a_missing_session_runs_nothing(harness: Any) -> None:
    h = harness
    h.ddb_stub.add_client_error(
        "update_item", service_error_code="ConditionalCheckFailedException"
    )
    assert queues.guarded(_guard(h), _boom) == queues.NO_SESSION
    h.finish()


def test_a_failure_to_record_still_returns(harness: Any) -> None:
    h = harness
    _attempts(h, 1)
    h.ddb_stub.add_client_error("update_item", service_error_code="InternalServerError")
    assert queues.guarded(_guard(h), _boom) == queues.ERROR
    h.finish()


def test_a_successful_handler_returns_its_outcome(harness: Any) -> None:
    h = harness
    _attempts(h, 1)
    assert queues.guarded(_guard(h), lambda: "COMMITTED") == "COMMITTED"
    h.finish()


# -- the commit handler over an SQS batch ----------------------------------------


def test_commit_batch_with_a_raising_commit_returns_and_records(harness: Any) -> None:
    h = harness
    seen = _capture(h)
    _attempts(h, 1)
    h.get_item()
    h.s3_stub.add_client_error("head_object", service_error_code="AccessDenied")
    _set_ok(h)  # commit_error
    event = {
        "Records": [
            {"body": json.dumps({"session_id": SID, "finalize_token": "tok-1"})}
        ]
    }
    deps = CommitDeps(
        table=SessionTable(h.ddb, h.TABLE),
        s3=h.s3,
        writer=CatalogWriter(h.catalog),
        bucket="raw-bucket",
        sqs=h.sqs,
        extract_queue_url="extract-url",
    )
    assert commit.run_batch(event, deps, now=500) == [queues.ERROR]
    h.finish()
    error = seen[-1]["values"][":commit_error"]
    assert error["class"] == "AccessDenied"  # botocore's modeled ClientError subclass
    assert "HeadObject" in error["message"]
