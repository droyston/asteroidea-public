"""`make live-web-smoke` (LIVE): every page and tab of the hosted app, headless.

Run it after every `make web-publish`. Steps, each printing PASS or FAIL: the AWS
sign-in check, a new researcher, the `sandbox-test` sign-in (stand-in for
managed login), the targets (the NWB file, a NIfTI file, every catalog dataset, read
from the API), then a headless Playwright run (`web/e2e/live/smoke.spec.ts`,
1440 x 900 and 375 x 812, tokens placed in `sessionStorage`, labelled) that reaches
each page by user navigation from `/`.
The spec only measures: per step it records console errors, uncaught page errors, failed
requests, HTTP statuses of 400 or more, error Notices and data views that draw empty
where the API says rows exist. This module judges them: any such issue fails the step,
except a 404 the step table lists as expected. A step the spec never reached fails.
`report.md`, `smoke.json` and one screenshot per step go to
`~/asteroidea-smoke/<run-id>/`.
"""

import json
import os
import re
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
    from collections.abc import Mapping, Sequence

HOME_ENV: Final = "SMOKE_HOME"
SPEC: Final = "e2e/live/smoke.spec.ts"
CONFIG: Final = "playwright.live.config.ts"
SPEC_TIMEOUT_S: Final = 3000.0
VIEWPORTS: Final = ("desktop", "phone")
TARGETS_FILE: Final = "targets.json"
HTTP_ERROR: Final = 400
NOT_FOUND: Final = 404
ABORTED: Final = "net::ERR_ABORTED"
KINDS: Final = ("console", "pageerror", "requestfailed", "http", "notice", "empty")
SUBSTITUTIONS: Final = (
    "Headless mode. Substitutions in this run:",
    "  - the account email is an .invalid address (no inbox, nothing is mailed)",
    "  - sign-in uses the sandbox-test client, not managed login; the tokens are",
    "    placed in sessionStorage before each page loads",
)
TOKENS_NOTE: Final = "substitution: tokens placed in sessionStorage"
DATASET_PAGE: Final = "Dataset page "


@dataclass(frozen=True)
class SmokeStep:
    """A step the spec must reach, and the 404 URLs (regexes) it may see."""

    name: str
    allowed_404: tuple[str, ...] = ()


# Every step but the per-dataset pages (named `Dataset page <id> <section>`, at least
# one required). An empty `allowed_404` means no 4xx or 5xx is expected in the step.
STEPS: Final = (
    SmokeStep("Discover"),
    SmokeStep("Dataset detail pane"),
    SmokeStep("Data list"),
    SmokeStep("Subject page"),
    SmokeStep("File Overview"),
    SmokeStep("File Units"),
    SmokeStep("File Electrodes"),
    SmokeStep("File Series"),
    SmokeStep("File Events"),
    SmokeStep("File LFP overview"),
    SmokeStep("File LFP window"),
    SmokeStep("NIfTI Overview"),
    SmokeStep("NIfTI Image"),
    SmokeStep("Session page"),
    SmokeStep("Session Units"),
    SmokeStep("Session Tuning"),
    SmokeStep("Session Events"),
    SmokeStep("Session Files"),
    SmokeStep("Session Results"),
    SmokeStep("Second dataset session"),
    SmokeStep("My results"),
    SmokeStep("Get started"),
)
STEP_NAMES: Final = tuple(s.name for s in STEPS)
BY_NAME: Final = {s.name: s for s in STEPS}
# Required only when `make web-smoke RS_RESULT_FILE=<file>` is given.
RESULT_FILE_ENV: Final = "RS_RESULT_FILE"
RESULT_STEPS: Final = (SmokeStep("Result page"), SmokeStep("Result file page"))


def steps_for(environ: Mapping[str, str]) -> dict[str, SmokeStep]:
    """Return the run's step table: the always-on steps, plus the result steps."""
    table = dict(BY_NAME)
    if environ.get(RESULT_FILE_ENV, ""):
        table.update({s.name: s for s in RESULT_STEPS})
    return table


@dataclass(frozen=True)
class Failure:
    """One reason a step failed."""

    kind: str
    text: str
    url: str = ""

    def line(self) -> str:
        """Return the failure as one report line."""
        where = f" ({self.url})" if self.url else ""
        return f"{self.kind}: {self.text}{where}"


