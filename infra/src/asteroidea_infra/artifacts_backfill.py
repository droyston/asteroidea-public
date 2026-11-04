"""`make artifacts-backfill [DRY=1]` (admin, live).

Result records saved by earlier versions lack `author_email`, `data_key` and the index
objects. This lists every `artifacts/<sub>/<id>/provenance.json` and writes only what
is missing, never a field or object that is already filled:

- `author_email`: Cognito `ListUsers` filtered on `sub` with the caller's credentials,
  so the Lambda gets no grant. One lookup per author.
- `data_key`: today's `artifacts/<sub>/<id>/data/<name>`.
- `index/by-id/<id>`, one `index/by-input/<input>/<id>` per source and input, and, for
  a finalized record, `index/by-time/<inverted ms>-<id>` (not written while the email
  is unresolved, so a re-run fills it).

The record is rewritten with `IfMatch` on the ETag it was read with, so a write by the
PlatformFunction in between is refused, not overwritten. Index objects are created
with `IfNoneMatch: *`, so an existing object is never replaced. Idempotent: a second
run writes nothing. A record created after the deploy by an older SDK wheel (version 1,
no email) is filled by the next run.

`DRY=1` only lists what would be written. PASS or FAIL per record, with the boto3 or
Cognito error text; exit 1 on any FAIL, 4 without the sandbox env (`require_sandbox`).
"""

import argparse
import json
import sys
from typing import TYPE_CHECKING, Any, Final, Protocol

from botocore.exceptions import ClientError

from asteroidea_infra.admin import session_for, stack_outputs
from asteroidea_infra.sandbox_guard import require_sandbox
from asteroidea_ingest.artifacts import (
    COMMITTED,
    INDEX_BY_ID,
    RECORD_FILE,
    SUMMARY_FIELDS,
    WITHDRAWN,
    by_input_prefix,
    by_time_key,
    input_ids,
)
from asteroidea_ingest.aws_types import S3_NOT_FOUND

if TYPE_CHECKING:
    from collections.abc import Iterator

ARTIFACTS_PREFIX: Final = "artifacts/"
EXIT_FAIL: Final = 1
PRECONDITION_FAILED: Final = frozenset({"PreconditionFailed", "412"})
KEY_PARTS: Final = 4
FINALIZED: Final = frozenset({COMMITTED, WITHDRAWN})


class BackfillS3(Protocol):
    """The S3 client methods the back-fill calls."""

    def list_objects_v2(self, **kwargs: object) -> dict[str, Any]:
        """List one page of keys."""
        ...

    def get_object(self, **kwargs: object) -> dict[str, Any]:
        """Read one object."""
        ...

    def head_object(self, **kwargs: object) -> dict[str, Any]:
        """Read object metadata."""
        ...

    def put_object(self, **kwargs: object) -> dict[str, Any]:
        """Write one object."""
        ...


class BackfillCognito(Protocol):
    """The Cognito method the back-fill calls."""

    def list_users(self, **kwargs: object) -> dict[str, Any]:
        """List users matching a filter."""
        ...


class EmailLookup:
    """Cognito `ListUsers` by `sub`, one call per author; a miss is cached as None."""

    def __init__(self, cognito: BackfillCognito, pool: str) -> None:
        """Bind the client and the user pool id."""
        self._cognito = cognito
        self._pool = pool
        self._seen: dict[str, str | None] = {}

    def __call__(self, sub: str) -> str | None:
        """Return the author's email, or None when no user has that `sub`."""
        if sub not in self._seen:
            found = self._cognito.list_users(
                UserPoolId=self._pool, Filter=f'sub = "{sub}"', Limit=1
            )
            self._seen[sub] = _email_of(found.get("Users", []))
        return self._seen[sub]


def _email_of(users: list[dict[str, Any]]) -> str | None:
    for user in users:
        for attribute in user.get("Attributes", []):
            if attribute["Name"] == "email" and attribute.get("Value"):
                return str(attribute["Value"])
    return None


def record_keys(s3: BackfillS3, bucket: str) -> Iterator[str]:
    """Yield every `artifacts/<sub>/<id>/provenance.json` key, over all pages."""
    token = ""
    while True:
        page = s3.list_objects_v2(
            Bucket=bucket,
            Prefix=ARTIFACTS_PREFIX,
            MaxKeys=1000,
            **({"ContinuationToken": token} if token else {}),
        )
        for item in page.get("Contents", []):
            parts = item["Key"].split("/")
            if len(parts) == KEY_PARTS and parts[3] == RECORD_FILE:
                yield str(item["Key"])
        token = page.get("NextContinuationToken")
        if not token:
            return


