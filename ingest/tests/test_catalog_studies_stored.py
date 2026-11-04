"""GET /catalog/studies from the stored dataset summaries.

The Athena fake here runs every query text as written against SQLite tables and
answers each cell as `VarCharValue` text typed by its column (arrays as "[a, b]"), as
Athena does; `ExecutionParameters` arrive as typed literals and are bound in order.
So the scoped texts the summary builder runs and the unscoped texts the live route
runs are compared on the same rows, not on scripted answers.

The summary store fake is the derived bucket as CatalogRole sees it: GetObject on
`summary/*/_dataset.json` only (anything else is AccessDenied), and ListBucket on
`summary/*`, so a missing key is NoSuchKey.
"""

import io
import json
import logging
import re
import sqlite3
from typing import TYPE_CHECKING, Any, Final, cast, override

import pytest
from botocore.exceptions import ClientError

from asteroidea_ingest import catalog_api
from asteroidea_ingest.athena_query import AthenaRunner
from asteroidea_ingest.catalog_api import CatalogApi

from .conftest import jwt_event
from .test_catalog_api import FakeAthena
from .test_catalog_studies import check_golden, studies_fake

if TYPE_CHECKING:
    from collections.abc import Iterator

    from asteroidea_ingest.aws_types import S3

DERIVED: Final = "derived-bucket"
STUDIES: Final = "GET /catalog/studies"
CALLER: Final = "user-1"
ALPHA, BETA, GAMMA = "alpha", "beta", "gamma"
LFP_PATH: Final = "/processing/ecephys/LFP/ElectricalSeries"
GRANTED: Final = re.compile(r"summary/.+/_dataset\.json")
TYPES: Final = {
    "n_units": "bigint",
    "n_trials": "bigint",
    "units": "bigint",
    "channels": "bigint",
    "asset_count": "bigint",
    "subject_count": "bigint",
    "total_bytes": "bigint",
    "size_bytes": "bigint",
    "shape": "array",
    "rate_hz": "double",
    "created_at": "timestamp",
}

ACCESS_DENIED, NO_SUCH_KEY = "AccessDenied", "NoSuchKey"
COLUMNS: Final = {
    "studies": "study, title, authors, lab, citation, doi",
    "study_datasets": "study, dataset, role",
    "datasets": "dataset, title, tier, license, doi, source_archive, source_dataset, "
    "source_version, ingest_method, created_by, created_at",
    "subjects": "dataset, subject, species, sex, age",
    "assets": "asset_id, dataset, subject, bids_session, datatype, suffix, format, "
    "size_bytes, s3_key",
    "asset_headers": "asset_id, n_units, n_trials",
    "series": "asset_id, path, neurodata_type, shape, rate_hz",
    "units": "dataset, asset_id, location",
    "electrodes": "dataset, asset_id, location",
}


def _assets() -> list[tuple[object, ...]]:
    rows: list[tuple[object, ...]] = [
        ("a1", ALPHA, "s1", "1", None, "ecephys", "nwb", 1000, "raw/alpha/s1/a1.nwb"),
        ("a1ev", ALPHA, "s1", "1", "func", "events", "tsv", 10, "raw/alpha/s1/ev.tsv"),
        ("a-none", ALPHA, None, None, None, "scans", "tsv", 5, "raw/alpha/scans.tsv"),
        ("b1", BETA, "s1", None, None, "behavior", "nwb", 300, "raw/beta/s1/b1.nwb"),
        ("b2", BETA, "s9", "2", "dwi", "dwi", "nifti", 70, "raw/beta/s9/dwi.nii"),
    ]
    # 22 files in one session: the session lists the first 20 by s3_key
    rows += [
        (
            f"t{i:02d}",
            ALPHA,
            "s3",
            None,
            "anat",
            "T1w",
            "nifti",
            1,
            f"raw/alpha/s3/{i:02d}",
        )
        for i in range(22)
    ]
    return rows


_OPEN: list[sqlite3.Connection] = []


@pytest.fixture(autouse=True)
def _close_databases() -> Iterator[None]:
    yield
    while _OPEN:
        _OPEN.pop().close()


