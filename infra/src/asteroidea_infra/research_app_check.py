"""`make live-research-app` (LIVE): the research app's headless and own-browser passes.

`PASS=1` runs rows R0 to R8 (below); `PASS=2` runs rows R9 to R13 (see `PASS_TWO` and
the section "pass 2").

Steps, each printing PASS or FAIL: the AWS sign-in check, a new researcher, the
`sandbox-test` sign-in (stand-in for managed login), a labelled warm-up
that calls `/compute/rates`, `/compute/overview` and `/compute/spectrogram` once each
(the cold wall is recorded and kept out of the row budgets), then a headless Playwright
run (`web/e2e/live/rf-run.spec.ts`, 1440 x 900, tokens placed in `sessionStorage`,
labelled) that drives rows R0 to R8 with the page's own controls. The spec only
measures; this module judges each row against its budgets. Then, unless
`--no-prompt`, the tester repeats R1 to R8 in their own browser and answers y or n per
row.
`report.md` and one screenshot per row go to `~/asteroidea-rf/<run-id>/`.
"""

import contextlib
import io
import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from asteroidea_infra import analysis_check, first_hour
from asteroidea_infra.admin import add_researcher
from asteroidea_infra.live_api import browsers_path, ensure_browser
from asteroidea_infra.runner import (
    REPO,
    api_send,
    commit_sha,
    execute,
    last_json,
    sandbox_sign_in,
    sign_in,
)
from asteroidea_infra.runner import api_get as _get
from asteroidea_infra.runner import write_report as write_run_report
from asteroidea_infra.sandbox_guard import require_sandbox
from asteroidea_infra.steps import (
    HEADLESS_DOMAIN,
    Console,
    Report,
    StepFailureError,
    StepResult,
    all_passed,
    format_summary,
    run_step,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

HOME_ENV: Final = "RF_HOME"
DATASET: Final = "dandi-000623"
CS62_R2: Final = "1d50b507-a8a1-58d5-83a6-199f232a1582"
SERIES_NAME: Final = "LFP_micro"
SERIES_NDIM: Final = 2
SPEC_TIMEOUT_S: Final = 1800.0
SPEC: Final = "e2e/live/rf-run.spec.ts"
CONFIG: Final = "playwright.live.config.ts"
ROWS_FILE: Final = "rows.json"
WARM_FILE: Final = "warmup.json"
MEGABYTE: Final = 1024 * 1024
UNIT_S: Final = "s"
UNIT_B: Final = "B"
SUBSTITUTIONS: Final = (
    "Headless mode. Substitutions in this run:",
    "  - the account email is an .invalid address (no inbox, nothing is mailed)",
    "  - sign-in uses the sandbox-test client, not managed login (R0 measures the",
    "    app's first load with the tokens already in sessionStorage)",
    "  - R8's new tab receives the same tokens, as it would after managed login",
    "  - the warm-up calls each compute route once before the rows; its cold wall is",
    "    recorded and excluded from the row budgets",
)
WARMUP_NOTE: Final = "substitution: cold wall excluded from the row budgets"
TOKENS_NOTE: Final = "substitution: tokens placed in sessionStorage"
PASS_ONE_ROWS: Final = ("R0", "R1", "R2", "R3", "R4", "R5", "R6", "R7", "R8")


@dataclass(frozen=True)
class Limit:
    """One measured part of a row and its ceiling (seconds or bytes)."""

    part: str
    ceiling: float
    unit: str = UNIT_S
    judged: bool = True  # False: shown against the ceiling, never makes the row FAIL


@dataclass(frozen=True)
class RowSpec:
    """A budgeted row: what the tester does, and what it may cost."""

    row: str
    title: str
    limits: tuple[Limit, ...]
    budget_note: str
    note: str = ""


@dataclass(frozen=True)
class Pass:
    """What differs between the passes: steps, human rows, report text and clean-up."""

    number: int
    title: str
    steps: tuple[analysis_check.Step, ...]
    substitutions: tuple[str, ...]
    human_rows: tuple[RowSpec, ...]
    human_hint: str
    shot_ids: tuple[str, ...]
    seams: tuple[str, ...]
    out_dir: str = "browser"
    finish: Callable[[analysis_check.AnalysisRun], Sequence[StepResult]] | None = None
    needs: Mapping[str, tuple[str, ...]] = field(default_factory=dict)


# Budgets per row. Where it gives a median and a
# maximum for a single run (R4), the maximum is the ceiling. R8 is "R0 + R7": one
# sign-in plus the two loads a deep link makes (overview and window), so 5 + 15 + 15.
ROWS: Final = (
    RowSpec(
        "R0", "Open the app, signed in", (Limit("row", 5.0),), "sign-in at most 5 s"
    ),
    RowSpec(
        "R1",
        "Nav Catalog lists datasets incl. dandi-000623",
        (Limit("row", 3.16),),
        "p50 2.33 s, max 3.16 s",
    ),
    RowSpec(
        "R2",
        "Open dandi-000623: subjects CS41, CS62",
        (Limit("row", 4.0),),
        "at most 4 s",
    ),
    RowSpec(
        "R3",
        "sub-CS62, the R2 NWB file: 96 electrodes, 44 units, 41 trials",
        (Limit("subject", 4.0), Limit("files", 4.0), Limit("file", 4.0)),
        "at most 4 s a tab",
    ),
    RowSpec(
        "R4",
        "Units: unit 0, intervals:trials, -2 s to +2 s, 50 ms, z-score",
        (Limit("row", 8.0), Limit("body", 5.0 * MEGABYTE, UNIT_B)),
        "warm p50 at most 4 s, max 8 s; body at most 5 MB",
    ),
    RowSpec(
        "R5",
        "Units aligned to Movie scene cuts: 93 cuts",
        (Limit("row", 10.0),),
        "warm max 10 s",
    ),
    RowSpec(
        "R6",
        "Events tab: 41 trials on the timeline",
        (Limit("row", 4.0),),
        "at most 4 s",
    ),
    RowSpec(
        "R7",
        "LFP tab: overview, a 60 s window at t=300, one pan",
        (Limit("overview", 15.0), Limit("window", 15.0), Limit("pan", 15.0)),
        "overview visible, then a 60 s window and one pan; warm at most 15 s each",
    ),
    RowSpec(
        "R8",
        "Copy R7's URL, open it in a new tab: same tab, channel and window",
        (Limit("row", 35.0),),
        "R0 + R7: 5 s + overview 15 s + window 15 s",
    ),
)
ROW_IDS: Final = tuple(r.row for r in ROWS)
HUMAN_ROWS: Final = ROWS[1:]


def _fmt(value: float, unit: str) -> str:
    return f"{value / MEGABYTE:.2f} MB" if unit == UNIT_B else f"{value:.2f} s"


def judge_row(spec: RowSpec, observed: Mapping[str, Any] | None) -> StepResult:
    """Judge one row: it ran, it held, it met every ceiling."""
    name = f"{spec.row} {spec.title}"
    if observed is None:
        return StepResult(
            name, passed=False, detail="the browser run did not reach this row"
        )
    parts = observed.get("parts") or {}
    seconds = float(observed.get("seconds", 0.0))
    detail = str(observed.get("detail", ""))
    if observed.get("notReached") is True:
        return StepResult(
            name, passed=False, detail=f"not reached: {detail}", seconds=seconds
        )
    if observed.get("skipped") is True:
        return StepResult(
            name, passed=True, detail=f"skipped: {detail}", note="skipped, not run"
        )
    if observed.get("ok") is not True:
        return StepResult(
            name,
            passed=False,
            detail=f"FAILED in the browser: {detail}",
            seconds=seconds,
        )
    shown: list[str] = []
    over: list[str] = []
    for limit in spec.limits:
        value = (
            seconds
            if limit.part == "row" and limit.part not in parts
            else parts.get(limit.part)
        )
        if not isinstance(value, int | float) or isinstance(value, bool):
            over.append(f"{limit.part} not measured")
            continue
        got = _fmt(float(value), limit.unit)
        word = "limit" if limit.judged else "reference budget"
        text = f"{limit.part} {got} ({word} {_fmt(limit.ceiling, limit.unit)})"
        shown.append(text)
        if limit.judged and value > limit.ceiling:
            over.append(f"{limit.part} over its limit")
    tail = f"; OVER: {', '.join(over)}" if over else ""
    return StepResult(
        name,
        passed=not over,
        detail=f"{detail}; {'; '.join(shown)}; budget {spec.budget_note}{tail}",
        seconds=seconds,
        note=spec.note or (TOKENS_NOTE if spec.row == "R0" else ""),
    )


def parse_rows(text: str) -> dict[str, Mapping[str, Any]]:
    """Return the spec's `rows.json` by row id; a malformed file is a failed run."""
    try:
        data = json.loads(text)
    except ValueError as err:
        msg = f"rows.json is not JSON: {err}"
        raise StepFailureError(
            msg, "Read the Playwright output in browser.log."
        ) from err
    if not isinstance(data, list):
        msg = "rows.json is not a list"
        raise StepFailureError(msg, "Read the Playwright output in browser.log.")
    return {str(r["row"]): r for r in data if isinstance(r, dict) and "row" in r}


def judge_all(rows: Mapping[str, Mapping[str, Any]]) -> list[StepResult]:
    """Judge R0 to R8 in order; a row the spec never reached fails."""
    return [judge_row(spec, rows.get(spec.row)) for spec in ROWS]


def screenshots(out: Path, ids: Sequence[str] = ROW_IDS) -> dict[str, Path]:
    """Return the screenshot of each row in `ids` that has one."""
    return {row: out / f"{row}.png" for row in ids if (out / f"{row}.png").is_file()}


# --- LIVE steps ----------------------------------------------------------------------


def _is_micro(path: object, shape: object) -> bool:
    """Whether a series row is the two-dimensional `LFP_micro` ElectricalSeries."""
    if not isinstance(path, str) or not isinstance(shape, list):
        return False
    parts = path.rstrip("/").split("/")
    return (
        len(parts) >= SERIES_NDIM
        and parts[-2] == SERIES_NAME
        and len(shape) == SERIES_NDIM
    )


def micro_series(rows: Sequence[Mapping[str, Any]]) -> str:
    """Return the path of the two-dimensional `LFP_micro` ElectricalSeries in `rows`."""
    found = [str(r["path"]) for r in rows if _is_micro(r.get("path"), r.get("shape"))]
    if len(found) != 1:
        msg = f"{len(found)} two-dimensional {SERIES_NAME} series in the series table"
        raise StepFailureError(msg, "Check the file is extracted (make reextract).")
    return found[0]


def warm_up(run: analysis_check.AnalysisRun) -> list[StepResult]:
    """Call each compute route once; record the cold walls in `warmup.json`."""
    started = time.monotonic()
    table, _ = _get(run, f"/catalog/assets/{CS62_R2}/tables/series")
    series = micro_series(table["rows"])
    run.outputs["series"] = series
    calls = {
        "rates": (
            f"/compute/rates?asset_id={CS62_R2}&units=0&align=intervals:trials"
            "&pre_s=2&post_s=2&bin_s=0.05&zscore=1&raster=1"
        ),
        "overview": f"/compute/overview?asset_id={CS62_R2}&series={series}",
        "spectrogram": (
            f"/compute/spectrogram?asset_id={CS62_R2}&series={series}"
            "&channel=0&start_s=300&duration_s=60"
        ),
    }
    walls = {name: _get(run, path)[1] for name, path in calls.items()}
    (run.base / WARM_FILE).write_text(
        json.dumps({"series": series, "cold_s": walls}, indent=2)
    )
    text = ", ".join(f"{name} {wall:.1f} s" for name, wall in walls.items())
    return [
        StepResult(
            "Warm-up",
            passed=True,
            detail=f"cold walls, not judged: {text}",
            seconds=time.monotonic() - started,
            note=WARMUP_NOTE,
        )
    ]


def browser_dir(run: analysis_check.AnalysisRun) -> Path:
    """Return where the spec writes `rows.json` and the screenshots."""
    return run.base / "browser"


def browser_env(run: analysis_check.AnalysisRun) -> dict[str, str]:
    """Return the spec's environment: the URL, the tokens, the file and series."""
    return {
        **os.environ,
        "PLAYWRIGHT_BROWSERS_PATH": browsers_path(),
        "RF_APP_URL": run.outputs["WebUrl"],
        "RF_API_URL": run.outputs["ApiUrl"],
        "RF_ACCESS_TOKEN": run.outputs["access_token"],
        "RF_REFRESH_TOKEN": run.outputs["refresh_token"],
        "RF_ASSET": CS62_R2,
        "RF_SERIES": run.outputs["series"],
        "RF_OUT": str(browser_dir(run)),
    }


def browser_rows(run: analysis_check.AnalysisRun) -> list[StepResult]:
    """Run the spec headless at 1440 x 900; judge R0 to R8 from what it measured."""
    return drive_browser(SPEC, browser_dir(run), browser_env(run), judge_all)


def drive_browser(
    spec: str,
    out: Path,
    env: Mapping[str, str],
    judge: Callable[[Mapping[str, Mapping[str, Any]]], list[StepResult]],
) -> list[StepResult]:
    """Run `spec` headless with `env`; `judge` the rows it wrote to `out`."""
    out.mkdir(parents=True, exist_ok=True)
    ensure_browser(env)
    done = execute(
        ["./node_modules/.bin/playwright", "test", "-c", CONFIG, spec],
        cwd=REPO / "web",
        env=env,
        timeout=SPEC_TIMEOUT_S,
    )
    (out / "browser.log").write_text(done.out + done.err)
    rows_path = out / ROWS_FILE
    if not rows_path.is_file():
        lines = (done.err or done.out).strip().splitlines()
        msg = (
            f"the browser run wrote no rows.json: {lines[-1] if lines else 'no output'}"
        )
        raise StepFailureError(msg, f"Read {out / 'browser.log'}.")
    return judge(parse_rows(rows_path.read_text()))


STEPS: Final = (
    ("Checking the AWS sign-in", first_hour.check_sso),
    ("Creating the researcher", analysis_check.create_researcher),
    ("Signing in", sign_in),
    ("Warming the compute routes", warm_up),
    ("Driving R0 to R8 in a headless browser", browser_rows),
)
# what each step needs; a failed need marks the step "not reached".
NEEDS: Final = {
    "Creating the researcher": ("Checking the AWS sign-in",),
    "Signing in": ("Creating the researcher",),
    "Warming the compute routes": ("Signing in",),
    "Driving R0 to R8 in a headless browser": ("Warming the compute routes",),
}


# --- the own-browser pass ---------------------------------------------------


def human_pass(
    console: Console,
    url: str,
    rows: Sequence[RowSpec] = HUMAN_ROWS,
    hint: str = "",
) -> list[StepResult]:
    """Ask the tester to repeat `rows` in their own browser; y or n per row."""
    console.say()
    console.say(
        f"Now repeat {rows[0].row} to {rows[-1].row} yourself in your own browser"
        f" at {url}"
    )
    console.say("Sign in with your own account (the headless researcher has no inbox).")
    if hint:
        console.say(hint)
    results = []
    for spec in rows:
        started = time.monotonic()
        console.say(f"{spec.row}: {spec.title}  [budget: {spec.budget_note}]")
        ok = console.confirm(f"{spec.row} as described?", default=False)
        results.append(
            StepResult(
                f"{spec.row} (tester) {spec.title}",
                passed=ok,
                detail="tester answered y"
                if ok
                else "tester answered n or gave no answer",
                seconds=time.monotonic() - started,
                note="tester's own browser",
            )
        )
    return results


SEAMS_ONE: Final = (
    (
        "- R5: that the rates equal `make live-analysis`'s scene-cut step is"
        " unchecked here; the response is saved as R5-rates.json to compare."
    ),
    (
        "- The manual rows are the tester's word; a skipped prompt"
        " (`--no-prompt`) is not a pass."
    ),
)


def write_report(
    run: analysis_check.AnalysisRun,
    seconds: float,
    *,
    human: Sequence[StepResult] = (),
    pass_spec: Pass | None = None,
) -> Path:
    """Write `report.md`: the steps, the manual rows and one screenshot per row."""
    chosen = PASS_ONE if pass_spec is None else pass_spec
    report = Report(run.run_id, "headless", commit_sha(), title=chosen.title)
    report.results = [*run.results, *human]
    report.ids = {
        "email": run.email,
        "app": run.outputs.get("WebUrl", ""),
        "run": str(run.base),
        "total": f"{seconds:.0f} s",
    }
    shots = screenshots(run.base / chosen.out_dir, chosen.shot_ids)
    lines = ["", "## Screenshots", ""]
    lines.extend(f"- {row}: {path}" for row, path in shots.items())
    lines.extend(f"- {row}: none" for row in chosen.shot_ids if row not in shots)
    lines.extend(["", "## Unverified seams", "", *chosen.seams])
    return write_run_report(run, report, lines)


# --- pass 2: R9 to R13 ----------------------------------------------------------------

SPEC2: Final = "e2e/live/rf-run2.spec.ts"
BROWSER2: Final = "browser2"
EXPECTED_FILE: Final = "expected.json"
WARM2_FILE: Final = "warmup2.json"
WORKER: Final = Path(__file__).with_name("research_app_worker.py")
SDK_PINS: Final = ("numpy==2.5.4", "polars==2.0.0")  # the SDK's `analysis` extra
LABEL_PREFIX: Final = "baseline-"
HTTP_NO_CONTENT: Final = 204
SDK_TIMEOUT_S: Final = 600.0
SECOND_NOTE: Final = (
    "substitution: second account through the sandbox-test client, tokens in "
    "sessionStorage"
)
RTOL_NOTE: Final = (
    "the route rounds to 6 significant digits; the SDK result is rounded the same "
    "way, then compared at rtol 1e-9"
)

ROWS2: Final = (
    RowSpec(
        "R9",
        "Image tab, ds004473 sub-1 T1w: three planes, crosshair in world mm, "
        "volume fetched once",
        (Limit("presign", 3.0), Limit("volume", 5.0, judged=False)),
        "presign at most 3 s; volume: network-bound, tester judges; 10,740,856 B",
    ),
    RowSpec(
        "R9b",
        "Image tab, ds004798 sub-p62cs anatomical T1w (if present): the same checks",
        (Limit("presign", 3.0), Limit("volume", 5.0, judged=False)),
        "presign at most 3 s; volume: network-bound, tester judges; "
        "skipped when the catalog has no such file",
    ),
    RowSpec(
        "R10",
        "Events tab, CS62 R2 file: drag a label baseline-<run-id>, save, reload: "
        "it persists, yours",
        (Limit("post", 3.0),),
        "POST at most 3 s",
    ),
    RowSpec(
        "R11",
        "Second researcher sees the label; edit refused: only the author can change it",
        (Limit("patch", 3.0),),
        "as R10 (the refused PATCH at most 3 s)",
        SECOND_NOTE,
    ),
    RowSpec(
        "R12",
        "Units aligned to label_name:baseline-<run-id>: rates around the label start",
        (Limit("row", 8.0), Limit("body", 5.0 * MEGABYTE, UNIT_B)),
        "as R4: at most 8 s; body at most 5 MB",
    ),
)
ROW_IDS2: Final = tuple(r.row for r in ROWS2)
SDK_SPEC: Final = RowSpec(
    "R13",
    "Fresh venv: asteroidea.labels(<id>) and analysis.rates on sql() unit_spikes "
    "equal R4's rates",
    (Limit("labels", 5.0), Limit("rates", 5.0), Limit("sql", 10.0)),
    "at most 5 s (labels, rates); sql() against the 10 s catalog-query bound",
    RTOL_NOTE,
)
PASS_TWO_ROWS: Final = (*ROW_IDS2, SDK_SPEC.row)
HUMAN_ROWS2: Final = tuple(s for s in ROWS2 if s.row in {"R9", "R10", "R12"})


def judge_pass2(rows: Mapping[str, Mapping[str, Any]]) -> list[StepResult]:
    """Judge R9 to R12 in order; a row the spec never reached fails."""
    return [judge_row(spec, rows.get(spec.row)) for spec in ROWS2]


SPLIT_FIELDS = (
    ("auth_s", "auth"),
    ("http_s", "http"),
    ("table_s", "table"),
    ("warm_labels_s", "warm second call"),
)


def labels_split_text(split: object) -> str:
    """Return the `labels()` time split for the R13 row, or say it is not reported."""
    if isinstance(split, dict):
        shown: list[str] = []
        for key, word in SPLIT_FIELDS:
            v = split.get(key)
            if not isinstance(v, int | float) or isinstance(v, bool):
                return "labels split: not reported"
            shown.append(f"{word} {float(v):.2f} s")
        return f"labels split: {', '.join(shown)}"
    return "labels split: not reported"


def sdk_measured(data: Mapping[str, Any]) -> dict[str, Any]:
    """Turn the worker's facts into a row `judge_row` can judge (ok, seconds, parts)."""
    parts = {str(k): float(v) for k, v in (data.get("parts") or {}).items()}
    label = data.get("label")
    problems: list[str] = []
    if data.get("n_labels") != 1:
        problems.append(f"{data.get('n_labels')} labels carry the run's name, not 1")
    elif not (isinstance(label, dict) and label.get("mine") is True):
        problems.append("the label is not marked mine")
    if data.get("n_onsets") != data.get("n_trials"):
        problems.append(
            f"sql() gave {data.get('n_onsets')} trial onsets; the route used"
            f" {data.get('n_trials')}"
        )
    if data.get("equal") is not True:
        problems.append(
            f"rates differ from the route's (max relative difference"
            f" {data.get('max_rel_diff')}, rtol {data.get('rtol')}; edges equal:"
            f" {data.get('edges_equal')})"
        )
    name = label.get("name") if isinstance(label, dict) else None
    detail = "; ".join(problems) or (
        f"label {name} is mine; unit {data.get('unit')}, {data.get('n_onsets')}"
        f" trials, {data.get('n_spikes')} spikes, {data.get('bins')} bins equal to"
        f" the route's (max relative difference {data.get('max_rel_diff')})"
    )
    detail = f"{detail}; {labels_split_text(data.get('labels_split'))}"
    return {
        "ok": not problems,
        "seconds": sum(parts.values()),
        "detail": detail,
        "parts": parts,
    }


def label_name(run: analysis_check.AnalysisRun) -> str:
    """Return the name of the label this run creates."""
    return f"{LABEL_PREFIX}{run.run_id}"


def browser2_dir(run: analysis_check.AnalysisRun) -> Path:
    """Return where the pass-2 spec writes `rows.json` and the screenshots."""
    return run.base / BROWSER2


def second_researcher(run: analysis_check.AnalysisRun) -> list[StepResult]:
    """Create the second researcher (R11) and sign it in; keep its tokens (labelled)."""
    started = time.monotonic()
    session = run.need_session()
    email = f"rf2-{run.run_id}@{HEADLESS_DOMAIN}"
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            add_researcher(session, email)
    except SystemExit as err:
        raise StepFailureError(
            str(err.code), "Enter an address that has not been added before."
        ) from err
    got = sandbox_sign_in(session, run.outputs, email, run.base / "config2")
    run.outputs["email_2"] = email
    run.outputs["access_token_2"] = str(got["AccessToken"])
    run.outputs["refresh_token_2"] = str(got["RefreshToken"])
    return [
        StepResult(
            "Second researcher",
            passed=True,
            detail=f"{email} created in group researchers and signed in",
            seconds=time.monotonic() - started,
            note=SECOND_NOTE,
        )
    ]


def rates_path(unit: int) -> str:
    """Return R4's rates request for `unit` (raw rates, no raster, no z-score)."""
    return (
        f"/compute/rates?asset_id={CS62_R2}&units={unit}&align=intervals:trials"
        "&pre_s=2&post_s=2&bin_s=0.05&zscore=0"
    )


def pass_two_targets(run: analysis_check.AnalysisRun) -> list[StepResult]:
    """Read the first unit; keep the route's rates for R13; warm the routes."""
    started = time.monotonic()
    units, units_s = _get(run, f"/catalog/assets/{CS62_R2}/tables/units")
    ids = [int(str(r["unit_index"])) for r in units["rows"]]
    if not ids:
        msg = "the units table of the CS62 R2 file is empty"
        raise StepFailureError(msg, "Check the file is extracted (make reextract).")
    unit = min(ids)
    _, labels_s = _get(run, f"/files/{CS62_R2}/labels")
    rates, rates_s = _get(run, rates_path(unit))
    entry = next(u for u in rates["units"] if int(u["unit_index"]) == unit)
    expected = {
        "unit": unit,
        "n_trials": int(rates["n_trials"]),
        "edges": rates["edges"],
        "rate_hz": entry["rate_hz"],
    }
    (run.base / EXPECTED_FILE).write_text(json.dumps(expected))
    walls = {"units": units_s, "labels": labels_s, "rates": rates_s}
    (run.base / WARM2_FILE).write_text(json.dumps({"unit": unit, "cold_s": walls}))
    text = ", ".join(f"{name} {wall:.1f} s" for name, wall in walls.items())
    return [
        StepResult(
            "Targets and warm-up",
            passed=True,
            detail=(
                f"unit {unit}; {expected['n_trials']} trials; "
                f"cold walls, not judged: {text}"
            ),
            seconds=time.monotonic() - started,
            note=WARMUP_NOTE,
        )
    ]


def browser2_env(run: analysis_check.AnalysisRun) -> dict[str, str]:
    """Return the pass-2 spec's environment: URL, both token pairs, file, label."""
    return {
        **os.environ,
        "PLAYWRIGHT_BROWSERS_PATH": browsers_path(),
        "RF_APP_URL": run.outputs["WebUrl"],
        "RF_API_URL": run.outputs["ApiUrl"],
        "RF_ACCESS_TOKEN": run.outputs["access_token"],
        "RF_REFRESH_TOKEN": run.outputs["refresh_token"],
        "RF_ACCESS_TOKEN_2": run.outputs["access_token_2"],
        "RF_REFRESH_TOKEN_2": run.outputs["refresh_token_2"],
        "RF_ASSET": CS62_R2,
        "RF_LABEL": label_name(run),
        "RF_OUT": str(browser2_dir(run)),
    }


def browser2_rows(run: analysis_check.AnalysisRun) -> list[StepResult]:
    """Run the pass-2 spec headless at 1440 x 900; judge R9 to R12."""
    return drive_browser(SPEC2, browser2_dir(run), browser2_env(run), judge_pass2)


def install_sdk(run: analysis_check.AnalysisRun) -> list[StepResult]:
    """Build the SDK wheel; install it with the `analysis` extra's pins."""
    return analysis_check.install(run, SDK_PINS)


def sdk_rows(run: analysis_check.AnalysisRun) -> list[StepResult]:
    """R13: run the worker in the fresh venv; judge what it measured."""
    started = time.monotonic()
    unit = json.loads((run.base / EXPECTED_FILE).read_text())["unit"]
    done = execute(
        [
            str(run.python),
            str(WORKER),
            CS62_R2,
            str(unit),
            label_name(run),
            str(run.base / EXPECTED_FILE),
        ],
        env=run.child_env,
        timeout=SDK_TIMEOUT_S,
    )
    data: dict[str, Any] = dict(last_json(done.out))
    error = data.get("error")
    if isinstance(error, str):
        raise StepFailureError(error, "The error above is the SDK's own; report it.")
    if done.code != 0 or not data:
        lines = (done.err or done.out).strip().splitlines()
        msg = f"the R13 worker crashed: {lines[-1] if lines else 'no output'}"
        raise StepFailureError(msg, "Report this as a defect of the worker.")
    run.data["R13"] = data
    (run.base / "r13.json").write_text(json.dumps(data, indent=2, sort_keys=True))
    result = judge_row(SDK_SPEC, sdk_measured(data))
    return [
        StepResult(
            result.name,
            result.passed,
            result.detail,
            time.monotonic() - started,
            result.note,
        )
    ]


def cleanup_labels(run: analysis_check.AnalysisRun) -> list[StepResult]:
    """Delete, as their author, the labels this run created; none may remain."""
    started = time.monotonic()
    if "access_token" not in run.outputs:
        return [
            StepResult(
                "Cleanup", passed=True, detail="nothing created (never signed in)"
            )
        ]
    name = label_name(run)
    path = f"/files/{CS62_R2}/labels"
    found, _ = _get(run, path)
    own = [x for x in found["labels"] if x.get("name") == name]
    for item in own:
        status, text, _ = api_send(run, "DELETE", f"{path}/{item['label_id']}")
        if status != HTTP_NO_CONTENT:
            msg = f"DELETE of label {item['label_id']} answered {status}: {text[:200]}"
            raise StepFailureError(msg, "Delete the label by hand in the app.")
    after, _ = _get(run, path)
    left = [x for x in after["labels"] if x.get("name") == name]
    if left:
        msg = f"{len(left)} label(s) named {name} remain"
        raise StepFailureError(msg, "Delete the label by hand in the app.")
    return [
        StepResult(
            "Cleanup",
            passed=True,
            detail=f"deleted {len(own)} label(s) named {name}; none remain",
            seconds=time.monotonic() - started,
        )
    ]


STEPS2: Final = (
    ("Checking the AWS sign-in", first_hour.check_sso),
    ("Creating the researcher", analysis_check.create_researcher),
    ("Signing in", sign_in),
    ("Creating the second researcher", second_researcher),
    ("Reading the targets and warming the routes", pass_two_targets),
    ("Driving R9 to R12 in a headless browser", browser2_rows),
    ("Building and installing the SDK", install_sdk),
    ("R13 in the fresh venv", sdk_rows),
)

NEEDS2: Final = {
    "Creating the researcher": ("Checking the AWS sign-in",),
    "Signing in": ("Creating the researcher",),
    "Creating the second researcher": ("Signing in",),
    "Reading the targets and warming the routes": ("Signing in",),
    "Driving R9 to R12 in a headless browser": (
        "Creating the second researcher",
        "Reading the targets and warming the routes",
    ),
    "R13 in the fresh venv": (
        "Building and installing the SDK",
        "Reading the targets and warming the routes",
        "Signing in",
    ),
}

SUBSTITUTIONS2: Final = (
    "Headless mode. Substitutions in this run:",
    "  - the account emails are .invalid addresses (no inbox, nothing is mailed)",
    "  - sign-in uses the sandbox-test client, not managed login (tokens in",
    "    sessionStorage)",
    "  - R11's second researcher is created by this run and signed in the same way",
    "  - a warm-up reads the units table, the labels route and R4's rates once each;",
    "    their cold walls are recorded and excluded from the row budgets",
    "  - R13 compares with the route's rates after the route's own rounding to",
    "    6 significant digits (rtol 1e-9 holds after it, not on the unrounded values)",
)
SEAMS_TWO: Final = (
    (
        "- R9: the crosshair readout is present and the three planes are drawn;"
        " that the mm values match the file's affine is covered offline"
        " (`web/e2e/mri.spec.ts`), not here."
    ),
    (
        "- R10: the headless label is made by a drag in the Labels lane with the"
        " page's own controls; the run deletes it at the end (Cleanup row)."
    ),
    (
        "- R11: the refusal is read from the page's read-only reason and from a"
        " PATCH by the second account (HTTP 403), not from a click on a disabled"
        " button (the page offers none)."
    ),
    (
        "- The manual rows are the tester's word; a skipped prompt"
        " (`--no-prompt`) is not a pass."
    ),
)
PASS_ONE: Final = Pass(
    1,
    "Research frontend run",
    STEPS,
    SUBSTITUTIONS,
    HUMAN_ROWS,
    "",
    ROW_IDS,
    SEAMS_ONE,
    needs=NEEDS,
)
PASS_TWO: Final = Pass(
    2,
    "Research frontend run (pass 2)",
    STEPS2,
    SUBSTITUTIONS2,
    HUMAN_ROWS2,
    "R10 makes a label of your own on the file: delete it yourself afterwards.",
    PASS_TWO_ROWS,
    SEAMS_TWO,
    BROWSER2,
    cleanup_labels,
    NEEDS2,
)
PASSES: Final = {1: PASS_ONE, 2: PASS_TWO}


def run_live(
    *,
    prompt: bool = True,
    read: Callable[[str], str] = input,
    passes: int = 1,
) -> int:
    """Run the LIVE steps; exit 0 only when every step and every prompt passed."""
    pass_spec = PASSES[passes]
    profile, account = require_sandbox()
    console = Console(headless=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    home = Path(os.environ.get(HOME_ENV) or Path.home() / "asteroidea-rf")
    run = analysis_check.AnalysisRun(console, stamp, home / stamp, profile, account)
    run.base.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    console.say(f"asteroidea research-frontend run {stamp} (pass {pass_spec.number})")
    for line in pass_spec.substitutions:
        console.say(line)
    console.say()
    try:
        complete = analysis_check.run_continuing(run, pass_spec.steps, pass_spec.needs)
    except EOFError, KeyboardInterrupt:
        console.say("Stopped before the end.")
        complete = False
    human: list[StepResult] = []
    if complete and prompt:
        asking = Console(headless=False, read=read)
        human = human_pass(
            asking,
            run.outputs.get("WebUrl", ""),
            pass_spec.human_rows,
            pass_spec.human_hint,
        )
    if pass_spec.finish is not None:
        finish = pass_spec.finish
        run.results.extend(run_step(console, "Cleaning up", lambda: finish(run)))
    seconds = time.monotonic() - started
    console.say()
    for line in format_summary([*run.results, *human]):
        console.say(line)
    console.say()
    console.say(f"Total time: {seconds:.0f} s")
    report = write_report(run, seconds, human=human, pass_spec=pass_spec)
    console.say(f"Report and screenshots: {report}")
    ok = all_passed([*run.results, *human], complete=complete)
    console.say("RF PASS" if ok else "RF FAIL")
    return 0 if ok else 1


USAGE: Final = (
    "usage: python -m asteroidea_infra.research_app_check run [1|2] [--no-prompt]\n"
)


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point: `python -m asteroidea_infra.research_app_check run ...`."""
    args = list(sys.argv[1:] if argv is None else argv)
    no_prompt = "--no-prompt" in args
    rest = [a for a in args if a != "--no-prompt"]
    if rest in (["run"], ["run", "1"]):
        return run_live(prompt=not no_prompt)
    if rest == ["run", "2"]:
        return run_live(prompt=not no_prompt, passes=2)
    sys.stderr.write(USAGE)
    return 2


if __name__ == "__main__":
    sys.exit(main())
