"""Catalog depth routes (research-frontend C3) and the shared Athena runner."""

import json
from typing import Any, override

import pytest
from botocore.exceptions import ClientError

from asteroidea_ingest import catalog_api

from .conftest import jwt_event
from .test_catalog_api import FakeAthena, Rows

DATASET_ROUTE = "GET /catalog/datasets/{dataset}"
TABLE_ROUTE = "GET /catalog/assets/{asset_id}/tables/{table}"
EVENTS_ROUTE = "GET /catalog/assets/{asset_id}/events"
ASSET_COLS = [("asset_id", "varchar"), ("dataset", "varchar"), ("s3_key", "varchar")]


def _get(
    fake: FakeAthena,
    route: str,
    *,
    path: dict[str, str] | None = None,
    query: dict[str, str] | None = None,
    groups: str = "[researchers]",
) -> tuple[int, dict[str, Any]]:
    event = {
        **jwt_event(route, groups=groups),
        "pathParameters": path,
        "queryStringParameters": query,
    }
    resp = fake.api().handle(event)
    return resp["statusCode"], json.loads(resp["body"])


def _client_error(operation: str) -> ClientError:
    return ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "slow down"}}, operation
    )


class PollErrorAthena(FakeAthena):
    @override
    def get_query_execution(self, **kwargs: Any) -> dict[str, Any]:
        self.events.append(f"poll {kwargs['QueryExecutionId']}")
        error = _client_error("GetQueryExecution")
        raise error


class ResultsErrorAthena(FakeAthena):
    @override
    def get_query_results(self, **kwargs: Any) -> dict[str, Any]:
        self.events.append(f"results {kwargs['QueryExecutionId']}")
        error = _client_error("GetQueryResults")
        raise error


def test_get_query_execution_client_error_is_502_and_stops_the_query() -> None:
    fake = PollErrorAthena()
    status, body = _get(fake, "GET /catalog/datasets")
    assert (status, body) == (
        502,
        {"error": "upstream_failed", "detail": "the catalog query failed: slow down"},
    )
    assert fake.stopped == ["q1"]


def test_get_query_results_client_error_is_502() -> None:
    fake = ResultsErrorAthena()
    status, body = _get(fake, "GET /catalog/datasets")
    assert (status, body) == (
        502,
        {"error": "upstream_failed", "detail": "the catalog query failed: slow down"},
    )


def test_dataset_body_and_fixed_queries() -> None:
    fake = FakeAthena(
        (
            [("dataset", "varchar"), ("title", "varchar"), ("asset_count", "bigint")],
            [["ds1", "A title", "3"]],
        ),
        ([("subject", "varchar"), ("species", "varchar")], [["sub-CS41", "human"]]),
        (
            [("subject", "varchar"), ("bids_session", "varchar")],
            [["sub-CS41", "ses-1"], ["sub-CS62", "ses-1"]],
        ),
        (
            [("format", "varchar"), ("count", "bigint"), ("bytes", "bigint")],
            [["nwb", "2", "900"]],
        ),
    )
    status, body = _get(fake, DATASET_ROUTE, path={"dataset": "ds1"})
    assert status == 200
    assert body["dataset"] == "ds1"
    assert body["facts"] == {"dataset": "ds1", "title": "A title", "asset_count": 3}
    assert body["subjects"] == [{"subject": "sub-CS41", "species": "human"}]
    assert len(body["sessions"]) == 2
    assert body["formats"] == [{"format": "nwb", "count": 2, "bytes": 900}]
    assert "query_ms" in body
    assert [s["QueryString"] for s in fake.started] == [
        catalog_api.DATASET_FACTS_SQL,
        catalog_api.DATASET_SUBJECTS_SQL,
        catalog_api.DATASET_SESSIONS_SQL,
        catalog_api.DATASET_FORMATS_SQL,
    ]
    assert all(s["ExecutionParameters"] == ["'ds1'"] for s in fake.started)