def classify(
    issue: Mapping[str, Any], allowed_404: Sequence[str] = ()
) -> Failure | None:
    """Return the failure an issue is, or None when it is expected or benign.

    Expected: a 404 whose URL matches a step's `allowed_404`; a request the page itself
    cancelled (`net::ERR_ABORTED`, a fetch dropped by navigation). Every other
    console error, page error, failed request, status of 400 or more, error Notice and
    empty view fails.
    """
    kind = str(issue.get("kind", ""))
    text = str(issue.get("text", "")).strip()
    url = str(issue.get("url", ""))
    status = issue.get("status", 0)
    if kind not in KINDS:
        return Failure("unknown-issue", f"{kind!r}: {text}", url)
    if kind == "requestfailed" and text == ABORTED:
        return None
    if (
        kind == "http"
        and status == NOT_FOUND
        and any(re.search(p, url) for p in allowed_404)
    ):
        return None
    if kind == "http" and not (isinstance(status, int) and status >= HTTP_ERROR):
        return None
    return Failure(kind, text, url)


def step_name(record: Mapping[str, Any]) -> str:
    """Return the report name of a record: `<viewport> <step>`."""
    return f"{record.get('viewport', '?')} {record.get('step', '?')}"


def judge_record(
    record: Mapping[str, Any], table: Mapping[str, SmokeStep] = BY_NAME
) -> StepResult:
    """Judge one recorded step: it ran, and it saw no failing issue."""
    name = step_name(record)
    known = table.get(str(record.get("step", "")))
    allowed = known.allowed_404 if known is not None else ()
    seconds = float(record.get("seconds", 0.0))
    detail = str(record.get("detail", ""))
    shot = str(record.get("screenshot", ""))
    failures = [
        f
        for f in (classify(i, allowed) for i in record.get("issues") or [])
        if f is not None
    ]
    if record.get("ok") is not True:
        failures.insert(0, Failure("step", detail or "the step did not complete"))
    if not failures:
        return StepResult(name, passed=True, detail=detail, seconds=seconds)
    page = str(record.get("step", "?"))
    body = "; ".join(f.line() for f in failures)
    return StepResult(
        name,
        passed=False,
        detail=f"page {page}, viewport {record.get('viewport', '?')}: {body}; "
        f"screenshot {shot}",
        seconds=seconds,
    )


def judge_all(
    records: Sequence[Mapping[str, Any]],
    table: Mapping[str, SmokeStep] = BY_NAME,
) -> list[StepResult]:
    """Judge every record, then fail each required step a viewport never reached."""
    results = [judge_record(r, table) for r in records]
    for view in VIEWPORTS:
        names = [str(r.get("step", "")) for r in records if r.get("viewport") == view]
        results.extend(
            StepResult(
                f"{view} {name}",
                passed=False,
                detail="the browser run did not reach this step",
            )
            for name in table
            if name not in names
        )
        if not any(n.startswith(DATASET_PAGE) for n in names):
            results.append(
                StepResult(
                    f"{view} {DATASET_PAGE.strip()}",
                    passed=False,
                    detail="the browser run reached no catalog dataset page",
                )
            )
    return results


def parse_records(texts: Mapping[str, str]) -> list[dict[str, Any]]:
    """Return the spec's records from `{viewport: smoke-<viewport>.json text}`.

    A viewport with no file contributes nothing, so `judge_all` fails its steps.
    """
    records: list[dict[str, Any]] = []
    for view, text in texts.items():
        try:
            data = json.loads(text)
        except ValueError as err:
            msg = f"smoke-{view}.json is not JSON: {err}"
            raise StepFailureError(msg, "Read browser.log beside it.") from err
        if not isinstance(data, list):
            msg = f"smoke-{view}.json is not a list"
            raise StepFailureError(msg, "Read browser.log beside it.")
        records.extend(r for r in data if isinstance(r, dict))
    return records


# --- LIVE steps ----------------------------------------------------------------------


