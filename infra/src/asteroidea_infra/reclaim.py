"""`make reclaim [DRY=1] [SESSION=<id>]` (admin, live).

Finds sessions whose writer stopped and re-sends each to its FIFO queue; it never
invokes a function. Commit: FINALIZING or ACCEPTED with `lease_until` absent or past.
Extract: COMMITTED with `extract_state` PENDING or EXTRACTING and
`extract_lease_until` absent or past. A session whose attempts reached 3 is never
re-sent, and a FAILED extraction is never retried. The dedup id is
`<session_id>:<lease>` (0 when absent), so a new lease is never swallowed by the
5-minute deduplication window. Prints both DLQ depths.
`FORCE=1 SESSION=<id>` first expires the lease of that one session, only when
it is resumable (commit: ACCEPTED or FINALIZING; extract: COMMITTED with PENDING or
EXTRACTING) and its attempts are below 3; it then re-sends as above. It also
re-runs a FAILED extraction on purpose (COMMITTED with `extract_state` FAILED): the
write sets `extract_state` PENDING, `extract_attempts` 0 and expires the lease. It
refuses COMMITTED+EXTRACTED (or SKIPPED), attempts at 3 on any other resumable state
and every other state, says why, purges nothing and changes nothing. It prints what it
changed.
Exit codes: 1 a named session is not eligible or a stack lookup fails; 2 FORCE without
a session; 4 no sandbox.
"""

import argparse
import sys
import time
from typing import TYPE_CHECKING, Any, Final, Literal, NamedTuple

from asteroidea_infra.admin import session_for, stack_outputs
from asteroidea_infra.sandbox_guard import require_sandbox
from asteroidea_ingest import queues, sessions

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping

    from boto3 import Session

    from asteroidea_ingest.aws_types import Sqs

COMMIT_STATES: Final = frozenset({sessions.FINALIZING, sessions.ACCEPTED})
EXTRACT_STATES: Final = frozenset({sessions.EXTRACT_PENDING, sessions.EXTRACTING})
EXIT_NOT_ELIGIBLE: Final = 1
EXIT_USAGE: Final = 2
QUEUE_OUTPUTS: Final = {"commit": "CommitQueueUrl", "extract": "ExtractQueueUrl"}
DLQ_OUTPUTS: Final = {"commit": "CommitDlqUrl", "extract": "ExtractDlqUrl"}
Queue = Literal["commit", "extract"]


class Resend(NamedTuple):
    """One message to re-send: which queue, its body and its deduplication id."""

    queue: Queue
    body: dict[str, str]
    dedup_id: str


def _attempts(item: Mapping[str, Any], name: str) -> int:
    return int(item.get(name, 0))


def _commit_resend(item: Mapping[str, Any], now: int) -> Resend | str:
    sid, state = str(item["session_id"]), item.get("state")
    if not sessions.lease_expired(item, sessions.COMMIT_LEASE, now):
        return f"{state}: lease_until {item[sessions.COMMIT_LEASE]} not yet past"
    if _attempts(item, "commit_attempts") >= queues.MAX_ATTEMPTS:
        return f"{state}: commit_attempts reached {queues.MAX_ATTEMPTS}"
    return Resend(
        "commit",
        {"session_id": sid, "finalize_token": str(item["finalize_token"])},
        queues.reclaim_dedup_id(sid, int(item.get(sessions.COMMIT_LEASE, 0))),
    )


def _extract_resend(item: Mapping[str, Any], now: int) -> Resend | str:
    sid, extract_state = str(item["session_id"]), item.get("extract_state")
    if not sessions.lease_expired(item, sessions.EXTRACT_LEASE, now):
        lease = item[sessions.EXTRACT_LEASE]
        return f"{extract_state}: extract_lease_until {lease} not yet past"
    if _attempts(item, "extract_attempts") >= queues.MAX_ATTEMPTS:
        return f"{extract_state}: extract_attempts reached {queues.MAX_ATTEMPTS}"
    return Resend(
        "extract",
        {"session_id": sid},
        queues.reclaim_dedup_id(sid, int(item.get(sessions.EXTRACT_LEASE, 0))),
    )