def test_dataset_404_when_unknown() -> None:
    cols = [("dataset", "varchar")]
    fake = FakeAthena((cols, []), (cols, []), (cols, []), (cols, []))
    status, body = _get(fake, DATASET_ROUTE, path={"dataset": "nope"})
    assert (status, body) == (404, {"error": "not_found", "detail": "no such dataset"})


def test_dataset_400_regex_starts_no_query() -> None:
    fake = FakeAthena()
    assert _get(fake, DATASET_ROUTE, path={"dataset": "Bad_Name"})[0] == 400
    assert fake.started == []


@pytest.mark.parametrize("route", [DATASET_ROUTE, TABLE_ROUTE, EVENTS_ROUTE])
def test_depth_routes_refuse_outsiders_before_any_query(route: str) -> None:
    fake = FakeAthena()
    status, _ = _get(
        fake,
        route,
        path={"dataset": "ds1", "asset_id": "a", "table": "units"},
        groups="[other]",
    )
    assert status == 403
    assert fake.started == []


@pytest.mark.parametrize("table", sorted(catalog_api.TABLE_SQL))
def test_table_route_fixed_text_per_table_and_paging(table: str) -> None:
    cols = [("asset_id", "varchar"), ("n", "bigint")]
    rows: Rows = [["a1", str(i)] for i in range(201)]
    fake = FakeAthena((cols, rows))
    status, body = _get(
        fake, TABLE_ROUTE, path={"asset_id": "a1", "table": table}, query={"page": "2"}
    )
    assert status == 200
    assert (body["page"], body["more"], len(body["rows"])) == (2, True, 200)
    sent = fake.started[0]
    assert sent["QueryString"] == catalog_api.TABLE_SQL[table]
    assert sent["QueryString"].endswith("OFFSET ? LIMIT 201")
    assert sent["ExecutionParameters"] == ["'a1'", "400"]
    assert f"FROM {table} " in sent["QueryString"]


@pytest.mark.parametrize("table", ["assets", "subjects", "unit_spikes", "x; drop", ""])
def test_table_route_400_outside_the_allowlist(table: str) -> None:
    fake = FakeAthena()
    status, body = _get(fake, TABLE_ROUTE, path={"asset_id": "a1", "table": table})
    assert status == 400
    assert "series, electrodes, units, intervals" in body["detail"]
    assert fake.started == []


def test_table_route_400_bad_page() -> None:
    fake = FakeAthena()
    status, _ = _get(
        fake,
        TABLE_ROUTE,
        path={"asset_id": "a1", "table": "units"},
        query={"page": "1001"},
    )
    assert status == 400
    assert fake.started == []


def test_events_route_passes_the_entity_key_as_a_parameter() -> None:
    key = "raw/ds4/sub-p62cs/ses-1/func/sub-p62cs_ses-1_task-movie_run-1_bold.nii.gz"
    fake = FakeAthena(
        (ASSET_COLS, [["a1", "ds4", key]]),
        ([("table_name", "varchar"), ("start_time", "double")], [["trials", "1.5"]]),
        ([("onset_s", "double"), ("trial_type", "varchar")], [["0.5", "x"]]),
    )
    status, body = _get(fake, EVENTS_ROUTE, path={"asset_id": "a1"})
    assert status == 200
    assert body["intervals"] == [{"table_name": "trials", "start_time": 1.5}]
    assert body["events"] == [{"onset_s": 0.5, "trial_type": "x"}]
    assert (body["intervals_more"], body["events_more"]) == (False, False)
    asset, intervals, events = fake.started
    assert asset["QueryString"] == catalog_api.ASSET_SQL
    assert intervals["QueryString"] == catalog_api.INTERVALS_SQL
    assert intervals["ExecutionParameters"] == ["'a1'"]
    assert events["QueryString"] == catalog_api.EVENTS_SQL
    assert events["ExecutionParameters"] == [
        "'ds4'",
        "'sub-p62cs/ses-1/func/sub-p62cs_ses-1_task-movie_run-1'",
    ]
    assert "LIMIT 20001" in catalog_api.EVENTS_SQL
    assert catalog_api.EVENT_ROWS == 20000


