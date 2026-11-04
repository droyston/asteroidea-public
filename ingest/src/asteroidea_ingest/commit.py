"""Commit Lambda: verify, claim, copy, append once.

Row column names come from `asteroidea_infra.schema` (plain data, no `aws_cdk`); the
commit image copies that module next to this package (`ingest/Dockerfile`).
"""

import base64
import os
import time
from dataclasses import dataclass
from datetime import UTC, timedelta
from functools import partial
from typing import TYPE_CHECKING, Any, Final

import boto3
from botocore.exceptions import ClientError
from pyiceberg.catalog import load_catalog

from asteroidea_infra import schema
from asteroidea_ingest import bids_path, ids, queues, sessions
from asteroidea_ingest.aws_types import S3_NOT_FOUND
from asteroidea_ingest.catalog_writer import CatalogWriter, utc_now_naive
from asteroidea_ingest.sessions import SessionTable, Transition

if TYPE_CHECKING:
    from datetime import datetime

    from asteroidea_ingest.aws_types import S3, Sqs

SKIPPED: Final = "SKIPPED"
LOST_CLAIM: Final = "LOST_CLAIM"
NOT_FOUND_CODES: Final = S3_NOT_FOUND  # import_file.py imports this name
CRC64NVME: Final = "CRC64NVME"
SHA256: Final = "SHA256"
# The HeadObject field that carries each supported `expected_checksum` algorithm.
HEAD_CHECKSUM_FIELD: Final = {
    CRC64NVME: "ChecksumCRC64NVME",
    SHA256: "ChecksumSHA256",
}
RETENTION_ENV: Final = "RAW_RETENTION_DAYS"
GOVERNANCE: Final = "GOVERNANCE"
UPLOAD: Final = "upload"
FORMATS: Final = {
    ".nwb": "nwb",
    ".nii": "nifti",
    ".nii.gz": "nifti",
    ".json": "json",
    ".tsv": "tsv",
    ".csv": "csv",
}
OTHER_FORMAT: Final = "other"


@dataclass(frozen=True, slots=True)
class CommitDeps:
    """What one commit needs: sessions table, S3, catalog writer, bucket, queue."""

    table: SessionTable
    s3: S3
    writer: CatalogWriter
    bucket: str
    sqs: Sqs
    extract_queue_url: str


def retention_days() -> int:
    """Read `RAW_RETENTION_DAYS`; raise when it is absent or below 1.

    Called before any state transition, so a misconfigured deployment leaves every
    session as it was.
    """
    raw = os.environ.get(RETENTION_ENV, "")
    try:
        days = int(raw)
    except ValueError:
        days = 0
    if days < 1:
        msg = f"{RETENTION_ENV} must be an integer of at least 1, got {raw!r}"
        raise ValueError(msg)
    return days


def _lock(
    deps: CommitDeps, session: dict[str, Any], raw_version: str, until: datetime
) -> None:
    """Set governance retention on the verified raw version.

    Idempotent: a re-run sets a later date, which governance allows without bypass.
    """
    deps.s3.put_object_retention(
        Bucket=deps.bucket,
        Key=session["raw_key"],
        VersionId=raw_version,
        Retention={"Mode": GOVERNANCE, "RetainUntilDate": until},
    )


def _incomplete(
    table: SessionTable,
    session: dict[str, Any],
    *,
    expect: str,
    reason: str,
    now: int,
) -> str:
    sessions.transition(
        table,
        session["session_id"],
        Transition(
            expect=expect,
            to=sessions.INCOMPLETE,
            now=now,
            token=session["finalize_token"],
            set_values={"error": {"reason": reason}},
        ),
    )
    return sessions.INCOMPLETE


def expected_checksum(session: dict[str, Any]) -> tuple[str, str]:
    """Return `(algorithm, value)`.

    An item without `expected_checksum` uses the original CRC.
    """
    expected = session.get("expected_checksum")
    if expected is None:
        return CRC64NVME, str(session["client_crc64nvme"])
    return str(expected["algorithm"]), str(expected["value"])


def file_format(extension: str) -> str:
    """Map a BIDS extension (`.nii.gz`) to the `assets.format` value."""
    return FORMATS.get(extension.lower(), OTHER_FORMAT)


def _sha256_hex(value: str) -> str:
    return base64.b64decode(value).hex()


