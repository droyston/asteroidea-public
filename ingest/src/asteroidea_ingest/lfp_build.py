"""LfpBuildFunction: the LFP index and overview of one NWB file version.

One message ``{asset_id, dataset, s3_key, s3_version_id}`` from the LFP build queue
(standard SQS, its own DLQ; never the extract FIFO, so the extract queue's ordering
is untouched). The
builder opens the raw version once with h5py through the SDK reader (``RemoteFile``
under ``BlockFile``, as ``compute.py`` does), lists every ``ElectricalSeries`` group
and builds each one's chunk-offset index (``lfp_index.build_index``). It then closes
the h5py file, so the block cache is freed, and builds each overview
(``lfp_overview.build_overview``) from exact byte ranges via the index: one chunk
column at a time, each chunk fetched once per column (``lfp_index.plan_column``),
with up to ``FETCH_WORKERS`` ranged GETs in flight.

A series that cannot be indexed (timestamps and no rate, unchunked, shuffle or another
filter, a non-numeric dtype) or summarised (under one segment) is skipped with its
reason, logged and listed in the manifest. Objects go to the derived bucket:

    lfp/<asset_id>/<s3_version_id>/<series_slug>/index.bin
    lfp/<asset_id>/<s3_version_id>/<series_slug>/overview.bin
    lfp/<asset_id>/<s3_version_id>/manifest.json   (written last)

The manifest is written after every data object, so a reader that finds it finds
every object it lists. The keys depend only on the message, so a rebuild of the same
asset version rewrites the same keys. No catalog row is written.

A message with `kind` `summary` or `session_summary` builds a dataset or session
summary instead (`dataset_summary.run_message`); a message with no `kind` is an LFP
build. The summary kinds read the catalog through Athena and send to this queue, so
their clients are made only when such a message arrives.

This module is the compute image's (CMD override); it imports h5py and numpy.
"""

import hashlib
import json
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING, Any, Final, Protocol

import boto3
import h5py
from botocore.config import Config

from asteroidea._blocks import BlockFile
from asteroidea._remote import RemoteFile, Source
from asteroidea_ingest import dataset_summary, lfp_index, lfp_overview
from asteroidea_ingest.lfp_index import LfpIndex, LfpIndexError
from asteroidea_ingest.lfp_overview import OverviewError

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    import numpy as np
    from numpy.typing import NDArray

    from asteroidea._remote import GetReply, ObjectClient

logger = logging.getLogger(__name__)

BUILDER_VERSION: Final = "lfp-build-1"
MANIFEST_VERSION: Final = 1
PREFIX: Final = "lfp"
INDEX_NAME: Final = "index.bin"
OVERVIEW_NAME: Final = "overview.bin"
MANIFEST_NAME: Final = "manifest.json"
NEURODATA_TYPE: Final = "ElectricalSeries"
LFP: Final = "lfp"
FETCH_WORKERS: Final = 8
# Only LFP-rate series are indexed and summarised: a 30 kHz raw series would be read
# whole and could run past the 900 s timeout into the DLQ.
MAX_LFP_RATE_HZ: Final = 2500.0
# One ranged GET carries at most this many bytes; a longer merged range is split.
MAX_GET_BYTES: Final = 8 * 1024 * 1024
_SLUG_UNSAFE: Final = re.compile(r"[^A-Za-z0-9_.-]+")
_SLUG_HASH_CHARS: Final = 8


class DerivedS3(Protocol):
    """The S3 calls the builder makes: ranged GETs, the size HEAD, the writes."""

    def get_object(self, **kwargs: str | int) -> GetReply:
        """Read a byte range of one object version."""
        ...

    def head_object(self, **kwargs: str) -> Mapping[str, Any]:
        """Return the object version's metadata (``ContentLength``)."""
        ...

    def put_object(self, **kwargs: str | bytes) -> object:
        """Write one derived object."""
        ...


