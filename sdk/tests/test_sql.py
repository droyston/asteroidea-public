from typing import TYPE_CHECKING, cast

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from botocore.exceptions import ClientError

from asteroidea import _browse, _sql
from asteroidea.errors import QueryError
from asteroidea_infra import schema

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from asteroidea._auth import Auth

ROWS: dict[str, dict[str, list[str]]] = {
    "datasets": {"dataset": ["d1", "d2"], "title": ["One", "Two"]},
    "subjects": {
        "dataset": ["d1", "d1", "d2"],
        "subject": ["sub-01", "sub-02", "sub-01"],
    },
    "subject_links": {"dataset": ["d1"], "subject": ["sub-01"], "subject_key": ["K1"]},
    "sessions": {
        "dataset": ["d1", "d2"],
        "subject": ["sub-01", "sub-01"],
        "bids_session": ["ses-1", "ses-1"],
    },
    "assets": {
        "asset_id": ["a1", "a2"],
        "dataset": ["d1", "d2"],
        "s3_key": ["k1", "k2"],
        "subject": ["sub-01", "sub-01"],
        "bids_session": ["ses-1", "ses-1"],
        "format": ["nwb", "nifti"],
    },
    "asset_headers": {
        "asset_id": ["a1"],
        "dataset": ["d1"],
        "format": ["nwb"],
        "n_units": ["44"],
    },
}


class FakeGlue:
    def __init__(self, locations: Mapping[str, str | None]) -> None:
        self.locations = locations
        self.calls: list[tuple[str, str]] = []

    def get_table(self, **kwargs: str) -> dict[str, dict[str, object]]:
        name = kwargs["Name"]
        self.calls.append((kwargs["DatabaseName"], name))
        location = self.locations.get(name)
        params = {} if location is None else {"metadata_location": location}
        return {"Table": {"Name": name, "Parameters": params}}


@pytest.fixture
def parquet(tmp_path: Path) -> dict[str, str]:
    paths = {}
    for spec in schema.TABLES:
        data = ROWS.get(spec.name, {})
        size = len(next(iter(data.values()), []))
        columns = {c.name: data.get(c.name, [None] * size) for c in spec.columns}
        path = tmp_path / f"{spec.name}.parquet"
        pq.write_table(pa.table(columns), path)
        paths[spec.name] = str(path)
    return paths


def local_scan(
    con: duckdb.DuckDBPyConnection, location: str
) -> duckdb.DuckDBPyRelation:
    return con.sql("SELECT * FROM read_parquet(?)", params=[location])


def nothing(*_: object) -> None:
    return


def engine(counter: list[str] | None = None) -> _sql.Engine:
    def scan(con: duckdb.DuckDBPyConnection, location: str) -> duckdb.DuckDBPyRelation:
        if counter is not None:
            counter.append(location)
        return local_scan(con, location)

    return _sql.Engine(scan=scan, setup=nothing, secret=nothing)


def run(
    parquet: dict[str, str], query: str, params: list[str | None] | None = None
) -> pa.Table:
    wanted = {n: parquet[n] for n in _sql.tables_needed(query, params)}
    return engine().run(query, params, wanted, None)


def test_table_names_are_the_fourteen() -> None:
    assert len(_sql.TABLE_NAMES) == 14
    assert len(set(_sql.TABLE_NAMES)) == 14


def test_one_get_table_per_known_table() -> None:
    glue = FakeGlue({name: f"s3://x/{name}/v1.json" for name in _sql.TABLE_NAMES})
    found = _sql.metadata_locations(glue, _sql.TABLE_NAMES)
    assert found == {name: f"s3://x/{name}/v1.json" for name in _sql.TABLE_NAMES}
    assert glue.calls == [("asteroidea", name) for name in _sql.TABLE_NAMES]


def test_table_without_metadata_is_a_query_error() -> None:
    glue = FakeGlue({name: "s3://x" for name in _sql.TABLE_NAMES if name != "units"})
    with pytest.raises(QueryError, match="units"):
        _sql.metadata_locations(glue, _sql.TABLE_NAMES)


def test_glue_refusal_is_a_query_error() -> None:
    class Refusing:
        def get_table(self, **_: str) -> dict[str, dict[str, object]]:
            raise ClientError({"Error": {"Code": "X", "Message": "no"}}, "GetTable")

    with pytest.raises(QueryError, match="assets"):
        _sql.metadata_locations(Refusing(), _sql.TABLE_NAMES)


def test_sql_returns_an_arrow_table(parquet: dict[str, str]) -> None:
    result = run(parquet, "SELECT count(*) AS n FROM datasets")
    assert isinstance(result, pa.Table)
    assert result.to_pylist() == [{"n": 2}]


def test_only_the_named_tables_get_views(parquet: dict[str, str]) -> None:
    counter: list[str] = []
    eng = engine(counter)
    query = "SELECT count(*) FROM datasets"
    eng.run(query, None, {"datasets": parquet["datasets"]}, None)
    assert counter == [parquet["datasets"]]
    views = eng.run(
        "SELECT view_name FROM duckdb_views() WHERE NOT internal", None, {}, None
    )
    assert views.column("view_name").to_pylist() == ["datasets"]


def test_referenced_matches_whole_identifiers_case_insensitively() -> None:
    assert _sql.referenced("select * from Assets") == {"assets"}
    assert _sql.referenced("select * from assets_old") == set()
    assert _sql.referenced("select 1 from main.units u") == {"units"}