def asset_row(
    session: dict[str, Any],
    *,
    asset_id: str,
    raw_version: str,
    crc64nvme: str,
    committed_at: datetime,
) -> dict[str, Any]:
    """Build the `assets` row: the original columns plus provenance.

    Keys are taken from `schema.ASSETS.columns`, so a column the spec adds without a
    value here fails the commit tests rather than being written as null.
    """
    parts = bids_path.parse(session["relpath"])
    source = session.get("source") or {}
    meta = session.get("dataset_meta") or {}
    algorithm, value = expected_checksum(session)
    values = {
        "asset_id": asset_id,
        "session_id": session["session_id"],
        "dataset": session["dataset"],
        "s3_key": session["raw_key"],
        "s3_version_id": raw_version,
        "size_bytes": session["size"],
        "checksum_crc64nvme": crc64nvme,
        "subject": parts.subject,
        "bids_session": parts.bids_session,
        "datatype": parts.datatype,
        "suffix": parts.suffix,
        "extension": parts.extension,
        "committed_at": committed_at,
        "upload_id": session.get("upload_id"),
        "ingest_method": session.get("ingest_method", UPLOAD),
        "format": file_format(parts.extension),
        "checksum_sha256": _sha256_hex(value) if algorithm == SHA256 else None,
        "source_archive": source.get("archive"),
        "source_dataset": source.get("dataset"),
        "source_version": source.get("version"),
        "source_path": source.get("path"),
        "source_url": source.get("url"),
        "source_digest": source.get("digest"),
        # A file's own licence (an ancillary file) wins over the dataset's.
        "license": source.get("license") or meta.get("license"),
        "ingested_by": session["owner_sub"],
    }
    return {column.name: values[column.name] for column in schema.ASSETS.columns}


def dataset_row(session: dict[str, Any], *, created_at: datetime) -> dict[str, Any]:
    """Build the `datasets` row from the session's `dataset_meta` and `source`."""
    source = session.get("source") or {}
    meta = session.get("dataset_meta") or {}
    values = {
        "dataset": session["dataset"],
        "title": meta.get("title"),
        "description": meta.get("description"),
        "license": meta.get("license"),
        "source_archive": source.get("archive"),
        "source_dataset": source.get("dataset"),
        "source_version": source.get("version"),
        "doi": meta.get("doi"),
        "url": meta.get("url"),
        "ingest_method": session.get("ingest_method", UPLOAD),
        "created_by": session["owner_sub"],
        "created_at": created_at,
        "tier": meta.get("tier"),
    }
    return {column.name: values[column.name] for column in schema.DATASETS_SPEC.columns}


def _key(spec: schema.TableSpec, row: dict[str, Any]) -> dict[str, Any]:
    return {name: row[name] for name in spec.key}


def _verify_landing(
    deps: CommitDeps, session: dict[str, Any]
) -> tuple[str | None, str]:
    """Return `(landing version id, "")`, or `(None, reason)` when landing fails."""
    try:
        head = deps.s3.head_object(
            Bucket=deps.bucket, Key=session["landing_key"], ChecksumMode="ENABLED"
        )
    except ClientError as err:
        if err.response["Error"]["Code"] not in NOT_FOUND_CODES:
            raise
        return None, "landing object missing"
    algorithm, expected = expected_checksum(session)
    head_field = HEAD_CHECKSUM_FIELD.get(algorithm)
    if head_field is None:
        return None, f"unsupported checksum algorithm {algorithm!r}"
    if head.get(head_field) != expected or head["ContentLength"] != session["size"]:
        return None, "checksum or size mismatch"
    return str(head["VersionId"]), ""


def claim_mode(session: dict[str, Any] | None, token: str, now: int) -> str | None:
    """Return FINALIZING (a fresh claim), ACCEPTED (a resume) or `None` (skip).

    A resume needs the same token and a commit lease that is absent (items from the
    first version carry none) or past: a crashed or timed-out run is re-claimed,
    a live one is not.
    """
    if session is None or session.get("finalize_token") != token:
        return None
    state = session["state"]
    if state == sessions.FINALIZING:
        return sessions.FINALIZING
    if state == sessions.ACCEPTED and sessions.lease_expired(
        session, sessions.COMMIT_LEASE, now
    ):
        return sessions.ACCEPTED
    return None


def _adoptable(
    deps: CommitDeps, session: dict[str, Any], algorithm: str, expected: str
) -> tuple[str, str] | None:
    """Return `(version, crc)` of a raw object an earlier run already copied.

    Adopted only when its CRC64NVME and size equal the session's. A SHA256 session
    has no client CRC to compare, so it is copied again (a new raw version).
    """
    try:
        head = deps.s3.head_object(
            Bucket=deps.bucket, Key=session["raw_key"], ChecksumMode="ENABLED"
        )
    except ClientError as err:
        if err.response["Error"]["Code"] not in NOT_FOUND_CODES:
            raise
        return None
    crc = head.get("ChecksumCRC64NVME")
    if (
        algorithm != CRC64NVME
        or crc != expected
        or head["ContentLength"] != session["size"]
    ):
        return None
    return str(head["VersionId"]), str(crc)


def _copy(
    deps: CommitDeps,
    session: dict[str, Any],
    landing_version: str,
    algorithm: str,
    expected: str,
) -> tuple[str, str] | None:
    """Copy landing to raw; `None` (and the version deleted) on a CRC mismatch."""
    copied = deps.s3.copy_object(
        Bucket=deps.bucket,
        Key=session["raw_key"],
        CopySource={
            "Bucket": deps.bucket,
            "Key": session["landing_key"],
            "VersionId": landing_version,
        },
        ChecksumAlgorithm=CRC64NVME,
    )
    raw_version = copied["VersionId"]
    dest = copied["CopyObjectResult"].get("ChecksumCRC64NVME")
    # A CRC64NVME session is checked end to end. A SHA256 session (import) was
    # verified in landing/ by SHA256; its raw copy is of that exact version, and the
    # CRC S3 computes on the copy is what the row records.
    if dest is None or (algorithm == CRC64NVME and dest != expected):
        deps.s3.delete_object(
            Bucket=deps.bucket, Key=session["raw_key"], VersionId=raw_version
        )
        return None
    return str(raw_version), str(dest)


