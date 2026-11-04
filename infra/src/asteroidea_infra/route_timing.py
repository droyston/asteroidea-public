"""`make live-route-timing`: warm and cold walls of the two slow routes.

LIVE and read-only apart from the labels it creates and deletes on the CS62 R2 file.
A new `sandbox-test` researcher calls the presign route `N` times on the ds004473
sub-1 T1w and creates then deletes `N` labels, recording the client wall of each call.
Then the `timing` lines the two Lambdas wrote (`asteroidea_ingest.route_log`) are read
back through CloudWatch Logs Insights for that window and printed as a table: per
route, call 1 (cold or not, as the log says) and the p50 and max of the rest, per phase.

The AWS calls go through an injected `logs` client; the parsing and the table are pure.
Exit 0 unless a call fails (exit 1, the call named).
"""

import json
import os
import re
import statistics
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Protocol

from asteroidea_infra import analysis_check, first_hour
from asteroidea_infra.research_app_check import CS62_R2
from asteroidea_infra.runner import HTTP_OK, api_send, sign_in
from asteroidea_infra.runner import api_get as _get
from asteroidea_infra.sandbox_guard import require_sandbox
from asteroidea_infra.steps import (
    Console,
    StepFailureError,
    StepResult,
    all_passed,
    format_summary,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

HOME_ENV: Final = "RF_TIMING_HOME"
CALLS: Final = 6
DATASET_SUFFIX: Final = "ds004473"
SUBJECT: Final = "1"
MAX_PAGES: Final = 20
T1W: Final = re.compile(r"(^|/)anat/[^/]*_T1w\.nii(\.gz)?$")
HTTP_CREATED: Final = 201
HTTP_NO_CONTENT: Final = 204
LABEL_PREFIX: Final = "timing-"
FUNCTIONS: Final = {
    "catalog_download": "CatalogFunction",
    "labels_create": "LabelsFunction",
}
PHASES: Final = {
    "catalog_download": ("athena_ms", "presign_ms", "total_ms"),
    "labels_create": (
        "provenance_ms",
        "athena_ms",
        "glue_ms",
        "s3_ms",
        "ddb_ms",
        "total_ms",
    ),
}
QUERY: Final = (
    "fields @timestamp, @message | filter ispresent(timing) "
    "| sort @timestamp asc | limit 1000"
)
# CloudWatch delivers a line some seconds after it is written: 30 polls of 3 s
WAIT_POLLS: Final = 30
POLL_S: Final = 3.0
QUERY_POLLS: Final = 60
SKEW_S: Final = 30


class Logs(Protocol):
    """The slice of the CloudWatch Logs client this module calls."""

    def describe_log_groups(self, **kwargs: object) -> dict[str, Any]:
        """List log groups."""
        ...

    def start_query(self, **kwargs: object) -> dict[str, Any]:
        """Start a Logs Insights query."""
        ...

    def get_query_results(self, **kwargs: object) -> dict[str, Any]:
        """Read a query's rows."""
        ...


@dataclass(frozen=True)
class Call:
    """One client call: the route it exercised, and its wall in seconds."""

    route: str
    wall_s: float


def parse_rows(rows: Sequence[Sequence[Mapping[str, str]]]) -> list[dict[str, Any]]:
    """Return the timing lines of Logs Insights `rows` (`@message` JSON), in order."""
    out: list[dict[str, Any]] = []
    for row in rows:
        message = next((c["value"] for c in row if c.get("field") == "@message"), "")
        try:
            line = json.loads(message[message.index("{") :])
        except ValueError:  # no "{" in the text, or not JSON
            continue
        if isinstance(line, dict) and "timing" in line and "phases" in line:
            out.append(line)
    return out


def aggregate(lines: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Group lines by route: the first call, then p50 and max of the rest."""
    routes: dict[str, dict[str, Any]] = {}
    for route, phases in PHASES.items():
        mine = [x for x in lines if x.get("timing") == route]
        if not mine:
            continue
        rest = mine[1:]
        stats: dict[str, tuple[float, float] | None] = {}
        for phase in phases:
            values = [float(x["phases"][phase]) for x in rest if phase in x["phases"]]
            stats[phase] = (statistics.median(values), max(values)) if values else None
        routes[route] = {"n": len(mine), "first": mine[0], "rest": stats}
    return routes


def _ms(value: float) -> str:
    return f"{value:7.0f}"


def table(
    routes: Mapping[str, Mapping[str, Any]], calls: Sequence[Call] = ()
) -> list[str]:
    """Return the report lines: per route, the first call and the rest, per phase."""
    out: list[str] = []
    for route, phases in PHASES.items():
        out.append(f"{route}")
        walls = [c.wall_s for c in calls if c.route == route]
        if walls:
            rest = walls[1:]
            tail = (
                f"; rest p50 {statistics.median(rest):.2f} s, max {max(rest):.2f} s"
                if rest
                else ""
            )
            out.append(f"  client wall: call 1 {walls[0]:.2f} s{tail}")
        found = routes.get(route)
        if found is None:
            out.append(
                "  log lines: 0 (no log lines found; deploy first, or logs are late)"
            )
            continue
        first = found["first"]
        out.append(
            f"  log lines: {found['n']}; call 1 cold={str(first['cold']).lower()}"
        )
        out.append(f"  {'phase':<14}{'call 1 ms':>10}{'p50 ms':>9}{'max ms':>9}")
        for phase in phases:
            one = first["phases"].get(phase)
            rest = found["rest"][phase]
            out.append(
                f"  {phase:<14}"
                f"{'-' if one is None else _ms(float(one)):>10}"
                f"{'-' if rest is None else _ms(rest[0]):>9}"
                f"{'-' if rest is None else _ms(rest[1]):>9}"
            )
    return out


def log_groups(logs: Logs) -> dict[str, str]:
    """Map each route to its Lambda's log group (the construct id is in the name)."""
    names: list[str] = []
    token = ""
    while True:
        args: dict[str, Any] = {"logGroupNamePrefix": "/aws/lambda/"}
        if token:
            args["nextToken"] = token
        page = logs.describe_log_groups(**args)
        names.extend(str(g["logGroupName"]) for g in page.get("logGroups", []))
        token = str(page.get("nextToken") or "")
        if not token:
            break
    found: dict[str, str] = {}
    for route, marker in FUNCTIONS.items():
        hits = sorted(n for n in names if marker.lower() in n.lower())
        if not hits:
            msg = f"no log group for {marker} under /aws/lambda/"
            raise StepFailureError(msg, "Deploy ServicesStack, then retry.")
        found[route] = hits[0]
    return found


def read_lines(
    logs: Logs,
    groups: Sequence[str],
    start: float,
    end: float,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> list[dict[str, Any]]:
    """Run the Insights query over `groups` for the window; return the timing lines."""
    qid = logs.start_query(
        logGroupNames=list(groups),
        startTime=int(start),
        endTime=int(end),
        queryString=QUERY,
    )["queryId"]
    for _ in range(QUERY_POLLS):
        got = logs.get_query_results(queryId=qid)
        status = got.get("status")
        if status == "Complete":
            return parse_rows(got.get("results", []))
        if status in {"Failed", "Cancelled", "Timeout"}:
            break
        sleep(1.0)
    msg = f"Logs Insights query {qid} did not complete (last status {status})"
    raise StepFailureError(msg, "Retry; the window is still in the logs.")


def wait_for_lines(
    logs: Logs,
    groups: Mapping[str, str],
    start: float,
    expected: Mapping[str, int],
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> list[dict[str, Any]]:
    """Re-query until every route shows its expected count, or `WAIT_POLLS` pass."""
    lines: list[dict[str, Any]] = []
    for _ in range(WAIT_POLLS):
        end = time.time() + SKEW_S
        lines = read_lines(logs, list(groups.values()), start, end, sleep=sleep)
        counts = {r: sum(x["timing"] == r for x in lines) for r in expected}
        if all(counts[r] >= n for r, n in expected.items()):
            break
        sleep(POLL_S)
    return lines


def _rows(found: object, key: str) -> list[dict[str, Any]]:
    value = found.get(key) if isinstance(found, dict) else None
    if not isinstance(value, list):
        return []
    return [{str(k): v for k, v in x.items()} for x in value if isinstance(x, dict)]


def find_t1w(get: Callable[[str], Any]) -> str:
    """Return the asset id of the ds004473 sub-1 anatomical T1w, from the API."""
    datasets = [str(d["dataset"]) for d in _rows(get("/catalog/datasets"), "datasets")]
    dataset = next((d for d in datasets if d.endswith(DATASET_SUFFIX)), None)
    if dataset is None:
        msg = f"no dataset ending {DATASET_SUFFIX} among: {', '.join(datasets)}"
        raise StepFailureError(msg, "Import the dataset (make import), then retry.")
    subjects = [
        str(s["subject"])
        for s in _rows(get(f"/catalog/datasets/{dataset}"), "subjects")
    ]
    subject = next((s for s in subjects if s in {SUBJECT, f"sub-{SUBJECT}"}), None)
    if subject is None:
        msg = f"{dataset} has no subject {SUBJECT}"
        raise StepFailureError(msg, "Check the catalog, then retry.")
    for page in range(MAX_PAGES):
        found = get(
            f"/catalog/datasets/{dataset}/assets?subject={subject}&format=nifti&page={page}"
        )
        for asset in _rows(found, "assets"):
            if T1W.search(str(asset.get("path", ""))):
                return str(asset["asset_id"])
        if not (isinstance(found, dict) and found.get("more") is True):
            break
    msg = f"no anatomical T1w for sub {subject} of {dataset}"
    raise StepFailureError(msg, "Check the catalog, then retry.")


def measure(run: analysis_check.AnalysisRun, calls: int = CALLS) -> list[StepResult]:
    """Call the two routes `calls` times each; keep the walls and the window."""
    started = time.monotonic()
    window_start = time.time() - SKEW_S
    asset = find_t1w(lambda path: _get(run, path)[0])
    walls: list[Call] = []
    for _ in range(calls):
        status, text, wall = api_send(run, "GET", f"/catalog/assets/{asset}/download")
        if status != HTTP_OK:
            msg = f"GET download answered {status}: {text[:200]}"
            raise StepFailureError(msg, "Read the answer, then retry.")
        walls.append(Call("catalog_download", wall))
    path = f"/files/{CS62_R2}/labels"
    name = f"{LABEL_PREFIX}{run.run_id}"
    try:
        for _ in range(calls):
            body = {"name": name, "note": "", "start_s": 1.0, "stop_s": 2.0}
            status, text, wall = api_send(run, "POST", path, body)
            if status != HTTP_CREATED:
                msg = f"POST label answered {status}: {text[:200]}"
                raise StepFailureError(msg, "Read the answer, then retry.")
            walls.append(Call("labels_create", wall))
            label_id = json.loads(text)["label_id"]
            status, text, _ = api_send(run, "DELETE", f"{path}/{label_id}")
            if status != HTTP_NO_CONTENT:
                msg = f"DELETE label answered {status}: {text[:200]}"
                raise StepFailureError(msg, "Delete the label by hand in the app.")
    finally:
        _remove_leftovers(run, path, name)
    run.data["timing"] = {"start": window_start, "calls": walls, "asset": asset}
    return [
        StepResult(
            "Calls",
            passed=True,
            detail=f"{calls} downloads of {asset}; {calls} labels created and deleted",
            seconds=time.monotonic() - started,
        )
    ]


def _remove_leftovers(run: analysis_check.AnalysisRun, path: str, name: str) -> None:
    """Delete any label of this run still present (a call failed after its POST)."""
    try:
        found, _ = _get(run, path)
    except StepFailureError:
        return
    for item in _rows(found, "labels"):
        if item.get("name") == name:
            api_send(run, "DELETE", f"{path}/{item['label_id']}")


def report(
    run: analysis_check.AnalysisRun, logs: Logs | None = None
) -> list[StepResult]:
    """Read the timing lines back for the window and print the table."""
    started = time.monotonic()
    client = logs or run.need_session().client("logs", region_name="us-east-1")
    saved = run.data["timing"]
    calls = [c for c in saved["calls"] if isinstance(c, Call)]
    groups = log_groups(client)
    expected = {r: sum(c.route == r for c in calls) for r in FUNCTIONS}
    seen = wait_for_lines(client, groups, saved["start"], expected)
    ours = {str(saved["asset"]), CS62_R2}  # other callers' lines in the window are out
    lines = [x for x in seen if x.get("asset_id") in ours]
    for text in table(aggregate(lines), calls):
        run.console.say(text)
    return [
        StepResult(
            "Timing lines",
            passed=True,
            detail=f"{len(lines)} lines read from {', '.join(groups.values())}",
            seconds=time.monotonic() - started,
        )
    ]


STEPS: Final = (
    ("Checking the AWS sign-in", first_hour.check_sso),
    ("Creating the researcher", analysis_check.create_researcher),
    ("Signing in", sign_in),
    ("Calling the routes", measure),
    ("Reading the timing lines", report),
)


def run_live() -> int:
    """Run the LIVE steps; exit 0 unless a call or a read failed."""
    profile, account = require_sandbox()
    console = Console(headless=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    home = Path(os.environ.get(HOME_ENV) or Path.home() / "asteroidea-timing")
    run = analysis_check.AnalysisRun(console, stamp, home / stamp, profile, account)
    run.base.mkdir(parents=True, exist_ok=True)
    console.say(f"asteroidea route timing {stamp}")
    complete = analysis_check.run_all(run, STEPS)
    console.say()
    for line in format_summary(run.results):
        console.say(line)
    ok = all_passed(run.results, complete=complete)
    console.say("TIMING DONE" if ok else "TIMING FAIL")
    return 0 if ok else 1


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point: `python -m asteroidea_infra.route_timing run`."""
    args = list(sys.argv[1:] if argv is None else argv)
    if args == ["run"]:
        return run_live()
    sys.stderr.write("usage: python -m asteroidea_infra.route_timing run\n")
    return 2


if __name__ == "__main__":
    sys.exit(main())