def _database() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:", check_same_thread=False)
    _OPEN.append(db)
    db.create_function("strpos", 2, lambda a, b: str(a).find(str(b)) + 1)
    for table, columns in COLUMNS.items():
        db.execute(f"CREATE TABLE {table} ({columns})")
    db.execute(
        "INSERT INTO studies VALUES ('st', 'A study', 'A, B', 'Lab', 'A (2026)', NULL)"
    )
    db.executemany(
        "INSERT INTO study_datasets VALUES (?, ?, ?)",
        [("st", ALPHA, "ephys"), ("st", BETA, "behavior")],
    )
    db.executemany(
        "INSERT INTO datasets VALUES (?, ?, ?, NULL, NULL, NULL, NULL, NULL, ?, ?, ?)",
        [
            (name, f"Title {name}", tier, method, by, "2026-10-01 12:00:00.000")
            for name, tier, method, by in (
                (ALPHA, None, "import", "importer"),
                (BETA, "sandbox", "import", "importer"),
                (GAMMA, None, "upload", CALLER),
            )
        ],
    )
    db.executemany(
        "INSERT INTO subjects VALUES (?, ?, ?, ?, ?)",
        [
            (ALPHA, "s1", "Homo sapiens", "F", "P30Y"),
            (ALPHA, "s2", "Homo sapiens", "M", None),  # no files
            (BETA, "s1", "Mus musculus", None, "P60D"),  # same name, other dataset
        ],
    )
    db.executemany("INSERT INTO assets VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", _assets())
    db.executemany(
        "INSERT INTO asset_headers VALUES (?, ?, ?)", [("a1", 5, 3), ("b1", 0, 0)]
    )
    db.executemany(
        "INSERT INTO series VALUES (?, ?, ?, ?, ?)",
        [
            ("a1", LFP_PATH, "ElectricalSeries", "[100, 4]", 1000.0),
            ("a1", "/acquisition/raw", "ElectricalSeries", "[900, 4]", 30000.0),
            ("b1", "/processing/lfp_like", "TimeSeries", "[10]", None),
        ],
    )
    db.executemany(
        "INSERT INTO units VALUES (?, ?, ?)",
        [
            (ALPHA, "a1", "CA1"),
            (ALPHA, "a1", "CA1"),
            (ALPHA, "a1", None),
            (BETA, "b1", "CA1"),
        ],
    )
    db.executemany(
        "INSERT INTO electrodes VALUES (?, ?, ?)",
        [(ALPHA, "a1", "CA1")] * 3 + [(ALPHA, "a1", "DG"), (BETA, "b1", None)],
    )
    return db


def _literal(param: str) -> str:
    """Undo `render_param` for a string: strip the quotes, undouble the inner ones."""
    assert param.startswith("'")
    assert param.endswith("'")
    return param[1:-1].replace("''", "'")


def _text(value: object) -> dict[str, str]:
    return {} if value is None else {"VarCharValue": str(value)}


class SqliteAthena:
    """Athena over SQLite: runs each started text with its typed literals bound."""

    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db
        self.started: list[dict[str, Any]] = []
        self.events: list[str] = []

    def start_query_execution(self, **kwargs: Any) -> dict[str, Any]:
        self.started.append(kwargs)
        self.events.append("start")
        return {"QueryExecutionId": f"q{len(self.started)}"}

    def get_query_execution(self, **_: Any) -> dict[str, Any]:
        return {"QueryExecution": {"Status": {"State": "SUCCEEDED"}}}

    def get_query_results(self, **kwargs: Any) -> dict[str, Any]:
        query = self.started[int(kwargs["QueryExecutionId"][1:]) - 1]
        params = [_literal(p) for p in query.get("ExecutionParameters", [])]
        cursor = self.db.execute(query["QueryString"], params)
        names = [str(d[0]) for d in cursor.description]
        rows = cursor.fetchall()
        return {
            "ResultSet": {
                "Rows": [
                    {"Data": [{"VarCharValue": n} for n in names]},
                    *({"Data": [_text(v) for v in row]} for row in rows),
                ],
                "ResultSetMetadata": {
                    "ColumnInfo": [
                        {"Name": n, "Type": TYPES.get(n, "varchar")} for n in names
                    ]
                },
            }
        }

    def stop_query_execution(self, **_: Any) -> dict[str, Any]:
        return {}

    def texts(self) -> list[tuple[str, list[str]]]:
        return [
            (str(q["QueryString"]), list(q.get("ExecutionParameters", [])))
            for q in self.started
        ]


def _error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, "GetObject")