def select(item: Mapping[str, Any], now: int) -> Resend | str:
    """Return the re-send for `item`, or the reason it is not eligible."""
    state, extract_state = item.get("state"), item.get("extract_state")
    if state in COMMIT_STATES:
        return _commit_resend(item, now)
    if state == sessions.COMMITTED and extract_state in EXTRACT_STATES:
        return _extract_resend(item, now)
    return f"state {state}, extract_state {extract_state}: nothing to reclaim"


class Expiry(NamedTuple):
    """What a forced reclaim writes: the lease (old, new), a guard and extra fields.

    `extra` is empty for a resumable session; a FAILED extraction also gets
    `extract_state` PENDING and `extract_attempts` 0.
    """

    attribute: str
    old: int | None
    new: int
    guard: dict[str, str]
    extra: dict[str, Any]


def plan_force(item: Mapping[str, Any], now: int) -> Expiry | str:
    """Return the lease to expire for `item`, or why a forced reclaim refuses it."""
    state, extract_state = item.get("state"), item.get("extract_state")
    if state in COMMIT_STATES:
        attribute, kind, guard = sessions.COMMIT_LEASE, "commit", {"state": str(state)}
    elif state == sessions.COMMITTED and extract_state in EXTRACT_STATES:
        attribute, kind = sessions.EXTRACT_LEASE, "extract"
        guard = {"state": sessions.COMMITTED, "extract_state": str(extract_state)}
    elif state == sessions.COMMITTED and extract_state == queues.EXTRACT_FAILED:
        # an operator's deliberate re-run; the attempts start again.
        old = item.get(sessions.EXTRACT_LEASE)
        return Expiry(
            sessions.EXTRACT_LEASE,
            None if old is None else int(old),
            now - 1,
            {"state": sessions.COMMITTED, "extract_state": queues.EXTRACT_FAILED},
            {"extract_state": sessions.EXTRACT_PENDING, "extract_attempts": 0},
        )
    elif state == sessions.COMMITTED:
        return f"COMMITTED with extract_state {extract_state}: nothing resumable"
    else:
        return f"state {state}: not a resumable state"
    if _attempts(item, f"{kind}_attempts") >= queues.MAX_ATTEMPTS:
        return f"{kind}_attempts reached {queues.MAX_ATTEMPTS}"
    old = item.get(attribute)
    return Expiry(attribute, None if old is None else int(old), now - 1, guard, {})


def force_expire(
    table: sessions.SessionTable, item: Mapping[str, Any], now: int
) -> Expiry | str:
    """Expire the lease of `item` when `plan_force` allows; returns what changed."""
    plan = plan_force(item, now)
    if isinstance(plan, str):
        return plan
    names = {"#lease": plan.attribute, "#updated_at": "updated_at"}
    condition = []
    for i, key in enumerate(plan.guard):
        names[f"#g{i}"] = key
        condition.append(f"#g{i} = :g{i}")
    values: dict[str, Any] = {":new": plan.new, ":now": now}
    values |= {f":g{i}": v for i, v in enumerate(plan.guard.values())}
    sets = ["#lease = :new", "#updated_at = :now"]
    for i, (key, value) in enumerate(plan.extra.items()):
        names[f"#x{i}"] = key
        values[f":x{i}"] = value
        sets.append(f"#x{i} = :x{i}")
    table.ddb.update_item(
        TableName=table.name,
        Key=sessions.marshal({"session_id": item["session_id"]}),
        UpdateExpression="SET " + ", ".join(sets),
        ConditionExpression=" AND ".join(condition),
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=sessions.marshal(values),
    )
    return plan


