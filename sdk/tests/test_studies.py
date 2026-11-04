import datetime as dt
import decimal
import itertools
import json
import urllib.parse
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

import asteroidea
from asteroidea import _sql, _studies
from asteroidea._http import Reply
from asteroidea.errors import QueryError
from asteroidea_infra import schema
from asteroidea_ingest import catalog_api

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    import duckdb

    from asteroidea._auth import Auth

ROWS: dict[str, dict[str, list[Any]]] = {
    "datasets": {
        "dataset": ["d1", "d2", "d3"],
        "title": ["One", "Two", "Three"],
        "ingest_method": ["import", "upload", "upload"],
        "tier": [None, None, "sandbox"],
    },
    "studies": {
        "study": ["s1", "s2"],
        "title": ["Study 1", "Empty"],
    },
    "study_datasets": {
        "study": ["s1", "s1"],
        "dataset": ["d1", "d2"],
        "role": ["primary", None],
    },
    "subjects": {
        "dataset": ["d1", "d1", "d2", "d3"],
        "subject": ["sub-01", "sub-02", "sub-01", "sub-09"],
    },
    "subject_links": {
        "dataset": ["d1", "d2"],
        "subject": ["sub-01", "sub-01"],
        "subject_key": ["K1", "K1"],
        "status": [None, "confirmed"],
        "linked_by": [None, "me"],
        "basis": [None, "age"],
    },
    "assets": {
        "asset_id": ["a1", "a2", "a3", "a4", "a5"],
        "dataset": ["d1", "d2", "d1", "d1", "d3"],
        "s3_key": ["k1", "k2", "k3", "k4", "k5"],
        "subject": ["sub-01", "sub-01", "sub-01", "sub-02", None],
        "datatype": [None, "anat", "events", None, None],
        "suffix": ["ecephys", "T1w", "events", "ecephys", None],
        "format": ["nwb", "nifti", "tsv", "nwb", "nwb"],
    },
    "asset_headers": {
        "asset_id": ["a1", "a4"],
        "n_units": [44, 0],
        "n_trials": [0, 0],
    },
    "series": {
        "asset_id": ["a1"],
        "path": ["/acquisition/LFP"],
        "neurodata_type": ["ElectricalSeries"],
    },
}


@pytest.fixture
def reads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Auth:
    paths = {}
    for spec in schema.TABLES:
        data = ROWS.get(spec.name, {})
        size = len(next(iter(data.values()), []))
        columns = {c.name: data.get(c.name, [None] * size) for c in spec.columns}
        path = tmp_path / f"{spec.name}.parquet"
        pq.write_table(pa.table(columns), path)
        paths[spec.name] = str(path)

    def scan(con: duckdb.DuckDBPyConnection, location: str) -> duckdb.DuckDBPyRelation:
        return con.sql("SELECT * FROM read_parquet(?)", params=[location])

    def nothing(*_: object) -> None:
        return

    engine = _sql.Engine(scan=scan, setup=nothing, secret=nothing)

    def fake(_: object, query: str, params: list[str | None] | None = None) -> pa.Table:
        wanted = {n: paths[n] for n in _sql.tables_needed(query, params)}
        return engine.run(query, params, wanted, None)

    monkeypatch.setattr(_sql, "sql", fake)
    auth = SimpleNamespace(
        config=SimpleNamespace(api_url=API),
        tokens=lambda: SimpleNamespace(access_token="tok"),
        rows=lambda query, params: fake(None, query, params).to_pylist(),
    )
    return cast("Auth", auth)


API = "https://api.example"


def _json(value: object) -> str | int:
    if isinstance(value, dt.datetime):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return int(value)
    raise TypeError(value)


class Routes:
    """`GET /catalog/studies` and `/catalog/links`, answered by the route's own code.

    Each body is `catalog_api`'s fold over its own texts run on the fixture's tables,
    so the participants built from it follow the route's rule, not a copy of it.
    """

    def __init__(self, auth: Auth) -> None:
        self.rows = cast("Any", auth).rows
        self.calls: list[tuple[str, str]] = []
        self.status = 200

    def __call__(
        self, method: str, url: str, headers: Mapping[str, str], body: object
    ) -> Reply:
        assert headers["Authorization"] == "Bearer tok"
        assert body is None
        self.calls.append((method, url.removeprefix(API)))
        if self.status != 200:
            return Reply(self.status, b'{"error": "too_many", "detail": "filter"}')
        parts = urllib.parse.urlsplit(url)
        if parts.path == "/catalog/studies":
            texts = [
                catalog_api.STUDIES_SQL,
                catalog_api.STUDY_DATASETS_SQL,
                catalog_api.DATASETS_SQL,
                *catalog_api.ENTRY_SQL,
            ]
            found = catalog_api.studies_body([self.rows(t, []) for t in texts], "me")
        else:
            assert parts.path == "/catalog/links"
            query = dict(urllib.parse.parse_qsl(parts.query))
            text, values = catalog_api.links_query(query)
            found = {"links": self.rows(text, [str(v) for v in values])}
        return Reply(200, json.dumps(found, default=_json).encode())