def _exists(s3: BackfillS3, bucket: str, key: str) -> bool:
    try:
        s3.head_object(Bucket=bucket, Key=key)
    except ClientError as err:
        if err.response["Error"]["Code"] in S3_NOT_FOUND:
            return False
        raise
    return True


def _create_index(
    s3: BackfillS3, bucket: str, key: str, body: dict[str, Any], *, dry: bool
) -> bool:
    """Create `key` unless it exists; return True when it was (or would be) written."""
    if dry:
        return not _exists(s3, bucket, key)
    try:
        s3.put_object(
            Bucket=bucket,
            Key=key,
            Body=json.dumps(body).encode(),
            ContentType="application/json",
            IfNoneMatch="*",
        )
    except ClientError as err:
        if err.response["Error"]["Code"] in PRECONDITION_FAILED:
            return False
        raise
    return True


def _state_indexes(record: dict[str, Any]) -> dict[str, dict[str, Any]]:
    artifact_id, state = record["artifact_id"], record["state"]
    wanted: dict[str, dict[str, Any]] = {
        f"{INDEX_BY_ID}{artifact_id}": {
            "author_sub": record["author_sub"],
            "state": state,
        }
    }
    for input_id in input_ids(record):
        wanted[f"{by_input_prefix(input_id)}{artifact_id}"] = {"state": state}
    return wanted


def backfill_record(
    s3: BackfillS3, bucket: str, key: str, email_of: EmailLookup, *, dry: bool
) -> str:
    """Fill what one record lacks; return what was (or would be) written.

    Raises on an S3, Cognito or JSON error; the caller prints it as the FAIL text.
    """
    got = s3.get_object(Bucket=bucket, Key=key)
    record: dict[str, Any] = json.loads(got["Body"].read())
    wrote: list[str] = []
    problem = ""
    added: dict[str, Any] = {}
    if not record.get("data_key"):
        added["data_key"] = (
            f"artifacts/{record['author_sub']}/{record['artifact_id']}"
            f"/data/{record['name']}"
        )
    if not record.get("author_email"):
        email = email_of(record["author_sub"])
        if email is None:
            problem = f"no Cognito user with sub {record['author_sub']}"
        else:
            added["author_email"] = email
    if added:
        record = {**record, **added}
        wrote += sorted(added)
        if not dry:
            s3.put_object(
                Bucket=bucket,
                Key=key,
                Body=json.dumps(record).encode(),
                ContentType="application/json",
                IfMatch=got["ETag"],
            )
    wanted = _state_indexes(record)
    finalized = record.get("finalized_at") is not None
    if finalized and record["state"] in FINALIZED and record.get("author_email"):
        wanted[by_time_key(record["finalized_at"], record["artifact_id"])] = {
            **{k: record.get(k) for k in SUMMARY_FIELDS},
            "author_email": record["author_email"],
        }
    wrote += [
        k for k, body in wanted.items() if _create_index(s3, bucket, k, body, dry=dry)
    ]
    verb = "would write" if dry else "wrote"
    done = f"{verb} {', '.join(wrote)}" if wrote else "nothing to write"
    if problem:
        msg = f"{problem}; {done}"
        raise LookupError(msg)
    return done


def run(
    s3: BackfillS3,
    cognito: BackfillCognito,
    *,
    bucket: str,
    pool: str,
    dry: bool,
) -> int:
    """Back-fill every record; print PASS or FAIL per record; return the FAIL count."""
    write = sys.stdout.write
    email_of = EmailLookup(cognito, pool)
    fails = total = 0
    for key in record_keys(s3, bucket):
        total += 1
        try:
            done = backfill_record(s3, bucket, key, email_of, dry=dry)
        except (ClientError, LookupError, KeyError, ValueError) as err:
            fails += 1
            write(f"FAIL {key}: {type(err).__name__}: {err}\n")
        else:
            write(f"PASS {key}: {done}\n")
    note = " (dry run, nothing written)" if dry else ""
    write(f"{total} record(s), {fails} FAIL{note}\n")
    return fails


def main() -> None:
    """Resolve the bucket and user pool from the stacks, then run."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    profile, account = require_sandbox()
    session = session_for(profile, account)
    bucket = stack_outputs(session, "DataStack")["ArtifactsBucketName"]
    pool = stack_outputs(session)["UserPoolId"]
    fails = run(
        session.client("s3"),
        session.client("cognito-idp"),
        bucket=bucket,
        pool=pool,
        dry=args.dry_run,
    )
    if fails:
        raise SystemExit(EXIT_FAIL)


if __name__ == "__main__":
    main()