@dataclass(frozen=True, slots=True)
class BuildMessage:
    """One build request: an asset and its pinned raw object version."""

    asset_id: str
    dataset: str
    s3_key: str
    s3_version_id: str

    @classmethod
    def parse(cls, body: Mapping[str, object]) -> BuildMessage:
        """Read the four fields; a missing or empty one is a ``ValueError``."""
        values = {name: body.get(name) for name in ("asset_id", "dataset")}
        values |= {name: body.get(name) for name in ("s3_key", "s3_version_id")}
        missing = [k for k, v in values.items() if not isinstance(v, str) or not v]
        if missing:
            msg = f"build message lacks {', '.join(missing)}"
            raise ValueError(msg)
        return cls(**{k: str(v) for k, v in values.items()})

    def body(self) -> dict[str, str]:
        """Return the JSON-ready message body."""
        return {
            "asset_id": self.asset_id,
            "dataset": self.dataset,
            "s3_key": self.s3_key,
            "s3_version_id": self.s3_version_id,
        }


@dataclass(frozen=True, slots=True)
class BuildDeps:
    """The client and buckets of one build."""

    s3: DerivedS3
    raw_bucket: str
    derived_bucket: str
    workers: int = FETCH_WORKERS


def series_slug(series_path: str) -> str:
    """Return the key segment of a series path: readable, S3-safe and unique.

    ``/processing/ecephys/LFP_micro/ElectricalSeries`` gives
    ``processing.ecephys.LFP_micro.ElectricalSeries-<8 hex>``; the hash is of the
    exact path, so two paths that sanitise alike still get two slugs.
    """
    path = series_path.strip("/")
    readable = _SLUG_UNSAFE.sub("_", path.replace("/", "."))
    digest = hashlib.sha256(path.encode()).hexdigest()[:_SLUG_HASH_CHARS]
    return f"{readable}-{digest}"


def version_prefix(asset_id: str, s3_version_id: str) -> str:
    """Return ``lfp/<asset_id>/<s3_version_id>``, the prefix of one asset version."""
    return f"{PREFIX}/{asset_id}/{s3_version_id}"


def manifest_key(asset_id: str, s3_version_id: str) -> str:
    """Return the manifest's key for one asset version."""
    return f"{version_prefix(asset_id, s3_version_id)}/{MANIFEST_NAME}"


def series_key(asset_id: str, s3_version_id: str, series_path: str, name: str) -> str:
    """Return the key of one series' ``index.bin`` or ``overview.bin``."""
    slug = series_slug(series_path)
    return f"{version_prefix(asset_id, s3_version_id)}/{slug}/{name}"


def _attr_text(value: object) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


def find_series(h5file: h5py.File) -> list[str]:
    """Return the path of every ``ElectricalSeries`` group, sorted, with a leading /."""
    found: list[str] = []

    def visit(name: str, obj: object) -> None:
        """Collect the path of each ElectricalSeries group; return nothing."""
        if not isinstance(obj, h5py.Group):
            return
        kind = obj.attrs.get("neurodata_type")
        if kind is not None and _attr_text(kind) == NEURODATA_TYPE:
            found.append(f"/{name}")

    h5file.visititems(visit)
    return sorted(found)


class RangeFetcher:
    """Fetches byte ranges of one object version with parallel ranged GETs.

    Each GET is one ``RemoteFile`` read (the SDK reader: version-pinned, one request
    per read, renewed once on expired credentials). A range longer than
    ``MAX_GET_BYTES`` is fetched in parts and joined.
    """

    def __init__(self, source: Source, s3: ObjectClient, workers: int) -> None:
        """Hold the source; no request is made yet."""
        self.source = source
        self.s3 = s3
        self.workers = workers
        self.requests = 0
        self.bytes_fetched = 0

    def _get(self, part: tuple[int, int]) -> bytes:
        start, stop = part
        remote = RemoteFile(self.source, lambda: self.s3, lambda: self.s3)
        remote.seek(start)
        buffer = bytearray(stop - start)
        got = remote.readinto(buffer)
        if got != stop - start:
            msg = f"ranged read [{start}, {stop}) returned {got} bytes"
            raise lfp_index.ChunkDataError(msg)
        return bytes(buffer)

    def fetch(self, ranges: list[lfp_index.ByteRange]) -> list[bytes]:
        """Return the bytes of each range, in order."""
        parts: list[tuple[int, int]] = []
        owners: list[int] = []
        for number, byte_range in enumerate(ranges):
            for start in range(byte_range.start, byte_range.stop, MAX_GET_BYTES):
                parts.append((start, min(start + MAX_GET_BYTES, byte_range.stop)))
                owners.append(number)
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            blobs = list(pool.map(self._get, parts))
        self.requests += len(parts)
        self.bytes_fetched += sum(len(b) for b in blobs)
        joined: list[list[bytes]] = [[] for _ in ranges]
        for owner, blob in zip(owners, blobs, strict=True):
            joined[owner].append(blob)
        return [b"".join(pieces) for pieces in joined]