@pytest.fixture
def routes(reads: Auth, monkeypatch: pytest.MonkeyPatch) -> Routes:
    fake = Routes(reads)
    monkeypatch.setattr(_studies, "send", fake)
    return fake


def test_studies_one_row_per_study_and_dataset(reads: Auth) -> None:
    rows = _studies.studies(reads).to_pylist()
    got = [(r["study"], r["dataset"], r["role"], r["tier"]) for r in rows]
    assert got == [
        ("s1", "d1", "primary", "curated"),
        ("s1", "d2", None, "upload"),
        ("s2", None, None, None),
    ]
    assert rows[0]["dataset_title"] == "One"


def test_studies_tier_filter(reads: Auth) -> None:
    curated = _studies.studies(reads, tier="curated").to_pylist()
    assert [(r["study"], r["dataset"]) for r in curated] == [("s1", "d1")]
    upload = _studies.studies(reads, tier="upload").to_pylist()
    assert [r["dataset"] for r in upload] == ["d2"]
    assert _studies.studies(reads, tier="x' OR 'a'='a").num_rows == 0


@pytest.mark.usefixtures("routes")
def test_participants_rows_and_modalities(reads: Auth) -> None:
    rows = {r["subject_key"]: r for r in _studies.participants(reads).to_pylist()}
    assert sorted(rows) == ["K1", "d1/sub-02", "d3/sub-09"]
    assert rows["K1"]["datasets"] == ["d1", "d2"]
    assert rows["K1"]["subjects"] == ["d1/sub-01", "d2/sub-01"]
    assert rows["K1"]["modalities"] == ["units", "lfp", "anat", "task"]
    assert rows["d1/sub-02"]["modalities"] == ["other:ecephys"]
    assert rows["d3/sub-09"]["modalities"] == []


@pytest.mark.usefixtures("routes")
def test_participants_has_keeps_those_with_all(reads: Auth) -> None:
    both = _studies.participants(reads, has=["anat", "units"])
    assert both.column("subject_key").to_pylist() == ["K1"]
    assert _studies.participants(reads, has=["anat", "ieeg"]).num_rows == 0
    assert _studies.participants(reads, has="anat").num_rows == 1


@pytest.mark.usefixtures("routes")
def test_participants_study_filter(reads: Auth) -> None:
    keys = _studies.participants(reads, study="s1").column("subject_key").to_pylist()
    assert keys == ["K1", "d1/sub-02"]
    assert _studies.participants(reads, study="s2").num_rows == 0


@pytest.mark.usefixtures("routes")
def test_participants_empty_has_schema(reads: Auth) -> None:
    empty = _studies.participants(reads, has=["ieeg"])
    assert empty.column_names == ["subject_key", "datasets", "subjects", "modalities"]


def test_participants_reads_two_routes_and_no_table(
    reads: Auth, routes: Routes, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*_: object) -> pa.Table:
        pytest.fail("participants() read a catalog table")

    monkeypatch.setattr(_sql, "sql", refuse)
    assert _studies.participants(reads).num_rows == 3
    assert _studies.participants(reads, study="s1").num_rows == 2
    assert routes.calls == [
        ("GET", "/catalog/studies"),
        ("GET", "/catalog/links"),
        ("GET", "/catalog/studies"),
        ("GET", "/catalog/links?study=s1"),
    ]


def test_participants_route_refusal_is_a_query_error(
    reads: Auth, routes: Routes
) -> None:
    routes.status = 400
    with pytest.raises(QueryError, match="400 too_many: filter"):
        _studies.participants(reads)


def test_links_default_status_and_filters(reads: Auth) -> None:
    rows = _studies.links(reads).to_pylist()
    assert [(r["dataset"], r["status"], r["linked_by"]) for r in rows] == [
        ("d1", "pending", None),
        ("d2", "confirmed", "me"),
    ]
    assert rows[1]["basis"] == "age"
    assert _studies.links(reads, study="s1").num_rows == 2
    assert _studies.links(reads, study="s2").num_rows == 0
    assert _studies.links(reads, subject_key="K1").num_rows == 2
    assert _studies.links(reads, subject_key="K2").num_rows == 0


@pytest.mark.usefixtures("routes")
def test_public_calls_are_exported_and_delegate(
    reads: Auth, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(asteroidea, "default_auth", lambda: reads)
    for name in ("studies", "participants", "links"):
        assert name in asteroidea.__all__
    assert asteroidea.studies(tier="curated").num_rows == 1
    assert asteroidea.participants(has=["anat"]).num_rows == 1
    assert asteroidea.links(subject_key="K1").num_rows == 2


def test_modality_vocabulary_matches_the_catalog_route() -> None:
    assert _studies.MODALITIES == catalog_api.MODALITIES
    values = [None, "anat", "ieeg", "func", "beh", "events", "T1w", "nwb", ""]
    for datatype, suffix, fmt in itertools.product(values, repeat=3):
        assert _studies.file_kind(datatype, suffix, fmt) == catalog_api.file_kind(
            datatype, suffix, fmt
        )
