"""Catalog read routes over a fake Athena."""

import json
from dataclasses import replace
from typing import Any
from urllib.parse import parse_qs, urlsplit

import boto3
import pytest
from botocore.config import Config
from botocore.exceptions import ClientError

from asteroidea_ingest import catalog_api
from asteroidea_ingest.athena_query import render_param
from asteroidea_ingest.catalog_api import CatalogApi, assets_query
from asteroidea_ingest.uploads_api import group_refusal

from .conftest import jwt_event

ASSET_COLS = [
    ("asset_id", "varchar"),
    ("s3_key", "varchar"),
    ("subject", "varchar"),
    ("bids_session", "varchar"),
    ("datatype", "varchar"),
    ("format", "varchar"),
    ("size_bytes", "bigint"),
    ("committed_at", "timestamp"),
]
Rows = list[list[str | None]]
ATHENA_PARAM_MAX = 1024


def _asset_row(i: int, subject: str | None = "sub-1") -> list[str | None]:
    return [
        f"id{i}",
        f"raw/ds1/sub-1/f{i:04d}.nwb",
        subject,
        None,
        "ecephys",
        "nwb",
        str(100 + i),
        "2026-01-01 00:00:00.000",
    ]


class FakeAthena:
    """Scripted Athena: one result set per started query, states per poll."""

    def __init__(self, *results: tuple[list[tuple[str, str]], Rows]) -> None:
        self.results = list(results)
        self.started: list[dict[str, Any]] = []
        self.stopped: list[str] = []
        # Each get_query_execution pops one; the last one repeats.
        self.states: list[dict[str, str]] = [{"State": "SUCCEEDED"}]
        self.start_error: ClientError | None = None
        self.clock = 0.0
        self.events: list[str] = []

    def sleep(self, seconds: float) -> None:
        self.clock += seconds

    def start_query_execution(self, **kwargs: Any) -> dict[str, Any]:
        if self.start_error is not None:
            raise self.start_error
        # Athena refuses an empty parameter list and any value over 1024 characters.
        params = kwargs.get("ExecutionParameters")
        if params is not None and (
            not params or any(not 1 <= len(p) <= ATHENA_PARAM_MAX for p in params)
        ):
            raise ClientError(
                {"Error": {"Code": "InvalidRequestException", "Message": "param"}},
                "StartQueryExecution",
            )
        self.started.append(kwargs)
        self.events.append("start")
        return {"QueryExecutionId": f"q{len(self.started)}"}

    def get_query_execution(self, **kwargs: Any) -> dict[str, Any]:
        self.events.append(f"poll {kwargs['QueryExecutionId']}")
        status = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        return {"QueryExecution": {"Status": status}}

    def get_query_results(self, **kwargs: Any) -> dict[str, Any]:
        cols, rows = self.results[int(kwargs["QueryExecutionId"][1:]) - 1]
        header = [{"VarCharValue": n} for n, _ in cols]
        body = [
            {"Data": [{} if v is None else {"VarCharValue": v} for v in r]}
            for r in rows
        ]
        return {
            "ResultSet": {
                "Rows": [{"Data": header}, *body],
                "ResultSetMetadata": {
                    "ColumnInfo": [{"Name": n, "Type": t} for n, t in cols]
                },
            }
        }

    def stop_query_execution(self, **kwargs: Any) -> dict[str, Any]:
        self.stopped.append(kwargs["QueryExecutionId"])
        return {}

    def api(self) -> CatalogApi:
        return CatalogApi(
            athena=self,
            workgroup="asteroidea",
            database="asteroidea",
            clock=lambda: self.clock,
            sleep=self.sleep,
        )


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


ASSETS_ROUTE = "GET /catalog/datasets/{dataset}/assets"
DS = {"dataset": "ds1"}


def test_datasets_body_types_nulls_and_query_ms() -> None:
    cols = [
        ("dataset", "varchar"),
        ("title", "varchar"),
        ("asset_count", "bigint"),
        ("subject_count", "bigint"),
        ("total_bytes", "bigint"),
        ("created_by", "varchar"),
    ]
    fake = FakeAthena((cols, [["ds1", None, "3", "2", "300", "user-2"]]))
    fake.states = [{"State": "RUNNING"}, {"State": "SUCCEEDED"}]
    status, body = _get(fake, "GET /catalog/datasets")
    assert status == 200
    assert body["datasets"] == [
        {
            "dataset": "ds1",
            "title": None,
            "asset_count": 3,
            "subject_count": 2,
            "total_bytes": 300,
            "mine": False,
        }
    ]
    assert body["query_ms"] == 200  # one 200 ms poll on the fake clock
    assert fake.started[0]["QueryString"] == catalog_api.DATASETS_SQL
    assert "ExecutionParameters" not in fake.started[0]
    assert fake.started[0]["WorkGroup"] == "asteroidea"
    assert fake.started[0]["QueryExecutionContext"] == {"Database": "asteroidea"}


