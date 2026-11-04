"""Catalog read Lambda behind the HTTP API.

The JWT authorizer runs before this code and answers 401. This module adds the group
check (403), the request checks (400), the not-found check (404) and Athena's own
failures (502, 503). Every value reaches Athena as an `ExecutionParameters` entry and
never inside the query text. Athena cannot bind a NULL, so an absent filter is absent
from the query text: the assets route has twelve fixed texts (subject absent or
present, session absent,
`_` or present, format absent or present).
"""

import json
import logging
import os
import re
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from asteroidea_ingest import bids_path
from asteroidea_ingest.api_common import (
    ApiError,
    Invocation,
    Request,
    Response,
    Timing,
    json_response,
    serve,
)
from asteroidea_ingest.athena_query import (
    BUDGET_SECONDS,
    SLOW,
    AthenaRunner,
    Params,
)
from asteroidea_ingest.aws_types import S3_NOT_FOUND
from asteroidea_ingest.upload_rules import DATASET_RE

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from asteroidea_ingest.aws_types import S3, Athena

logger = logging.getLogger(__name__)

__all__ = ["BUDGET_SECONDS", "DISPATCH", "SLOW", "CatalogApi", "handler"]

DISPATCH: Final = {
    "GET /catalog/datasets": "datasets",
    "GET /catalog/datasets/{dataset}": "dataset",
    "GET /catalog/datasets/{dataset}/assets": "assets",
    "GET /catalog/assets/{asset_id}": "asset",
    "GET /catalog/assets/{asset_id}/tables/{table}": "table",
    "GET /catalog/assets/{asset_id}/events": "events",
    "GET /catalog/assets/{asset_id}/download": "download",
    "GET /catalog/studies": "studies",
    "GET /catalog/links": "links",
    "GET /catalog/assets": "asset_batch",
}
PAGE_SIZE: Final = 200
MAX_PAGE: Final = 1000
MAX_FILTER_LEN: Final = 64
DOWNLOAD_SECONDS: Final = 300  # the presigned GET lives five minutes
EVENT_ROWS: Final = 20000  # the events texts say LIMIT 20001 (a test pins the pair)

# `tier` reads null as curated for an import and as upload otherwise. `created_by` is
# read only to set `mine` and never leaves the Lambda.
DATASETS_SQL: Final = (
    "SELECT d.dataset, d.title, "
    "coalesce(d.tier, CASE WHEN d.ingest_method = 'import' THEN 'curated' "
    "ELSE 'upload' END) AS tier, "
    "d.license, d.doi, d.source_archive, d.source_dataset, "
    "d.source_version, d.ingest_method, d.created_by, d.created_at, "
    "coalesce(a.asset_count, 0) AS asset_count, "
    "coalesce(a.subject_count, 0) AS subject_count, "
    "coalesce(a.total_bytes, 0) AS total_bytes "
    "FROM datasets d LEFT JOIN (SELECT dataset, count(*) AS asset_count, "
    "count(DISTINCT subject) AS subject_count, sum(size_bytes) AS total_bytes "
    "FROM assets GROUP BY dataset) a ON a.dataset = d.dataset ORDER BY d.dataset"
)
_ASSETS_HEAD: Final = (
    "SELECT asset_id, s3_key, subject, bids_session, datatype, format, size_bytes, "
    "committed_at FROM assets WHERE dataset = ?"
)
_ASSETS_TAIL: Final = " ORDER BY s3_key OFFSET ? LIMIT 201"
SUBJECT_PREDICATE: Final = " AND subject = ?"
SESSION_PREDICATE: Final = " AND bids_session = ?"
SESSION_NULL_PREDICATE: Final = " AND bids_session IS NULL"
SESSION_NONE: Final = "_"  # the session key's spelling of "no session"
SESSION_RE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
FORMAT_PREDICATE: Final = " AND format = ?"
ASSET_SQL: Final = "SELECT * FROM assets WHERE asset_id = ?"
# the presign route re-reads an asset row it has seen. A row's `s3_key` and
# `s3_version_id` change only when a dataset is re-imported, so a warm environment keeps
# the fields `_download` needs for 5 minutes, at most 256 assets (about 100 KB).
DOWNLOAD_CACHE_TTL_S: Final = 300.0
DOWNLOAD_CACHE_MAX: Final = 256
_DOWNLOAD_CACHE: OrderedDict[str, tuple[float, dict[str, object]]] = OrderedDict()
_DOWNLOAD_LOCK = threading.Lock()
_DOWNLOAD_FIELDS: Final = ("s3_key", "s3_version_id", "size_bytes", "format")


def cache_get(asset_id: str, now: float) -> dict[str, object] | None:
    """Return the cached download fields of a fresh entry, else None."""
    with _DOWNLOAD_LOCK:
        entry = _DOWNLOAD_CACHE.get(asset_id)
        if entry is None:
            return None
        if now - entry[0] >= DOWNLOAD_CACHE_TTL_S:
            del _DOWNLOAD_CACHE[asset_id]
            return None
        _DOWNLOAD_CACHE.move_to_end(asset_id)
        return entry[1]


def cache_put(asset_id: str, row: dict[str, object], now: float) -> None:
    """Keep `row` for `asset_id`, dropping the least recently used past the bound."""
    with _DOWNLOAD_LOCK:
        _DOWNLOAD_CACHE[asset_id] = (now, row)
        _DOWNLOAD_CACHE.move_to_end(asset_id)
        while len(_DOWNLOAD_CACHE) > DOWNLOAD_CACHE_MAX:
            _DOWNLOAD_CACHE.popitem(last=False)


def clear_download_cache() -> None:
    """Forget every cached asset row (a test starts a fresh execution environment)."""
    with _DOWNLOAD_LOCK:
        _DOWNLOAD_CACHE.clear()


