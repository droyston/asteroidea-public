"""Dataset and session summaries: the LFP builder's `summary` and `session_summary`.

A `{"kind": "summary", "dataset"}` message builds the per-dataset summary from nine
Athena reads of the catalog tables, the studies route's seven texts scoped to the
dataset (its stored `catalog` entry), and, for each NWB file, the event types the
`/compute/events` route resolves (`ComputeApi.events_body`). It writes
`summary/<dataset>/_dataset.json` conditionally, then sends one
`{"kind": "session_summary", "dataset", "subject", "session"}` message per qualifying
session (a units table and at least one `average` event type) to the builder's own
queue, so each invocation stays bounded.

A `session_summary` message builds the per-session summary of the session's NWB file
with the most units (ties by `asset_id`): the body `GET /compute/events` returns for
the file (`events.json`), the first page `GET /catalog/assets/{asset_id}/tables/units`
returns (`units.json`, without the route's `query_ms`; the manifest's `units_table`
says whether more rows exist), and per average event type, the body
`GET /compute/rates` returns at the display settings and the body `GET
/compute/tuning` returns for the type's first attribute that resolves. Each comes from
the route's own function (`ComputeApi.events_body`, `CatalogApi.table_body`,
`ComputeApi.rates_body`, `ComputeApi.tuning_body`), so a stored part equals the route
body for the same inputs. Parts go under
`summary/<dataset>/<subject>/<session>/<digest>/` with `manifest.json` last; they are
immutable under their digest, so a build whose manifest already exists only moves the
pointer `latest.json`.

`_dataset.json` and `latest.json` are written only when their `as_of` is not older
than the stored one, with `IfMatch` on the ETag read (or `IfNoneMatch: *` when there
is none), and re-read and retried on 412.

The modality table is an open set: a `(kind, name)` pair not in `MODALITIES` is kept
as `other:<name>`, never dropped.

This module runs in the compute image (the LFP builder's). It reads `compute_api` and
`lfp_build` attributes only inside functions, because `compute_api` imports
`lfp_build`, which imports this module.
"""

import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from botocore.exceptions import ClientError

from asteroidea import analysis
from asteroidea_ingest import catalog_api, compute_api, lfp_build
from asteroidea_ingest.api_common import ApiError
from asteroidea_ingest.athena_query import AthenaRunner
from asteroidea_ingest.aws_types import S3_NOT_FOUND

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping
    from typing import Unpack

    from asteroidea_ingest.aws_types import (
        Athena,
        QueryIdArgs,
        QueryResultsArgs,
        StartQueryArgs,
    )

logger = logging.getLogger(__name__)

SCHEMA: Final = "asteroidea.dataset-summary/1"
# A reader accepts every minor version of the major it knows: a minor adds parts.
SESSION_SCHEMA_MAJOR: Final = "asteroidea.session-summary/1"
SESSION_SCHEMA: Final = f"{SESSION_SCHEMA_MAJOR}.1"
# Part of the digest's code input: a builder that stores other parts moves it.
BUILDER_VERSION: Final = "summary-2"
PREFIX: Final = catalog_api.SUMMARY_PREFIX
DATASET_FILE: Final = catalog_api.SUMMARY_FILE
LATEST_FILE: Final = "latest.json"
MANIFEST_FILE: Final = "manifest.json"
EVENTS_PART: Final = "events.json"
UNITS_PART: Final = "units.json"
UNITS_TABLE: Final = "units"
NO_SESSION: Final = "_"
SUMMARY: Final = "summary"
SESSION_SUMMARY: Final = "session_summary"
KINDS: Final = frozenset({SUMMARY, SESSION_SUMMARY})
DISPLAY: Final[dict[str, Any]] = {
    "version": "display-1",
    "pre_s": 1.0,
    "post_s": 2.0,
    "bin_s": 0.05,
    "z_baseline_s": [-1.0, 0.0],
}
MAX_IDS: Final = 20  # asset ids listed per recording group
WRITE_ATTEMPTS: Final = 5
BUILD_BUDGET_S: Final = 600.0  # under the builder's 900 s timeout
PRECONDITION_FAILED: Final = frozenset(
    {"PreconditionFailed", "412", "ConditionalRequestConflict", "409"}
)
AVERAGE: Final = "average"
_KEY_SAFE: Final = re.compile(r"[A-Za-z0-9_.+-]+")
_PART_UNSAFE: Final = re.compile(r"[^A-Za-z0-9_.-]+")
_ANALYSIS_SOURCE: Final = Path(analysis.__file__)

# (kind, name) -> display modality. Kinds: a BIDS datatype with its suffix as the
# name; "nwb" with "Units" or "intervals" (rows in those tables); an NWB series'
# module (`processing/<module>`, else `acquisition` or `stimulus`) with its
# neurodata type, `ElectricalSeries:ieeg` when the series name says ieeg; "file"
# with the suffix or format of any other subject file. Checked against the eight real
# datasets.
MODALITIES: Final[dict[tuple[str, str], str]] = {
    ("nwb", "Units"): "units",
    ("nwb", "intervals"): "task",
    ("ecephys", "ElectricalSeries"): "lfp",
    ("ecephys", "ElectricalSeries:ieeg"): "ieeg",
    ("behavior", "SpatialSeries"): "behavior",
    ("behavior", "TimeSeries"): "behavior",
    ("stimulus", "AnnotationSeries"): "task",
    ("stimulus", "TimeSeries"): "task",
    ("anat", "T1w"): "anat",
    ("anat", "T2w"): "anat",
    ("func", "bold"): "func",
    ("func", "sbref"): "func",
    ("func", "events"): "task",
    ("fmap", "epi"): "fmap",
}

