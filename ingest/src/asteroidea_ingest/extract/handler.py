"""ExtractFunction: one `{session_id}` message, 9 catalog tables.

Dispatch is by `assets.format` (from the extension): `nwb`, `nifti`, `json` (BIDS
sidecar), `tsv` named `participants.tsv` or ending `_events.tsv`; any other format is
SKIPPED. The claim is
`PENDING` -> `EXTRACTING` (a stale EXTRACTING lease is re-claimable); each table is
appended at most once per key, so a resumed run skips finished tables, and
`asset_headers` is written last. A transient error is retried in the invocation
(`retry.py`); any other failure sets `extract_state = FAILED` and is
re-raised so `queues.guarded` records `extract_error` (class and message) and returns:
the handler never raises.

After every EXTRACTED or SKIPPED outcome, a re-extraction and a rows-by-pointer
commit, one `{"kind": "summary", "dataset"}` message goes to the LFP build queue, so
the dataset summary is rewritten; a failed send is logged, never raised.
"""

import contextlib
import json
import logging
import os
import time
from dataclasses import dataclass, field, replace
from functools import partial
from typing import TYPE_CHECKING, Any, Final, Protocol

import boto3
from botocore.exceptions import ClientError
from pyiceberg.catalog import load_catalog

from asteroidea_ingest import bids_path, queues, sessions
from asteroidea_ingest.catalog_writer import CatalogWriter, utc_now_naive
from asteroidea_ingest.extract import bids, nifti, retry, rows
from asteroidea_ingest.extract.nwb import AssetScope, NwbRows, extract_nwb
from asteroidea_ingest.extract.remote import open_remote
from asteroidea_ingest.sessions import SessionTable

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from datetime import datetime

    from pynwb import NWBHDF5IO

    from asteroidea_ingest.aws_types import S3

logger = logging.getLogger(__name__)
SKIPPED: Final = "SKIPPED"
EXTRACTED: Final = "EXTRACTED"
NOT_CLAIMED: Final = "NOT_CLAIMED"
PRESIGN_SECONDS: Final = 3600
NIFTI_VERSION: Final = "nifti-1"
BIDS_VERSION: Final = "bids-1"
# Table order; asset_headers is written last.
NWB_TABLES: Final = ("series", "electrodes", "units", "intervals", "unit_spikes")
# A re-extraction replaces these per asset; `subjects` and `sessions` stay
# append-if-absent.
REPLACED_TABLES: Final = frozenset({*NWB_TABLES, "events", "asset_headers"})


# A pointer message may append only to these (what `_append` sends to append_if_absent
# on the re-extract path); every other table it names must be in REPLACED_TABLES.
APPENDED_TABLES: Final = frozenset({"subjects", "sessions"})


class NoExtractorError(ValueError):
    """The asset's format has no extractor; a re-extraction records it as FAILED."""


class RowWriter(Protocol):
    """The two catalog writes an extractor makes (``CatalogWriter`` has both)."""

    def replace(
        self, table_name: str, key: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]
    ) -> object:
        """Replace the rows matching ``key`` with ``rows``."""
        ...

    def append_if_absent(
        self, table_name: str, key: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]
    ) -> object:
        """Append ``rows`` unless a row matching ``key`` exists."""
        ...


@dataclass(frozen=True, slots=True)
class ExtractDeps:
    """What one extraction needs; ``open_nwb`` takes a presigned GET URL.

    With ``reextract`` set, the per-asset tables are replaced (one commit each) and
    ``replaced`` collects the names of those already replaced.
    """

    table: SessionTable
    s3: S3
    writer: RowWriter
    bucket: str
    open_nwb: Callable[[str], NWBHDF5IO] = open_remote
    sleep: Callable[[float], None] = time.sleep
    reextract: bool = False
    replaced: list[str] = field(default_factory=list)
    # Sends one LFP build message; None sends nothing.
    request_lfp_build: Callable[[dict[str, str]], None] | None = None
    # Sends one dataset summary message (the same queue); None sends nothing.
    request_summary: Callable[[dict[str, str]], None] | None = None
    # The derived store a rows-by-pointer message names; None refuses one.
    derived: rows.RowStore | None = None
    derived_bucket: str | None = None


