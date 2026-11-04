"""`make live-ux-playtest` (LIVE): the UX play-test, headless then guided.

One command, in two parts. Part one is headless and user-inputs-only on the hosted app:
a new researcher signs in with the sandbox-test client (stand-in for
managed login; the tokens are placed in `sessionStorage` before each page loads), then
`web/e2e/ux-playtest.spec.ts` walks the acceptance flow
(rows 2, 4, 5, 7 and 8) at 1440 x 900 and 375 x 812 by clicking links, and
records per page the wait to the shell's ready mark and each API request's time. This
module prints those against about 5 s as `miss` lines: a miss is a reading, never a
FAIL. A page the walk never reached, or a failed check, is a FAIL.

Part two prompts for the human-only steps (unless `--no-prompt`): row 1 (managed login),
row 3 (squint test 1) and row 6 (squint test 2), each PASS or FAIL on the tester's word.
A skipped prompt is not a pass; the report says so.

Screenshots, the records, `ux-playtest.log` and `report.md` go to
`~/asteroidea-playtest/<timestamp>/`.
"""

import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from asteroidea_infra import analysis_check, first_hour
from asteroidea_infra.live_api import browsers_path, ensure_browser
from asteroidea_infra.research_app_check import CS62_R2
from asteroidea_infra.runner import REPO, api_get, commit_sha, execute, sign_in
from asteroidea_infra.runner import write_report as write_run_report
from asteroidea_infra.sandbox_guard import require_sandbox
from asteroidea_infra.steps import (
    Console,
    Report,
    StepFailureError,
    StepResult,
    all_passed,
    format_summary,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

HOME_ENV: Final = "UX_PLAYTEST_HOME"
SPEC: Final = "e2e/ux-playtest.spec.ts"
CONFIG: Final = "playwright.live.config.ts"
SPEC_TIMEOUT_S: Final = 1500.0
VIEWPORTS: Final = ("desktop", "phone")
TARGETS_FILE: Final = "targets.json"
LOG_FILE: Final = "ux-playtest.log"
BUDGET_S: Final = 5.0
NO_SESSION: Final = "_"
PAGES: Final = (
    "home",
    "study",
    "session",
    "find",
    "results",
    "setup",
    "file",
    "uploads",
)
SUBSTITUTIONS: Final = (
    "Headless mode. Substitutions in this run:",
    "  - the account email is an .invalid address (no inbox, nothing is mailed)",
    "  - sign-in uses the sandbox-test client, not managed login; the tokens are",
    "    placed in sessionStorage before each page loads (row 1 is the tester's)",
)
TOKENS_NOTE: Final = "substitution: tokens placed in sessionStorage"


@dataclass(frozen=True)
class HumanStep:
    """A step only a person can judge: the row, what to do and what to compare."""

    row: str
    title: str
    budget: str
    lines: tuple[str, ...]


HUMAN_STEPS: Final = (
    HumanStep(
        "Row 1",
        "sign-in by managed login",
        "10 s",
        (
            "Open the app in your own browser and sign in with managed login.",
            "Return to the app.",
        ),
    ),
    HumanStep(
        "Row 3",
        "squint test 1",
        "30 s",
        (
            "Look at the home. Name the studies, their modalities and the participants",
            "that have both MRI and ephys. Compare with the route's answer above.",
        ),
    ),
    HumanStep(
        "Row 6",
        "squint test 2",
        "30 s",
        (
            "Open the CS62 R2 session, scene cuts. Name three units that fire more,",
            "three that fire less, and the baseline. Compare with the page's own",
            "classification and its baseline note.",
        ),
    ),
)
QUESTIONS: Final = {
    "Row 1": "signed in and landed on the home?",
    "Row 3": "your answer matches the route's answer?",
    "Row 6": "your answer matches the page's classification and baseline?",
}


# --- pure parts ----------------------------------------------------------------------


def parse_records(texts: Mapping[str, str]) -> list[dict[str, Any]]:
    """Return the spec's page records from `{viewport: ux-<viewport>.json text}`."""
    records: list[dict[str, Any]] = []
    for view, text in texts.items():
        try:
            data = json.loads(text)
        except ValueError as err:
            msg = f"ux-{view}.json is not JSON: {err}"
            raise StepFailureError(msg, "Read browser.log beside it.") from err
        if not isinstance(data, list):
            msg = f"ux-{view}.json is not a list"
            raise StepFailureError(msg, "Read browser.log beside it.")
        records.extend(r for r in data if isinstance(r, dict))
    return records


def miss_lines(
    records: Sequence[Mapping[str, Any]], budget: float = BUDGET_S
) -> list[str]:
    """Return one `miss` line per page wait or API request over `budget` seconds.

    A miss is a reading: the caller prints it and never turns it into a FAIL.
    """
    lines: list[str] = []
    for rec in records:
        where = f"{rec.get('viewport', '?')} {rec.get('page', '?')}"
        wait = float(rec.get("wait_s", 0.0))
        if rec.get("reached") is True and wait > budget:
            lines.append(
                f"miss  {where}: {wait:.1f} s to data-ready="
                f'"{rec.get("ready", "?")}" (budget {budget:.0f} s)'
            )
        lines.extend(
            f"miss  {where}: {req.get('method', 'GET')} {req.get('path', '?')}"
            f" took {float(req.get('ms', 0)) / 1000:.1f} s"
            f" (status {req.get('status', '?')}, budget {budget:.0f} s)"
            for req in rec.get("requests") or []
            if float(req.get("ms", 0)) / 1000 > budget
        )
    return lines


def timing_lines(records: Sequence[Mapping[str, Any]]) -> list[str]:
    """Return one line per page: wait, request count, slowest request."""
    lines = []
    for rec in records:
        reqs = rec.get("requests") or []
        slowest = max((float(r.get("ms", 0)) for r in reqs), default=0.0) / 1000
        wait = (
            f"{float(rec.get('wait_s', 0.0)):.2f} s"
            if rec.get("reached") is True
            else "not reached"
        )
        lines.append(
            f"{rec.get('viewport', '?'):<8}{rec.get('page', '?'):<9}{wait:<13}"
            f"{len(reqs):>3} requests, slowest {slowest:.2f} s,"
            f" via {rec.get('via', '?')}"
        )
    return lines


def judge_page(rec: Mapping[str, Any]) -> StepResult:
    """Judge one record: the page reached its ready mark and every check held."""
    name = f"{rec.get('viewport', '?')} {rec.get('page', '?')}"
    wait = float(rec.get("wait_s", 0.0))
    shot = str(rec.get("screenshot", ""))
    if rec.get("reached") is not True:
        return StepResult(
            name,
            passed=False,
            detail=f"never reached data-ready={rec.get('ready', '?')!r};"
            f" screenshot {shot}",
            seconds=wait,
        )
    failed = [c for c in rec.get("checks") or [] if c.get("ok") is not True]
    if failed:
        body = "; ".join(
            f"{c.get('name', '?')} ({c.get('detail', '')})" for c in failed
        )
        return StepResult(
            name,
            passed=False,
            detail=f"{body}; screenshot {shot}",
            seconds=wait,
        )
    return StepResult(name, passed=True, detail=f"ready in {wait:.2f} s", seconds=wait)


def judge_all(records: Sequence[Mapping[str, Any]]) -> list[StepResult]:
    """Judge every record, then fail each page a viewport never recorded."""
    results = [judge_page(r) for r in records]
    for view in VIEWPORTS:
        seen = {str(r.get("page", "")) for r in records if r.get("viewport") == view}
        results.extend(
            StepResult(
                f"{view} {page}",
                passed=False,
                detail="the browser run did not reach this page",
            )
            for page in PAGES
            if page not in seen
        )
    return results


def expected_answer(studies: Mapping[str, Any], links: Mapping[str, Any]) -> list[str]:
    """Return the route's answer for the squint test 1 prompt, as plain lines."""
    lines = ["The routes answer:"]
    for study in studies.get("studies") or []:
        roles = ", ".join(
            f"{d.get('dataset', '?')} ({d.get('role', '?')})"
            for d in study.get("datasets") or []
        )
        lines.append(f"  study {study.get('study', '?')}: {study.get('title', '?')}")
        lines.append(f"    datasets: {roles}")
    by_subject: dict[str, list[str]] = {}
    for link in links.get("links") or []:
        key = str(link.get("subject_key") or link.get("subject", "?"))
        by_subject.setdefault(key, []).append(
            f"{link.get('dataset', '?')} {link.get('status', '?')}"
        )
    lines.extend(
        f"  participant {key}: {', '.join(rows)}"
        for key, rows in sorted(by_subject.items())
    )
    return lines


def human_pass(
    console: Console, expected: Sequence[str], steps: Sequence[HumanStep] = HUMAN_STEPS
) -> list[StepResult]:
    """Prompt for each human-only step; PASS only on a yes (console not headless)."""
    console.say()
    console.say("Now the steps only you can judge, in your own browser.")
    results = []
    for step in steps:
        started = time.monotonic()
        console.say()
        console.say(f"{step.row}: {step.title}  [budget: {step.budget}]")
        for line in step.lines:
            console.say(f"  {line}")
        if step.row == "Row 3":
            for line in expected:
                console.say(f"  {line}")
        ok = console.confirm(QUESTIONS[step.row], default=False)
        results.append(
            StepResult(
                f"{step.row} (tester) {step.title}",
                passed=ok,
                detail="tester answered y"
                if ok
                else "tester answered n or gave no answer",
                seconds=time.monotonic() - started,
                note="tester's own browser",
            )
        )
    return results


# --- LIVE steps ----------------------------------------------------------------------


def find_targets(run: analysis_check.AnalysisRun) -> list[StepResult]:
    """Read the studies and links routes and the CS62 R2 asset; write `targets.json`."""
    started = time.monotonic()
    studies, _ = api_get(run, "/catalog/studies")
    links, _ = api_get(run, "/catalog/links")
    asset, _ = api_get(run, f"/catalog/assets/{CS62_R2}")
    row = asset["asset"]
    dataset, subject = row.get("dataset"), row.get("subject")
    if not isinstance(dataset, str) or not isinstance(subject, str):
        msg = f"asset {CS62_R2} has no dataset or subject in the catalog"
        raise StepFailureError(msg, "Check the file is imported.")
    session = row.get("bids_session")
    owner = next(
        (
            s
            for s in studies.get("studies") or []
            if any(d.get("dataset") == dataset for d in s.get("datasets") or [])
        ),
        None,
    )
    if owner is None:
        msg = f"no study contains dataset {dataset}"
        raise StepFailureError(msg, "Check /catalog/studies lists it.")
    targets = {
        "study": str(owner["study"]),
        "title": str(owner["title"]),
        "titles": [str(s["title"]) for s in studies.get("studies") or []],
        "dataset": dataset,
        "subject": subject,
        "session": session if isinstance(session, str) and session else NO_SESSION,
        "asset": CS62_R2,
        "expected": expected_answer(studies, links),
    }
    (run.base / TARGETS_FILE).write_text(json.dumps(targets, indent=2))
    return [
        StepResult(
            "Targets",
            passed=True,
            detail=f"study {targets['study']};"
            f" session {dataset}/{subject}/{targets['session']}",
            seconds=time.monotonic() - started,
        )
    ]


def browser_dir(run: analysis_check.AnalysisRun) -> Path:
    """Return where the spec writes its records and screenshots."""
    return run.base / "browser"


def browser_env(run: analysis_check.AnalysisRun) -> dict[str, str]:
    """Return the spec's environment: LIVE=1, the URLs, the tokens and the targets."""
    return {
        **os.environ,
        "LIVE": "1",
        "PLAYWRIGHT_BROWSERS_PATH": browsers_path(),
        "UX_APP_URL": run.outputs["WebUrl"],
        "UX_API_URL": run.outputs["ApiUrl"],
        "UX_ACCESS_TOKEN": run.outputs["access_token"],
        "UX_REFRESH_TOKEN": run.outputs["refresh_token"],
        "UX_TARGETS_FILE": str(run.base / TARGETS_FILE),
        "UX_OUT": str(browser_dir(run)),
        "RF_OUT": str(browser_dir(run)),
    }


def browser_steps(run: analysis_check.AnalysisRun) -> list[StepResult]:
    """Run the spec headless at both sizes; print the readings; judge every page."""
    out = browser_dir(run)
    out.mkdir(parents=True, exist_ok=True)
    env = browser_env(run)
    ensure_browser(env)
    done = execute(
        ["./node_modules/.bin/playwright", "test", "-c", CONFIG, SPEC],
        cwd=REPO / "web",
        env=env,
        timeout=SPEC_TIMEOUT_S,
    )
    (out / "browser.log").write_text(done.out + done.err)
    texts = {
        view: (out / f"ux-{view}.json").read_text()
        for view in VIEWPORTS
        if (out / f"ux-{view}.json").is_file()
    }
    if not texts:
        lines = (done.err or done.out).strip().splitlines()
        msg = f"the browser run wrote no ux-*.json: {lines[-1] if lines else 'none'}"
        raise StepFailureError(msg, f"Read {out / 'browser.log'}.")
    records = parse_records(texts)
    (run.base / "ux.json").write_text(json.dumps(records, indent=2))
    run.console.say()
    run.console.say(f"Readings (budget about {BUDGET_S:.0f} s; a miss is not a FAIL):")
    for line in timing_lines(records):
        run.console.say(f"  {line}")
    misses = miss_lines(records)
    for line in misses or ["no misses"]:
        run.console.say(f"  {line}")
    (run.base / "misses.txt").write_text("\n".join(misses) + "\n")
    return judge_all(records)


STEPS_LIVE: Final = (
    ("Checking the AWS sign-in", first_hour.check_sso),
    ("Creating the researcher", analysis_check.create_researcher),
    ("Signing in", sign_in),
    ("Finding the study and session to visit", find_targets),
    ("Walking the pages in a headless browser", browser_steps),
)

NEEDS: Final = {
    "Creating the researcher": ("Checking the AWS sign-in",),
    "Signing in": ("Creating the researcher",),
    "Finding the study and session to visit": ("Signing in",),
    "Walking the pages in a headless browser": (
        "Finding the study and session to visit",
    ),
}


def write_report(
    run: analysis_check.AnalysisRun,
    seconds: float,
    owner: Sequence[StepResult] = (),
    *,
    asked: bool,
) -> Path:
    """Write `report.md`: PASS or FAIL per step, the misses, the failures, the time."""
    report = Report(run.run_id, "headless", commit_sha(), title="UX play-test run")
    report.results = [*run.results, *owner]
    report.ids = {
        "email": run.email,
        "app": run.outputs.get("WebUrl", ""),
        "run": str(run.base),
        "total": f"{seconds:.0f} s",
        "note": TOKENS_NOTE,
    }
    misses_file = run.base / "misses.txt"
    misses = misses_file.read_text().splitlines() if misses_file.is_file() else []
    failed = [r for r in report.results if not r.passed]
    lines = ["", "## Misses (readings against the budget, not failures)", ""]
    lines.extend(f"- {m}" for m in misses if m)
    if not any(misses):
        lines.append("None.")
    lines.extend(["", "## Failures", ""])
    lines.extend(f"- {r.name}: {r.detail}" for r in failed)
    if not failed:
        lines.append("None.")
    if not asked:
        lines.extend(
            [
                "",
                "## Unverified seams",
                "",
                (
                    "- Rows 1, 3 and 6 were not asked (`NO_PROMPT=1`); a skipped"
                    " prompt is not a pass."
                ),
            ]
        )
    return write_run_report(run, report, lines)


def tee(path: Path) -> Callable[[str], None]:
    """Return a `write` that prints a line and appends it to the log file."""

    def write(line: str) -> None:
        sys.stdout.write(line + "\n")
        with path.open("a") as log:
            log.write(line + "\n")

    return write


def run_live(*, prompt: bool = True, read: Callable[[str], str] = input) -> int:
    """Run the LIVE steps; exit 0 only when every step and every prompt passed."""
    profile, account = require_sandbox()
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    home = Path(os.environ.get(HOME_ENV) or Path.home() / "asteroidea-playtest")
    base = home / stamp
    base.mkdir(parents=True, exist_ok=True)
    write = tee(base / LOG_FILE)
    console = Console(headless=True, write=write)
    run = analysis_check.AnalysisRun(console, stamp, base, profile, account)
    started = time.monotonic()
    console.say(f"asteroidea UX play-test {stamp}")
    for line in SUBSTITUTIONS:
        console.say(line)
    console.say()
    try:
        complete = analysis_check.run_continuing(run, STEPS_LIVE, NEEDS)
    except EOFError, KeyboardInterrupt:
        console.say("Stopped before the end.")
        complete = False
    owner: list[StepResult] = []
    asked = complete and prompt
    if asked:
        file = base / TARGETS_FILE
        targets = json.loads(file.read_text()) if file.is_file() else {}
        asking = Console(headless=False, read=read, write=write)
        asking.say(f"Open {run.outputs.get('WebUrl', '')} in your own browser.")
        owner = human_pass(asking, targets.get("expected") or [])
    seconds = time.monotonic() - started
    console.say()
    for line in format_summary([*run.results, *owner]):
        console.say(line)
    console.say()
    console.say(f"Total time: {seconds:.0f} s")
    console.say(
        f"Report, log and screenshots: {write_report(run, seconds, owner, asked=asked)}"
    )
    ok = all_passed([*run.results, *owner], complete=complete)
    console.say("UX PLAYTEST PASS" if ok else "UX PLAYTEST FAIL")
    return 0 if ok else 1


USAGE: Final = "usage: python -m asteroidea_infra.ux_playtest run [--no-prompt]\n"


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point: `python -m asteroidea_infra.ux_playtest run [--no-prompt]`."""
    args = list(sys.argv[1:] if argv is None else argv)
    no_prompt = "--no-prompt" in args
    rest = [a for a in args if a != "--no-prompt"]
    if rest == ["run"]:
        return run_live(prompt=not no_prompt)
    sys.stderr.write(USAGE)
    return 2


if __name__ == "__main__":
    sys.exit(main())