def column_reader(
    index: LfpIndex, fetch: Callable[[list[lfp_index.ByteRange]], list[bytes]]
) -> Callable[[int, int], NDArray[np.generic]]:
    """Return ``read(first, stop)`` for ``build_overview``: one chunk column per call.

    ``build_overview`` asks for exactly one chunk column of channels per call; each
    call fetches that column's chunks once (``plan_column``) and decodes all of its
    channels together.
    """
    width = index.chunk_shape[1]

    def read(first: int, stop: int) -> NDArray[np.generic]:
        """Return channels `first` to `stop` of one chunk column; else OverviewError."""
        column = first // width
        if first != column * width or stop > min((column + 1) * width, index.shape[1]):
            msg = f"channels {first}..{stop - 1} are not one chunk column of {width}"
            raise OverviewError(msg)
        ranges = lfp_index.plan_column(index, column)
        block = lfp_index.decode_column(index, fetch(ranges), column)
        return block[:, : stop - first]

    return read


def _skip(path: str, reason: str) -> dict[str, str]:
    logger.warning("LFP build skips %s: %s", path, reason)
    return {"path": path, "reason": reason}


def _put(deps: BuildDeps, key: str, body: bytes, content_type: str) -> None:
    deps.s3.put_object(
        Bucket=deps.derived_bucket, Key=key, Body=body, ContentType=content_type
    )


def _indexes(
    source: Source, deps: BuildDeps
) -> tuple[list[LfpIndex], list[dict[str, str]]]:
    """Open the file once with h5py; index every ElectricalSeries it holds."""
    built: list[LfpIndex] = []
    skipped: list[dict[str, str]] = []
    remote = RemoteFile(source, lambda: deps.s3, lambda: deps.s3)
    with h5py.File(BlockFile(remote), "r") as h5file:
        for path in find_series(h5file):
            try:
                index = lfp_index.build_index(h5file, path)
            except LfpIndexError as err:
                skipped.append(_skip(path, f"{type(err).__name__}: {err}"))
                continue
            if index.rate_hz > MAX_LFP_RATE_HZ:
                reason = f"rate {index.rate_hz:g} Hz over {MAX_LFP_RATE_HZ:g}"
                skipped.append(_skip(path, reason))
                continue
            built.append(index)
    return built, skipped


