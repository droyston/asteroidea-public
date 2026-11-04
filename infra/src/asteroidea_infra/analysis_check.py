"""`make live-analysis`: a new researcher analyses real data through the SDK.

The run reuses the `make live-first-hour` framework: its `Console`,
`run_step`, `StepResult`, `Report` and the one subprocess helper `execute()`.
It creates the researcher (the `make researcher` logic), builds the SDK wheel,
installs `asteroidea[nwb,nifti]` with numpy and pandas into a fresh venv under
`~/asteroidea-analysis/<run-id>/`, signs in (guided: copy-paste managed login;
`--headless`: the `sandbox-test` client, labelled), then runs the six analysis steps of
`analysis_check_worker.py` in that venv, one process per step, and judges each step's
numbers against its limit. It stops at the first FAIL and writes `report.md`.
"""

import argparse
import contextlib
import io
import math
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from asteroidea_infra import first_hour
from asteroidea_infra.admin import EMAIL_RE, researcher
from asteroidea_infra.runner import (
    REPO,
    SANDBOX_CLIENT_NOTE,
    Run,
    commit_sha,
    execute,
    last_json,
    must,
)
from asteroidea_infra.runner import write_report as write_run_report
from asteroidea_infra.sandbox_guard import require_sandbox
from asteroidea_infra.steps import (
    Console,
    Report,
    StepFailureError,
    StepResult,
    all_passed,
    format_summary,
    run_step,
    suggest_email,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

WORKER: Final = Path(__file__).with_name("analysis_check_worker.py")
HOME_ENV: Final = "ANALYSIS_HOME"
MIB: Final = 1024 * 1024
DANDI: Final = "dandi-000623"
OPENNEURO: Final = "openneuro-ds004798"
LICENCES: Final = {DANDI: "CC-BY-4.0", OPENNEURO: "CC0"}
SUBJECTS: Final = ("sub-CS41", "sub-CS62")
# Exact pins of uv.lock (numpy 2.5.4, pandas 3.1.0), so the venv matches the repo.
EXTRA_PINS: Final = ("numpy==2.5.4", "pandas==3.1.0")


@dataclass(frozen=True)
class Limit:
    """A bound and where it comes from (a measured number, or an estimate)."""

    value: float
    basis: str


# A catalog query is bounded at 10 s; a live run measured sql() at 5.3 s cold and
# 0.26 s warm.
SQL_S: Final = Limit(10.0, "catalog query bound; measured 5.3 s cold")
# Opening a file, its units and one LFP slice: at most 60 s and 64 MiB (an
# estimate; a live run measured 28.1 s and 45.4 MiB).
NWB_S: Final = Limit(60.0, "NWB open, estimated; a run took 28.1 s")
NWB_BYTES: Final = Limit(64 * MIB, "NWB read, estimated; a run took 45.4 MiB")
# Volume 0 of a gzipped image: at most 20 s and 32 MiB (an estimate; a live run
# measured 3.9 s and 4.0 MiB).
GZ_S: Final = Limit(20.0, "gzipped volume, estimated; a run took 3.9 s")
GZ_BYTES: Final = Limit(32 * MIB, "gzipped volume, estimated; a run took 4.0 MiB")
# At least one amygdala unit per subject (an estimate).
MIN_UNITS: Final = Limit(1, "estimated: one amygdala unit per subject")
# scenecut_info.csv has 93 rows.
CUTS: Final = Limit(93, "measured: rows in scenecut_info.csv")
LFP_SHAPE: Final = [1000, 8]
VOLUME_NDIM: Final = 3
SANDBOX_NOTE: Final = SANDBOX_CLIENT_NOTE
HEADLESS_SUBSTITUTIONS: Final = (
    "Headless mode. Substitutions in this run:",
    "  - the account email is an .invalid address (no inbox, nothing is mailed)",
    "  - sign-in uses the sandbox-test client, not managed login",
)


@dataclass
class AnalysisRun(Run):
    """One analysis run: the acceptance run state plus each step's worker output."""

    data: dict[str, dict[str, Any]] = field(default_factory=dict)


def _num(data: Mapping[str, Any], key: str) -> float:
    value = data.get(key)
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    msg = f"worker value {key} is not a number: {value!r}"
    raise StepFailureError(msg, "Report this as a defect.")


def _mapping(data: Mapping[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key)
    return {str(k): v for k, v in value.items()} if isinstance(value, dict) else {}


def _within(seconds: float, limit: Limit) -> tuple[bool, str]:
    ok = seconds <= limit.value
    return ok, f"{seconds:.1f} s (limit {limit.value:.0f} s, {limit.basis})"


def _bytes_within(fetched: float, limit: Limit) -> tuple[bool, str]:
    ok = fetched <= limit.value
    return ok, (
        f"{fetched / MIB:.1f} MiB fetched "
        f"(limit {limit.value / MIB:.0f} MiB, {limit.basis})"
    )


# --- judges ----------------------------------------------------------------------


def judge_datasets(data: Mapping[str, Any]) -> StepResult:
    """Step 1: both datasets listed, each with a title and its licence."""
    found = _mapping(data, "found")
    problems = []
    for name, licence in LICENCES.items():
        row = found.get(name)
        if row is None:
            problems.append(f"{name} missing")
        elif not row.get("title") or row.get("license") != licence:
            problems.append(
                f"{name} title {row.get('title')!r} licence {row.get('license')!r}"
                f" (want {licence})"
            )
    fast, timing = _within(_num(data, "seconds"), SQL_S)
    listed = ", ".join(f"{n} {found[n].get('license')}" for n in sorted(found))
    detail = f"datasets(): {listed}; {timing}"
    if problems:
        detail += "; " + "; ".join(problems)
    return StepResult(
        "1 Datasets", not problems and fast, detail, _num(data, "seconds")
    )


def judge_subjects(data: Mapping[str, Any]) -> StepResult:
    """Step 2: each patient key has subjects in both datasets."""
    by_key = _mapping(data, "by_key")
    parts = []
    ok = bool(by_key)
    for key in sorted(by_key):
        datasets = by_key[key]
        both = set(LICENCES) <= set(datasets)
        ok = ok and both
        names = ", ".join(f"{d}:{'/'.join(s)}" for d, s in sorted(datasets.items()))
        parts.append(f"{key} -> {names or 'none'}")
    fast, timing = _within(_num(data, "seconds"), SQL_S)
    return StepResult(
        "2 Subjects",
        ok and fast,
        f"subject_index: {'; '.join(parts)}; {timing}",
        _num(data, "seconds"),
    )


def judge_units(data: Mapping[str, Any]) -> StepResult:
    """Step 3: at least one amygdala unit for each of CS41 and CS62."""
    per = _mapping(data, "per_subject")
    counts = {name: int(per.get(name, 0)) for name in SUBJECTS}
    ok = all(n >= MIN_UNITS.value for n in counts.values())
    groups = data.get("groups") or []
    shown = ", ".join(f"{g['subject']} {g['bids_session']} {g['n']}" for g in groups)
    fast, timing = _within(_num(data, "seconds"), SQL_S)
    per_text = ", ".join(f"{n} {c}" for n, c in counts.items())
    return StepResult(
        "3 Amygdala units",
        ok and fast,
        f"amygdala units {per_text} (limit >= {MIN_UNITS.value:.0f} each, "
        f"{MIN_UNITS.basis}); by session: {shown}; {timing}",
        _num(data, "seconds"),
    )


def judge_scenecuts(data: Mapping[str, Any]) -> StepResult:
    """Step 4: 93 cuts, every frame inside the timestamps, finite rates per unit."""
    n_cuts = int(_num(data, "n_cuts"))
    n_stamps = int(_num(data, "n_timestamps"))
    outside = int(_num(data, "frames_outside"))
    frames = (
        f"frames {int(_num(data, 'frame_min'))}..{int(_num(data, 'frame_max'))} "
        f"of {n_stamps} timestamps at {data.get('stimulus_path')}"
    )
    units: list[Mapping[str, Any]] = data.get("units") or []
    finite = all(
        math.isfinite(float(u["pre_hz"])) and math.isfinite(float(u["post_hz"]))
        for u in units
    )
    fast, timing = _within(_num(data, "seconds"), NWB_S)
    small, fetched = _bytes_within(_num(data, "fetched"), NWB_BYTES)
    problems = []
    if n_cuts != CUTS.value:
        problems.append(f"{n_cuts} cuts, expected {CUTS.value:.0f} ({CUTS.basis})")
    if outside:
        problems.append(f"{outside} cut frames outside the {n_stamps} timestamps")
    if not outside and not units:
        problems.append("no amygdala unit in this file")
    if not finite:
        problems.append("a rate is not finite")
    if units:
        pre = sum(float(u["pre_hz"]) for u in units) / len(units)
        post = sum(float(u["post_hz"]) for u in units) / len(units)
        rates = (
            f"{len(units)} amygdala units, mean rate {pre:.2f} Hz in [-1, 0) s, "
            f"{post:.2f} Hz in [0, 1) s"
        )
    else:
        rates = "no rates"
    detail = f"{n_cuts} cuts, {frames}; {rates}; {timing}; {fetched}"
    if problems:
        detail += "; " + "; ".join(problems)
    return StepResult(
        "4 Scene cuts", not problems and fast and small, detail, _num(data, "seconds")
    )


def judge_lfp(data: Mapping[str, Any]) -> StepResult:
    """Step 5: `data[0:1000, 0:8]` of the LFP ElectricalSeries has shape (1000, 8)."""
    shape = list(data.get("shape") or [])
    fast, timing = _within(_num(data, "seconds"), NWB_S)
    small, fetched = _bytes_within(_num(data, "fetched"), NWB_BYTES)
    ok = shape == LFP_SHAPE and data.get("finite") is True
    return StepResult(
        "5 LFP slice",
        ok and fast and small,
        f"{data.get('path')} {data.get('series_shape')} -> slice {tuple(shape)} "
        f"(want (1000, 8)), finite {data.get('finite')}; {timing}; {fetched}",
        _num(data, "seconds"),
    )


def judge_fmri(data: Mapping[str, Any]) -> StepResult:
    """Step 6: volume 0 is 3-D with the catalog's `nifti_shape[:3]`."""
    shape = list(data.get("shape") or [])
    want = list(data.get("nifti_shape") or [])[:3]
    fast, timing = _within(_num(data, "seconds"), GZ_S)
    small, fetched = _bytes_within(_num(data, "fetched"), GZ_BYTES)
    ok = len(shape) == len(want) == VOLUME_NDIM and shape == want
    return StepResult(
        "6 fMRI volume",
        ok and fast and small,
        f"{data.get('s3_key')} volume 0 {tuple(shape)} (want {tuple(want)}); "
        f"{timing}; {fetched}",
        _num(data, "seconds"),
    )


# --- set-up steps ----------------------------------------------------------------


def create_researcher(run: AnalysisRun) -> list[StepResult]:
    """`make researcher` logic: create the user, write the run's platform.json."""
    started = time.monotonic()
    suggestion = suggest_email(
        os.environ.get("ASTEROIDEA_BUDGET_EMAIL"),
        datetime.now(UTC),
        headless=run.console.headless,
        tag="ana",
    )
    email = run.console.ask("Email address for the new researcher", suggestion)
    if not EMAIL_RE.match(email):
        msg = f"that is not an email address: {email!r}"
        raise StepFailureError(msg, "Enter an address like name@example.com.")
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            researcher(run.need_session(), email, run.config_dir)
    except SystemExit as err:
        raise StepFailureError(
            str(err.code), "Enter an address that has not been added before."
        ) from err
    run.email = email
    if not run.console.headless:
        run.console.say(
            f"A temporary password is on its way to {email}; "
            "keep it for the sign-in step."
        )
    return [
        StepResult(
            "Researcher",
            passed=True,
            detail=f"Researcher {email} created in group researchers",
            seconds=time.monotonic() - started,
            note="substitution: .invalid address" if run.console.headless else "",
        )
    ]


def build_wheel(out_dir: Path, next_step: str) -> Path:
    """Build the SDK wheel into `out_dir` and return its path."""
    must(
        [
            "uv",
            "build",
            "--package",
            "asteroidea",
            "--wheel",
            "--out-dir",
            str(out_dir),
        ],
        cwd=REPO,
        next_step=next_step,
    )
    return next(out_dir.glob("asteroidea-*.whl"))


def make_venv(venv: Path, wheel: Path, pins: Sequence[str], next_step: str) -> None:
    """Create a fresh venv at `venv` with asteroidea[nwb,nifti] and the exact pins."""
    must(["uv", "venv", "--clear", "-p", "3.14", str(venv)], next_step=next_step)
    must(
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(venv / "bin" / "python"),
            f"asteroidea[nwb,nifti] @ {wheel.as_uri()}",
            *pins,
        ],
        next_step=next_step,
    )


def install(run: AnalysisRun, pins: Sequence[str] = EXTRA_PINS) -> list[StepResult]:
    """Build the SDK wheel; install asteroidea[nwb,nifti] and `pins` in a venv."""
    started = time.monotonic()
    retry = "Fix the error above, then retry."
    wheel = build_wheel(run.base / "wheel", retry)
    make_venv(run.base / "venv", wheel, pins, retry)
    seconds = time.monotonic() - started
    return [
        StepResult(
            "Install",
            passed=True,
            detail=f"asteroidea[nwb,nifti] and {len(pins)} pins installed in "
            f"{seconds:.0f} s",
            seconds=seconds,
        )
    ]


# --- data steps ------------------------------------------------------------------


def worker(run: AnalysisRun, name: str) -> dict[str, Any]:
    """Run one worker step in the venv; its SDK error becomes the FAIL text."""
    done = execute([str(run.python), str(WORKER), name], env=run.child_env)
    data: dict[str, Any] = dict(last_json(done.out))
    error = data.get("error")
    if isinstance(error, str):
        raise StepFailureError(error, "The error above is the SDK's own; report it.")
    if done.code != 0 or not data:
        lines = (done.err or done.out).strip().splitlines()
        msg = f"the {name} step crashed: {lines[-1] if lines else 'no output'}"
        raise StepFailureError(msg, "Report this as a defect of the worker.")
    run.data[name] = data
    return data


def data_step(
    name: str, judge: Callable[[Mapping[str, Any]], StepResult]
) -> Callable[[AnalysisRun], list[StepResult]]:
    """Return the step that runs worker step `name` and judges its numbers."""

    def action(run: AnalysisRun) -> list[StepResult]:
        """Run the worker for this step and return its one judged result."""
        return [judge(worker(run, name))]

    return action


# --- the run ---------------------------------------------------------------------

Step = tuple[str, "Callable[[AnalysisRun], Sequence[StepResult]]"]
STEPS: Final[tuple[Step, ...]] = (
    ("Checking the AWS sign-in", first_hour.check_sso),
    ("Creating the researcher", create_researcher),
    ("Building and installing the SDK", install),
    ("Signing in", first_hour.sign_in),
    ("1. Listing datasets", data_step("datasets", judge_datasets)),
    ("2. Finding patients 41 and 62", data_step("subjects", judge_subjects)),
    ("3. Counting amygdala units", data_step("units", judge_units)),
    ("4. Firing rates around scene cuts", data_step("scenecuts", judge_scenecuts)),
    ("5. Reading an LFP slice", data_step("lfp", judge_lfp)),
    ("6. Reading an fMRI volume", data_step("fmri", judge_fmri)),
)


def run_all(run: AnalysisRun, steps: Sequence[Step] = STEPS) -> bool:
    """Run the steps in order into `run.results`; stop at the first failure.

    Returns True when every step ran.
    """
    for title, action in steps:
        done = run_step(run.console, title, lambda a=action: a(run))
        run.results.extend(done)
        if not all(r.passed for r in done):
            return False
    return True


def run_continuing(
    run: AnalysisRun,
    steps: Sequence[Step],
    needs: Mapping[str, Sequence[str]],
) -> bool:
    """Run every step into `run.results`; a FAIL does not stop the run.

    `needs` maps a step title to the titles whose success it depends on. A step with
    a needed step that failed (or was itself not reached) is not run: it gets a FAIL
    result "not reached: <titles>". Every other step runs, so later rows still
    measure. Returns True once every step has run or been marked; the caller reads
    `run.results` for any FAIL. `run_all` is unchanged for the other runners.
    """
    broken: set[str] = set()
    for title, action in steps:
        blocked = [n for n in needs.get(title, ()) if n in broken]
        if blocked:
            result = StepResult(
                title,
                passed=False,
                detail=f"not reached: {', '.join(blocked)} did not pass",
            )
            run.console.say(result.line())
            run.results.append(result)
            broken.add(title)
            continue
        done = run_step(run.console, title, lambda a=action: a(run))
        run.results.extend(done)
        if not all(r.passed for r in done):
            broken.add(title)
    return True


def unit_table(data: Mapping[str, Any]) -> list[str]:
    """Return the per-unit rate table of step 4 as Markdown lines."""
    units: list[Mapping[str, Any]] = data.get("units") or []
    if not units:
        return []
    lines = [
        "",
        "## Step 4: amygdala units around scene cuts",
        "",
        f"- file: {data.get('nwb_key')} ({data.get('nwb_asset_id')})",
        (
            f"- frame timestamps: {data.get('stimulus_path')} "
            f"({data.get('n_timestamps')} frames); in the file "
            f"{data.get('stimulus_paths_in_file')}, in `series` "
            f"{data.get('stimulus_paths_in_catalog')}"
        ),
        (
            f"- cuts: {data.get('n_cuts')}, first at {data.get('first_cut_s')} s, "
            f"last at {data.get('last_cut_s')} s (`timestamps[shot_start_fr - 1]`)"
        ),
        "",
        (
            "| unit_index | unit_id | location | spikes | rate [-1, 0) s (Hz) "
            "| rate [0, 1) s (Hz) |"
        ),
        "|---|---|---|---|---|---|",
    ]
    lines.extend(
        f"| {u['unit_index']} | {u['unit_id']} | {u['location']} | {u['n_spikes']} "
        f"| {float(u['pre_hz']):.3f} | {float(u['post_hz']):.3f} |"
        for u in units
    )
    return lines


def write_report(run: AnalysisRun) -> Path:
    """Write `report.md`: the steps, the per-unit rates and the ids."""
    report = Report(
        run.run_id,
        "headless" if run.console.headless else "guided",
        commit_sha(),
        title="Researcher analysis run",
    )
    report.results = list(run.results)
    report.ids = {"email": run.email}
    for name, data in run.data.items():
        for key in ("asset_id", "nwb_asset_id", "csv_asset_id", "path", "s3_key"):
            if key in data:
                report.ids[f"{name}.{key}"] = data[key]
    return write_run_report(run, report, unit_table(run.data.get("scenecuts", {})))


def main(argv: Sequence[str] | None = None) -> int:
    """Run the analysis; exit 0 only when every step passed."""
    parser = argparse.ArgumentParser(
        description="Researcher analysis run through the SDK only"
    )
    parser.add_argument(
        "--headless", action="store_true", help="no human; substitutions"
    )
    args = parser.parse_args(argv)
    profile, account = require_sandbox()
    console = Console(headless=args.headless)
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    home = Path(os.environ.get(HOME_ENV) or Path.home() / "asteroidea-analysis")
    run = AnalysisRun(console, stamp, home / stamp, profile, account)
    run.base.mkdir(parents=True, exist_ok=True)
    console.say(f"asteroidea researcher analysis, run {stamp}")
    if args.headless:
        for line in HEADLESS_SUBSTITUTIONS:
            console.say(line)
    console.say()
    try:
        complete = run_all(run)
    except EOFError, KeyboardInterrupt:
        console.say()
        console.say("Stopped before the end.")
        complete = False
    console.say()
    for line in format_summary(run.results):
        console.say(line)
    console.say()
    console.say(f"Full report with per-unit rates and ids: {write_report(run)}")
    ok = all_passed(run.results, complete=complete)
    console.say("ANALYSIS PASS" if ok else "ANALYSIS FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
