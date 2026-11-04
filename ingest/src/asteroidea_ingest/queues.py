"""Writer queues: send helpers, event shape, guard.

Both catalog writers read one SQS FIFO queue each, every message in the one group
`catalog`, so each writer handles one message at a time. A FIFO group stops behind a
message that is never deleted, so the handlers never raise: `guarded` records the
error on the session item and returns.
"""

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Literal

from botocore.exceptions import ClientError

from asteroidea_ingest import sessions

if TYPE_CHECKING:
    from collections.abc import Callable

    from asteroidea_ingest.aws_types import Sqs
    from asteroidea_ingest.sessions import SessionTable

GROUP: Final = "catalog"
MAX_ATTEMPTS: Final = 3
ERROR: Final = "ERROR"
EXHAUSTED: Final = "EXHAUSTED"
NO_SESSION: Final = "NO_SESSION"
EXTRACT_FAILED: Final = "FAILED"
EXTRACT_DONE: Final = "EXTRACTED"
Kind = Literal["commit", "extract"]
logger = logging.getLogger(__name__)


def commit_dedup_id(session_id: str, finalize_token: str) -> str:
    """Return the finalize and import dedup id; SQS swallows a duplicate finalize."""
    return f"{session_id}:{finalize_token}"


def extract_dedup_id(session_id: str) -> str:
    """Return the dedup id of the commit's extract send."""
    return f"{session_id}:x"


def reclaim_dedup_id(session_id: str, lease_until: int) -> str:
    """Return a reclaim send's dedup id; a new lease gives a new id.

    So the 5-minute deduplication window never swallows a reclaim.
    """
    return f"{session_id}:{lease_until}"


def send(sqs: Sqs, queue_url: str, body: dict[str, str], dedup_id: str) -> None:
    """Send one message in the `catalog` group with an explicit deduplication id."""
    sqs.send_message(
        QueueUrl=queue_url,
        MessageBody=json.dumps(body, sort_keys=True),
        MessageGroupId=GROUP,
        MessageDeduplicationId=dedup_id,
    )


def send_commit(sqs: Sqs, queue_url: str, session_id: str, finalize_token: str) -> None:
    """Queue one commit: `{session_id, finalize_token}`."""
    send(
        sqs,
        queue_url,
        {"session_id": session_id, "finalize_token": finalize_token},
        commit_dedup_id(session_id, finalize_token),
    )


def send_extract(sqs: Sqs, queue_url: str, session_id: str) -> None:
    """Queue one extraction: `{session_id}`."""
    send(sqs, queue_url, {"session_id": session_id}, extract_dedup_id(session_id))


def send_reextract(
    sqs: Sqs, queue_url: str, tracking_id: str, asset: dict[str, str]
) -> None:
    """Queue one re-extraction: `{session_id: <tracking id>, <asset fields>}`.

    `asset` holds asset_id, dataset, relpath, s3_key, s3_version_id, file_format and
    size; the message is self-contained because asset rows outlive sessions.
    """
    send(sqs, queue_url, {"session_id": tracking_id, **asset}, f"{tracking_id}:r")


def bodies(event: dict[str, Any]) -> list[dict[str, str]]:
    """Return the JSON bodies of an SQS event source batch (`Records[].body`)."""
    return [json.loads(record["body"]) for record in event.get("Records", [])]


@dataclass(frozen=True, slots=True)
class Guard:
    """Which writer runs, on which session, at what time."""

    table: SessionTable
    kind: Kind
    session_id: str
    now: int


def _attempt(guard: Guard) -> int | None:
    """Add one to `<kind>_attempts`; `None` when the session item does not exist."""
    name = f"{guard.kind}_attempts"
    try:
        resp = guard.table.ddb.update_item(
            TableName=guard.table.name,
            Key=sessions.marshal({"session_id": guard.session_id}),
            UpdateExpression="ADD #attempts :one",
            ConditionExpression="attribute_exists(session_id)",
            ExpressionAttributeNames={"#attempts": name},
            ExpressionAttributeValues=sessions.marshal({":one": 1}),
            ReturnValues="UPDATED_NEW",
        )
    except ClientError as err:
        if err.response["Error"]["Code"] == sessions.CONDITIONAL_FAILED:
            return None
        raise
    return int(sessions.unmarshal(resp["Attributes"])[name])


@dataclass(frozen=True, slots=True)
class _Condition:
    expression: str
    names: dict[str, str]
    values: dict[str, Any]


def _set(guard: Guard, values: dict[str, Any], condition: _Condition) -> None:
    """SET `values` when `condition` holds; a failed condition is not an error."""
    expression = "SET " + ", ".join(f"#{k} = :{k}" for k in values)
    try:
        guard.table.ddb.update_item(
            TableName=guard.table.name,
            Key=sessions.marshal({"session_id": guard.session_id}),
            UpdateExpression=expression,
            ConditionExpression=condition.expression,
            ExpressionAttributeNames={f"#{k}": k for k in values} | condition.names,
            ExpressionAttributeValues=sessions.marshal(
                {f":{k}": v for k, v in values.items()} | condition.values
            ),
        )
    except ClientError as err:
        if err.response["Error"]["Code"] != sessions.CONDITIONAL_FAILED:
            raise


def _exhaust(guard: Guard) -> None:
    """Stop the session for good: INCOMPLETE, or `extract_state = FAILED`."""
    if guard.kind == "commit":
        _set(
            guard,
            {
                "state": sessions.INCOMPLETE,
                "updated_at": guard.now,
                "error": {"reason": "commit attempts exhausted"},
            },
            _Condition(
                "#state IN (:finalizing, :accepted)",
                {},
                {":finalizing": sessions.FINALIZING, ":accepted": sessions.ACCEPTED},
            ),
        )
    else:
        _set(
            guard,
            {"extract_state": EXTRACT_FAILED, "updated_at": guard.now},
            _Condition("#extract_state <> :done", {}, {":done": EXTRACT_DONE}),
        )


def _record(guard: Guard, exc: Exception) -> None:
    """Record `<kind>_error = {class, message}` on the item, never collapsed."""
    error = {"class": type(exc).__name__, "message": str(exc)}
    _set(
        guard,
        {f"{guard.kind}_error": error, "updated_at": guard.now},
        _Condition("attribute_exists(#session_id)", {"#session_id": "session_id"}, {}),
    )


def guarded(guard: Guard, work: Callable[[], str]) -> str:
    """Run `work` for one delivery and never raise.

    Every delivery adds one to `<kind>_attempts`. Beyond `MAX_ATTEMPTS` the work does
    not run and the session is stopped (INCOMPLETE "commit attempts exhausted", or
    `extract_state = FAILED`). An exception is recorded as `<kind>_error`; on the last
    allowed attempt the session is stopped too. Returns the work's outcome, `ERROR`,
    `EXHAUSTED` or `NO_SESSION`.
    """
    try:
        attempts = _attempt(guard)
        if attempts is None:
            return NO_SESSION
        if attempts > MAX_ATTEMPTS:
            _exhaust(guard)
            return EXHAUSTED
        try:
            return work()
        except Exception as exc:
            logger.exception("%s of %s failed", guard.kind, guard.session_id)
            _record(guard, exc)
            if attempts >= MAX_ATTEMPTS:
                _exhaust(guard)
            return ERROR
    except Exception:
        # Recording failed too (for example DynamoDB refused): log and still return,
        # so the message is deleted and the group moves on.
        logger.exception(
            "could not record the %s outcome of %s", guard.kind, guard.session_id
        )
        return ERROR