# GET /catalog/assets?ids=: at most 100 ids, each bound as its own value, because
# Athena caps one execution parameter at 1024 characters (100 uuids are 3.7 KB).
# The text depends only on the count. `latest_version` is the newest committed version
# of the same object (dataset and key): an asset id names one version, so a re-upload
# adds a row with a new id and the older row is no longer current.
MAX_BATCH_IDS: Final = 100
BATCH_ID_RE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_BATCH_HEAD: Final = (
    "WITH picked AS (SELECT a.asset_id, a.dataset, a.s3_key, a.subject, "
    "a.bids_session, a.datatype, a.suffix, a.format, a.s3_version_id, a.committed_at "
    "FROM assets a WHERE a.asset_id IN ("
)
_BATCH_TAIL: Final = (
    ")), latest AS (SELECT x.dataset, x.s3_key, "
    "max_by(x.s3_version_id, x.committed_at) AS latest_version FROM assets x "
    "JOIN (SELECT DISTINCT dataset, s3_key FROM picked) k "
    "ON x.dataset = k.dataset AND x.s3_key = k.s3_key GROUP BY x.dataset, x.s3_key) "
    "SELECT p.asset_id, p.dataset, p.s3_key, p.subject, p.bids_session, p.datatype, "
    "p.suffix, p.format, p.s3_version_id, p.committed_at, l.latest_version "
    "FROM picked p LEFT JOIN latest l ON l.dataset = p.dataset AND l.s3_key = p.s3_key"
)


def batch_sql(count: int) -> str:
    """Return the batch lookup text for `count` ids: one `?` per id."""
    return _BATCH_HEAD + ", ".join(["?"] * count) + _BATCH_TAIL


def batch_ids(query: Mapping[str, str]) -> list[str]:
    """Return the distinct ids of `ids=`, in order; 400 unless 1 to 100 valid ids."""
    raw = query.get("ids") or ""
    ids = list(dict.fromkeys(i for i in raw.split(",") if i))
    if not 1 <= len(ids) <= MAX_BATCH_IDS:
        raise ApiError(
            400,
            f"ids must list 1 to {MAX_BATCH_IDS} asset ids, separated by commas",
            code="invalid_ids",
        )
    bad = next((i for i in ids if not BATCH_ID_RE.match(i)), None)
    if bad is not None:
        raise ApiError(
            400,
            f"asset id {bad[:MAX_FILTER_LEN]!r} must match {BATCH_ID_RE.pattern}",
            code="invalid_ids",
        )
    return ids


def batch_entry(row: Row) -> dict[str, object]:
    """Name one asset in research terms: place, version and whether it is current."""
    dataset = str(row["dataset"])
    latest = row.get("latest_version")
    return {
        "asset_id": row["asset_id"],
        "dataset": dataset,
        "subject": row["subject"],
        "session": row["bids_session"],
        "datatype": row["datatype"],
        "suffix": row["suffix"],
        "format": row["format"],
        "path": str(row["s3_key"]).removeprefix(f"raw/{dataset}/"),
        "version": row["s3_version_id"],
        "committed_at": row["committed_at"],
        "current": latest is None or latest == row["s3_version_id"],
    }