class SummaryStore:
    """The derived bucket as CatalogRole reads it (see the module docstring)."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.gets: list[str] = []

    def put(self, dataset: str, body: object) -> None:
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.objects[catalog_api.summary_key(dataset)] = raw

    def keys(self) -> list[str]:
        return list(self.objects)

    def list_objects_v2(self, **kwargs: Any) -> dict[str, Any]:
        assert kwargs["Prefix"] == "summary/"
        assert kwargs["Delimiter"] == "/"
        names = sorted({k.split("/")[1] for k in self.keys()})
        return {
            "CommonPrefixes": [{"Prefix": f"summary/{n}/"} for n in names],
            "IsTruncated": False,
        }

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        bucket, key = kwargs["Bucket"], kwargs["Key"]
        self.gets.append(key)
        if bucket != DERIVED or not GRANTED.fullmatch(key):
            raise _error(ACCESS_DENIED)
        if key not in self.objects:
            raise _error(NO_SUCH_KEY)
        return {"Body": io.BytesIO(self.objects[key])}


class RefusingStore(SummaryStore):
    """A store that answers one key AccessDenied, as a missing grant would."""

    def __init__(self, refused: str) -> None:
        super().__init__()
        self.refused = refused

    @override
    def keys(self) -> list[str]:
        return [*self.objects, self.refused]  # listed, but the grant refuses the GET

    @override
    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        if kwargs["Key"] == self.refused:
            self.gets.append(kwargs["Key"])
            raise _error(ACCESS_DENIED)
        return super().get_object(**kwargs)


def _api(athena: object, store: SummaryStore | None) -> CatalogApi:
    return CatalogApi(
        athena=cast("Any", athena),
        workgroup="asteroidea",
        database="asteroidea",
        clock=lambda: 0.0,
        sleep=lambda _: None,
        s3=None if store is None else cast("S3", store),
        derived_bucket=DERIVED if store is not None else "",
    )


def _studies(athena: object, store: SummaryStore | None) -> dict[str, Any]:
    event = {**jwt_event(STUDIES, sub=CALLER), "queryStringParameters": None}
    resp = _api(athena, store).handle(event)
    assert resp["statusCode"] == 200, resp["body"]
    body = json.loads(resp["body"])
    body.pop("query_ms")
    return body


def _live_body() -> dict[str, Any]:
    return _studies(SqliteAthena(_database()), None)


def _summary(entry: object) -> dict[str, Any]:
    return {"schema": "asteroidea.dataset-summary/1", "as_of": "x", "catalog": entry}


def _stored_by_the_builder(*datasets: str) -> SummaryStore:
    """Store each dataset's entry as the summary builder does: `dataset_entry`."""
    store = SummaryStore()
    api = _api(SqliteAthena(_database()), None)
    for name in datasets:
        store.put(name, _summary(api.dataset_entry(name, deadline=1e9)))
    return store


TABLE_TEXTS: Final = [
    (catalog_api.STUDIES_SQL, []),
    (catalog_api.STUDY_DATASETS_SQL, []),
    (catalog_api.DATASETS_SQL, []),
]


def _scoped(name: str) -> list[tuple[str, list[str]]]:
    return [
        (sql, [f"'{name}'"] * sql.count("?")) for sql in catalog_api.DATASET_ENTRY_SQL
    ]


# --- the scoped texts are the unscoped texts' rows of one dataset ---


def _rows(db: sqlite3.Connection, sql: str, params: list[str]) -> list[tuple[Any, ...]]:
    return db.execute(sql, params).fetchall()


@pytest.mark.parametrize("dataset", [ALPHA, BETA, GAMMA])
def test_each_scoped_text_returns_the_unscoped_rows_of_its_dataset(
    dataset: str,
) -> None:
    db = _database()
    pairs = zip(catalog_api.ENTRY_SQL, catalog_api.DATASET_ENTRY_SQL, strict=True)
    for unscoped, scoped in pairs:
        everything = _rows(db, unscoped, [])
        mine = [row for row in everything if row[0] == dataset]
        assert _rows(db, scoped, [dataset] * scoped.count("?")) == mine, scoped
    # the comparison has rows to compare, and other datasets' rows to leave out
    assert _rows(db, catalog_api.NWB_SQL, [])


def test_a_planted_scope_difference_is_caught() -> None:
    db = _database()
    scoped = catalog_api.DATASET_ENTRY_SQL[2]  # the session file ids
    planted = scoped.replace(" AND subject IS NOT NULL", "")
    assert planted != scoped
    everything = _rows(db, catalog_api.SESSION_ASSETS_SQL, [])
    mine = [row for row in everything if row[0] == ALPHA]
    assert _rows(db, scoped, [ALPHA]) == mine
    assert _rows(db, planted, [ALPHA]) != mine


# --- the summary-backed body is the live body ---


