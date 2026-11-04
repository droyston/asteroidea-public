"""Session states and DynamoDB access shared by the API and commit Lambda."""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import ClientError

from asteroidea_ingest import timing

if TYPE_CHECKING:
    from collections.abc import Mapping

    from asteroidea_ingest.aws_types import DynamoDb

OPEN: Final = "OPEN"
FINALIZING: Final = "FINALIZING"
ACCEPTED: Final = "ACCEPTED"
COMMITTED: Final = "COMMITTED"
INCOMPLETE: Final = "INCOMPLETE"
# `extract_state`; COMMITTED sets PENDING, ExtractFunction the rest.
EXTRACT_PENDING: Final = "PENDING"
EXTRACTING: Final = "EXTRACTING"

OWNER_INDEX: Final = "by_owner"
LIST_LIMIT: Final = 50

CONDITIONAL_FAILED: Final = "ConditionalCheckFailedException"
OPEN_TTL_SECONDS: Final = 48 * 3600
COMMITTED_TTL_SECONDS: Final = 30 * 24 * 3600
# Leases: derived once in `timing` by its lease rule.
COMMIT_LEASE_SECONDS: Final = timing.COMMIT_LEASE_SECONDS
EXTRACT_BACKLOG_SECONDS: Final = timing.EXTRACT_BACKLOG_SECONDS
EXTRACT_LEASE_SECONDS: Final = timing.EXTRACT_LEASE_SECONDS
REEXTRACT_LEASE_SECONDS: Final = timing.REEXTRACT_LEASE_SECONDS
COMMIT_LEASE: Final = "lease_until"
EXTRACT_LEASE: Final = "extract_lease_until"

# Re-extraction tracking items: `session_id = reextract:<run_id>:<asset_id>`.
# They carry no `owner_sub` or `created_at` (so the sparse `by_owner` index never holds
# them) and use `reextract_state`, never `state` or `extract_state` (so `make reclaim`
# never acts on them).
REEXTRACT_PREFIX: Final = "reextract:"
REEXTRACT_STATE: Final = "reextract_state"
REEXTRACT_LEASE: Final = "reextract_lease_until"
REEXTRACT_QUEUED: Final = "QUEUED"
REEXTRACT_DONE: Final = "DONE"
REEXTRACT_FAILED: Final = "FAILED"
REEXTRACT_TTL_SECONDS: Final = 7 * 24 * 3600

_SER = TypeSerializer()
_DESER = TypeDeserializer()


def marshal(values: Mapping[str, Any]) -> dict[str, Any]:
    """Convert plain values to DynamoDB attribute values."""
    return {k: _SER.serialize(v) for k, v in values.items()}


def unmarshal(item: Mapping[str, Any]) -> dict[str, Any]:
    """Convert DynamoDB attribute values to plain values (numbers become `int`)."""
    out = {k: _DESER.deserialize(v) for k, v in item.items()}
    return {k: int(v) if hasattr(v, "as_tuple") else v for k, v in out.items()}


@dataclass(frozen=True, slots=True)
class SessionTable:
    """A DynamoDB client plus the name of the sessions table it reads and writes."""

    ddb: DynamoDb
    name: str


@dataclass(frozen=True, slots=True)
class Transition:
    """One conditional state change.

    With `token`, the condition also requires `finalize_token = token`. With
    `lease`, it also requires that lease attribute to be absent (items from the
    first version carry none) or earlier than `now`: the resume arm.
    """

    expect: str
    to: str
    now: int
    token: str | None = None
    set_values: Mapping[str, Any] | None = None
    lease: str | None = None


def lease_expired(item: Mapping[str, Any], lease: str, now: int) -> bool:
    """Return whether `item[lease]` is absent or earlier than `now`."""
    return lease not in item or int(item[lease]) < now


def lease_condition(lease: str) -> str:
    """Return the DynamoDB form of `lease_expired` (`#<lease>`, `:now`)."""
    return f"(attribute_not_exists(#{lease}) OR #{lease} < :now)"


def get(table: SessionTable, session_id: str) -> dict[str, Any] | None:
    """Read one session item (strongly consistent); `None` when absent."""
    resp = table.ddb.get_item(
        TableName=table.name,
        Key=marshal({"session_id": session_id}),
        ConsistentRead=True,
    )
    item = resp.get("Item")
    return unmarshal(item) if item else None


def by_owner(table: SessionTable, owner_sub: str) -> list[dict[str, Any]]:
    """Return the owner's sessions from `by_owner`, newest first, at most 50."""
    resp = table.ddb.query(
        TableName=table.name,
        IndexName=OWNER_INDEX,
        KeyConditionExpression="owner_sub = :owner_sub",
        ExpressionAttributeValues=marshal({":owner_sub": owner_sub}),
        ScanIndexForward=False,
        Limit=LIST_LIMIT,
    )
    return [unmarshal(item) for item in resp.get("Items", [])]