ASSETS_SQL: Final = (
    "SELECT asset_id, subject, bids_session, datatype, suffix, format, size_bytes, "
    "s3_key, s3_version_id, committed_at FROM assets WHERE dataset = ?"
)
SUBJECTS_SQL: Final = (
    "SELECT subject, species, sex, age, extracted_at FROM subjects WHERE dataset = ?"
)
SESSIONS_SQL: Final = (
    "SELECT subject, bids_session, start_time, extracted_at FROM sessions "
    "WHERE dataset = ?"
)
HEADERS_SQL: Final = (
    "SELECT asset_id, format, n_units, n_trials, nifti_shape, nifti_voxel_mm, "
    "tr_seconds, extracted_at FROM asset_headers WHERE dataset = ?"
)
SERIES_SQL: Final = (
    "SELECT asset_id, path, neurodata_type, shape, rate_hz FROM series "
    "WHERE dataset = ?"
)
UNITS_SQL: Final = (
    "SELECT asset_id, location, count(*) AS n FROM units WHERE dataset = ? "
    "GROUP BY asset_id, location"
)
ELECTRODES_SQL: Final = (
    "SELECT asset_id, location, count(*) AS n, count(x) AS with_xyz FROM electrodes "
    "WHERE dataset = ? GROUP BY asset_id, location"
)
INTERVALS_SQL: Final = (
    "SELECT asset_id, table_name, count(*) AS n FROM intervals WHERE dataset = ? "
    "GROUP BY asset_id, table_name"
)
EVENTS_SQL: Final = (
    "SELECT asset_id, trial_type, count(*) AS n FROM events WHERE dataset = ? "
    "GROUP BY asset_id, trial_type"
)
DATASET_QUERIES: Final = (
    ASSETS_SQL,
    SUBJECTS_SQL,
    SESSIONS_SQL,
    HEADERS_SQL,
    SERIES_SQL,
    UNITS_SQL,
    ELECTRODES_SQL,
    INTERVALS_SQL,
    EVENTS_SQL,
)
SESSION_ASSETS_SQL: Final = (
    "SELECT asset_id, format, s3_version_id, committed_at FROM assets "
    "WHERE dataset = ? AND subject = ? AND bids_session = ?"
)
NO_SESSION_ASSETS_SQL: Final = (
    "SELECT asset_id, format, s3_version_id, committed_at FROM assets "
    "WHERE dataset = ? AND subject = ? AND bids_session IS NULL"
)
SESSION_UNITS_SQL: Final = (
    "SELECT asset_id, count(*) AS n FROM units WHERE dataset = ? AND subject = ? "
    "GROUP BY asset_id"
)

type Row = dict[str, object]


class SummaryError(RuntimeError):
    """A summary message the builder cannot act on, or a write it cannot land."""


class CountingAthena:
    """An Athena client that counts the queries it starts (the summary's figure)."""

    def __init__(self, athena: Athena) -> None:
        """Wrap ``athena``; no query is started yet."""
        self.athena = athena
        self.started = 0

    def start_query_execution(self, **kwargs: Unpack[StartQueryArgs]) -> dict[str, Any]:
        """Start one query and count it."""
        self.started += 1
        return self.athena.start_query_execution(**kwargs)

    def get_query_execution(self, **kwargs: Unpack[QueryIdArgs]) -> dict[str, Any]:
        """Read a query's state."""
        return self.athena.get_query_execution(**kwargs)

    def get_query_results(self, **kwargs: Unpack[QueryResultsArgs]) -> dict[str, Any]:
        """Read one page of a finished query's rows."""
        return self.athena.get_query_results(**kwargs)

    def stop_query_execution(self, **kwargs: Unpack[QueryIdArgs]) -> dict[str, Any]:
        """Stop a running query."""
        return self.athena.stop_query_execution(**kwargs)


@dataclass(frozen=True, slots=True)
class SummaryDeps:
    """The clients, buckets and queue sender of one summary build.

    ``s3`` reads raw versions (the NWB frame series, a recipe's stimulus CSV) and
    reads and writes the derived bucket. ``send`` sends one message to the builder's
    own queue; None sends nothing.
    """

    athena: Athena
    workgroup: str
    database: str
    s3: lfp_build.DerivedS3
    raw_bucket: str
    derived_bucket: str
    send: Callable[[dict[str, str]], None] | None = None
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    now: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))