HEADERS_SQL: Final = "SELECT * FROM asset_headers WHERE asset_id = ?"
# The header facts of one dataset (and its existence check): the datasets row with the
# counts of its own files only, so the page never waits on the full-assets GROUP BY of
# DATASETS_SQL. The one parameter is bound once, in the CTE `d`.
DATASET_FACTS_SQL: Final = (
    "WITH d AS (SELECT ? AS dataset) "
    "SELECT x.dataset, x.title, x.license, x.doi, x.source_archive, x.source_dataset, "
    "x.source_version, "
    "coalesce(a.asset_count, 0) AS asset_count, "
    "coalesce(a.subject_count, 0) AS subject_count, "
    "coalesce(a.total_bytes, 0) AS total_bytes "
    "FROM datasets x JOIN d ON x.dataset = d.dataset "
    "LEFT JOIN (SELECT y.dataset, count(*) AS asset_count, "
    "count(DISTINCT y.subject) AS subject_count, sum(y.size_bytes) AS total_bytes "
    "FROM assets y JOIN d ON y.dataset = d.dataset GROUP BY y.dataset) a "
    "ON a.dataset = x.dataset"
)
# The subjects of a dataset: its `subjects` rows, plus every subject its files name
# that has no row (the list's subject_count counts assets.subject, so the two agree).
# The one parameter is bound once, in the CTE `d`, so each query still sends one value.
DATASET_SUBJECTS_SQL: Final = (
    "WITH d AS (SELECT ? AS dataset) "
    "SELECT coalesce(s.subject, a.subject) AS subject, s.species, s.sex, s.age "
    "FROM (SELECT x.subject, x.species, x.sex, x.age FROM subjects x "
    "JOIN d ON x.dataset = d.dataset) s "
    "FULL OUTER JOIN (SELECT DISTINCT y.subject FROM assets y "
    "JOIN d ON y.dataset = d.dataset WHERE y.subject IS NOT NULL) a "
    "ON s.subject = a.subject ORDER BY 1"
)
DATASET_SESSIONS_SQL: Final = (
    "SELECT subject, bids_session, start_time, description FROM sessions "
    "WHERE dataset = ? ORDER BY subject, bids_session"
)
DATASET_FORMATS_SQL: Final = (
    "SELECT format, count(*) AS count, sum(size_bytes) AS bytes FROM assets "
    "WHERE dataset = ? GROUP BY format ORDER BY format"
)
# One fixed text per allowlisted table; the values are the asset id and the offset.
TABLE_SQL: Final = {
    "series": "SELECT * FROM series WHERE asset_id = ? ORDER BY path "
    "OFFSET ? LIMIT 201",
    "electrodes": "SELECT * FROM electrodes WHERE asset_id = ? "
    "ORDER BY electrode_index OFFSET ? LIMIT 201",
    "units": "SELECT * FROM units WHERE asset_id = ? ORDER BY unit_index "
    "OFFSET ? LIMIT 201",
    "intervals": "SELECT * FROM intervals WHERE asset_id = ? "
    "ORDER BY table_name, row_index OFFSET ? LIMIT 201",
}
INTERVALS_SQL: Final = (
    "SELECT * FROM intervals WHERE asset_id = ? ORDER BY table_name, row_index "
    "LIMIT 20001"
)
EVENTS_SQL: Final = (
    "SELECT * FROM events WHERE dataset = ? AND events_for = ? "
    "ORDER BY asset_id, row_index LIMIT 20001"
)
# GET /catalog/studies reads these three tables; none takes a value.
STUDIES_SQL: Final = (
    "SELECT study, title, authors, lab, citation, doi FROM studies ORDER BY study"
)
STUDY_DATASETS_SQL: Final = (
    "SELECT study, dataset, role FROM study_datasets ORDER BY study, dataset"
)
# Every dataset's subjects: its `subjects` rows plus each subject its files name.
ALL_SUBJECTS_SQL: Final = (
    "SELECT coalesce(s.dataset, a.dataset) AS dataset, "
    "coalesce(s.subject, a.subject) AS subject, s.species, s.sex, s.age "
    "FROM subjects s FULL OUTER JOIN (SELECT DISTINCT dataset, subject FROM assets "
    "WHERE subject IS NOT NULL) a ON s.dataset = a.dataset AND s.subject = a.subject "
    "ORDER BY 1, 2"
)
# The distinct (datatype, suffix, format) of each subject's files other than NWB; an
# NWB file's modalities come from its header and series rows.
KINDS_SQL: Final = (
    "SELECT dataset, subject, datatype, suffix, format FROM assets "
    "WHERE subject IS NOT NULL AND coalesce(format, '') <> 'nwb' "
    "GROUP BY dataset, subject, datatype, suffix, format ORDER BY 1, 2, 3, 4, 5"
)
SESSION_ASSET_IDS: Final = 20  # the text says n <= 20 (a test pins the pair)
SESSION_ASSETS_SQL: Final = (
    "SELECT dataset, subject, bids_session, asset_id FROM (SELECT dataset, subject, "
    "bids_session, asset_id, row_number() OVER (PARTITION BY dataset, subject, "
    "bids_session ORDER BY s3_key) AS n FROM assets WHERE subject IS NOT NULL) "
    "WHERE n <= 20 ORDER BY dataset, subject, bids_session, n"
)
NWB_SQL: Final = (
    "SELECT a.dataset, a.subject, a.bids_session, a.asset_id, a.suffix, h.n_units, "
    "h.n_trials FROM assets a LEFT JOIN asset_headers h ON h.asset_id = a.asset_id "
    "WHERE a.subject IS NOT NULL AND a.format = 'nwb' "
    "ORDER BY a.dataset, a.subject, a.bids_session, a.asset_id"
)
LFP_SQL: Final = (
    "SELECT a.dataset, a.subject, s.asset_id, s.path, s.shape, s.rate_hz "
    "FROM series s JOIN assets a ON a.asset_id = s.asset_id "
    "WHERE a.subject IS NOT NULL AND s.neurodata_type = 'ElectricalSeries' "
    "AND strpos(upper(s.path), 'LFP') > 0 ORDER BY 1, 2, 3, 4"
)
UNIT_REGIONS_SQL: Final = (
    "SELECT dataset, location, count(*) AS units FROM units "
    "WHERE location IS NOT NULL GROUP BY dataset, location ORDER BY 1, 2"
)
CHANNEL_REGIONS_SQL: Final = (
    "SELECT dataset, location, count(*) AS channels FROM electrodes "
    "WHERE location IS NOT NULL GROUP BY dataset, location ORDER BY 1, 2"
)
# The live aggregate's seven texts, in `dataset_entries` order. The studies route runs
# them only when a dataset has no stored entry; none takes a value.
ENTRY_SQL: Final = (
    ALL_SUBJECTS_SQL,
    KINDS_SQL,
    SESSION_ASSETS_SQL,
    NWB_SQL,
    LFP_SQL,
    UNIT_REGIONS_SQL,
    CHANNEL_REGIONS_SQL,
)
# The same seven scoped to one dataset: every `?` is the dataset. The dataset summary
# builder runs them (`CatalogApi.dataset_entry`) and stores the folded entry.
DATASET_ENTRY_SQL: Final = (
    (
        "SELECT coalesce(s.dataset, a.dataset) AS dataset, "
        "coalesce(s.subject, a.subject) AS subject, s.species, s.sex, s.age "
        "FROM (SELECT dataset, subject, species, sex, age FROM subjects "
        "WHERE dataset = ?) s FULL OUTER JOIN (SELECT DISTINCT dataset, subject "
        "FROM assets WHERE dataset = ? AND subject IS NOT NULL) a "
        "ON s.dataset = a.dataset AND s.subject = a.subject ORDER BY 1, 2"
    ),
    (
        "SELECT dataset, subject, datatype, suffix, format FROM assets "
        "WHERE dataset = ? AND subject IS NOT NULL AND coalesce(format, '') <> 'nwb' "
        "GROUP BY dataset, subject, datatype, suffix, format ORDER BY 1, 2, 3, 4, 5"
    ),
    (
        "SELECT dataset, subject, bids_session, asset_id FROM (SELECT dataset, "
        "subject, bids_session, asset_id, row_number() OVER (PARTITION BY dataset, "
        "subject, bids_session ORDER BY s3_key) AS n FROM assets WHERE dataset = ? "
        "AND subject IS NOT NULL) WHERE n <= 20 "
        "ORDER BY dataset, subject, bids_session, n"
    ),
    (
        "SELECT a.dataset, a.subject, a.bids_session, a.asset_id, a.suffix, h.n_units, "
        "h.n_trials FROM assets a LEFT JOIN asset_headers h ON h.asset_id = a.asset_id "
        "WHERE a.dataset = ? AND a.subject IS NOT NULL AND a.format = 'nwb' "
        "ORDER BY a.dataset, a.subject, a.bids_session, a.asset_id"
    ),
    (
        "SELECT a.dataset, a.subject, s.asset_id, s.path, s.shape, s.rate_hz "
        "FROM series s JOIN assets a ON a.asset_id = s.asset_id "
        "WHERE a.dataset = ? AND a.subject IS NOT NULL "
        "AND s.neurodata_type = 'ElectricalSeries' "
        "AND strpos(upper(s.path), 'LFP') > 0 ORDER BY 1, 2, 3, 4"
    ),
    (
        "SELECT dataset, location, count(*) AS units FROM units "
        "WHERE dataset = ? AND location IS NOT NULL GROUP BY dataset, location "
        "ORDER BY 1, 2"
    ),
    (
        "SELECT dataset, location, count(*) AS channels FROM electrodes "
        "WHERE dataset = ? AND location IS NOT NULL GROUP BY dataset, location "
        "ORDER BY 1, 2"
    ),
)
# The stored dataset summary the studies route reads, and its entry's field.
SUMMARY_PREFIX: Final = "summary"
SUMMARY_FILE: Final = "_dataset.json"
CATALOG_FIELD: Final = "catalog"
# Parallel summary GETs, and the S3 client's connection pool (botocore's default is
# 10): sized for the ~50 to 100 small objects a studies request reads.
SUMMARY_READERS: Final = 32
SUMMARY_LIST_PAGE: Final = 1000
# At most this many datasets without a stored entry run the scoped texts (seven
# queries each); more run the seven unscoped texts once.
SCOPED_FALLBACK_MAX: Final = 2
# GET /catalog/links: one fixed text per filter form, each read to one row past the cap.
MAX_LINKS: Final = 5000  # the texts say LIMIT 5001 (a test pins the pair)
LINKS_SQL: Final[dict[tuple[str, ...], str]] = {
    (): (
        "SELECT dataset, subject, subject_key, coalesce(status, 'pending') AS status, "
        "linked_by, linked_at, basis, note FROM subject_links "
        "ORDER BY dataset, subject, subject_key LIMIT 5001"
    ),
    ("study",): (
        "SELECT dataset, subject, subject_key, coalesce(status, 'pending') AS status, "
        "linked_by, linked_at, basis, note FROM subject_links "
        "WHERE dataset IN (SELECT dataset FROM study_datasets WHERE study = ?) "
        "ORDER BY dataset, subject, subject_key LIMIT 5001"
    ),
    ("subject_key",): (
        "SELECT dataset, subject, subject_key, coalesce(status, 'pending') AS status, "
        "linked_by, linked_at, basis, note FROM subject_links "
        "WHERE subject_key = ? "
        "ORDER BY dataset, subject, subject_key LIMIT 5001"
    ),
    ("dataset", "subject"): (
        "SELECT dataset, subject, subject_key, coalesce(status, 'pending') AS status, "
        "linked_by, linked_at, basis, note FROM subject_links "
        "WHERE dataset = ? AND subject = ? "
        "ORDER BY dataset, subject, subject_key LIMIT 5001"
    ),
}
_LINK_FILTERS: Final = ("study", "subject_key", "dataset", "subject")
MAX_BODY_BYTES: Final = 5 * 1024 * 1024  # under the 6 MiB buffered Lambda limit
# The named modalities in the order a client lists them; any other kind of file is
# `other:<name>`, never dropped.
MODALITIES: Final = ("units", "lfp", "ieeg", "anat", "func", "task")
_BIDS_MODALITIES: Final = frozenset({"ieeg", "anat", "func"})
_ASSET_KEYS: Final = (
    "asset_id",
    "s3_key",
    "subject",
    "bids_session",
    "datatype",
    "format",
    "size_bytes",
    "committed_at",
)


