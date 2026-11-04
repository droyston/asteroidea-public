"""`make rf-timing`: offline parts.

The calls and the CloudWatch reads are LIVE and run by hand; here the log parsing, the
aggregation, the table, the group discovery and the Insights polling run against fakes.
"""

import json
from typing import Any, Final

import pytest

from asteroidea_infra import route_timing
from asteroidea_infra.steps import StepFailureError

ASSET: Final = "a1"
BASE: Final = "/catalog/datasets/openneuro-ds004473"


def line(route: str, *, cold: bool, **phases: int) -> dict[str, Any]:
    return {"timing": route, "phases": phases, "cold": cold, "asset_id": ASSET}


def row(payload: dict[str, Any], prefix: str = "") -> list[dict[str, str]]:
    return [
        {"field": "@timestamp", "value": "2026-10-26 04:00:00.000"},
        {"field": "@message", "value": prefix + json.dumps(payload)},
    ]


DOWNLOAD = [
    line("catalog_download", cold=True, athena_ms=4000, presign_ms=300, total_ms=4300),
    line("catalog_download", cold=False, athena_ms=1000, presign_ms=5, total_ms=1005),
    line("catalog_download", cold=False, athena_ms=3000, presign_ms=7, total_ms=3007),
    line("catalog_download", cold=False, athena_ms=2000, presign_ms=6, total_ms=2006),
]
LABELS = [
    line("labels_create", cold=False, provenance_ms=3000, ddb_ms=40, total_ms=3040),
    line("labels_create", cold=False, provenance_ms=2000, ddb_ms=20, total_ms=2020),
]


def test_parse_rows_keeps_timing_lines_in_order_and_skips_the_rest() -> None:
    rows = [
        row(DOWNLOAD[0]),
        [{"field": "@message", "value": "START RequestId: x"}],
        [{"field": "@message", "value": '{"other": 1}'}],
        row(DOWNLOAD[1], prefix="2026-10-26T04:00:01Z\tabc\tINFO\t"),
        [{"field": "@timestamp", "value": "t"}],
    ]
    assert route_timing.parse_rows(rows) == DOWNLOAD[:2]


def test_aggregate_takes_call_one_apart_and_the_median_and_max_of_the_rest() -> None:
    routes = route_timing.aggregate([*DOWNLOAD, *LABELS])
    got = routes["catalog_download"]
    assert got["n"] == 4
    assert got["first"]["cold"] is True
    assert got["rest"]["athena_ms"] == (2000.0, 3000.0)
    assert got["rest"]["total_ms"] == (2006.0, 3007.0)
    assert routes["labels_create"]["rest"]["provenance_ms"] == (2000.0, 2000.0)


def test_aggregate_with_one_line_has_no_rest_and_a_missing_route_is_absent() -> None:
    routes = route_timing.aggregate(DOWNLOAD[:1])
    assert routes["catalog_download"]["rest"]["athena_ms"] is None
    assert "labels_create" not in routes


def test_table_prints_call_one_p50_and_max_per_phase() -> None:
    routes = route_timing.aggregate([*DOWNLOAD, *LABELS])
    calls = [route_timing.Call("catalog_download", w) for w in (4.5, 1.2, 3.2, 2.2)]
    text = "\n".join(route_timing.table(routes, calls))
    assert "catalog_download" in text
    assert "call 1 cold=true" in text
    assert "client wall: call 1 4.50 s; rest p50 2.20 s, max 3.20 s" in text
    athena = next(x for x in text.splitlines() if x.strip().startswith("athena_ms"))
    assert athena.split() == ["athena_ms", "4000", "2000", "3000"]
    assert "labels_create" in text
    assert "call 1 cold=false" in text


def test_table_names_a_route_with_no_lines() -> None:
    text = "\n".join(route_timing.table(route_timing.aggregate(DOWNLOAD)))
    assert "no log lines found" in text
    assert "log lines: 0" in text


class FakeLogs:
    def __init__(self, groups: list[str], answers: list[dict[str, Any]]) -> None:
        self.groups = groups
        self.answers = answers
        self.queries: list[dict[str, Any]] = []

    def describe_log_groups(self, **kwargs: Any) -> dict[str, Any]:
        assert kwargs["logGroupNamePrefix"] == "/aws/lambda/"
        return {"logGroups": [{"logGroupName": g} for g in self.groups]}

    def start_query(self, **kwargs: Any) -> dict[str, Any]:
        self.queries.append(kwargs)
        return {"queryId": "q1"}

    def get_query_results(self, **kwargs: Any) -> dict[str, Any]:
        assert kwargs["queryId"] == "q1"
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]


def test_log_groups_finds_the_two_functions_by_construct_id() -> None:
    logs = FakeLogs(
        [
            "/aws/lambda/asteroidea-ServicesStack-Api-CatalogFunctionAB12-xyz",
            "/aws/lambda/asteroidea-ServicesStack-Api-LabelsFunctionCD34-xyz",
            "/aws/lambda/asteroidea-ServicesStack-Api-ImportsFunction-xyz",
        ],
        [],
    )
    got = route_timing.log_groups(logs)
    assert got["catalog_download"].endswith("CatalogFunctionAB12-xyz")
    assert got["labels_create"].endswith("LabelsFunctionCD34-xyz")


def test_log_groups_names_the_missing_function() -> None:
    with pytest.raises(StepFailureError, match="LabelsFunction"):
        route_timing.log_groups(FakeLogs(["/aws/lambda/x-CatalogFunction-1"], []))


def test_read_lines_polls_until_complete_and_queries_the_window() -> None:
    done = {"status": "Complete", "results": [row(DOWNLOAD[0])]}
    logs = FakeLogs([], [{"status": "Running"}, done])
    sleeps: list[float] = []
    got = route_timing.read_lines(logs, ["g1", "g2"], 100.7, 200.2, sleep=sleeps.append)
    assert got == DOWNLOAD[:1]
    assert sleeps == [1.0]
    query = logs.queries[0]
    assert query["logGroupNames"] == ["g1", "g2"]
    assert (query["startTime"], query["endTime"]) == (100, 200)
    assert "ispresent(timing)" in query["queryString"]


def test_read_lines_fails_on_a_failed_query() -> None:
    logs = FakeLogs([], [{"status": "Failed"}])
    with pytest.raises(StepFailureError, match="Failed"):
        route_timing.read_lines(logs, ["g"], 0, 1, sleep=lambda _s: None)


def test_find_t1w_walks_dataset_subject_and_pages() -> None:
    pages = {
        "/catalog/datasets": {"datasets": [{"dataset": "openneuro-ds004473"}]},
        BASE: {"subjects": [{"subject": "sub-1"}]},
        f"{BASE}/assets?subject=sub-1&format=nifti&page=0": {
            "assets": [{"asset_id": "f", "path": "sub-1/func/sub-1_bold.nii.gz"}],
            "more": True,
        },
        f"{BASE}/assets?subject=sub-1&format=nifti&page=1": {
            "assets": [{"asset_id": "t1", "path": "sub-1/anat/sub-1_T1w.nii.gz"}],
            "more": False,
        },
    }
    assert route_timing.find_t1w(lambda p: pages[p]) == "t1"


def test_find_t1w_names_a_missing_dataset() -> None:
    with pytest.raises(StepFailureError, match="ds004473"):
        route_timing.find_t1w(lambda _p: {"datasets": [{"dataset": "other"}]})
