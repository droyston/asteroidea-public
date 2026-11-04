"""Dataset ownership.

One row per dataset in the owners table: `{dataset, owner_sub, owner_kind,
claimed_at}`. A researcher's first upload claims an unowned dataset name; a dataset
that already has raw objects but no row predates ownership and counts as admin-owned.
`make import` claims `admin`, and refuses a researcher-owned dataset.
"""

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

import boto3
from botocore.exceptions import ClientError

from asteroidea_ingest import sessions

if TYPE_CHECKING:
    from asteroidea_ingest.aws_types import ObjectLister, OwnerStore

RESEARCHER: Final = "researcher"
ADMIN: Final = "admin"
ADMIN_SUB: Final = "admin"


@dataclass(frozen=True, slots=True)
class Owners:
    """The owners table over an injected DynamoDB client."""

    ddb: OwnerStore
    name: str


def from_env() -> Owners:
    """Build `Owners` over a boto3 DynamoDB client and `OWNERS_TABLE`."""
    return Owners(boto3.client("dynamodb"), os.environ["OWNERS_TABLE"])


@dataclass(frozen=True, slots=True)
class RawProbe:
    """The raw bucket and an S3 client, for the "does the dataset predate us" probe."""

    s3: ObjectLister
    bucket: str


def _row(owners: Owners, dataset: str) -> dict[str, Any] | None:
    resp = owners.ddb.get_item(
        TableName=owners.name,
        Key=sessions.marshal({"dataset": dataset}),
        ConsistentRead=True,
    )
    item = resp.get("Item")
    return sessions.unmarshal(item) if item else None


def _put(
    owners: Owners,
    row: dict[str, str | int],
    condition: str,
    values: dict[str, str],
) -> bool:
    """PutItem `row` under `condition`; return False when the condition fails."""
    try:
        owners.ddb.put_item(
            TableName=owners.name,
            Item=sessions.marshal(row),
            ConditionExpression=condition,
            **(
                {"ExpressionAttributeValues": sessions.marshal(values)}
                if values
                else {}
            ),
        )
    except ClientError as err:
        if err.response["Error"]["Code"] == sessions.CONDITIONAL_FAILED:
            return False
        raise
    return True


def claim_for_researcher(
    owners: Owners,
    probe: RawProbe,
    dataset: str,
    sub: str,
    now: int,
) -> bool:
    """Return True when `sub` may write `dataset`; claim it when it is unowned.

    1. A row decides: the same `owner_sub` continues, another refuses.
    2. With no row, any object under `raw/<dataset>/` means the dataset predates
       ownership: write the admin row and refuse.
    3. With nothing listed, claim by a conditional PutItem; losing the race re-reads
       the row and refuses unless it is `sub`'s own.
    """
    row = _row(owners, dataset)
    if row is not None:
        return bool(row["owner_sub"] == sub)
    listed = probe.s3.list_objects_v2(
        Bucket=probe.bucket, Prefix=f"raw/{dataset}/", MaxKeys=1
    )
    if listed.get("KeyCount", 0) > 0 or listed.get("Contents"):
        claim_row = {
            "dataset": dataset,
            "owner_sub": ADMIN_SUB,
            "owner_kind": ADMIN,
            "claimed_at": now,
        }
        _put(owners, claim_row, "attribute_not_exists(dataset)", {})
        return False
    mine = {
        "dataset": dataset,
        "owner_sub": sub,
        "owner_kind": RESEARCHER,
        "claimed_at": now,
    }
    if _put(
        owners, mine, "attribute_not_exists(dataset) OR owner_sub = :sub", {":sub": sub}
    ):
        return True
    again = _row(owners, dataset)
    return again is not None and bool(again["owner_sub"] == sub)


def claim_for_admin(owners: Owners, dataset: str, owner_sub: str, now: int) -> bool:
    """Return True when an import may write `dataset`; claim it as admin if unowned.

    A researcher-owned dataset is refused.
    """
    row = _row(owners, dataset)
    if row is not None:
        return bool(row["owner_kind"] == ADMIN)
    claim_row = {
        "dataset": dataset,
        "owner_sub": owner_sub,
        "owner_kind": ADMIN,
        "claimed_at": now,
    }
    if _put(
        owners,
        claim_row,
        "attribute_not_exists(dataset) OR owner_kind = :admin",
        {":admin": ADMIN},
    ):
        return True
    again = _row(owners, dataset)
    return again is not None and bool(again["owner_kind"] == ADMIN)