def _finish(deps: ExtractDeps, sid: str, state: str, now: int) -> None:
    """Move EXTRACTING to ``state`` (a lost claim is not an error)."""
    try:
        deps.table.ddb.update_item(
            TableName=deps.table.name,
            Key=sessions.marshal({"session_id": sid}),
            UpdateExpression="SET #extract_state = :state, #updated_at = :now",
            ConditionExpression="#extract_state = :extracting",
            ExpressionAttributeNames={
                "#extract_state": "extract_state",
                "#updated_at": "updated_at",
            },
            ExpressionAttributeValues=sessions.marshal(
                {":state": state, ":extracting": sessions.EXTRACTING, ":now": now}
            ),
        )
    except ClientError as err:
        if err.response["Error"]["Code"] != sessions.CONDITIONAL_FAILED:
            raise


def _read(deps: ExtractDeps, session: dict[str, Any], length: int) -> bytes:
    """Return the first ``length`` bytes of the raw object version (a ranged GET)."""
    response = deps.s3.get_object(
        Bucket=deps.bucket,
        Key=session["raw_key"],
        VersionId=session["raw_version_id"],
        Range=f"bytes=0-{max(length, 1) - 1}",
    )
    return bytes(response["Body"].read())


def _text(deps: ExtractDeps, session: dict[str, Any]) -> str:
    size = int(session["size"])
    if size > bids.MAX_TEXT_BYTES:
        msg = f"text file is {size} bytes, over the {bids.MAX_TEXT_BYTES} limit"
        raise bids.BidsError(msg)
    return _read(deps, session, size).decode("utf-8")


def _append(
    deps: ExtractDeps, table: str, key: dict[str, Any], rows: list[dict[str, Any]]
) -> None:
    if deps.reextract and table in REPLACED_TABLES:
        deps.writer.replace(table, key, rows)  # no rows: clears the stale ones
        if table not in deps.replaced:
            deps.replaced.append(table)
    elif rows:
        deps.writer.append_if_absent(table, key, rows)


def _header(
    session: dict[str, Any], fmt: str, version: str, at: datetime
) -> dict[str, Any]:
    return {
        "asset_id": session["asset_id"],
        "dataset": session["dataset"],
        "format": fmt,
        "extractor_version": version,
        "extracted_at": at,
    }


def _extract_nwb(deps: ExtractDeps, session: dict[str, Any], at: datetime) -> None:
    parts = bids_path.parse(session["relpath"])
    url = deps.s3.generate_presigned_url(
        ClientMethod="get_object",
        Params={
            "Bucket": deps.bucket,
            "Key": session["raw_key"],
            "VersionId": session["raw_version_id"],
        },
        ExpiresIn=PRESIGN_SECONDS,
    )
    who = AssetScope(
        session["asset_id"], session["dataset"], parts.subject, parts.bids_session
    )
    with contextlib.closing(deps.open_nwb(url)) as io:
        rows: NwbRows = extract_nwb(io.read(), who, at)
    asset_key = {"asset_id": who.asset_id}
    for name in NWB_TABLES:
        _append(deps, name, asset_key, getattr(rows, name))
    for row in rows.subjects:
        key = {"dataset": row["dataset"], "subject": row["subject"]}
        _append(deps, "subjects", key, [row])
    for row in rows.sessions:
        key = {k: row[k] for k in ("dataset", "subject", "bids_session")}
        _append(deps, "sessions", key, [row])
    _append(deps, "asset_headers", asset_key, rows.asset_headers)
    _request_lfp_build(deps, session)


def _request_lfp_build(deps: ExtractDeps, session: dict[str, Any]) -> None:
    """Queue the LFP index and overview build of this NWB version.

    Called once the NWB tables are written, on the normal and the re-extract path.
    Never raises: a failed send is logged and the extraction result is unchanged.
    """
    if deps.request_lfp_build is None:
        return
    message = {
        "asset_id": str(session["asset_id"]),
        "dataset": str(session["dataset"]),
        "s3_key": str(session["raw_key"]),
        "s3_version_id": str(session["raw_version_id"]),
    }
    try:
        deps.request_lfp_build(message)
    except Exception:
        logger.exception("could not queue the LFP build of %s", message["asset_id"])