def test_events_route_flags_rows_over_the_cap() -> None:
    key = "raw/ds1/sub-1/f.nwb"
    over: Rows = [["a1", "0.0"]] * 20001
    fake = FakeAthena(
        (ASSET_COLS, [["a1", "ds1", key]]),
        ([("asset_id", "varchar"), ("start_time", "double")], over),
        ([("asset_id", "varchar"), ("onset_s", "double")], []),
    )
    _, body = _get(fake, EVENTS_ROUTE, path={"asset_id": "a1"})
    assert len(body["intervals"]) == 20000
    assert (body["intervals_more"], body["events_more"]) == (True, False)


def test_events_route_404_unknown_asset_runs_one_query() -> None:
    fake = FakeAthena((ASSET_COLS, []))
    status, body = _get(fake, EVENTS_ROUTE, path={"asset_id": "zz"})
    assert (status, body) == (404, {"error": "not_found", "detail": "no such asset"})
    assert len(fake.started) == 1


def test_dataset_subjects_sql_agrees_with_the_asset_subject_count() -> None:
    """A subject named only by files appears; one with a row keeps its fields."""
    duckdb = pytest.importorskip("duckdb")
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE subjects "
        "(dataset VARCHAR, subject VARCHAR, species VARCHAR, sex VARCHAR, age VARCHAR)"
    )
    con.execute("CREATE TABLE assets (dataset VARCHAR, subject VARCHAR)")
    con.execute(
        "INSERT INTO subjects VALUES ('ds1', 'sub-A', 'human', 'F', 'P40Y'), "
        "('other', 'sub-Z', NULL, NULL, NULL)"
    )
    con.execute(
        "INSERT INTO assets VALUES ('ds1', 'sub-A'), ('ds1', 'sub-A'), "
        "('ds1', 'sub-W1'), ('ds1', NULL), ('other', 'sub-Q')"
    )
    rows = con.execute(catalog_api.DATASET_SUBJECTS_SQL, ["ds1"]).fetchall()
    assert rows == [("sub-A", "human", "F", "P40Y"), ("sub-W1", None, None, None)]
    counted = con.execute(
        "SELECT count(DISTINCT subject) FROM assets WHERE dataset = ?", ["ds1"]
    ).fetchone()
    assert counted == (len(rows),)


def test_dataset_facts_sql_counts_only_its_own_dataset() -> None:
    """Counts, subjects and bytes come from this dataset's files only."""
    duckdb = pytest.importorskip("duckdb")
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE datasets (dataset VARCHAR, title VARCHAR, license VARCHAR, "
        "doi VARCHAR, source_archive VARCHAR, source_dataset VARCHAR, "
        "source_version VARCHAR)"
    )
    con.execute(
        "CREATE TABLE assets (dataset VARCHAR, subject VARCHAR, size_bytes BIGINT)"
    )
    con.execute(
        "INSERT INTO datasets VALUES "
        "('ds1', 'T1', 'CC0', NULL, 'dandi', '000001', '1'), "
        "('ds2', 'T2', 'CC0', NULL, 'dandi', '000002', '1'), "
        "('ds3', 'T3', 'CC0', NULL, 'dandi', '000003', '1')"
    )
    con.execute(
        "INSERT INTO assets VALUES ('ds1', 'sub-A', 10), ('ds1', 'sub-A', 20), "
        "('ds1', 'sub-B', 30), ('ds1', NULL, 40), ('ds2', 'sub-Z', 999)"
    )
    cur = con.execute(catalog_api.DATASET_FACTS_SQL, ["ds1"])
    cols = [c[0] for c in cur.description]
    rows = cur.fetchall()
    assert len(rows) == 1
    facts = dict(zip(cols, rows[0], strict=True))
    assert facts["title"] == "T1"
    counts = (facts["asset_count"], facts["subject_count"], facts["total_bytes"])
    assert counts == (4, 2, 100)
    empty = con.execute(catalog_api.DATASET_FACTS_SQL, ["ds3"]).fetchall()
    assert [r[-3:] for r in empty] == [(0, 0, 0)]
    assert con.execute(catalog_api.DATASET_FACTS_SQL, ["nope"]).fetchall() == []