def test_group_refusal_403_before_any_query() -> None:
    fake = FakeAthena()
    status, body = _get(fake, "GET /catalog/datasets", groups="[other]")
    assert status == 403
    assert body == {
        "error": "forbidden",
        "detail": "caller is not in the researchers group",
    }
    assert fake.started == []


def test_unknown_route_404() -> None:
    assert _get(FakeAthena(), "GET /catalog/nope")[0] == 404


def test_assets_body_path_and_more_from_the_201st_row() -> None:
    rows = [_asset_row(i) for i in range(201)]
    fake = FakeAthena((ASSET_COLS, rows))
    status, body = _get(fake, ASSETS_ROUTE, path=DS, query={"page": "2"})
    assert status == 200
    assert (body["page"], body["more"], len(body["assets"])) == (2, True, 200)
    first = body["assets"][0]
    assert first == {
        "asset_id": "id0",
        "path": "sub-1/f0000.nwb",
        "subject": "sub-1",
        "bids_session": None,
        "datatype": "ecephys",
        "format": "nwb",
        "size_bytes": 100,
        "committed_at": "2026-01-01 00:00:00.000",
    }
    assert "s3_key" not in first
    assert fake.started[0]["ExecutionParameters"] == ["'ds1'", "400"]


def test_assets_exactly_200_rows_is_not_more() -> None:
    fake = FakeAthena((ASSET_COLS, [_asset_row(i) for i in range(200)]))
    _, body = _get(fake, ASSETS_ROUTE, path=DS)
    assert (body["more"], len(body["assets"]), body["page"]) == (False, 200, 0)


TAIL = " ORDER BY s3_key OFFSET ? LIMIT 201"
HEAD = (
    "SELECT asset_id, s3_key, subject, bids_session, datatype, format, size_bytes, "
    "committed_at FROM assets WHERE dataset = ?"
)


SUBJECT_SQL = " AND subject = ?"
SESSION_SQL = " AND bids_session = ?"
SESSION_NULL_SQL = " AND bids_session IS NULL"
FORMAT_SQL = " AND format = ?"
_TEXT_CASES = [
    (sub, ses, fmt)
    for sub in (False, True)
    for ses in (None, "_", "ses-1")
    for fmt in (False, True)
]


@pytest.mark.parametrize(("with_sub", "ses", "with_fmt"), _TEXT_CASES)
def test_assets_twelve_query_texts_and_parameters(
    *, with_sub: bool, ses: str | None, with_fmt: bool
) -> None:
    query: dict[str, str] = {}
    sql: str = HEAD
    params: list[str | int] = ["ds1"]
    if with_sub:
        query["subject"] = "sub-1"
        sql, params = sql + SUBJECT_SQL, [*params, "sub-1"]
    if ses is not None:
        query["session"] = ses
    if ses == "_":
        sql += SESSION_NULL_SQL
    elif ses is not None:
        sql, params = sql + SESSION_SQL, [*params, ses]
    if with_fmt:
        query["format"] = "nwb"
        sql, params = sql + FORMAT_SQL, [*params, "nwb"]
    sql, params = sql + TAIL, [*params, 0]
    fake = FakeAthena((ASSET_COLS, []))
    assert _get(fake, ASSETS_ROUTE, path=DS, query=query)[0] == 200
    sent = fake.started[0]
    assert sent["QueryString"] == sql
    assert sent["ExecutionParameters"] == [render_param(p) for p in params]
    assert "_" not in params  # session _ binds no parameter
    assert all(isinstance(p, str | int) for p in params)  # no NULL is ever bound
    assert sent["QueryString"].index("OFFSET") < sent["QueryString"].index("LIMIT")
    # Values are bound, never spliced into the text.
    assert not any(p in sql for p in params if isinstance(p, str))


def test_assets_twelve_texts_are_distinct() -> None:
    texts = {
        assets_query("ds1", "s" if sub else None, "f" if fmt else None, 0, ses)[0]
        for sub, ses, fmt in _TEXT_CASES
    }
    assert len(texts) == 12