def commit(
    event: dict[str, str],
    deps: CommitDeps,
    *,
    now: int | None = None,
) -> str:
    """Run the commit for one `{session_id, finalize_token}` delivery.

    Outcomes: SKIPPED (state, token or a live lease not as claimable), INCOMPLETE,
    LOST_CLAIM, COMMITTED. A resume (ACCEPTED past its lease) adopts the raw object
    an earlier run copied, or copies again; the appends are idempotent by key.
    """
    now = int(time.time()) if now is None else now
    token = event["finalize_token"]
    session = sessions.get(deps.table, event["session_id"])
    mode = claim_mode(session, token, now)
    if session is None or mode is None:
        return SKIPPED
    sid = session["session_id"]
    days = retention_days()  # before any state transition

    # Verify in landing/ before anything becomes readable (read-only).
    landing_version, problem = _verify_landing(deps, session)
    if landing_version is None:
        return _incomplete(deps.table, session, expect=mode, reason=problem, now=now)
    algorithm, expected = expected_checksum(session)

    # Claim (or re-claim) before any write to S3 or the catalog.
    if not sessions.transition(
        deps.table,
        sid,
        Transition(
            expect=mode,
            to=sessions.ACCEPTED,
            now=now,
            token=token,
            set_values={
                "landing_version_id": landing_version,
                sessions.COMMIT_LEASE: now + sessions.COMMIT_LEASE_SECONDS,
            },
            lease=sessions.COMMIT_LEASE if mode == sessions.ACCEPTED else None,
        ),
    ):
        return LOST_CLAIM

    adopted = (
        _adoptable(deps, session, algorithm, expected)
        if mode == sessions.ACCEPTED
        else None
    )
    raw = adopted or _copy(deps, session, landing_version, algorithm, expected)
    if raw is None:
        return _incomplete(
            deps.table,
            session,
            expect=sessions.ACCEPTED,
            reason="raw copy checksum mismatch",
            now=now,
        )
    raw_version, dest = raw

    # Lock only the verified version: the mismatch rollback above ran first.
    committed_at = utc_now_naive()
    _lock(
        deps,
        session,
        raw_version,
        committed_at.replace(tzinfo=UTC) + timedelta(days=days),
    )
    asset = ids.asset_id(session["raw_key"], raw_version)
    row = asset_row(
        session,
        asset_id=asset,
        raw_version=raw_version,
        crc64nvme=dest,
        committed_at=committed_at,
    )
    snapshot_id = deps.writer.append_if_absent(
        schema.ASSETS.name, _key(schema.ASSETS, row), [row]
    )
    dataset = dataset_row(session, created_at=committed_at)
    deps.writer.append_if_absent(
        schema.DATASETS_SPEC.name, _key(schema.DATASETS_SPEC, dataset), [dataset]
    )
    sessions.transition(
        deps.table,
        sid,
        Transition(
            expect=sessions.ACCEPTED,
            to=sessions.COMMITTED,
            now=now,
            token=token,
            set_values={
                "raw_version_id": raw_version,
                "asset_id": asset,
                "snapshot_id": str(snapshot_id),
                "expires_at": now + sessions.COMMITTED_TTL_SECONDS,
                "extract_state": sessions.EXTRACT_PENDING,
                sessions.EXTRACT_LEASE: now + sessions.EXTRACT_BACKLOG_SECONDS,
            },
        ),
    )
    queues.send_extract(deps.sqs, deps.extract_queue_url, sid)
    return sessions.COMMITTED


def handler(event: dict[str, Any], _context: object) -> dict[str, list[str]]:
    """Lambda entry point for `asteroidea-commit.fifo` (batch size 1).

    Each record body is `{session_id, finalize_token}`. A missing or invalid
    `RAW_RETENTION_DAYS` raises here, before any record is touched, so SQS redelivers.
    """
    retention_days()
    table = SessionTable(boto3.client("dynamodb"), os.environ["SESSIONS_TABLE"])
    deps = CommitDeps(
        table=table,
        s3=boto3.client("s3"),
        writer=CatalogWriter(load_catalog("glue", type="glue")),
        bucket=os.environ["RAW_BUCKET"],
        sqs=boto3.client("sqs"),
        extract_queue_url=os.environ["EXTRACT_QUEUE_URL"],
    )
    return {"outcomes": run_batch(event, deps)}


def run_batch(
    event: dict[str, Any], deps: CommitDeps, *, now: int | None = None
) -> list[str]:
    """Run each record of an SQS batch through `queues.guarded`."""
    outcomes: list[str] = []
    for body in queues.bodies(event):
        at = int(time.time()) if now is None else now
        guard = queues.Guard(deps.table, "commit", body["session_id"], at)
        outcomes.append(queues.guarded(guard, partial(commit, body, deps, now=at)))
    return outcomes