def assets_query(
    dataset: str,
    subject: str | None,
    fmt: str | None,
    page: int,
    session: str | None = None,
) -> tuple[str, list[str | int]]:
    """Return the assets query text and its parameters; a predicate only when given.

    `session` `_` adds the fixed `bids_session IS NULL` text and binds nothing; any
    other session binds `bids_session = ?`. Twelve fixed texts in all.
    """
    params: list[str | int] = [dataset]
    sql = _ASSETS_HEAD
    if subject is not None:
        sql, params = sql + SUBJECT_PREDICATE, [*params, subject]
    if session == SESSION_NONE:
        sql += SESSION_NULL_PREDICATE
    elif session is not None:
        sql, params = sql + SESSION_PREDICATE, [*params, session]
    if fmt is not None:
        sql, params = sql + FORMAT_PREDICATE, [*params, fmt]
    return sql + _ASSETS_TAIL, [*params, PAGE_SIZE * page]


def _query_value(query: Mapping[str, str], name: str) -> str | None:
    value = query.get(name)
    if value is None or value == "":
        return None
    if len(value) > MAX_FILTER_LEN:
        raise ApiError(400, f"{name} must be at most {MAX_FILTER_LEN} characters")
    return str(value)


def _page(query: Mapping[str, str]) -> int:
    raw = query.get("page", "0")
    if not (raw.isascii() and raw.isdigit()) or int(raw) > MAX_PAGE:
        raise ApiError(400, f"page must be an integer from 0 to {MAX_PAGE}")
    return int(raw)


type Row = dict[str, object]
type SubjectKey = tuple[str, str]


def _table_sql(table: str) -> str:
    """Return the allowlisted table's fixed text; 400 for any other name."""
    sql = TABLE_SQL.get(table)
    if sql is None:
        allowed = ", ".join(TABLE_SQL)
        raise ApiError(400, f"table must be one of: {allowed}")
    return sql


def dataset_rows(rows: list[Row], sub: str) -> list[Row]:
    """Replace each row's `created_by` with `mine`: whether the caller created it."""
    out: list[Row] = []
    for row in rows:
        record = dict(row)
        record["mine"] = record.pop("created_by", None) == sub
        out.append(record)
    return out


def file_kind(datatype: object, suffix: object, fmt: object) -> str:
    """Name the modality of one non-NWB file: a named one or `other:<name>`.

    An `events` file is `task`; a BIDS `ieeg`, `anat` or `func` file is its datatype.
    Any other file is `other:` and its datatype, else its suffix, else its format.
    """
    if suffix == "events":
        return "task"
    if datatype in _BIDS_MODALITIES:
        return str(datatype)
    for name in (datatype, suffix, fmt):
        if name:
            return f"other:{name}"
    return "other:unknown"


def _ordered(kinds: set[str]) -> list[str]:
    named = [m for m in MODALITIES if m in kinds]
    return named + sorted(kinds - set(MODALITIES))


def _int(value: object) -> int:
    return value if isinstance(value, int) else 0


def _session(value: object) -> str:
    return SESSION_NONE if value is None else str(value)


def _channels(shape: object) -> int | None:
    """Return an ElectricalSeries' channel count: the second axis of its shape."""
    if isinstance(shape, list) and len(shape) > 1 and isinstance(shape[1], int):
        return shape[1]
    return None