def build(message: BuildMessage, deps: BuildDeps) -> dict[str, Any]:
    """Write every series' index and overview, then the manifest; return it."""
    started = time.monotonic()
    head = deps.s3.head_object(
        Bucket=deps.raw_bucket, Key=message.s3_key, VersionId=message.s3_version_id
    )
    source = Source(
        bucket=deps.raw_bucket,
        key=message.s3_key,
        version_id=message.s3_version_id,
        size=int(head["ContentLength"]),
    )
    indexes, skipped = _indexes(source, deps)
    fetcher = RangeFetcher(source, deps.s3, deps.workers)
    entries: list[dict[str, Any]] = []
    for index in indexes:
        try:
            overview = lfp_overview.build_overview(
                column_reader(index, fetcher.fetch), index
            )
        except OverviewError as err:
            skipped.append(_skip(index.series_path, f"OverviewError: {err}"))
            continue
        index_blob = lfp_index.dumps(index)
        overview_blob = lfp_overview.dumps(overview)
        keys = {
            name: series_key(
                message.asset_id, message.s3_version_id, index.series_path, name
            )
            for name in (INDEX_NAME, OVERVIEW_NAME)
        }
        _put(deps, keys[INDEX_NAME], index_blob, "application/octet-stream")
        _put(deps, keys[OVERVIEW_NAME], overview_blob, "application/octet-stream")
        entries.append(
            {
                "path": index.series_path,
                "slug": series_slug(index.series_path),
                "shape": list(index.shape),
                "chunk_shape": list(index.chunk_shape),
                "dtype": index.dtype,
                "rate_hz": index.rate_hz,
                "starting_time": index.starting_time,
                "index_key": keys[INDEX_NAME],
                "index_bytes": len(index_blob),
                "overview_key": keys[OVERVIEW_NAME],
                "overview_bytes": len(overview_blob),
                "overview_shape": list(overview.db.shape),
            }
        )
    manifest: dict[str, Any] = {
        "manifest_version": MANIFEST_VERSION,
        "builder_version": BUILDER_VERSION,
        "built_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "index_format": lfp_index.FORMAT_VERSION,
        "overview_format": lfp_overview.FORMAT_VERSION,
        **message.body(),
        "size_bytes": source.size,
        "series": entries,
        "skipped": skipped,
        "requests": fetcher.requests,
        "bytes_fetched": fetcher.bytes_fetched,
        "build_seconds": round(time.monotonic() - started, 3),
    }
    _put(
        deps,
        manifest_key(message.asset_id, message.s3_version_id),
        json.dumps(manifest, indent=1).encode(),
        "application/json",
    )
    logger.info(
        "LFP build %s@%s: %d series, %d skipped, %.1f s",
        message.asset_id,
        message.s3_version_id,
        len(entries),
        len(skipped),
        manifest["build_seconds"],
    )
    return manifest


def run_batch(
    event: Mapping[str, Any],
    deps: BuildDeps,
    summaries: Callable[[], dataset_summary.SummaryDeps] | None = None,
) -> list[str]:
    """Build each record of an SQS batch; a failure raises, so SQS redelivers.

    A record with no `kind` is an LFP build; `summary` and `session_summary` go to
    `dataset_summary.run_message` with the deps `summaries` makes (once per batch).
    Any other kind is a ``ValueError``.
    """
    done: list[str] = []
    made: dataset_summary.SummaryDeps | None = None
    for record in event.get("Records", []):
        body = json.loads(record["body"])
        kind = body.get("kind", LFP)
        if kind in dataset_summary.KINDS:
            if summaries is None:
                msg = f"a {kind} message needs the summary clients"
                raise ValueError(msg)
            made = made or summaries()
            done.append(dataset_summary.run_message(body, made))
            continue
        if kind != LFP:
            msg = f"unknown build message kind {kind!r}"
            raise ValueError(msg)
        manifest = build(BuildMessage.parse(body), deps)
        done.append(str(manifest["asset_id"]))
    return done


def summary_deps(deps: BuildDeps) -> dataset_summary.SummaryDeps:
    """Return the summary kinds' clients from the environment the stack sets.

    `ATHENA_WORKGROUP`, `GLUE_DATABASE` and `LFP_BUILD_QUEUE_URL` (this queue, for
    the session fan-out); a missing one is a ``KeyError`` naming it.
    """
    queue_url = os.environ["LFP_BUILD_QUEUE_URL"]
    sqs = boto3.client("sqs")

    def send(message: dict[str, str]) -> None:
        """Send one message to this builder's own queue; return nothing."""
        sqs.send_message(QueueUrl=queue_url, MessageBody=json.dumps(message))

    return dataset_summary.SummaryDeps(
        athena=boto3.client("athena"),
        workgroup=os.environ["ATHENA_WORKGROUP"],
        database=os.environ["GLUE_DATABASE"],
        s3=deps.s3,
        raw_bucket=deps.raw_bucket,
        derived_bucket=deps.derived_bucket,
        send=send,
    )


def handler(event: dict[str, Any], _context: object) -> dict[str, list[str]]:
    """Lambda entry point for the LFP build queue (batch size 1).

    A failure propagates: the queue redelivers it, then moves it to the DLQ.
    """
    deps = BuildDeps(
        s3=boto3.client("s3", config=Config(signature_version="s3v4")),
        raw_bucket=os.environ["RAW_BUCKET"],
        derived_bucket=os.environ["DERIVED_BUCKET"],
    )
    return {"built": run_batch(event, deps, partial(summary_deps, deps))}