def test_the_summary_backed_body_equals_the_live_body() -> None:
    live = _live_body()
    athena = SqliteAthena(_database())
    store = _stored_by_the_builder(ALPHA, BETA, GAMMA)
    stored = _studies(athena, store)
    assert stored == live
    # three table reads, no aggregate; one GET per dataset, of its summary only
    assert athena.texts() == TABLE_TEXTS
    assert sorted(store.gets) == [
        catalog_api.summary_key(n) for n in (ALPHA, BETA, GAMMA)
    ]
    # the fixture exercises the fold: a capped session, a cross-dataset name, LFP
    alpha = next(d for d in live["datasets"] if d["dataset"] == ALPHA)
    s3 = next(s for s in alpha["subjects"] if s["subject"] == "s3")
    assert len(s3["sessions"][0]["asset_ids"]) == catalog_api.SESSION_ASSET_IDS
    s1 = next(s for s in alpha["subjects"] if s["subject"] == "s1")
    assert s1["modalities"] == ["units", "lfp", "task"]
    assert alpha["regions"] == [
        {"region": "CA1", "units": 2, "channels": 3},
        {"region": "DG", "units": 0, "channels": 1},
    ]


def test_a_planted_stored_difference_reaches_the_body() -> None:
    live = _live_body()
    store = _stored_by_the_builder(ALPHA, BETA, GAMMA)
    key = catalog_api.summary_key(ALPHA)
    summary = json.loads(store.objects[key])
    s1 = next(s for s in summary["catalog"]["subjects"] if s["subject"] == "s1")
    s1["sessions"][0]["nwb"][0]["n_units"] = 999
    store.objects[key] = json.dumps(summary).encode()
    stored = _studies(SqliteAthena(_database()), store)
    assert stored != live
    alpha = next(d for d in stored["datasets"] if d["dataset"] == ALPHA)
    got = next(s for s in alpha["subjects"] if s["subject"] == "s1")
    assert got["sessions"][0]["nwb"][0]["n_units"] == 999


# --- a dataset with no stored entry reads live, never dropped ---


def test_a_missing_summary_and_one_without_catalog_read_live_for_those_only() -> None:
    live = _live_body()
    store = _stored_by_the_builder(ALPHA)
    no_entry = _summary(None)
    no_entry.pop("catalog")
    store.put(BETA, no_entry)  # a summary built before the entry existed
    athena = SqliteAthena(_database())
    stored = _studies(athena, store)
    assert stored == live
    # beta and gamma (missing) run the scoped texts together, one round
    assert athena.texts() == [*TABLE_TEXTS, *_scoped(BETA), *_scoped(GAMMA)]
    assert athena.events[3:] == ["start"] * 14


def test_over_the_scoped_limit_the_unscoped_texts_run_once() -> None:
    assert catalog_api.SCOPED_FALLBACK_MAX == 2
    live = _live_body()
    athena = SqliteAthena(_database())
    stored = _studies(athena, SummaryStore())  # three datasets, none stored
    assert stored == live
    assert athena.texts() == [
        *TABLE_TEXTS,
        *[(sql, []) for sql in catalog_api.ENTRY_SQL],
    ]


def test_a_refused_or_malformed_summary_reads_live_and_says_why(
    caplog: pytest.LogCaptureFixture,
) -> None:
    live = _live_body()
    store = RefusingStore(catalog_api.summary_key(ALPHA))
    store.put(BETA, b"{not json")
    store.put(GAMMA, _summary({"subjects": "not a list", "regions": []}))
    with caplog.at_level(logging.WARNING, logger="asteroidea_ingest.catalog_api"):
        stored = _studies(SqliteAthena(_database()), store)
    assert stored == live
    text = caplog.text
    assert f"{catalog_api.summary_key(ALPHA)} unreadable (AccessDenied)" in text
    assert f"{catalog_api.summary_key(BETA)} is not JSON" in text


def test_a_missing_summary_is_not_logged(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="asteroidea_ingest.catalog_api"):
        _studies(SqliteAthena(_database()), _stored_by_the_builder(ALPHA, BETA))
    assert caplog.text == ""


# --- the scripted fixture: the stored path answers the committed golden ---


def test_stored_entries_answer_the_studies_golden() -> None:
    fake = studies_fake()
    # each dataset's entry, folded from the fixture's seven entry result sets
    entry_rows = AthenaRunner(
        athena=FakeAthena(*fake.results[3:]),
        workgroup="asteroidea",
        database="asteroidea",
        clock=lambda: 0.0,
        sleep=lambda _: None,
    ).run_all([(sql, []) for sql in catalog_api.ENTRY_SQL], deadline=1e9)
    entries = catalog_api.dataset_entries(entry_rows)
    store = SummaryStore()
    for row in fake.results[2][1]:
        name = str(row[0])
        store.put(name, _summary(entries.get(name, catalog_api.empty_entry())))
    event = {**jwt_event(STUDIES), "queryStringParameters": None}
    resp = _api(fake, store).handle(event)
    body = json.loads(resp["body"])
    assert len(fake.started) == len(TABLE_TEXTS)
    check_golden("catalog-studies.golden.json", body)