@dataclass(slots=True)
class _Build:
    """The API over a counting Athena client, and the build's deadline."""

    deps: SummaryDeps
    athena: CountingAthena
    api: compute_api.ComputeApi
    started: float
    deadline: float

    @classmethod
    def open(cls, deps: SummaryDeps) -> _Build:
        counted = CountingAthena(deps.athena)
        api = compute_api.ComputeApi(
            athena=counted,
            workgroup=deps.workgroup,
            database=deps.database,
            s3=deps.s3,
            raw_bucket=deps.raw_bucket,
            clock=deps.clock,
            sleep=deps.sleep,
            derived_bucket=deps.derived_bucket,
        )
        started = deps.clock()
        return cls(deps, counted, api, started, started + BUILD_BUDGET_S)

    def runner(self) -> AthenaRunner:
        return AthenaRunner(
            athena=self.athena,
            workgroup=self.deps.workgroup,
            database=self.deps.database,
            clock=self.deps.clock,
            sleep=self.deps.sleep,
        )

    def catalog(self) -> catalog_api.CatalogApi:
        return catalog_api.CatalogApi(
            athena=self.athena,
            workgroup=self.deps.workgroup,
            database=self.deps.database,
            clock=self.deps.clock,
            sleep=self.deps.sleep,
        )

    def elapsed_ms(self) -> int:
        return int((self.deps.clock() - self.started) * 1000)


# --- keys and helpers ---


def dataset_key(dataset: str) -> str:
    """Return ``summary/<dataset>/_dataset.json``."""
    return f"{PREFIX}/{dataset}/{DATASET_FILE}"


def session_prefix(dataset: str, subject: str, session: str) -> str:
    """Return ``summary/<dataset>/<subject>/<session or _>``."""
    return f"{PREFIX}/{dataset}/{subject}/{session}"


def latest_key(dataset: str, subject: str, session: str) -> str:
    """Return the session's ``latest.json`` key."""
    return f"{session_prefix(dataset, subject, session)}/{LATEST_FILE}"


def modality(kind: str, name: str) -> str:
    """Return the display modality of ``(kind, name)``; ``other:<name>`` when unseen."""
    return MODALITIES.get((kind, name), f"other:{name}")


def series_modality(path: str, neurodata_type: str) -> str:
    """Return the modality of one NWB series from its module and neurodata type."""
    parts = path.strip("/").split("/")
    module = parts[1] if parts[0] == "processing" and len(parts) > 1 else parts[0]
    name = neurodata_type
    if neurodata_type == "ElectricalSeries" and "ieeg" in parts[-1].lower():
        name = f"{neurodata_type}:ieeg"
    return modality(module, name)


def part_name(*pieces: str) -> str:
    """Return a part file name: the pieces joined by ``-``, each made key-safe."""
    return "-".join(_PART_UNSAFE.sub("_", p) for p in pieces) + ".json"


def code_version() -> str:
    """Return the analysis code version: the builder version and analysis.py's sha."""
    sha = hashlib.sha256(_ANALYSIS_SOURCE.read_bytes()).hexdigest()[:12]
    return f"{BUILDER_VERSION}+{sha}"


def _text(value: object) -> str | None:
    return None if value is None else str(value)


def _int(value: object) -> int:
    return 0 if value is None else int(str(value))


def _float(value: object) -> float | None:
    return None if value is None else float(str(value))


def _stamp(value: object) -> str:
    """Return a timestamp cell as ``YYYY-MM-DDTHH:MM:SS``; ``""`` when null."""
    text = _text(value)
    return "" if text is None else text[:19].replace(" ", "T")


def _as_of(rows: Iterable[Row], *columns: str) -> str:
    return max((_stamp(r.get(c)) for r in rows for c in columns), default="")


def _session_of(row: Row) -> str:
    return _text(row.get("bids_session")) or NO_SESSION


def _json_bytes(body: Mapping[str, Any]) -> bytes:
    return json.dumps(body, allow_nan=False, separators=(",", ":")).encode()


def _check_name(value: object, name: str) -> str:
    if not isinstance(value, str) or not _KEY_SAFE.fullmatch(value):
        msg = f"summary message {name} must be a key-safe name, not {value!r}"
        raise SummaryError(msg)
    return value


# --- S3 reads and the conditional write ---


def _read_json(
    s3: lfp_build.DerivedS3, bucket: str, key: str
) -> tuple[dict[str, Any] | None, str | None]:
    """Return an object's JSON and ETag; ``(None, None)`` when it does not exist."""
    try:
        reply: Any = s3.get_object(Bucket=bucket, Key=key)
    except ClientError as err:
        if err.response.get("Error", {}).get("Code") in S3_NOT_FOUND:
            return None, None
        raise
    return json.loads(reply["Body"].read()), str(reply["ETag"])


def write_if_newer(
    s3: lfp_build.DerivedS3, bucket: str, key: str, body: Mapping[str, Any]
) -> str:
    """Write ``body`` unless the stored object has a newer ``as_of``.

    The write carries ``IfMatch`` on the ETag just read, or ``IfNoneMatch: *`` when
    no object exists, so a write in between is refused (412), re-read and retried, at
    most ``WRITE_ATTEMPTS`` times. Returns ``written`` or ``kept newer``.
    """
    mine = str(body.get("as_of") or "")
    for _ in range(WRITE_ATTEMPTS):
        current, etag = _read_json(s3, bucket, key)
        if current is not None and str(current.get("as_of") or "") > mine:
            return "kept newer"
        condition = {"IfMatch": etag} if etag is not None else {"IfNoneMatch": "*"}
        try:
            s3.put_object(
                Bucket=bucket,
                Key=key,
                Body=_json_bytes(body),
                ContentType="application/json",
                **condition,
            )
        except ClientError as err:
            if err.response.get("Error", {}).get("Code") in PRECONDITION_FAILED:
                continue
            raise
        return "written"
    msg = f"{key}: still changing after {WRITE_ATTEMPTS} conditional writes"
    raise SummaryError(msg)