def _request_summary(deps: ExtractDeps, dataset: str) -> None:
    """Queue the rewrite of the dataset's summary; never raises.

    Called after every EXTRACTED or SKIPPED outcome, a re-extraction and a
    rows-by-pointer commit. A failed send is logged and the outcome is unchanged.
    """
    if deps.request_summary is None:
        return
    try:
        deps.request_summary({"kind": "summary", "dataset": dataset})
    except Exception:
        logger.exception("could not queue the summary of %s", dataset)


class StandardQueue(Protocol):
    """The one SQS call the LFP build trigger makes (a standard queue: no group id)."""

    def send_message(self, **kwargs: str) -> object:
        """Send one message."""
        ...


def sqs_lfp_sender(
    sqs: StandardQueue, queue_url: str
) -> Callable[[dict[str, str]], None]:
    """Return a sender of one LFP build message to the standard queue at queue_url."""

    def send(message: dict[str, str]) -> None:
        """Send `message` as JSON to the queue; return nothing."""
        sqs.send_message(QueueUrl=queue_url, MessageBody=json.dumps(message))

    return send


def _extract_nifti(deps: ExtractDeps, session: dict[str, Any], at: datetime) -> None:
    extension = bids_path.parse(session["relpath"]).extension
    head = _read(deps, session, nifti.range_bytes(extension))
    row = _header(session, "nifti", NIFTI_VERSION, at) | nifti.parse(head, extension)
    _append(deps, "asset_headers", {"asset_id": session["asset_id"]}, [row])


def _extract_json(deps: ExtractDeps, session: dict[str, Any], at: datetime) -> None:
    row = _header(session, "json", BIDS_VERSION, at) | {
        "sidecar_json": bids.sidecar_json(_text(deps, session))
    }
    _append(deps, "asset_headers", {"asset_id": session["asset_id"]}, [row])


def _extract_participants(
    deps: ExtractDeps, session: dict[str, Any], at: datetime
) -> None:
    for row in bids.participants_rows(
        _text(deps, session),
        dataset=session["dataset"],
        source_asset_id=session["asset_id"],
        extracted_at=at,
    ):
        key = {"dataset": row["dataset"], "subject": row["subject"]}
        _append(deps, "subjects", key, [row])
    row = _header(session, "tsv", BIDS_VERSION, at)
    _append(deps, "asset_headers", {"asset_id": session["asset_id"]}, [row])


def _extract_events(deps: ExtractDeps, session: dict[str, Any], at: datetime) -> None:
    parts = bids_path.parse(session["relpath"])
    scope = AssetScope(
        session["asset_id"], session["dataset"], parts.subject, parts.bids_session
    ).row()
    rows = bids.events_rows(
        _text(deps, session),
        scope=scope,
        events_for=bids_path.entity_key(session["relpath"]),
    )
    asset_key = {"asset_id": session["asset_id"]}
    _append(deps, "events", asset_key, rows)
    header = _header(session, "tsv", BIDS_VERSION, at)
    _append(deps, "asset_headers", asset_key, [header])


def dispatch(session: dict[str, Any]) -> Callable[..., None] | None:
    """Return the extractor of the session's format, or None (no extractor)."""
    works = {
        "nwb": _extract_nwb,
        "nifti": _extract_nifti,
        "json": _extract_json,
        "participants": _extract_participants,
        "events": _extract_events,
    }
    name = bids.extractor_for(str(session["relpath"]))
    return None if name is None else works[name]


def extract(event: dict[str, str], deps: ExtractDeps, *, now: int | None = None) -> str:
    """Run the extraction for one `{session_id}` delivery.

    Outcomes: NOT_CLAIMED (not PENDING, or a live lease), SKIPPED (a format with no
    extractor), EXTRACTED. An exception sets FAILED and propagates to the guard.
    """
    now = int(time.time()) if now is None else now
    session = sessions.get(deps.table, event["session_id"])
    if session is None or session.get("state") != sessions.COMMITTED:
        return NOT_CLAIMED
    sid = session["session_id"]
    if not sessions.claim_extract(deps.table, sid, now):
        return NOT_CLAIMED
    try:
        work = dispatch(session)
        if work is None:
            _finish(deps, sid, SKIPPED, now)
            _request_summary(deps, str(session["dataset"]))
            return SKIPPED
        retry.run(lambda: work(deps, session, utc_now_naive()), sleep=deps.sleep)
    except Exception:
        _finish(deps, sid, queues.EXTRACT_FAILED, now)
        raise
    _finish(deps, sid, queues.EXTRACT_DONE, now)
    _request_summary(deps, str(session["dataset"]))
    return EXTRACTED