@dataclass
class _Subject:
    """One subject's facts while the studies body is assembled."""

    row: Row
    kinds: set[str]
    sessions: dict[str, dict[str, list[object]]]
    units: int = 0
    trials: int = 0
    lfp: list[Row] | None = None

    def session(self, name: str) -> dict[str, list[object]]:
        """Return the named session's lists, creating them on first use."""
        return self.sessions.setdefault(name, {"asset_ids": [], "nwb": []})

    def add_kind(self, row: Row) -> None:
        """Add a non-NWB file kind (a `KINDS_SQL` row)."""
        self.kinds.add(file_kind(row["datatype"], row["suffix"], row["format"]))

    def add_asset(self, row: Row) -> None:
        """Add one of a session's first file ids (a `SESSION_ASSETS_SQL` row)."""
        self.session(_session(row["bids_session"]))["asset_ids"].append(row["asset_id"])

    def add_lfp(self, row: Row) -> None:
        """Add an LFP series (an `LFP_SQL` row)."""
        self.kinds.add("lfp")
        entry = {
            "asset_id": row["asset_id"],
            "path": row["path"],
            "channels": _channels(row["shape"]),
            "rate_hz": row["rate_hz"],
        }
        self.lfp = [*(self.lfp or []), entry]

    def add_nwb(self, row: Row, lfp_assets: set[object]) -> None:
        """Add an NWB file and its header counts (an `NWB_SQL` row)."""
        units, trials = _int(row["n_units"]), _int(row["n_trials"])
        self.session(_session(row["bids_session"]))["nwb"].append(
            {"asset_id": row["asset_id"], "n_units": row["n_units"]}
        )
        self.units += units
        self.trials += trials
        if units > 0:
            self.kinds.add("units")
        if trials > 0:
            self.kinds.add("task")
        if units <= 0 and trials <= 0 and row["asset_id"] not in lfp_assets:
            self.kinds.add(f"other:{row['suffix'] or 'nwb'}")

    def body(self) -> Row:
        """Return the subject's entry of the studies body."""
        sessions = [
            {"session": name, "asset_ids": s["asset_ids"], "nwb": s["nwb"]}
            for name, s in sorted(self.sessions.items())
        ]
        return {
            "subject": self.row["subject"],
            "species": self.row.get("species"),
            "sex": self.row.get("sex"),
            "age": self.row.get("age"),
            "modalities": _ordered(self.kinds),
            "sessions": sessions,
            "units": self.units,
            "trials": self.trials,
            "lfp": self.lfp or [],
        }


def _fold(
    by_subject: dict[SubjectKey, _Subject],
    rows: list[Row],
    add: Callable[[_Subject, Row], None],
) -> None:
    """Apply `add` to each row's subject; a row of an unknown subject is skipped."""
    for row in rows:
        found = by_subject.get((str(row["dataset"]), str(row["subject"])))
        if found is not None:
            add(found, row)


def _regions(units: list[Row], channels: list[Row]) -> dict[str, list[Row]]:
    """Merge unit and electrode counts per dataset and verbatim location label."""
    merged: dict[str, dict[str, Row]] = {}
    for rows, field in ((units, "units"), (channels, "channels")):
        for row in rows:
            region = str(row["location"])
            entry = merged.setdefault(str(row["dataset"]), {}).setdefault(
                region, {"region": region, "units": 0, "channels": 0}
            )
            entry[field] = _int(row[field])
    return {
        dataset: [by_region[r] for r in sorted(by_region)]
        for dataset, by_region in merged.items()
    }


def _ms(seconds: float) -> int:
    """Return whole milliseconds, never negative."""
    return max(0, int(seconds * 1000))


def overlap_ms(a_began: float, a_ended: float, b_began: float, b_ended: float) -> int:
    """Return the milliseconds two intervals share, 0 when they do not meet."""
    return _ms(min(a_ended, b_ended) - max(a_began, b_began))


def summary_key(dataset: str) -> str:
    """Return the stored dataset summary's key, ``summary/<dataset>/_dataset.json``."""
    return f"{SUMMARY_PREFIX}/{dataset}/{SUMMARY_FILE}"


def empty_entry() -> dict[str, list[Row]]:
    """Return the entry of a dataset with no subjects and no regions."""
    return {"subjects": [], "regions": []}


def dataset_entries(results: list[list[Row]]) -> dict[str, dict[str, list[Row]]]:
    """Fold the seven `ENTRY_SQL` result sets into each dataset's studies entry.

    An entry is the dataset's `subjects` and `regions` in the studies body. The live
    route and the dataset summary builder both call this, so a stored entry is the
    live one for the same rows.

    Modalities per subject: `units` when an NWB header counts units, `task` when it
    counts trials or the subject has an `events` file, `lfp` when a series under an
    LFP path is an ElectricalSeries, the BIDS datatypes `ieeg`, `anat` and `func`, and
    `other:<name>` for any other file (an NWB file that gives none of the three is
    `other:` its suffix). Regions are the authors' `location` labels, verbatim.
    """
    (
        subjects,
        kinds,
        session_assets,
        nwb,
        lfp,
        unit_regions,
        channel_regions,
    ) = results
    by_subject = {
        (str(row["dataset"]), str(row["subject"])): _Subject(row, set(), {})
        for row in subjects
    }
    _fold(by_subject, kinds, _Subject.add_kind)
    _fold(by_subject, session_assets, _Subject.add_asset)
    _fold(by_subject, lfp, _Subject.add_lfp)
    lfp_assets = {row["asset_id"] for row in lfp}
    _fold(by_subject, nwb, lambda s, row: s.add_nwb(row, lfp_assets))
    entries: dict[str, dict[str, list[Row]]] = {}
    for (dataset, _), s in by_subject.items():
        entries.setdefault(dataset, empty_entry())["subjects"].append(s.body())
    for dataset, regions in _regions(unit_regions, channel_regions).items():
        entries.setdefault(dataset, empty_entry())["regions"] = regions
    return entries


def studies_from(
    tables: tuple[list[Row], list[Row], list[Row]],
    entries: Mapping[str, Mapping[str, Any]],
    sub: str,
) -> dict[str, Any]:
    """Assemble GET /catalog/studies from its three table reads and the entries.

    `tables` is the studies, study_datasets and datasets rows; a dataset with no
    entry has no subjects and no regions.
    """
    studies, links, datasets = tables
    members: dict[str, list[Row]] = {}
    for row in links:
        members.setdefault(str(row["study"]), []).append(
            {"dataset": row["dataset"], "role": row["role"]}
        )
    out: list[Row] = []
    for row in dataset_rows(datasets, sub):
        entry = entries.get(str(row["dataset"])) or empty_entry()
        out.append({**row, "subjects": entry["subjects"], "regions": entry["regions"]})
    return {
        "studies": [
            {**row, "datasets": members.get(str(row["study"]), [])} for row in studies
        ],
        "datasets": out,
    }


def studies_body(results: list[list[Row]], sub: str) -> dict[str, Any]:
    """Assemble GET /catalog/studies from the live aggregate's ten result sets.

    The order is the three table reads, then `ENTRY_SQL`.
    """
    studies, links, datasets, *rest = results
    return studies_from((studies, links, datasets), dataset_entries(rest), sub)