def test_a_name_in_a_string_literal_or_quoted_identifier_still_matches() -> None:
    assert "units" in _sql.referenced("SELECT 'units' AS x")
    assert "units" in _sql.referenced('SELECT * FROM "units"')
    assert "series" in _sql.referenced("SELECT * FROM query_table('series')")
    assert _sql.tables_needed('SELECT * FROM "Units"') == ["units"]


def test_a_table_name_passed_as_a_parameter_matches() -> None:
    assert _sql.tables_needed("SELECT * FROM query_table(?)", ["units"]) == ["units"]


def test_convenience_views_pull_in_their_tables() -> None:
    assert _sql.tables_needed("select * from subject_index") == [
        "subjects",
        "subject_links",
    ]
    assert _sql.tables_needed("select * from files") == ["assets", "asset_headers"]


ALL_NAMES = (
    "SELECT view_name FROM duckdb_views() WHERE NOT internal AND 'assets datasets "
    "subjects subject_links sessions asset_headers series electrodes units "
    "intervals unit_spikes events studies study_datasets subject_index files' <> ''"
)


def test_all_views_exist_when_every_name_is_used(parquet: dict[str, str]) -> None:
    views = run(parquet, ALL_NAMES).column("view_name")
    assert set(views.to_pylist()) == {*_sql.TABLE_NAMES, "subject_index", "files"}


def test_a_bound_view_is_reused_while_its_location_is_unchanged(
    parquet: dict[str, str],
) -> None:
    counter: list[str] = []
    eng = engine(counter)
    wanted = {"datasets": parquet["datasets"]}
    for _ in range(3):
        eng.run("SELECT * FROM datasets", None, wanted, None)
    assert counter == [parquet["datasets"]]


def test_a_changed_location_rebinds_and_shows_the_new_rows(
    parquet: dict[str, str], tmp_path: Path
) -> None:
    counter: list[str] = []
    eng = engine(counter)
    first = eng.run(
        "SELECT * FROM datasets", None, {"datasets": parquet["datasets"]}, None
    )
    assert first.num_rows == 2
    newer = tmp_path / "datasets-v2.parquet"
    pq.write_table(pa.table({"dataset": ["d1", "d2", "d3"]}), newer)
    second = eng.run("SELECT * FROM datasets", None, {"datasets": str(newer)}, None)
    assert second.num_rows == 3
    assert counter == [parquet["datasets"], str(newer)]


def test_a_renewed_credential_recreates_the_secret_once(
    parquet: dict[str, str],
) -> None:
    seen: list[object] = []
    eng = _sql.Engine(
        scan=local_scan, setup=nothing, secret=lambda _con, cred: seen.append(cred)
    )
    wanted = {"datasets": parquet["datasets"]}
    for cred in ("a", "a", "b", "b", "b"):
        eng.run("SELECT * FROM datasets", None, wanted, cred)
    assert seen == ["a", "b"]


def test_setup_runs_once_per_engine() -> None:
    calls: list[int] = []
    eng = _sql.Engine(
        scan=local_scan, setup=lambda _con: calls.append(1), secret=nothing
    )
    for _ in range(3):
        eng.run("SELECT 1", None, {}, None)
    assert calls == [1]


def test_subject_index_keys(parquet: dict[str, str]) -> None:
    rows = run(
        parquet,
        "SELECT subject, subject_key FROM subject_index "
        "WHERE dataset = 'd1' ORDER BY subject",
    ).to_pylist()
    assert rows == [
        {"subject": "sub-01", "subject_key": "K1"},
        {"subject": "sub-02", "subject_key": "d1/sub-02"},
    ]


def test_files_joins_headers(parquet: dict[str, str]) -> None:
    rows = run(
        parquet, "SELECT asset_id, n_units FROM files ORDER BY asset_id"
    ).to_pylist()
    assert rows == [
        {"asset_id": "a1", "n_units": "44"},
        {"asset_id": "a2", "n_units": None},
    ]


def test_duckdb_message_is_verbatim(parquet: dict[str, str]) -> None:
    with pytest.raises(duckdb.Error) as raised:
        duckdb.connect().execute("SELEC 1")
    with pytest.raises(QueryError) as err:
        run(parquet, "SELEC 1")
    assert str(err.value) == str(raised.value)


def test_unknown_table_is_a_query_error(parquet: dict[str, str]) -> None:
    with pytest.raises(QueryError, match="s1_probe"):
        run(parquet, "SELECT * FROM s1_probe")


@pytest.fixture
def browse(parquet: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> Auth:
    def fake(_: object, query: str, params: list[str | None] | None = None) -> pa.Table:
        return run(parquet, query, params)

    monkeypatch.setattr(_sql, "sql", fake)
    return cast("Auth", object())


def test_browse_calls(browse: Auth) -> None:
    assert _browse.datasets(browse).num_rows == 2
    assert _browse.subjects(browse).num_rows == 3
    keys = _browse.subjects(browse, dataset="d1").column("subject_key").to_pylist()
    assert keys == ["K1", "d1/sub-02"]
    assert _browse.sessions(browse, subject="sub-01").num_rows == 2
    assert _browse.sessions(browse, dataset="d2", subject="sub-01").num_rows == 1
    assert _browse.assets(browse).num_rows == 2
    nwb = _browse.assets(browse, file_format="nwb")
    assert nwb.column("n_units").to_pylist() == ["44"]
    assert _browse.assets(browse, dataset="d1", bids_session="ses-1").num_rows == 1


def test_browse_value_is_bound_not_spliced(browse: Auth) -> None:
    assert _browse.datasets(browse).num_rows == 2
    assert _browse.subjects(browse, dataset="d1' OR 'a'='a").num_rows == 0