def handler(event: dict[str, Any], _context: object) -> dict[str, list[str]]:
    """Lambda entry point for `asteroidea-extract.fifo` (batch size 1); never raises."""
    queue_url = os.environ.get("LFP_BUILD_QUEUE_URL")
    s3 = boto3.client("s3")
    sender = (
        None if queue_url is None else sqs_lfp_sender(boto3.client("sqs"), queue_url)
    )
    deps = ExtractDeps(
        table=SessionTable(boto3.client("dynamodb"), os.environ["SESSIONS_TABLE"]),
        s3=s3,
        writer=CatalogWriter(load_catalog("glue", type="glue")),
        bucket=os.environ["RAW_BUCKET"],
        request_lfp_build=sender,
        request_summary=sender,
        derived=s3,
        derived_bucket=os.environ.get("DERIVED_BUCKET"),
    )
    return {"outcomes": run_batch(event, deps)}


REEXTRACTED: Final = "REEXTRACTED"


def asset_session(body: Mapping[str, str]) -> dict[str, Any]:
    """Return the session-shaped dict the extractors read, from a re-extract message."""
    return {
        "asset_id": body["asset_id"],
        "dataset": body["dataset"],
        "relpath": body["relpath"],
        "raw_key": body["s3_key"],
        "raw_version_id": body["s3_version_id"],
        "size": int(body["size"]),
    }


def _run_reextract(body: Mapping[str, str], deps: ExtractDeps) -> None:
    session = asset_session(body)
    work = dispatch(session)
    if work is None:
        msg = f"no extractor for {session['relpath']}"
        raise NoExtractorError(msg)
    retry.run(lambda: work(deps, session, utc_now_naive()), sleep=deps.sleep)
    _request_summary(deps, str(session["dataset"]))


def _settle(
    tracking: str, deps: ExtractDeps, work: Callable[[ExtractDeps], None], now: int
) -> str:
    """Run ``work`` in replace mode, then mark the tracking item; never raises.

    DONE with ``tables_replaced``, or FAILED with the error class, message and the
    tables already replaced. Returns REEXTRACTED, or ERROR.
    """
    deps = replace(deps, reextract=True, replaced=[])
    fields: dict[str, Any] = {
        "reextract_state": sessions.REEXTRACT_DONE,
        "updated_at": now,
    }
    outcome = REEXTRACTED
    try:
        work(deps)
    except Exception as exc:
        logger.exception("re-extraction %s failed", tracking)
        fields = {
            "reextract_state": sessions.REEXTRACT_FAILED,
            "reextract_error": {
                "class": type(exc).__name__,
                "message": str(exc),
                "tables_replaced": list(deps.replaced),
            },
            "updated_at": now,
        }
        outcome = queues.ERROR
    fields["tables_replaced"] = list(deps.replaced)
    try:
        sessions.set_reextract(deps.table, tracking, fields)
    except Exception:
        logger.exception("could not record the outcome of %s", tracking)
        return queues.ERROR
    return outcome


def reextract(body: dict[str, str], deps: ExtractDeps, *, now: int) -> str:
    """Re-extract one asset from the message's own fields; never raises.

    Runs the format's extractor with ``replace`` on each per-asset table, then marks
    the tracking item DONE, or FAILED with the error class, message and the tables
    already replaced. Returns REEXTRACTED, or ERROR.
    """
    return _settle(body["session_id"], deps, partial(_run_reextract, body), now)


