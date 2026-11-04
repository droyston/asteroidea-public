"""The Athena runner the API Lambdas share.

Start, poll under one deadline, type rows. Every value reaches Athena as an
`ExecutionParameters` entry, never inside the query text. Athena types a parameter by
its text (`0` is an integer, `007` becomes 7, `1e5` a double), so a `str` is rendered as
a quoted varchar literal with single quotes doubled and only an `int` (an OFFSET) goes
bare. A failed query, a
`ClientError` from any Athena call and an exhausted budget are `api_common.ApiError`s
with status 502, 502 and 503.
"""

import re
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from botocore.exceptions import ClientError

from asteroidea_ingest.api_common import ApiError

if TYPE_CHECKING:
    from collections.abc import Callable

    from asteroidea_ingest.aws_types import Athena

type Params = Sequence[str | int]

POLL_SECONDS: Final = 0.2
BUDGET_SECONDS: Final = 18.0
DONE: Final = frozenset({"SUCCEEDED", "FAILED", "CANCELLED"})
SLOW: Final = "the catalog is slow to answer; try again"

_INTS: Final = frozenset({"tinyint", "smallint", "integer", "bigint"})
_FLOATS: Final = frozenset({"real", "float", "double"})


def render_param(value: str | int) -> str:
    """Render a str as a quoted varchar literal and an int as bare digits."""
    if isinstance(value, int):
        return str(value)
    return "'" + value.replace("'", "''") + "'"


_INT_LITERAL: Final = re.compile(r"[+-]?\d+")


def _array(value: str) -> object:
    """Parse Athena's text form of an array (`[a, b]`, elements unquoted) into a list.

    Every element an integer literal gives `list[int]`; otherwise every element a number
    gives `list[float]` (`float()` of Athena's text, so doubles keep full precision);
    `[]` gives `[]`. Any other element text (strings, nested arrays, structs)
    cannot be split safely, so the cell stays the raw string. The catalog's array
    columns are all numeric (`nifti_shape`, `shape`: `list<long>`; `nifti_voxel_mm`,
    `spike_times`: `list<double>`).
    """
    text = value.strip()
    if not (text.startswith("[") and text.endswith("]")):
        return value
    inner = text[1:-1].strip()
    if not inner:
        return []
    parts = [p.strip() for p in inner.split(",")]
    if all(_INT_LITERAL.fullmatch(p) for p in parts):
        return [int(p) for p in parts]
    try:
        return [float(p) for p in parts]
    except ValueError:
        return value


def _cell(value: str | None, type_: str) -> object:
    if value is None:
        return None
    if type_ == "array":
        return _array(value)
    if type_ in _INTS:
        return int(value)
    if type_ in _FLOATS:
        return float(value)
    if type_ == "boolean":
        return value == "true"
    return value


def _records(pages: list[dict[str, Any]]) -> list[dict[str, object]]:
    """Turn Athena result pages into row dicts, typed by the column metadata."""
    info = pages[0]["ResultSet"]["ResultSetMetadata"]["ColumnInfo"]
    names = [str(c["Name"]) for c in info]
    types = [str(c["Type"]) for c in info]
    rows = [r for page in pages for r in page["ResultSet"]["Rows"]][1:]  # header row
    return [
        {
            n: _cell(cell.get("VarCharValue"), t)
            for n, t, cell in zip(names, types, row["Data"], strict=True)
        }
        for row in rows
    ]


def _failure(err: ClientError) -> ApiError:
    message = err.response.get("Error", {}).get("Message", str(err))
    return ApiError(502, f"the catalog query failed: {message}")


@dataclass(frozen=True, slots=True)
class AthenaRunner:
    """Run fixed query texts over an injected Athena client, clock and sleep."""

    athena: Athena
    workgroup: str
    database: str
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep

    def run(self, sql: str, params: Params, deadline: float) -> list[dict[str, object]]:
        """Run one query."""
        return self.run_all([(sql, params)], deadline)[0]

    def run_all(
        self, queries: list[tuple[str, Params]], deadline: float
    ) -> list[list[dict[str, object]]]:
        """Start every query, then poll them together under one deadline."""
        pending: dict[str, int] = {}
        try:
            for index, (sql, params) in enumerate(queries):
                pending[self._start(sql, params)] = index
        except ApiError:
            self._stop(pending)
            raise
        results: list[list[dict[str, object]]] = [[] for _ in queries]
        try:
            self._poll(pending, results, deadline)
        except ClientError as err:
            self._stop(pending)
            raise _failure(err) from err
        return results

    def _poll(
        self,
        pending: dict[str, int],
        results: list[list[dict[str, object]]],
        deadline: float,
    ) -> None:
        while pending:
            for qid in list(pending):
                status = self.athena.get_query_execution(QueryExecutionId=qid)[
                    "QueryExecution"
                ]["Status"]
                state = status["State"]
                if state == "SUCCEEDED":
                    results[pending.pop(qid)] = self._results(qid)
                elif state in DONE:
                    pending.pop(qid)
                    self._stop(pending)
                    reason = status.get("StateChangeReason", state)
                    raise ApiError(502, f"the catalog query failed: {reason}")
            if pending and self.clock() >= deadline:
                self._stop(pending)
                raise ApiError(503, SLOW, code="slow")
            if pending:
                self.sleep(POLL_SECONDS)

    def _stop(self, pending: dict[str, int]) -> None:
        for qid in pending:
            self.athena.stop_query_execution(QueryExecutionId=qid)

    def _start(self, sql: str, params: Params) -> str:
        try:
            if params:
                started = self.athena.start_query_execution(
                    QueryString=sql,
                    WorkGroup=self.workgroup,
                    QueryExecutionContext={"Database": self.database},
                    ExecutionParameters=[render_param(p) for p in params],
                )
            else:
                started = self.athena.start_query_execution(
                    QueryString=sql,
                    WorkGroup=self.workgroup,
                    QueryExecutionContext={"Database": self.database},
                )
        except ClientError as err:
            raise _failure(err) from err
        return str(started["QueryExecutionId"])

    def _results(self, qid: str) -> list[dict[str, object]]:
        pages = [self.athena.get_query_results(QueryExecutionId=qid)]
        while token := pages[-1].get("NextToken"):
            pages.append(
                self.athena.get_query_results(QueryExecutionId=qid, NextToken=token)
            )
        return _records(pages)