def _asset(run: analysis_check.AnalysisRun, asset_id: str) -> dict[str, str]:
    """Return `{asset, dataset, subject}` for one asset id, from the catalog."""
    body, _ = api_get(run, f"/catalog/assets/{asset_id}")
    row = body["asset"]
    dataset, subject = row.get("dataset"), row.get("subject")
    if not isinstance(dataset, str) or not isinstance(subject, str):
        msg = f"asset {asset_id} has no dataset or subject in the catalog"
        raise StepFailureError(msg, "Check the file is imported.")
    return {"asset": asset_id, "dataset": dataset, "subject": subject}


NIFTI_PREFERRED: Final = ("openneuro-ds004473", "openneuro-ds004798")
NIFTI_SKIPPED_PREFIXES: Final = ("artifact-", "sample-", "sdk-live-")


def nifti_order(datasets: list[str]) -> list[str]:
    """Return the datasets a NIfTI target may come from, preferred first.

    Test datasets (`artifact-`, `sample-`, `sdk-live-`) are left out: they stay swept as
    dataset pages, but a target in one may be reachable only through a missing tab.
    """
    real = [d for d in datasets if not d.startswith(NIFTI_SKIPPED_PREFIXES)]
    first = [d for d in NIFTI_PREFERRED if d in real]
    return first + [d for d in real if d not in first]