def _lfp_states(deps: SummaryDeps, asset: Row) -> dict[str, str]:
    """Return each series path's overview state from the asset's LFP manifest."""
    key = lfp_build.manifest_key(str(asset["asset_id"]), str(asset["s3_version_id"]))
    manifest, _ = _read_json(deps.s3, deps.derived_bucket, key)
    if manifest is None:
        return {}
    states = {str(s["path"]): "built" for s in manifest.get("series", [])}
    for skipped in manifest.get("skipped", []):
        states[str(skipped["path"])] = f"skipped: {skipped['reason']}"
    return states


# --- the per-dataset summary ---


@dataclass(slots=True)
class _Tables:
    """The nine reads, grouped by asset where the rows carry an asset id."""

    assets: list[Row]
    subjects: list[Row]
    sessions: list[Row]
    headers: dict[str, Row]
    series: dict[str, list[Row]]
    units: dict[str, list[Row]]
    electrodes: dict[str, list[Row]]
    intervals: dict[str, list[Row]]
    events: dict[str, list[Row]]


def _by_asset(rows: list[Row]) -> dict[str, list[Row]]:
    out: dict[str, list[Row]] = {}
    for row in rows:
        out.setdefault(str(row["asset_id"]), []).append(row)
    return out


def _read_tables(build: _Build, dataset: str) -> _Tables:
    results = build.runner().run_all(
        [(sql, [dataset]) for sql in DATASET_QUERIES], build.deadline
    )
    assets, subjects, sessions, headers, series, units, electrodes, *rest = results
    intervals, events = rest
    return _Tables(
        assets=assets,
        subjects=subjects,
        sessions=sessions,
        headers={str(r["asset_id"]): r for r in headers},
        series=_by_asset(series),
        units=_by_asset(units),
        electrodes=_by_asset(electrodes),
        intervals=_by_asset(intervals),
        events=_by_asset(events),
    )


