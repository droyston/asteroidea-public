"""Re-extract read/derive task: one asset's rows to the derived store.

A Step Functions Map runs this per asset with bounded concurrency. It runs the same
extractor the in-handler re-extract runs (`handler.dispatch`, replace mode), but the
writer only records each catalog write; the operations go to the derived store as one
gzip bundle plus a manifest (`rows.py`). Nothing reaches the catalog here: the extract
FIFO's handler commits the rows serially from a rows-by-pointer message.

On success the result names the manifest (the pointer body); on failure the tracking
item is set FAILED with the error class, message and ``tables_replaced`` empty, as the
handler's re-extract does, and the result's outcome is ERROR. Never raises.
"""

import logging
import os
import time
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Final

import boto3

from asteroidea_ingest import queues, sessions
from asteroidea_ingest.catalog_writer import utc_now_naive
from asteroidea_ingest.extract import bids, retry, rows
from asteroidea_ingest.extract import handler as extract_handler
from asteroidea_ingest.extract.handler import ExtractDeps
from asteroidea_ingest.sessions import SessionTable

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

logger = logging.getLogger(__name__)
DERIVED: Final = "DERIVED"
BUNDLE_TYPE: Final = "application/gzip"
MANIFEST_TYPE: Final = "application/json"


class RowCollector:
    """A writer that records each catalog write, in order, and writes nothing."""

    def __init__(self) -> None:
        """Start with no operations."""
        self.operations: list[rows.Operation] = []

    def _record(
        self,
        op: str,
        table_name: str,
        key: Mapping[str, Any],
        batch: Sequence[Mapping[str, Any]],
    ) -> None:
        copied = [dict(row) for row in batch]
        self.operations.append(rows.Operation(op, table_name, dict(key), copied))

    def replace(
        self,
        table_name: str,
        key: Mapping[str, Any],
        rows: Sequence[Mapping[str, Any]],
    ) -> None:
        """Record a replace."""
        self._record(REPLACE, table_name, key, rows)

    def append_if_absent(
        self,
        table_name: str,
        key: Mapping[str, Any],
        rows: Sequence[Mapping[str, Any]],
    ) -> None:
        """Record an append-if-absent."""
        self._record(APPEND, table_name, key, rows)


REPLACE: Final = rows.REPLACE
APPEND: Final = rows.APPEND


def pointer_body(asset_id: str, run_id: str) -> dict[str, str]:
    """Return the rows-by-pointer message body the extract handler commits."""
    return {
        "asset_id": asset_id,
        "run_id": run_id,
        "manifest_key": rows.manifest_key(asset_id, run_id),
    }


def _derive(body: Mapping[str, str], deps: ExtractDeps) -> dict[str, str]:
    asset_id, run_id = body["asset_id"], body["run_id"]
    store, bucket = deps.derived, deps.derived_bucket
    if store is None or bucket is None:
        msg = "no derived store is configured for the derive task"
        raise rows.RowsError(msg)
    session = extract_handler.asset_session(body)
    work = extract_handler.dispatch(session)
    if work is None:
        msg = f"no extractor for {session['relpath']}"
        raise extract_handler.NoExtractorError(msg)
    collector = RowCollector()
    recording = replace(
        deps, writer=collector, reextract=True, replaced=[], request_lfp_build=None
    )

    def attempt() -> None:
        """Run the extractor once, discarding the operations of an earlier attempt."""
        collector.operations.clear()  # a retried attempt starts over
        work(recording, session, utc_now_naive())

    retry.run(attempt, sleep=deps.sleep)
    extractor = bids.extractor_for(str(session["relpath"]))
    header = {
        "asset_id": asset_id,
        "run_id": run_id,
        "dataset": session["dataset"],
        "relpath": session["relpath"],
        "s3_key": session["raw_key"],
        "s3_version_id": session["raw_version_id"],
        "extractor": extractor,
        "lfp_build": extractor == "nwb",
    }
    bundle, manifest = rows.build(collector.operations, header)
    manifest["bundle"]["key"] = rows.bundle_key(asset_id, run_id)
    store.put_object(
        Bucket=bucket,
        Key=rows.bundle_key(asset_id, run_id),
        Body=bundle,
        ContentType=BUNDLE_TYPE,
    )
    store.put_object(  # last: a manifest names only a bundle already written
        Bucket=bucket,
        Key=rows.manifest_key(asset_id, run_id),
        Body=rows.encode_json(manifest),
        ContentType=MANIFEST_TYPE,
    )
    return pointer_body(asset_id, run_id)


def derive(body: Mapping[str, str], deps: ExtractDeps, *, now: int) -> dict[str, str]:
    """Derive one asset's rows; return the pointer body with ``outcome`` DERIVED.

    ``body`` holds the re-extract message fields (asset_id, dataset, relpath, s3_key,
    s3_version_id, size) and ``run_id``. On failure the tracking item is FAILED and
    the outcome is ERROR (``queues.ERROR``); nothing was committed.
    """
    asset_id, run_id = body["asset_id"], body["run_id"]
    try:
        pointer = _derive(body, deps)
    except Exception as exc:
        tracking = sessions.reextract_id(run_id, asset_id)
        logger.exception("derive %s failed", tracking)
        fields = {
            "reextract_state": sessions.REEXTRACT_FAILED,
            "reextract_error": {
                "class": type(exc).__name__,
                "message": str(exc),
                "tables_replaced": [],
            },
            "tables_replaced": [],
            "updated_at": now,
        }
        try:
            sessions.set_reextract(deps.table, tracking, fields)
        except Exception:
            logger.exception("could not record the outcome of %s", tracking)
        return {"outcome": queues.ERROR, "asset_id": asset_id, "run_id": run_id}
    return {"outcome": DERIVED, **pointer}


def handler_deps() -> ExtractDeps:
    """Build DeriveFunction's dependencies from its environment.

    ``SESSIONS_TABLE`` (the FAILED write), ``RAW_BUCKET`` (the raw read) and
    ``DERIVED_BUCKET`` (the `rows/` writes). No catalog: the writer is a collector,
    which ``derive`` replaces with its own, so the function needs no Glue grant.
    """
    s3 = boto3.client("s3")
    return ExtractDeps(
        table=SessionTable(boto3.client("dynamodb"), os.environ["SESSIONS_TABLE"]),
        s3=s3,
        writer=RowCollector(),
        bucket=os.environ["RAW_BUCKET"],
        derived=s3,
        derived_bucket=os.environ["DERIVED_BUCKET"],
    )


def handler(event: dict[str, str], _context: object) -> dict[str, str]:
    """DeriveFunction entry point: one Map item (asset fields and ``run_id``).

    Returns ``derive``'s result; the state machine sends the pointer body to the
    extract FIFO when the outcome is DERIVED, and nothing otherwise. Never raises.
    """
    return derive(event, handler_deps(), now=int(time.time()))