def _check_operations(manifest: Mapping[str, Any], asset_id: str, run_id: str) -> None:
    """Refuse a manifest of another asset or run, or one naming a table it may not."""
    if manifest.get("asset_id") != asset_id or manifest.get("run_id") != run_id:
        msg = (
            f"manifest is for asset {manifest.get('asset_id')} run "
            f"{manifest.get('run_id')}, not asset {asset_id} run {run_id}"
        )
        raise rows.RowsError(msg)
    if manifest["bundle"].get("key") != rows.bundle_key(asset_id, run_id):
        msg = f"manifest names bundle {manifest['bundle'].get('key')}, not this run's"
        raise rows.RowsError(msg)
    listed: list[dict[str, Any]] = list(manifest["operations"])
    for item in listed:
        op, table, key = item.get("op"), item.get("table"), item.get("key")
        if op == rows.REPLACE and table in REPLACED_TABLES:
            if key != {"asset_id": asset_id}:
                msg = f"manifest replaces {table} rows of key {key}, not this asset"
                raise rows.RowsError(msg)
        elif op == rows.APPEND and table in APPENDED_TABLES:
            if not isinstance(key, dict) or key.get("dataset") != manifest["dataset"]:
                msg = f"manifest appends {table} rows of key {key}, not this dataset"
                raise rows.RowsError(msg)
        else:
            msg = (
                f"manifest names table {table} ({op}), which a re-extract never writes"
            )
            raise rows.RowsError(msg)
    if not listed or listed[-1].get("table") != "asset_headers":
        msg = "manifest does not end with asset_headers"
        raise rows.RowsError(msg)


def _commit_pointer(asset_id: str, run_id: str, key: str, deps: ExtractDeps) -> None:
    """Read, check, then commit a derive's operations in order (asset_headers last).

    Every check runs before the first commit, so a refused manifest or bundle
    replaces nothing.
    """
    if deps.derived is None or deps.derived_bucket is None:
        msg = "no derived store is configured for a rows-by-pointer message"
        raise rows.RowsError(msg)
    if key != rows.manifest_key(asset_id, run_id):
        msg = f"manifest key {key} is not this asset and run's"
        raise rows.RowsError(msg)
    manifest = rows.read_manifest(deps.derived, deps.derived_bucket, key)
    _check_operations(manifest, asset_id, run_id)
    operations = rows.read_operations(deps.derived, deps.derived_bucket, manifest)
    for op in operations:
        if op.op == rows.REPLACE:
            deps.writer.replace(op.table, op.key, op.rows)
            if op.table not in deps.replaced:
                deps.replaced.append(op.table)
        else:
            deps.writer.append_if_absent(op.table, op.key, op.rows)
    if manifest.get("lfp_build"):
        _request_lfp_build(
            deps,
            {
                "asset_id": asset_id,
                "dataset": manifest["dataset"],
                "raw_key": manifest["s3_key"],
                "raw_version_id": manifest["s3_version_id"],
            },
        )
    _request_summary(deps, str(manifest["dataset"]))


def commit_rows(body: dict[str, str], deps: ExtractDeps, *, now: int) -> str:
    """Commit a derive's rows by pointer; never raises.

    The body is ``{asset_id, run_id, manifest_key}``; the tracking item is
    ``reextract:<run_id>:<asset_id>`` and ends DONE or FAILED as ``reextract`` does.
    """
    asset_id, run_id = body["asset_id"], body["run_id"]
    tracking = sessions.reextract_id(run_id, asset_id)

    def work(d: ExtractDeps) -> None:
        """Commit the rows the message points at; raise on a bad manifest or bundle."""
        _commit_pointer(asset_id, run_id, body["manifest_key"], d)

    return _settle(tracking, deps, work, now)


def run_batch(
    event: dict[str, Any], deps: ExtractDeps, *, now: int | None = None
) -> list[str]:
    """Run each record of an SQS batch: a re-extraction, else `queues.guarded`."""
    outcomes: list[str] = []
    for body in queues.bodies(event):
        at = int(time.time()) if now is None else now
        if "manifest_key" in body:
            outcomes.append(commit_rows(body, deps, now=at))
            continue
        if "asset_id" in body:
            outcomes.append(reextract(body, deps, now=at))
            continue
        guard = queues.Guard(deps.table, "extract", body["session_id"], at)
        outcomes.append(queues.guarded(guard, partial(extract, body, deps, now=at)))
    return outcomes