def links_query(query: Mapping[str, str]) -> tuple[str, list[str | int]]:
    """Return the links query text and its values for one filter form.

    The forms: none, `study`, `subject_key`, or `dataset` with `subject`.
    """
    given = {name: _query_value(query, name) for name in _LINK_FILTERS}
    form = tuple(name for name in _LINK_FILTERS if given[name] is not None)
    sql = LINKS_SQL.get(form)
    if sql is None:
        raise ApiError(
            400, "give no filter, study, subject_key, or dataset with subject"
        )
    if "dataset" in form and not DATASET_RE.match(str(given["dataset"])):
        raise ApiError(400, "dataset must match ^[a-z0-9][a-z0-9_-]{0,62}$")
    return sql, [str(given[name]) for name in form]


def _stored_entry(s3: S3, bucket: str, dataset: str) -> dict[str, Any] | None:
    """Return the `catalog` entry of a dataset's stored summary, else None.

    None when the summary is missing (NoSuchKey), predates the entry, or is not the
    stored shape. Any other S3 refusal is logged with its code and also reads as
    None, so the route answers from the live aggregate instead of failing the page.
    """
    key = summary_key(dataset)
    try:
        reply = s3.get_object(Bucket=bucket, Key=key)
        body = json.loads(reply["Body"].read())
    except ClientError as err:
        code = str(err.response.get("Error", {}).get("Code"))
        if code not in S3_NOT_FOUND:
            logger.warning("summary %s unreadable (%s); reading live", key, code)
        return None
    except ValueError as err:
        logger.warning("summary %s is not JSON (%s); reading live", key, err)
        return None
    entry = body.get(CATALOG_FIELD) if isinstance(body, dict) else None
    if not (
        isinstance(entry, dict)
        and isinstance(entry.get("subjects"), list)
        and isinstance(entry.get("regions"), list)
    ):
        return None
    return entry