def test_assets_pagination_and_empty_filters() -> None:
    fake = FakeAthena((ASSET_COLS, []), (ASSET_COLS, []))
    q = {"subject": "sub-1", "format": "nwb", "page": "3"}
    assert _get(fake, ASSETS_ROUTE, path=DS, query=q)[0] == 200
    assert fake.started[0]["ExecutionParameters"] == [
        "'ds1'",
        "'sub-1'",
        "'nwb'",
        "600",
    ]
    # An empty filter is an absent filter.
    empty = {"subject": "", "session": "", "format": ""}
    assert _get(fake, ASSETS_ROUTE, path=DS, query=empty)[0] == 200
    assert fake.started[1]["QueryString"] == HEAD + TAIL


@pytest.mark.parametrize("session", ["ses 1", "ses'1", "-x", ".x", "a/b", "__"])
def test_assets_malformed_session_is_refused(session: str) -> None:
    fake = FakeAthena()
    status, body = _get(fake, ASSETS_ROUTE, path=DS, query={"session": session})
    assert status == 400
    assert body["error"] == "invalid_session"
    assert fake.started == []


@pytest.mark.parametrize(
    ("path", "query"),
    [
        ({"dataset": "Bad_Name"}, {}),
        ({"dataset": "ds1"}, {"page": "1001"}),
        ({"dataset": "ds1"}, {"page": "-1"}),
        ({"dataset": "ds1"}, {"page": "x"}),
        ({"dataset": "ds1"}, {"subject": "s" * 65}),
        ({"dataset": "ds1"}, {"format": "f" * 65}),
    ],
)
def test_assets_400_refusals_start_no_query(
    path: dict[str, str], query: dict[str, str]
) -> None:
    fake = FakeAthena()
    status, body = _get(fake, ASSETS_ROUTE, path=path, query=query)
    assert status == 400
    assert body["error"] == "invalid_request"
    assert fake.started == []


def test_page_limits_are_accepted() -> None:
    fake = FakeAthena((ASSET_COLS, []), (ASSET_COLS, []))
    assert _get(fake, ASSETS_ROUTE, path=DS, query={"page": "1000"})[0] == 200
    assert fake.started[0]["ExecutionParameters"] == ["'ds1'", "200000"]


def test_asset_detail_body() -> None:
    asset_cols = [("asset_id", "varchar"), ("size_bytes", "bigint")]
    head_cols = [
        ("asset_id", "varchar"),
        ("n_units", "bigint"),
        ("tr_seconds", "double"),
    ]
    fake = FakeAthena((asset_cols, [["a1", "7"]]), (head_cols, [["a1", "12", "2.5"]]))
    status, body = _get(fake, "GET /catalog/assets/{asset_id}", path={"asset_id": "a1"})
    assert status == 200
    assert body["asset"] == {"asset_id": "a1", "size_bytes": 7}
    assert body["headers"] == [{"asset_id": "a1", "n_units": 12, "tr_seconds": 2.5}]
    assert [s["QueryString"] for s in fake.started] == [
        catalog_api.ASSET_SQL,
        catalog_api.HEADERS_SQL,
    ]
    assert all(s["ExecutionParameters"] == ["'a1'"] for s in fake.started)


def test_asset_detail_404_when_no_row() -> None:
    cols = [("asset_id", "varchar")]
    fake = FakeAthena((cols, []), (cols, []))
    status, body = _get(fake, "GET /catalog/assets/{asset_id}", path={"asset_id": "x"})
    assert (status, body) == (404, {"error": "not_found", "detail": "no such asset"})


def test_failed_reason_passes_through_verbatim() -> None:
    fake = FakeAthena()
    reason = "SYNTAX_ERROR: line 1:179: mismatched input 'OFFSET'."
    fake.states = [{"State": "FAILED", "StateChangeReason": reason}]
    status, body = _get(fake, "GET /catalog/datasets")
    assert status == 502
    assert body == {
        "error": "upstream_failed",
        "detail": f"the catalog query failed: {reason}",
    }
    assert fake.stopped == []


def test_start_error_surfaces_the_driver_message() -> None:
    fake = FakeAthena()
    fake.start_error = ClientError(
        {"Error": {"Code": "InvalidRequestException", "Message": "bad workgroup"}},
        "StartQueryExecution",
    )
    status, body = _get(fake, "GET /catalog/datasets")
    assert (status, body) == (
        502,
        {
            "error": "upstream_failed",
            "detail": "the catalog query failed: bad workgroup",
        },
    )