def _levels(events: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    """Return each attribute's level counts, for 2 to 12 levels (the default rule)."""
    counts: dict[str, dict[str, int]] = {}
    for event in events:
        for name, value in event["attributes"].items():
            if value is None:
                continue
            per = counts.setdefault(str(name), {})
            per[str(value)] = per.get(str(value), 0) + 1
    low, high = analysis.DEFAULT_MIN_LEVELS, analysis.DEFAULT_MAX_LEVELS
    return {
        k: dict(sorted(v.items())) for k, v in counts.items() if low <= len(v) <= high
    }


def _nwb_events(build: _Build, asset_id: str) -> list[dict[str, Any]]:
    """Return the route's event types of one NWB file; none when it cannot resolve."""
    try:
        body = build.api.events_body(asset_id, build.deadline)
    except ApiError as err:
        logger.warning("summary: events of %s do not resolve: %s", asset_id, err)
        return []
    out: list[dict[str, Any]] = []
    for entry in body["event_types"]:
        item: dict[str, Any] = {
            "type": entry["name"],
            "averaging": entry["averaging"],
            "n": entry["n_events"],
            "levels": _levels(entry["events"]),
        }
        if "missing_frames" in entry:
            item["missing_frames"] = entry["missing_frames"]
        out.append(item)
    return out


def _bids_events(rows: list[Row]) -> list[dict[str, Any]]:
    """Return a BIDS events file's rows as the default recipe's `events` type."""
    if not rows:
        return []
    levels = {
        _text(r.get("trial_type")) or "": _int(r["n"])
        for r in rows
        if r.get("trial_type") is not None
    }
    return [
        {
            "type": "events",
            "averaging": AVERAGE,
            "n": sum(_int(r["n"]) for r in rows),
            "levels": {"trial_type": dict(sorted(levels.items()))} if levels else {},
        }
    ]


def _merge_events(groups: list[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for group in groups:
        for item in group:
            have = merged.get(item["type"])
            if have is None:
                merged[item["type"]] = {**item, "levels": dict(item["levels"])}
                continue
            have["n"] += item["n"]
            for attr, per in item["levels"].items():
                into = have["levels"].setdefault(attr, {})
                for level, n in per.items():
                    into[level] = into.get(level, 0) + n
    return list(merged.values())


def _regions(tables: _Tables, asset_ids: Iterable[str]) -> list[dict[str, Any]]:
    """Return units and channels per region label, verbatim; null labels are omitted."""
    found: dict[str, dict[str, Any]] = {}
    for asset_id in asset_ids:
        for column, rows in (
            ("units", tables.units.get(asset_id, [])),
            ("channels", tables.electrodes.get(asset_id, [])),
        ):
            for row in rows:
                label = _text(row.get("location"))
                if label is None:
                    continue
                entry = found.setdefault(
                    label, {"region": label, "units": 0, "channels": 0}
                )
                entry[column] += _int(row["n"])
    return sorted(found.values(), key=lambda e: (-e["units"], e["region"]))


def _asset_modalities(tables: _Tables, asset: Row) -> set[str]:
    asset_id = str(asset["asset_id"])
    if str(asset.get("format")) == "nwb":
        found: set[str] = set()
        if any(_int(r["n"]) for r in tables.units.get(asset_id, [])):
            found.add(modality("nwb", "Units"))
        if tables.intervals.get(asset_id, []) != []:
            found.add(modality("nwb", "intervals"))
        for row in tables.series.get(asset_id, []):
            found.add(series_modality(str(row["path"]), str(row["neurodata_type"])))
        return found or {modality("file", _text(asset.get("suffix")) or "nwb")}
    datatype = _text(asset.get("datatype"))
    suffix = _text(asset.get("suffix"))
    if datatype is not None:
        return {modality(datatype, suffix or datatype)}
    return {modality("file", suffix or _text(asset.get("format")) or "unknown")}


def _lfp_entries(deps: SummaryDeps, tables: _Tables, nwb: list[Row]) -> list[Any]:
    out: list[dict[str, Any]] = []
    for asset in nwb:
        rows = [
            r
            for r in tables.series.get(str(asset["asset_id"]), [])
            if str(r["neurodata_type"]) == "ElectricalSeries"
        ]
        if not rows:
            continue
        states = _lfp_states(deps, asset)
        for row in sorted(rows, key=lambda r: str(r["path"])):
            shape = row.get("shape")
            dims = [int(str(v)) for v in shape] if isinstance(shape, list) else []
            rate = _float(row.get("rate_hz"))
            samples = dims[0] if dims else None
            path = str(row["path"])
            out.append(
                {
                    "asset_id": str(asset["asset_id"]),
                    "path": path,
                    "channels": dims[1] if len(dims) > 1 else (1 if dims else None),
                    "rate_hz": rate,
                    "duration_s": (
                        None
                        if samples is None or rate is None or rate <= 0
                        else round(samples / rate, 3)
                    ),
                    "overview": states.get(path, "skipped: not built"),
                }
            )
    return out


def _nifti_entries(tables: _Tables, assets: list[Row]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for asset in assets:
        header = tables.headers.get(str(asset["asset_id"]))
        if str(asset.get("format")) != "nifti" or header is None:
            continue
        out.append(
            {
                "asset_id": str(asset["asset_id"]),
                "suffix": _text(asset.get("suffix")),
                "shape": header.get("nifti_shape"),
                "voxel_mm": header.get("nifti_voxel_mm"),
            }
        )
    return sorted(out, key=lambda e: e["asset_id"])


def _recordings(assets: list[Row]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], list[Row]] = {}
    for asset in assets:
        kind = (
            _text(asset.get("datatype")) or _text(asset.get("suffix")) or "other",
            _text(asset.get("format")) or "other",
        )
        groups.setdefault(kind, []).append(asset)
    return [
        {
            "datatype": datatype,
            "format": fmt,
            "files": len(rows),
            "bytes": sum(_int(r.get("size_bytes")) for r in rows),
            "asset_ids": sorted(str(r["asset_id"]) for r in rows)[:MAX_IDS],
        }
        for (datatype, fmt), rows in sorted(groups.items())
    ]


def _main_nwb(tables: _Tables, assets: list[Row]) -> Row | None:
    """Return the NWB file with the most units (ties by asset id), or None."""
    nwb = [a for a in assets if str(a.get("format")) == "nwb"]
    if not nwb:
        return None

    def units(asset: Row) -> int:
        return sum(_int(r["n"]) for r in tables.units.get(str(asset["asset_id"]), []))

    return min(nwb, key=lambda a: (-units(a), str(a["asset_id"])))


@dataclass(slots=True)
class _SessionView:
    entry: dict[str, Any]
    qualifies: bool
    units: int
    trials: int


def _session_view(
    build: _Build,
    tables: _Tables,
    dataset: str,
    key: tuple[str, str],
    assets: list[Row],
) -> _SessionView:
    subject, session = key
    nwb = sorted(
        (a for a in assets if str(a.get("format")) == "nwb"),
        key=lambda a: str(a["asset_id"]),
    )
    nwb_types = {
        str(a["asset_id"]): _nwb_events(build, str(a["asset_id"])) for a in nwb
    }
    bids_types = [
        _bids_events(tables.events.get(str(a["asset_id"]), [])) for a in assets
    ]
    events = _merge_events([*nwb_types.values(), *bids_types])
    main = _main_nwb(tables, assets)
    units = sum(
        _int(r["n"]) for a in assets for r in tables.units.get(str(a["asset_id"]), [])
    )
    main_units = (
        sum(_int(r["n"]) for r in tables.units.get(str(main["asset_id"]), []))
        if main is not None
        else 0
    )
    main_types = [] if main is None else nwb_types.get(str(main["asset_id"]), [])
    qualifies = main_units > 0 and any(
        t["averaging"] == AVERAGE and t["n"] > 0 for t in main_types
    )
    trials = sum(
        _int(r["n"])
        for a in assets
        for r in tables.intervals.get(str(a["asset_id"]), [])
        if str(r["table_name"]) == "trials"
    )
    start = next(
        (
            _stamp(r.get("start_time")) or None
            for r in tables.sessions
            if str(r["subject"]) == subject and _session_of(r) == session
        ),
        None,
    )
    bold = [
        tables.headers.get(str(a["asset_id"]))
        for a in assets
        if str(a.get("format")) == "nifti" and _text(a.get("suffix")) == "bold"
    ]
    tr = next((_float(h.get("tr_seconds")) for h in bold if h is not None), None)
    entry: dict[str, Any] = {
        "session": session,
        "start_time": start,
        "recordings": _recordings(assets),
        "lfp": _lfp_entries(build.deps, tables, nwb),
        "nifti": _nifti_entries(tables, assets),
        "func_runs": len(bold),
        "tr_s": tr,
        "events": [{k: v for k, v in e.items() if k != "averaging"} for e in events],
        "epochs": [],
        "session_summary": latest_key(dataset, subject, session) if qualifies else None,
    }
    return _SessionView(entry, qualifies, units, trials)


def _subject_rows(tables: _Tables) -> dict[str, Row]:
    return {str(r["subject"]): r for r in tables.subjects}


def _readiness(dataset: str, tables: _Tables, present: set[str]) -> dict[str, Any]:
    recipe = analysis.load_recipe(dataset)
    defaults = analysis.load_defaults(dataset)
    rates = defaults.rates
    electrodes = [r for rows in tables.electrodes.values() for r in rows]
    return {
        "defaults": {
            "block": compute_api.defaults_block(defaults),
            "bin_s": rates.get("bin_s"),
            "pre_s": rates.get("pre_s"),
            "post_s": rates.get("post_s"),
            "response_window_s": rates.get("response_window_s"),
            "baseline": rates.get("baseline"),
        },
        "recipe": {
            "version": recipe.version,
            "kind": "default" if recipe.default else "file",
        },
        "clock": {
            "lfp_clock_offset_s": recipe.lfp_clock_offset_s,
            "shared": sorted(present & {"units", "lfp", "ieeg", "task"}),
        },
        "electrodes": {
            "total": sum(_int(r["n"]) for r in electrodes),
            "with_coordinates": sum(_int(r.get("with_xyz")) for r in electrodes),
            "with_region": sum(
                _int(r["n"]) for r in electrodes if r.get("location") is not None
            ),
        },
        "sorting_quality": None,
    }


@dataclass(frozen=True, slots=True)
class DatasetBuild:
    """One dataset summary and the sessions that qualify for a session summary."""

    summary: dict[str, Any]
    sessions: list[tuple[str, str]]


def build_dataset(dataset: str, deps: SummaryDeps) -> DatasetBuild:
    """Read the catalog and resolve each NWB file's events; return the 3.3.1 body.

    The body also carries `catalog`: the dataset's entry in `GET /catalog/studies`
    (`CatalogApi.dataset_entry`, the route's own fold over its seven texts scoped to
    the dataset), which the route serves instead of aggregating the catalog live.
    """
    build = _Build.open(deps)
    tables = _read_tables(build, dataset)
    entry = build.catalog().dataset_entry(dataset, build.deadline)
    by_subject: dict[str, dict[str, list[Row]]] = {}
    for asset in tables.assets:
        subject = _text(asset.get("subject"))
        if subject is not None:
            sessions = by_subject.setdefault(subject, {})
            sessions.setdefault(_session_of(asset), []).append(asset)
    known = _subject_rows(tables)
    for subject in known:
        by_subject.setdefault(subject, {})
    subjects: list[dict[str, Any]] = []
    per_modality: dict[str, int] = {}
    qualifying: list[tuple[str, str]] = []
    for subject in sorted(by_subject):
        row = known.get(subject, {})
        sessions = by_subject[subject]
        views = [
            _session_view(build, tables, dataset, (subject, ses), assets)
            for ses, assets in sorted(sessions.items())
        ]
        qualifying += [(subject, v.entry["session"]) for v in views if v.qualifies]
        assets = [a for rows in sessions.values() for a in rows]
        modalities = sorted({m for a in assets for m in _asset_modalities(tables, a)})
        for name in modalities:
            per_modality[name] = per_modality.get(name, 0) + 1
        subjects.append(
            {
                "subject": subject,
                "species": _text(row.get("species")),
                "sex": _text(row.get("sex")),
                "age": _text(row.get("age")),
                "modalities": modalities,
                "units": sum(v.units for v in views),
                "trials": sum(v.trials for v in views),
                "regions": _regions(tables, (str(a["asset_id"]) for a in assets)),
                "sessions": [v.entry for v in views],
            }
        )
    every_id = [str(a["asset_id"]) for a in tables.assets]
    as_of = max(
        _as_of(tables.assets, "committed_at"),
        _as_of(tables.subjects, "extracted_at"),
        _as_of(tables.sessions, "extracted_at"),
        _as_of(tables.headers.values(), "extracted_at"),
    )
    summary: dict[str, Any] = {
        "schema": SCHEMA,
        "dataset": dataset,
        "as_of": as_of,
        "built_at": deps.now().isoformat(timespec="seconds"),
        "build_ms": build.elapsed_ms(),
        "athena_queries": build.athena.started,
        "code": code_version(),
        "species": sorted(
            {s for r in tables.subjects if (s := _text(r.get("species"))) is not None}
        ),
        "counts": {
            "assets": len(tables.assets),
            "bytes": sum(_int(a.get("size_bytes")) for a in tables.assets),
            "subjects": len(by_subject),
            "sessions": sum(len(s) for s in by_subject.values()),
        },
        "modalities": dict(sorted(per_modality.items())),
        "regions": _regions(tables, every_id),
        "subjects": subjects,
        "readiness": _readiness(dataset, tables, set(per_modality)),
        catalog_api.CATALOG_FIELD: entry,
    }
    return DatasetBuild(summary, qualifying)


def summarize_dataset(dataset: str, deps: SummaryDeps) -> dict[str, Any]:
    """Build, write conditionally, then fan out one message per qualifying session."""
    built = build_dataset(dataset, deps)
    outcome = write_if_newer(
        deps.s3, deps.derived_bucket, dataset_key(dataset), built.summary
    )
    sent = 0
    if deps.send is not None:
        for subject, session in built.sessions:
            deps.send(
                {
                    "kind": SESSION_SUMMARY,
                    "dataset": dataset,
                    "subject": subject,
                    "session": session,
                }
            )
            sent += 1
    logger.info(
        "summary %s: %s, %d session message(s), %d queries, %d ms",
        dataset,
        outcome,
        sent,
        built.summary["athena_queries"],
        built.summary["build_ms"],
    )
    return {"key": dataset_key(dataset), "write": outcome, "sessions_sent": sent}


# --- the per-session summary ---


def session_digest(
    recipe_version: str, defaults_version: str, code: str, assets: list[Row]
) -> str:
    """Return sha256 over the display, recipe, defaults and code versions and files.

    The digest without overrides.
    """
    inputs = {
        "display": DISPLAY["version"],
        "recipe": recipe_version,
        "defaults": defaults_version,
        "code": code,
        "assets": sorted([str(a["asset_id"]), str(a["s3_version_id"])] for a in assets),
    }
    return hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()


def _exists(deps: SummaryDeps, key: str) -> bool:
    found, _ = _read_json(deps.s3, deps.derived_bucket, key)
    return found is not None


def _session_assets(
    build: _Build, dataset: str, subject: str, session: str
) -> tuple[list[Row], dict[str, int]]:
    sql = NO_SESSION_ASSETS_SQL if session == NO_SESSION else SESSION_ASSETS_SQL
    params = (
        [dataset, subject] if session == NO_SESSION else [dataset, subject, session]
    )
    assets, units = build.runner().run_all(
        [(sql, params), (SESSION_UNITS_SQL, [dataset, subject])], build.deadline
    )
    counts = {str(r["asset_id"]): _int(r["n"]) for r in units}
    return assets, counts


def _selection(
    build: _Build, asset_id: str, count: int
) -> tuple[compute_api.Units, str]:
    """Return every unit, or the first ``MAX_UNITS`` as a range, and its query text."""
    if count <= compute_api.MAX_UNITS:
        return compute_api.Units("all"), "all"
    rows = build.runner().run(compute_api.UNITS_ALL_SQL, [asset_id], build.deadline)
    first = sorted(_int(r["unit_index"]) for r in rows)[: compute_api.MAX_UNITS]
    low, high = first[0], first[-1]
    return compute_api.Units("range", low=low, high=high), f"{low}-{high}"


def _attribute_order(entry: Mapping[str, Any]) -> list[str]:
    """Return the type's attributes in recipe order that have 2 to 12 levels.

    The same level rule as the dataset summary's `levels` (the default recipe's), so
    an identifier column such as `scene_id` is never the stored tuning attribute.
    """
    levels = _levels(entry["events"])
    seen: dict[str, None] = {}
    for event in entry["events"]:
        for name in event["attributes"]:
            if str(name) in levels:
                seen.setdefault(str(name), None)
    return list(seen)


def _skip(part: str, err: ApiError) -> dict[str, Any]:
    return {"part": part, "status": err.status, "code": err.code, "detail": err.detail}


def rates_request(
    asset_id: str, units: compute_api.Units, event_type: str
) -> compute_api.Request:
    """Return the rates request of the display settings for one event type."""
    return compute_api.Request(
        asset_id=asset_id,
        units=units,
        align=f"event:{event_type}",
        pre_s=float(DISPLAY["pre_s"]),
        post_s=float(DISPLAY["post_s"]),
        bin_s=float(DISPLAY["bin_s"]),
        zscore=True,
        raster=False,
    )


def _parts(
    build: _Build,
    asset_id: str,
    units: compute_api.Units,
    types: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """Return the rates and tuning parts of every average type, and what was skipped."""
    parts: dict[str, dict[str, Any]] = {}
    skipped: list[dict[str, Any]] = []
    api = build.api
    for entry in types:
        name = str(entry["name"])
        rates_part = part_name("rates", name)
        try:
            parts[rates_part] = api.rates_body(
                rates_request(asset_id, units, name), build.deps.clock(), build.deadline
            )
        except ApiError as err:
            skipped.append(_skip(rates_part, err))
        for attribute in _attribute_order(entry):
            tuning_part = part_name("tuning", name, attribute)
            request = compute_api.TuningRequest(asset_id, name, attribute, units)
            try:
                parts[tuning_part] = api.tuning_body(
                    request, build.deps.clock(), build.deadline
                )
            except ApiError as err:
                skipped.append(_skip(tuning_part, err))
                continue
            break
    return parts, skipped


def _units_table(
    build: _Build, asset_id: str
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Return the units table's first page, or None and what was skipped."""
    try:
        body = build.catalog().table_body(asset_id, UNITS_TABLE, 0, build.deadline)
    except ApiError as err:
        return None, _skip(UNITS_PART, err)
    return body, None


def summarize_session(
    dataset: str, subject: str, session: str, deps: SummaryDeps
) -> dict[str, Any]:
    """Build the 3.3.2 parts and manifest under their digest, then move ``latest``.

    Returns what was done: ``built``, ``exists`` (the digest's manifest was already
    there) or ``skipped`` with the reason.
    """
    build = _Build.open(deps)
    assets, counts = _session_assets(build, dataset, subject, session)
    nwb = [a for a in assets if str(a.get("format")) == "nwb"]
    if not nwb:
        return {"outcome": "skipped", "reason": "no NWB file in the session"}
    main = min(
        nwb, key=lambda a: (-counts.get(str(a["asset_id"]), 0), str(a["asset_id"]))
    )
    asset_id = str(main["asset_id"])
    total = counts.get(asset_id, 0)
    if total == 0:
        return {"outcome": "skipped", "reason": f"{asset_id} has no units"}
    events = build.api.events_body(asset_id, build.deadline)
    types = [
        e for e in events["event_types"] if e["averaging"] == AVERAGE and e["n_events"]
    ]
    if not types:
        return {"outcome": "skipped", "reason": f"{asset_id} has no average event type"}
    defaults = analysis.load_defaults(dataset)
    recipe = events["recipe"]
    code = code_version()
    digest = session_digest(str(recipe["version"]), defaults.version, code, assets)
    prefix = f"{session_prefix(dataset, subject, session)}/{digest}"
    manifest_key = f"{prefix}/{MANIFEST_FILE}"
    as_of = _as_of(assets, "committed_at")
    latest = {"digest": digest, "manifest": manifest_key, "as_of": as_of}
    if _exists(deps, manifest_key):
        outcome = write_if_newer(
            deps.s3, deps.derived_bucket, latest_key(dataset, subject, session), latest
        )
        return {"outcome": "exists", "digest": digest, "latest": outcome}
    units, _ = _selection(build, asset_id, total)
    parts: dict[str, dict[str, Any]] = {EVENTS_PART: events}
    skipped: list[dict[str, Any]] = []
    table, refused = _units_table(build, asset_id)
    if table is not None:
        parts[UNITS_PART] = table
    if refused is not None:
        skipped.append(refused)
    computed, computed_skipped = _parts(build, asset_id, units, types)
    parts.update(computed)
    skipped += computed_skipped
    size = 0
    for name, body in parts.items():
        blob = _json_bytes(body)
        size += len(blob)
        deps.s3.put_object(
            Bucket=deps.derived_bucket,
            Key=f"{prefix}/{name}",
            Body=blob,
            ContentType="application/json",
        )
    manifest = {
        "schema": SESSION_SCHEMA,
        "dataset": dataset,
        "subject": subject,
        "session": session,
        "digest": digest,
        "asset_id": asset_id,
        "display": DISPLAY,
        "inputs": {
            "recipe": recipe["version"],
            "defaults": defaults.version,
            "code": code,
            "assets": [
                {
                    "asset_id": str(a["asset_id"]),
                    "s3_version_id": str(a["s3_version_id"]),
                }
                for a in sorted(assets, key=lambda a: str(a["asset_id"]))
            ],
        },
        "units": {"total": total, "stored": min(total, compute_api.MAX_UNITS)},
        "units_table": (
            None
            if table is None
            else {"rows": len(table["rows"]), "more": table["more"]}
        ),
        "parts": list(parts),
        "skipped": skipped,
        "built_at": deps.now().isoformat(timespec="seconds"),
        "build_ms": build.elapsed_ms(),
        "athena_queries": build.athena.started,
        "bytes": size,
    }
    deps.s3.put_object(
        Bucket=deps.derived_bucket,
        Key=manifest_key,
        Body=_json_bytes(manifest),
        ContentType="application/json",
    )
    outcome = write_if_newer(
        deps.s3, deps.derived_bucket, latest_key(dataset, subject, session), latest
    )
    logger.info(
        "session summary %s/%s/%s: %d parts, %d skipped, %d ms",
        dataset,
        subject,
        session,
        len(parts),
        len(skipped),
        manifest["build_ms"],
    )
    return {"outcome": "built", "digest": digest, "latest": outcome}


def run_message(body: Mapping[str, object], deps: SummaryDeps) -> str:
    """Act on one `summary` or `session_summary` message; return what it named."""
    kind = body.get("kind")
    dataset = _check_name(body.get("dataset"), "dataset")
    if kind == SUMMARY:
        summarize_dataset(dataset, deps)
        return f"{SUMMARY}:{dataset}"
    if kind == SESSION_SUMMARY:
        subject = _check_name(body.get("subject"), "subject")
        session = _check_name(body.get("session"), "session")
        summarize_session(dataset, subject, session, deps)
        return f"{SESSION_SUMMARY}:{dataset}/{subject}/{session}"
    msg = f"summary message kind must be summary or session_summary, not {kind!r}"
    raise SummaryError(msg)
