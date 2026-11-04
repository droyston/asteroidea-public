"""Array cells: Athena returns `array<...>` as text, the runner types it as a list."""

import json
from pathlib import Path
from typing import Any

import pytest

from asteroidea_ingest.athena_query import AthenaRunner, _array

PAGE = Path(__file__).parent / "fixtures" / "athena-array-page.json"


class PageAthena:
    """Serves one captured page for the one query."""

    def __init__(self, page: dict[str, Any]) -> None:
        self.page = page

    def start_query_execution(self, **_: Any) -> dict[str, Any]:
        return {"QueryExecutionId": "q1"}

    def get_query_execution(self, **_: Any) -> dict[str, Any]:
        return {"QueryExecution": {"Status": {"State": "SUCCEEDED"}}}

    def get_query_results(self, **_: Any) -> dict[str, Any]:
        return self.page

    def stop_query_execution(self, **_: Any) -> dict[str, Any]:
        return {}


def _run(page: dict[str, Any]) -> list[dict[str, object]]:
    runner = AthenaRunner(athena=PageAthena(page), workgroup="w", database="d")
    return runner.run("SELECT 1", [], 1.0)


def _single(type_: str, text: str | None) -> dict[str, Any]:
    cell = {} if text is None else {"VarCharValue": text}
    return {
        "ResultSet": {
            "Rows": [{"Data": [{"VarCharValue": "v"}]}, {"Data": [cell]}],
            "ResultSetMetadata": {"ColumnInfo": [{"Name": "v", "Type": type_}]},
        }
    }


def test_captured_live_page_types_shape_as_a_list() -> None:
    rows = _run(json.loads(PAGE.read_text()))
    [micro] = [r for r in rows if str(r["path"]).endswith("LFP_micro/ElectricalSeries")]
    assert micro["shape"] == [732343, 56]
    assert micro["rate_hz"] == 1000.0


@pytest.mark.parametrize(
    ("text", "want"),
    [
        ("[]", []),
        ("[732343, 56]", [732343, 56]),
        ("[7]", [7]),
        ("[0.5, 1.0, 1.1]", [0.5, 1.0, 1.1]),
        ("[1.0E-4, 2.5]", [1.0e-4, 2.5]),
        ("[1, 2.5]", [1.0, 2.5]),
        ("[0.1234567890123456, 3.0]", [0.1234567890123456, 3.0]),
    ],
)
def test_array_cell_text_becomes_numbers(text: str, want: list[float]) -> None:
    got = _run(_single("array", text))[0]["v"]
    assert got == want
    assert isinstance(got, list)
    assert [type(x) for x in got] == [type(x) for x in want]


@pytest.mark.parametrize("text", ["[a, b]", "[[1, 2], [3]]", "{x=1}", "[1, ]"])
def test_non_numeric_array_text_stays_the_raw_string(text: str) -> None:
    assert _array(text) == text


def test_null_array_cell_is_none() -> None:
    assert _run(_single("array", None))[0]["v"] is None