def test_poll_budget_exhaustion_stops_the_query_and_503s() -> None:
    fake = FakeAthena()
    fake.states = [{"State": "RUNNING"}]
    status, body = _get(fake, "GET /catalog/datasets")
    assert (status, body) == (503, {"error": "slow", "detail": catalog_api.SLOW})
    assert fake.stopped == ["q1"]
    assert fake.clock >= catalog_api.BUDGET_SECONDS  # polled at 200 ms to the budget


def test_detail_starts_both_queries_before_polling() -> None:
    fake = FakeAthena(
        ([("asset_id", "varchar")], [["a1"]]), ([("asset_id", "varchar")], [])
    )
    _get(fake, "GET /catalog/assets/{asset_id}", path={"asset_id": "a1"})
    assert fake.events[:2] == ["start", "start"]
    assert fake.events[2].startswith("poll")


def test_detail_budget_exhaustion_stops_both_queries() -> None:
    fake = FakeAthena()
    fake.states = [{"State": "RUNNING"}]
    status, body = _get(fake, "GET /catalog/assets/{asset_id}", path={"asset_id": "a1"})
    assert (status, body) == (503, {"error": "slow", "detail": catalog_api.SLOW})
    assert fake.stopped == ["q1", "q2"]


def test_detail_one_failed_is_502_with_its_reason() -> None:
    fake = FakeAthena(([("asset_id", "varchar")], [["a1"]]))
    fake.states = [
        {"State": "SUCCEEDED"},
        {"State": "FAILED", "StateChangeReason": "boom"},
    ]
    status, body = _get(fake, "GET /catalog/assets/{asset_id}", path={"asset_id": "a1"})
    assert (status, body) == (
        502,
        {"error": "upstream_failed", "detail": "the catalog query failed: boom"},
    )


DOWNLOAD_ROUTE = "GET /catalog/assets/{asset_id}/download"
DOWNLOAD_COLS = [
    ("asset_id", "varchar"),
    ("s3_key", "varchar"),
    ("s3_version_id", "varchar"),
    ("format", "varchar"),
    ("size_bytes", "bigint"),
]


def _download(
    fake: FakeAthena, groups: str = "[researchers]"
) -> tuple[int, dict[str, Any]]:
    """Call the download route with a real (offline, fake-credential) S3 presigner."""
    s3 = boto3.client(
        "s3",
        region_name="us-east-1",
        aws_access_key_id="AKIDEXAMPLE",
        aws_secret_access_key="secret",
        config=Config(signature_version="s3v4"),
    )
    api = replace(fake.api(), s3=s3, raw_bucket="raw-bkt")
    event = {
        **jwt_event(DOWNLOAD_ROUTE, groups=groups),
        "pathParameters": {"asset_id": "id1"},
        "queryStringParameters": None,
    }
    resp = api.handle(event)
    return resp["statusCode"], json.loads(resp["body"])


def test_download_presigns_the_catalogued_version() -> None:
    row: list[str | None] = [
        "id1",
        "raw/ds1/sub-1/f1.nii.gz",
        "v-abc123",
        "nifti",
        "4096",
    ]
    status, body = _download(FakeAthena((DOWNLOAD_COLS, [row])))
    assert status == 200
    assert body["expires_in"] == 300
    assert body["size_bytes"] == 4096
    assert body["s3_version_id"] == "v-abc123"
    assert body["format"] == "nifti"
    url = urlsplit(body["url"])
    assert str(url.hostname).startswith("raw-bkt.s3")
    assert url.path == "/raw/ds1/sub-1/f1.nii.gz"
    query = parse_qs(url.query)
    assert query["versionId"] == ["v-abc123"]
    assert query["X-Amz-Expires"] == ["300"]


ROW_V1: list[str | None] = ["id1", "raw/ds1/f1.nii", "v-1", "nifti", "10"]
ROW_V2: list[str | None] = ["id1", "raw/ds1/f1.nii", "v-2", "nifti", "10"]


def test_a_seen_asset_is_presigned_without_a_second_athena_query() -> None:
    fake = FakeAthena((DOWNLOAD_COLS, [ROW_V1]))
    first = _download(fake)
    second = _download(fake)
    assert first[0] == second[0] == 200
    assert len(fake.started) == 1
    assert second[1]["s3_version_id"] == "v-1"
    assert second[1]["size_bytes"] == 10
    assert second[1]["format"] == "nifti"
    assert parse_qs(urlsplit(second[1]["url"]).query)["versionId"] == ["v-1"]