def anatomical(assets: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the first asset whose path is in an `/anat/` folder, else None."""
    for row in assets:
        if "/anat/" in f"/{row.get('path', '')}":
            return row
    return None


def _pick_nifti(
    run: analysis_check.AnalysisRun, datasets: list[str]
) -> dict[str, str] | None:
    """Return the anatomical NIfTI target of the first fitting dataset, or None."""
    for dataset in nifti_order(datasets):
        found, _ = api_get(run, f"/catalog/datasets/{dataset}/assets?format=nifti")
        row = anatomical(found["assets"])
        if row is not None:
            return _asset(run, str(row["asset_id"]))
    return None


def find_targets(run: analysis_check.AnalysisRun) -> list[StepResult]:
    """Read the NWB file, a NIfTI file and the dataset list; write `targets.json`."""
    started = time.monotonic()
    body, _ = api_get(run, "/catalog/datasets")
    datasets = [str(d["dataset"]) for d in body["datasets"]]
    nifti = _pick_nifti(run, datasets)
    targets = {
        "datasets": datasets,
        "nwb": _asset(run, CS62_R2),
        "nifti": nifti,
    }
    (run.base / TARGETS_FILE).write_text(json.dumps(targets, indent=2))
    got = (
        "no NIfTI file (the step will fail)"
        if nifti is None
        else f"NIfTI {nifti['asset']}"
    )
    return [
        StepResult(
            "Targets",
            passed=True,
            detail=f"{len(datasets)} datasets; NWB {CS62_R2}; {got}",
            seconds=time.monotonic() - started,
        )
    ]


def browser_dir(run: analysis_check.AnalysisRun) -> Path:
    """Return where the spec writes its records and screenshots."""
    return run.base / "browser"


def browser_env(run: analysis_check.AnalysisRun) -> dict[str, str]:
    """Return the spec's environment: the URLs, the tokens and the targets file."""
    result_file = os.environ.get(RESULT_FILE_ENV, "")
    return {
        **os.environ,
        # The spec runs in `web/`, so a repo-root relative path is made absolute here.
        "SMOKE_RS_RESULT_FILE": (
            str(Path(result_file).expanduser().resolve()) if result_file else ""
        ),
        "PLAYWRIGHT_BROWSERS_PATH": browsers_path(),
        "SMOKE_APP_URL": run.outputs["WebUrl"],
        "SMOKE_API_URL": run.outputs["ApiUrl"],
        "SMOKE_ACCESS_TOKEN": run.outputs["access_token"],
        "SMOKE_REFRESH_TOKEN": run.outputs["refresh_token"],
        "SMOKE_TARGETS_FILE": str(run.base / TARGETS_FILE),
        "SMOKE_OUT": str(browser_dir(run)),
        "RF_OUT": str(browser_dir(run)),
    }


def browser_steps(run: analysis_check.AnalysisRun) -> list[StepResult]:
    """Run the spec headless at both sizes; judge every recorded step."""
    out = browser_dir(run)
    out.mkdir(parents=True, exist_ok=True)
    env = browser_env(run)
    if env["SMOKE_RS_RESULT_FILE"] and not Path(env["SMOKE_RS_RESULT_FILE"]).is_file():
        msg = f"{RESULT_FILE_ENV} names no file: {env['SMOKE_RS_RESULT_FILE']}"
        raise StepFailureError(
            msg, "Run make live T=tests/live/test_research_synthesis.py first."
        )
    ensure_browser(env)
    done = execute(
        ["./node_modules/.bin/playwright", "test", "-c", CONFIG, SPEC],
        cwd=REPO / "web",
        env=env,
        timeout=SPEC_TIMEOUT_S,
    )
    (out / "browser.log").write_text(done.out + done.err)
    texts = {
        view: (out / f"smoke-{view}.json").read_text()
        for view in VIEWPORTS
        if (out / f"smoke-{view}.json").is_file()
    }
    if not texts:
        lines = (done.err or done.out).strip().splitlines()
        msg = f"the browser run wrote no smoke-*.json: {lines[-1] if lines else 'none'}"
        raise StepFailureError(msg, f"Read {out / 'browser.log'}.")
    records = parse_records(texts)
    (run.base / "smoke.json").write_text(json.dumps(records, indent=2))
    return judge_all(records, steps_for(os.environ))


STEPS_LIVE: Final = (
    ("Checking the AWS sign-in", first_hour.check_sso),
    ("Creating the researcher", analysis_check.create_researcher),
    ("Signing in", sign_in),
    ("Finding the files to visit", find_targets),
    ("Visiting every page and tab in a headless browser", browser_steps),
)


NEEDS: Final = {
    "Creating the researcher": ("Checking the AWS sign-in",),
    "Signing in": ("Creating the researcher",),
    "Finding the files to visit": ("Signing in",),
    "Visiting every page and tab in a headless browser": (
        "Finding the files to visit",
    ),
}


def write_report(run: analysis_check.AnalysisRun, seconds: float) -> Path:
    """Write `report.md`: PASS or FAIL per step, the failures in full, the wall time."""
    report = Report(run.run_id, "headless", commit_sha(), title="Web smoke run")
    report.results = list(run.results)
    report.ids = {
        "email": run.email,
        "app": run.outputs.get("WebUrl", ""),
        "run": str(run.base),
        "total": f"{seconds:.0f} s",
        "note": TOKENS_NOTE,
    }
    failed = [r for r in run.results if not r.passed]
    lines = ["", "## Failures", ""]
    lines.extend(f"- {r.name}: {r.detail}" for r in failed)
    if not failed:
        lines.append("None.")
    return write_run_report(run, report, lines)


def run_live() -> int:
    """Run the LIVE steps; exit 0 only when every step passed."""
    profile, account = require_sandbox()
    console = Console(headless=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    home = Path(os.environ.get(HOME_ENV) or Path.home() / "asteroidea-smoke")
    run = analysis_check.AnalysisRun(console, stamp, home / stamp, profile, account)
    run.base.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    console.say(f"asteroidea web smoke run {stamp}")
    for line in SUBSTITUTIONS:
        console.say(line)
    console.say()
    try:
        complete = analysis_check.run_continuing(run, STEPS_LIVE, NEEDS)
    except EOFError, KeyboardInterrupt:
        console.say("Stopped before the end.")
        complete = False
    seconds = time.monotonic() - started
    console.say()
    for line in format_summary(run.results):
        console.say(line)
    console.say()
    console.say(f"Total time: {seconds:.0f} s")
    console.say(f"Report and screenshots: {write_report(run, seconds)}")
    ok = all_passed(run.results, complete=complete)
    console.say("SMOKE PASS" if ok else "SMOKE FAIL")
    return 0 if ok else 1


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point: `python -m asteroidea_infra.web_smoke run`."""
    args = list(sys.argv[1:] if argv is None else argv)
    if args == ["run"]:
        return run_live()
    sys.stderr.write("usage: python -m asteroidea_infra.web_smoke run\n")
    return 2


if __name__ == "__main__":
    sys.exit(main())