def transition(table: SessionTable, session_id: str, change: Transition) -> bool:
    """Conditionally move `state` from `change.expect` to `change.to`.

    Return whether the claim won.
    """
    sets = {"state": change.to, "updated_at": change.now, **(change.set_values or {})}
    names = {f"#{k}": k for k in [*sets, "state"]}
    values = marshal({f":{k}": v for k, v in sets.items()} | {":expect": change.expect})
    condition = "#state = :expect"
    if change.token is not None:
        names.update({"#finalize_token": "finalize_token"})
        values.update(marshal({":token": change.token}))
        condition += " AND #finalize_token = :token"
    if change.lease is not None:
        names.update({f"#{change.lease}": change.lease})
        values.update(marshal({":now": change.now}))
        condition += f" AND {lease_condition(change.lease)}"
    expression = "SET " + ", ".join(f"#{k} = :{k}" for k in sets)
    try:
        table.ddb.update_item(
            TableName=table.name,
            Key=marshal({"session_id": session_id}),
            UpdateExpression=expression,
            ConditionExpression=condition,
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )
    except ClientError as err:
        if err.response["Error"]["Code"] == CONDITIONAL_FAILED:
            return False
        raise
    return True


def extract_claimable(item: Mapping[str, Any], now: int) -> bool:
    """Return whether an extraction may claim `item` (PENDING, or a stale EXTRACTING).

    FAILED, EXTRACTED and SKIPPED are never claimed, so a failed extraction is never
    retried.
    """
    state = str(item.get("extract_state"))
    return state == EXTRACT_PENDING or (
        state == EXTRACTING and lease_expired(item, EXTRACT_LEASE, now)
    )


EXTRACT_CLAIM_CONDITION: Final = (
    f"#extract_state = :pending OR (#extract_state = :extracting AND "
    f"{lease_condition(EXTRACT_LEASE)})"
)


def reextract_id(run_id: str, asset_id: str) -> str:
    """Return the tracking item's `session_id`."""
    return f"{REEXTRACT_PREFIX}{run_id}:{asset_id}"


def reextract_item(run_id: str, asset_id: str, now: int) -> dict[str, Any]:
    """Return a QUEUED tracking item: lease now + 600 s, `expires_at` now + 7 days."""
    return {
        "session_id": reextract_id(run_id, asset_id),
        "asset_id": asset_id,
        "run_id": run_id,
        REEXTRACT_STATE: REEXTRACT_QUEUED,
        REEXTRACT_LEASE: now + REEXTRACT_LEASE_SECONDS,
        "expires_at": now + REEXTRACT_TTL_SECONDS,
        "updated_at": now,
    }


def set_reextract(
    table: SessionTable, session_id: str, fields: Mapping[str, Any]
) -> None:
    """SET `fields` on an existing tracking item (a deleted item is not recreated)."""
    names = {f"#{k}": k for k in fields} | {"#session_id": "session_id"}
    try:
        table.ddb.update_item(
            TableName=table.name,
            Key=marshal({"session_id": session_id}),
            UpdateExpression="SET " + ", ".join(f"#{k} = :{k}" for k in fields),
            ConditionExpression="attribute_exists(#session_id)",
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=marshal({f":{k}": v for k, v in fields.items()}),
        )
    except ClientError as err:
        if err.response["Error"]["Code"] != CONDITIONAL_FAILED:
            raise


def claim_extract(table: SessionTable, session_id: str, now: int) -> bool:
    """Move `extract_state` to EXTRACTING with a fresh lease; return whether it won."""
    try:
        table.ddb.update_item(
            TableName=table.name,
            Key=marshal({"session_id": session_id}),
            UpdateExpression=(
                "SET #extract_state = :extracting, "
                f"#{EXTRACT_LEASE} = :lease, #updated_at = :now"
            ),
            ConditionExpression=EXTRACT_CLAIM_CONDITION,
            ExpressionAttributeNames={
                "#extract_state": "extract_state",
                f"#{EXTRACT_LEASE}": EXTRACT_LEASE,
                "#updated_at": "updated_at",
            },
            ExpressionAttributeValues=marshal(
                {
                    ":pending": EXTRACT_PENDING,
                    ":extracting": EXTRACTING,
                    ":lease": now + EXTRACT_LEASE_SECONDS,
                    ":now": now,
                }
            ),
        )
    except ClientError as err:
        if err.response["Error"]["Code"] == CONDITIONAL_FAILED:
            return False
        raise
    return True