def test_loading_the_asset_page_seeds_the_download_cache() -> None:
    """The asset page read seeds the cache; the presign then skips Athena."""
    head_cols = [("asset_id", "varchar")]
    fake = FakeAthena((DOWNLOAD_COLS, [ROW_V1]), (head_cols, []))
    status, _ = _get(fake, "GET /catalog/assets/{asset_id}", path={"asset_id": "id1"})
    assert status == 200
    page_queries = len(fake.started)
    status, body = _download(fake)
    assert status == 200
    assert body["s3_version_id"] == "v-1"
    assert len(fake.started) == page_queries  # zero Athena calls for the presign


def test_the_cache_expires_after_its_ttl_and_the_row_is_read_again() -> None:
    fake = FakeAthena((DOWNLOAD_COLS, [ROW_V1]), (DOWNLOAD_COLS, [ROW_V2]))
    assert _download(fake)[1]["s3_version_id"] == "v-1"
    fake.clock += catalog_api.DOWNLOAD_CACHE_TTL_S - 1
    assert _download(fake)[1]["s3_version_id"] == "v-1"
    assert len(fake.started) == 1
    fake.clock += 1
    assert _download(fake)[1]["s3_version_id"] == "v-2"
    assert len(fake.started) == 2


def test_a_404_is_not_cached() -> None:
    fake = FakeAthena((DOWNLOAD_COLS, []), (DOWNLOAD_COLS, [ROW_V1]))
    assert _download(fake)[0] == 404
    status, body = _download(fake)
    assert (status, body["s3_version_id"]) == (200, "v-1")
    assert len(fake.started) == 2


def test_the_cache_is_bounded_and_drops_the_least_recently_used(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(catalog_api, "DOWNLOAD_CACHE_MAX", 2)
    for name in ("a", "b"):
        catalog_api.cache_put(name, {"s3_version_id": name}, 0.0)
    assert catalog_api.cache_get("a", 1.0) is not None
    catalog_api.cache_put("c", {"s3_version_id": "c"}, 2.0)
    assert catalog_api.cache_get("b", 3.0) is None
    assert catalog_api.cache_get("a", 3.0) is not None
    assert catalog_api.cache_get("c", 3.0) is not None


def test_download_unknown_asset_is_404() -> None:
    status, body = _download(FakeAthena((DOWNLOAD_COLS, [])))
    assert status == 404
    assert body == {"error": "not_found", "detail": "no such asset"}


def test_download_outside_the_researchers_group_is_403() -> None:
    status, _ = _download(FakeAthena((DOWNLOAD_COLS, [])), groups="[]")
    assert status == 403


@pytest.mark.parametrize("value", ["0", "62", "007", "1e5", "o'neil"])
def test_a_numeric_looking_varchar_value_is_sent_as_a_quoted_literal(
    value: str,
) -> None:
    """Athena types a bare `62` as an integer; each varchar `?` must stay a varchar."""
    wire = "'" + value.replace("'", "''") + "'"
    fake = FakeAthena((ASSET_COLS, []))
    query = {"subject": value, "format": value, "page": "2"}
    assert _get(fake, ASSETS_ROUTE, path=DS, query=query)[0] == 200
    assert fake.started[0]["ExecutionParameters"] == ["'ds1'", wire, wire, "400"]
    fake = FakeAthena(([("asset_id", "varchar")], []), ([("asset_id", "varchar")], []))
    _get(fake, "GET /catalog/assets/{asset_id}", path={"asset_id": value})
    assert all(s["ExecutionParameters"] == [wire] for s in fake.started)


def test_render_param_quotes_a_str_and_leaves_an_int_bare() -> None:
    assert render_param("62") == "'62'"
    assert render_param("it's") == "'it''s'"
    assert render_param(400) == "400"


@pytest.mark.parametrize(
    "claim",
    ["[researchers curators]", "[curators]", "curators", ["curators"]],
)
def test_group_refusal_none_for_a_curator(claim: Any) -> None:
    assert group_refusal({"cognito:groups": claim}, "curators") is None


@pytest.mark.parametrize("claim", ["[researchers]", "[curators-extra]", ""])
def test_group_refusal_403_outside_curators(claim: Any) -> None:
    refusal = group_refusal({"cognito:groups": claim}, "curators")
    assert refusal is not None
    assert refusal["statusCode"] == 403
    assert json.loads(refusal["body"]) == {
        "error": "forbidden",
        "detail": "caller is not in the curators group",
    }


def test_group_refusal_default_group_is_researchers() -> None:
    assert group_refusal({"cognito:groups": "[researchers curators]"}) is None
    assert group_refusal({"cognito:groups": "[curators]"}) is not None