def reclaim(
    items: Iterable[Mapping[str, Any]],
    now: int,
    *,
    send: Callable[[Resend], None] | None,
) -> list[Resend]:
    """Select from `items` and send each (unless `send` is `None`: a dry run)."""
    chosen: list[Resend] = []
    for item in items:
        picked = select(item, now)
        if isinstance(picked, str):
            continue
        verb = "would send" if send is None else "send"
        sys.stdout.write(
            f"{verb} {picked.queue} {item['session_id']} state={item.get('state')} "
            f"extract_state={item.get('extract_state')} dedup={picked.dedup_id}\n"
        )
        if send is not None:
            send(picked)
        chosen.append(picked)
    return chosen


def scan_items(table: sessions.SessionTable) -> list[dict[str, Any]]:
    """Return every session item (the table is small; no new index)."""
    items: list[dict[str, Any]] = []
    page = table.ddb.scan(TableName=table.name)
    items.extend(sessions.unmarshal(i) for i in page.get("Items", []))
    while "LastEvaluatedKey" in page:
        page = table.ddb.scan(
            TableName=table.name, ExclusiveStartKey=page["LastEvaluatedKey"]
        )
        items.extend(sessions.unmarshal(i) for i in page.get("Items", []))
    return items


def sessions_table_name(session: Session) -> str:
    """Return the sessions table's name from the DataStack resources."""
    cfn = session.client("cloudformation")
    for res in cfn.describe_stack_resources(StackName="DataStack")["StackResources"]:
        if (
            res["LogicalResourceId"].startswith("SessionsTable")
            and res["ResourceType"] == "AWS::DynamoDB::Table"
        ):
            return str(res["PhysicalResourceId"])
    msg = "sessions table not found in DataStack"
    raise SystemExit(msg)


def dlq_depths(sqs: Sqs, outputs: Mapping[str, str]) -> dict[str, int]:
    """Return ApproximateNumberOfMessages of both DLQs."""
    depths: dict[str, int] = {}
    for queue, key in DLQ_OUTPUTS.items():
        attrs = sqs.get_queue_attributes(
            QueueUrl=outputs[key], AttributeNames=["ApproximateNumberOfMessages"]
        )
        depths[queue] = int(attrs["Attributes"]["ApproximateNumberOfMessages"])
    return depths


def main() -> None:
    """List (`--dry-run`) or re-send stopped sessions, then print the DLQ depths."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--session", default="")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    if args.force and not args.session:
        sys.stderr.write("--force needs --session <id>\n")
        raise SystemExit(EXIT_USAGE)
    profile, account = require_sandbox()
    session = session_for(profile, account)
    outputs = stack_outputs(session)
    table = sessions.SessionTable(
        session.client("dynamodb"), sessions_table_name(session)
    )
    sqs = session.client("sqs")
    now = int(time.time())
    if args.session:
        item = sessions.get(table, args.session)
        if item is None:
            sys.stderr.write(f"no session {args.session}\n")
            raise SystemExit(EXIT_NOT_ELIGIBLE)
        if args.force and not args.dry_run:
            changed = force_expire(table, item, now)
            if isinstance(changed, str):
                sys.stderr.write(f"refused: {args.session}: {changed}\n")
                raise SystemExit(EXIT_NOT_ELIGIBLE)
            sys.stdout.write(
                f"FORCE {args.session}: {changed.attribute} {changed.old} -> "
                f"{changed.new}; also set {changed.extra or 'nothing else'}; "
                "nothing purged\n"
            )
            item = {**item, changed.attribute: changed.new, **changed.extra}
        picked = select(item, now)
        if isinstance(picked, str):
            sys.stderr.write(f"not eligible: {args.session}: {picked}\n")
            raise SystemExit(EXIT_NOT_ELIGIBLE)
        items = [item]
    else:
        items = scan_items(table)

    def send(resend: Resend) -> None:
        """Send one re-send message to its FIFO queue; return nothing."""
        queues.send(
            sqs, outputs[QUEUE_OUTPUTS[resend.queue]], resend.body, resend.dedup_id
        )

    chosen = reclaim(items, now, send=None if args.dry_run else send)
    sys.stdout.write(
        f"{'would send' if args.dry_run else 'sent'} {len(chosen)} message(s); "
        f"dlq depths: {dlq_depths(sqs, outputs)}\n"
    )


if __name__ == "__main__":
    main()