@dataclass(frozen=True, slots=True)
class CatalogApi:
    """The catalog routes over an injected Athena client, clock and sleep."""

    athena: Athena
    workgroup: str
    database: str
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    s3: S3 | None = None
    raw_bucket: str = ""
    derived_bucket: str = ""

    def handle(
        self, event: dict[str, Any], *, invocation: Invocation | None = None
    ) -> Response:
        """Route one HTTP API (payload v2) event."""
        return serve(event, api=self, dispatch=DISPATCH, invocation=invocation)

    def _answer(self, read: Callable[[float], dict[str, Any]]) -> Response:
        """Run `read` under the Athena budget; the 200 body gains `query_ms`."""
        started = self.clock()
        body = read(started + BUDGET_SECONDS)
        body["query_ms"] = int((self.clock() - started) * 1000)
        return json_response(200, body)

    def _answer_capped(self, read: Callable[[float], dict[str, Any]]) -> Response:
        """Answer as `_answer` does; 413 when the body passes `MAX_BODY_BYTES`."""
        response = self._answer(read)
        size = len(response["body"].encode())
        if size > MAX_BODY_BYTES:
            raise ApiError(
                413,
                f"the response is {size} bytes, over the {MAX_BODY_BYTES} limit",
                code="too_large",
            )
        return response

    def datasets(self, request: Request, _timing: Timing) -> Response:
        """GET /catalog/datasets."""
        return self._answer(
            lambda deadline: {
                "datasets": dataset_rows(
                    self._run(DATASETS_SQL, [], deadline), request.sub
                )
            }
        )

    def studies(self, request: Request, timing: Timing) -> Response:
        """GET /catalog/studies: studies, datasets, subjects, sessions and regions.

        Three table reads in Athena; each dataset's subjects and regions come from
        its stored summary's `catalog` entry. The summaries are listed and read in a
        second thread while the Athena round runs, then joined; the timing line
        carries `athena_ms`, `summaries_ms` and `overlap_ms`. A dataset whose
        summary is missing, has no entry, or cannot be read takes its entry from the
        live aggregate (`_live_entries`), so no dataset is dropped. `timing.cache`
        says `summary` when every entry was stored, else `summary+live`.
        """

        def read(deadline: float) -> dict[str, Any]:
            # The summary fetch (list the prefix, then GET each dataset's file) runs
            # in a second thread while the Athena round runs here; both are joined.
            with ThreadPoolExecutor(max_workers=1) as background:
                fetching = background.submit(self._fetch_summaries)
                athena_began = self.clock()
                studies, links, datasets = self._run_all(
                    [(STUDIES_SQL, []), (STUDY_DATASETS_SQL, []), (DATASETS_SQL, [])],
                    deadline,
                )
                athena_ended = self.clock()
                fetched = fetching.result()
            entries, summaries_began, summaries_ended, listed = fetched
            # Only the datasets the response lists need an entry.
            names = [str(row["dataset"]) for row in dataset_rows(datasets, request.sub)]
            if not listed:  # the listing failed: read each needed summary now
                with timing.phase("summaries_fallback"):
                    entries = self._stored_entries(names)
            entries = {name: entries[name] for name in names if name in entries}
            missing = [name for name in names if name not in entries]
            timing.phases.update(
                athena_ms=_ms(athena_ended - athena_began),
                summaries_ms=_ms(summaries_ended - summaries_began),
                overlap_ms=overlap_ms(
                    athena_began, athena_ended, summaries_began, summaries_ended
                ),
            )
            timing.cache = "summary+live" if missing else "summary"
            entries.update(self._live_entries(missing, deadline))
            return studies_from((studies, links, datasets), entries, request.sub)

        return self._answer_capped(read)

    def dataset_entry(self, dataset: str, deadline: float) -> dict[str, list[Row]]:
        """Return one dataset's studies entry from the seven scoped texts.

        The dataset summary builder stores this as its `catalog` field.
        """
        return self._scoped_entries([dataset], deadline)[dataset]

    def _scoped_entries(
        self, names: list[str], deadline: float
    ) -> dict[str, dict[str, list[Row]]]:
        """Run `DATASET_ENTRY_SQL` for each named dataset in one round; fold each."""
        width = len(DATASET_ENTRY_SQL)
        results = self._run_all(
            [
                (sql, [name] * sql.count("?"))
                for name in names
                for sql in DATASET_ENTRY_SQL
            ],
            deadline,
        )
        return {
            name: dataset_entries(results[i * width : (i + 1) * width]).get(
                name, empty_entry()
            )
            for i, name in enumerate(names)
        }

    def _live_entries(
        self, missing: list[str], deadline: float
    ) -> dict[str, dict[str, list[Row]]]:
        """Return the live entry of each dataset with no stored one.

        Up to `SCOPED_FALLBACK_MAX` datasets run the scoped texts, seven per dataset;
        more run the seven unscoped `ENTRY_SQL` texts once and keep the missing ones.
        """
        if not missing:
            return {}
        if len(missing) <= SCOPED_FALLBACK_MAX:
            return self._scoped_entries(missing, deadline)
        live = dataset_entries(
            self._run_all([(sql, []) for sql in ENTRY_SQL], deadline)
        )
        return {name: live.get(name, empty_entry()) for name in missing}

    def _fetch_summaries(self) -> tuple[dict[str, dict[str, Any]], float, float, bool]:
        """List the stored summaries and read every one in parallel.

        Returns the entries by dataset, the clock at start and end, and whether the
        listing worked (False: the caller reads the datasets it needs by name).
        """
        began = self.clock()
        names = self._listed_datasets()
        entries = {} if names is None else self._stored_entries(names)
        return entries, began, self.clock(), names is not None

    def _listed_datasets(self) -> list[str] | None:
        """Return the datasets that have a key under `summary/`, else None.

        One delimiter listing: each dataset is a common prefix. None when there is
        no store or the listing is refused (logged); the route then reads by name.
        """
        s3, bucket = self.s3, self.derived_bucket
        if s3 is None or not bucket:
            return []
        prefix = f"{SUMMARY_PREFIX}/"
        names: list[str] = []
        token: str | None = None
        try:
            while True:
                page = (
                    s3.list_objects_v2(
                        Bucket=bucket,
                        Prefix=prefix,
                        Delimiter="/",
                        MaxKeys=SUMMARY_LIST_PAGE,
                    )
                    if token is None
                    else s3.list_objects_v2(
                        Bucket=bucket,
                        Prefix=prefix,
                        Delimiter="/",
                        MaxKeys=SUMMARY_LIST_PAGE,
                        ContinuationToken=token,
                    )
                )
                names += [
                    str(p["Prefix"])[len(prefix) : -1]
                    for p in page.get("CommonPrefixes", [])
                ]
                token = str(page.get("NextContinuationToken", "")) or None
                if not page.get("IsTruncated") or token is None:
                    return names
        except ClientError as err:
            code = str(err.response.get("Error", {}).get("Code"))
            logger.warning("summary listing refused (%s); reading by name", code)
            return None

    def _stored_entries(self, names: list[str]) -> dict[str, dict[str, Any]]:
        """Read each dataset's stored entry in parallel; absent ones are left out."""
        s3, bucket = self.s3, self.derived_bucket
        if s3 is None or not bucket or not names:
            return {}

        def one(name: str) -> tuple[str, dict[str, Any] | None]:
            return name, _stored_entry(s3, bucket, name)

        with ThreadPoolExecutor(max_workers=SUMMARY_READERS) as pool:
            found = list(pool.map(one, names))
        return {name: entry for name, entry in found if entry is not None}

    def links(self, request: Request, _timing: Timing) -> Response:
        """GET /catalog/links: subject links, filtered by one form, at most 5000."""
        sql, params = links_query(request.query)

        def read(deadline: float) -> dict[str, Any]:
            rows = self._run(sql, params, deadline)
            if len(rows) > MAX_LINKS:
                raise ApiError(
                    400,
                    f"more than {MAX_LINKS} links match; filter by study, "
                    "subject_key, or dataset with subject",
                    code="too_many",
                )
            return {"links": rows}

        return self._answer_capped(read)

    def asset_batch(self, request: Request, _timing: Timing) -> Response:
        """GET /catalog/assets?ids=: up to 100 files named in research terms.

        The body lists the found assets in the order asked and the ids not found under
        `missing`. Each entry says whether its version is still the newest of its file.
        """
        ids = batch_ids(request.query)

        def read(deadline: float) -> dict[str, Any]:
            rows = self._run(batch_sql(len(ids)), ids, deadline)
            found = {str(r["asset_id"]): batch_entry(r) for r in rows}
            return {
                "assets": [found[i] for i in ids if i in found],
                "missing": [i for i in ids if i not in found],
            }

        return self._answer(read)

    def dataset(self, request: Request, _timing: Timing) -> Response:
        """GET /catalog/datasets/{dataset}."""
        name = str(request.path_params.get("dataset", ""))
        return self._answer(lambda deadline: self._dataset(name, deadline))

    def assets(self, request: Request, _timing: Timing) -> Response:
        """GET /catalog/datasets/{dataset}/assets."""
        name = str(request.path_params.get("dataset", ""))
        return self._answer(
            lambda deadline: self._assets(request.query, name, deadline)
        )

    def asset(self, request: Request, _timing: Timing) -> Response:
        """GET /catalog/assets/{asset_id}."""
        asset_id = str(request.path_params.get("asset_id", ""))
        return self._answer(lambda deadline: self._asset(asset_id, deadline))

    def table(self, request: Request, _timing: Timing) -> Response:
        """GET /catalog/assets/{asset_id}/tables/{table}."""
        asset_id = str(request.path_params.get("asset_id", ""))
        table = str(request.path_params.get("table", ""))
        return self._answer(
            lambda deadline: self._table(request.query, asset_id, table, deadline)
        )

    def events(self, request: Request, _timing: Timing) -> Response:
        """GET /catalog/assets/{asset_id}/events."""
        asset_id = str(request.path_params.get("asset_id", ""))
        return self._answer(lambda deadline: self._events(asset_id, deadline))

    def download(self, request: Request, timing: Timing) -> Response:
        """GET /catalog/assets/{asset_id}/download."""
        asset_id = str(request.path_params.get("asset_id", ""))
        return self._answer(lambda deadline: self._download(asset_id, deadline, timing))

    def _assets(
        self, query: Mapping[str, str], dataset: str, deadline: float
    ) -> dict[str, Any]:
        if not DATASET_RE.match(dataset):
            raise ApiError(400, "dataset must match ^[a-z0-9][a-z0-9_-]{0,62}$")
        subject = _query_value(query, "subject")
        session = _query_value(query, "session")
        if session not in (None, SESSION_NONE) and not SESSION_RE.match(str(session)):
            raise ApiError(
                400,
                "session must be _ or match ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$",
                code="invalid_session",
            )
        fmt = _query_value(query, "format")
        page = _page(query)
        sql, params = assets_query(dataset, subject, fmt, page, session)
        rows = self._run(sql, params, deadline)
        prefix = f"raw/{dataset}/"
        assets: list[dict[str, object]] = []
        for row in rows[:PAGE_SIZE]:
            key = str(row["s3_key"])
            record = {k: row[k] for k in _ASSET_KEYS if k != "s3_key"}
            record["path"] = key.removeprefix(prefix)
            assets.append(record)
        return {"assets": assets, "page": page, "more": len(rows) > PAGE_SIZE}

    def _asset(self, asset_id: str, deadline: float) -> dict[str, Any]:
        found, headers = self._run_all(
            [(ASSET_SQL, [asset_id]), (HEADERS_SQL, [asset_id])], deadline
        )
        if not found:
            raise ApiError(404, "no such asset")
        # the file page loads the asset before the Image tab asks to presign,
        # so seed the download cache from this row and make that first presign a hit.
        if all(k in found[0] for k in _DOWNLOAD_FIELDS):
            row = {k: found[0][k] for k in _DOWNLOAD_FIELDS}
            cache_put(asset_id, row, self.clock())
        return {"asset": found[0], "headers": headers}

    def _download(
        self, asset_id: str, deadline: float, timing: Timing
    ) -> dict[str, Any]:
        """Presign a GET on the catalogued object version.

        The timing line (`catalog_download`) carries the asset id, the cache
        outcome and the `athena_ms` (on a miss) and `presign_ms` phases.
        """
        began = self.clock()
        timing.asset_id, timing.cache = asset_id, "miss"
        row = cache_get(asset_id, self.clock())
        if row is None:
            found = self._run(ASSET_SQL, [asset_id], deadline)
            timing.phases["athena_ms"] = int((self.clock() - began) * 1000)
            if not found:
                raise ApiError(404, "no such asset")  # a 404 is not cached
            row = {k: found[0][k] for k in _DOWNLOAD_FIELDS}
            cache_put(asset_id, row, self.clock())
        else:
            timing.cache = "hit"
        if self.s3 is None:  # pragma: no cover
            msg = "CatalogApi.s3 is not set"
            raise RuntimeError(msg)
        version = str(row["s3_version_id"])
        presigning = self.clock()
        url = self.s3.generate_presigned_url(
            ClientMethod="get_object",
            Params={
                "Bucket": self.raw_bucket,
                "Key": str(row["s3_key"]),
                "VersionId": version,
            },
            ExpiresIn=DOWNLOAD_SECONDS,
        )
        timing.phases["presign_ms"] = int((self.clock() - presigning) * 1000)
        return {
            "url": url,
            "expires_in": DOWNLOAD_SECONDS,
            "size_bytes": row["size_bytes"],
            "s3_version_id": version,
            "format": row["format"],
        }

    def _dataset(self, dataset: str, deadline: float) -> dict[str, Any]:
        if not DATASET_RE.match(dataset):
            raise ApiError(400, "dataset must match ^[a-z0-9][a-z0-9_-]{0,62}$")
        params = [dataset]
        found, subjects, sessions, formats = self._run_all(
            [
                (DATASET_FACTS_SQL, params),
                (DATASET_SUBJECTS_SQL, params),
                (DATASET_SESSIONS_SQL, params),
                (DATASET_FORMATS_SQL, params),
            ],
            deadline,
        )
        if not found:
            raise ApiError(404, "no such dataset")
        return {
            "dataset": dataset,
            "facts": found[0],
            "subjects": subjects,
            "sessions": sessions,
            "formats": formats,
        }

    def _table(
        self, query: Mapping[str, str], asset_id: str, table: str, deadline: float
    ) -> dict[str, Any]:
        _table_sql(table)
        return self.table_body(asset_id, table, _page(query), deadline)

    def table_body(
        self, asset_id: str, table: str, page: int, deadline: float
    ) -> dict[str, Any]:
        """Return one page of an allowlisted table: the route body less `query_ms`.

        The route and the per-session summary builder both call this, so a stored
        `units` part is the route's first page.
        """
        sql = _table_sql(table)
        rows = self._run(sql, [asset_id, PAGE_SIZE * page], deadline)
        return {"rows": rows[:PAGE_SIZE], "page": page, "more": len(rows) > PAGE_SIZE}

    def _events(self, asset_id: str, deadline: float) -> dict[str, Any]:
        found = self._run(ASSET_SQL, [asset_id], deadline)
        if not found:
            raise ApiError(404, "no such asset")
        dataset = str(found[0]["dataset"])
        relpath = str(found[0]["s3_key"]).removeprefix(f"raw/{dataset}/")
        intervals, events = self._run_all(
            [
                (INTERVALS_SQL, [asset_id]),
                (EVENTS_SQL, [dataset, bids_path.entity_key(relpath)]),
            ],
            deadline,
        )
        return {
            "intervals": intervals[:EVENT_ROWS],
            "events": events[:EVENT_ROWS],
            "intervals_more": len(intervals) > EVENT_ROWS,
            "events_more": len(events) > EVENT_ROWS,
        }

    def _runner(self) -> AthenaRunner:
        return AthenaRunner(
            athena=self.athena,
            workgroup=self.workgroup,
            database=self.database,
            clock=self.clock,
            sleep=self.sleep,
        )

    def _run(
        self, sql: str, params: Params, deadline: float
    ) -> list[dict[str, object]]:
        return self._runner().run(sql, params, deadline)

    def _run_all(
        self, queries: list[tuple[str, Params]], deadline: float
    ) -> list[list[dict[str, object]]]:
        return self._runner().run_all(queries, deadline)


def handler(event: dict[str, Any], context: object) -> Response:
    """Lambda entry point."""
    return CatalogApi(
        athena=boto3.client("athena"),
        workgroup=os.environ["ATHENA_WORKGROUP"],
        database=os.environ["GLUE_DATABASE"],
        s3=boto3.client(
            "s3",
            config=Config(
                signature_version="s3v4", max_pool_connections=SUMMARY_READERS
            ),
        ),
        raw_bucket=os.environ["RAW_BUCKET"],
        derived_bucket=os.environ["DERIVED_BUCKET"],
    ).handle(event, invocation=Invocation.of(context))
