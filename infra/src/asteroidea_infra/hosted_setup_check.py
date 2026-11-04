"""`make live-hosted-setup` (LIVE): the outsider's first hour, headless.

Steps, each printing PASS or FAIL: the AWS sign-in check, a new researcher, the
`sandbox-test` sign-in (stand-in for managed login), `GET /get-started`,
the command it shows (`uvx --from <wheel url> asteroidea-setup <hosted url>`) run with
`--answers <file>` in a fresh temporary `HOME` (repo off `PATH`, working directory
outside the repo, the sign-in token cache placed where `asteroidea-setup` reads a cached
sign-in; prompts answered from a file, labelled), and `nbconvert --execute` on the
downloaded notebook with the venv's interpreter.
`run PASS=2` replaces the notebook step with `asteroidea-setup --pass2`
in the same fresh `HOME`: its human rows are answered from a file, labelled, and the
SDK rows (save, list, download, ownership refusal) run for real.
`report.md` and the executed copy go to `~/asteroidea-outsider/<run-id>/`.
"""

import json
import os
import shlex
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final

from asteroidea._auth import CACHE_NAME, TokenCache
from asteroidea_infra import analysis_check, first_hour, notebook
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

HOME_ENV: Final = "OUTSIDER_HOME"
SETUP_TIMEOUT_S: Final = 1800.0
COMMAND_WORDS: Final = ("uvx", "--from", "<wheel url>", "asteroidea-setup", "<app url>")
NOTEBOOK_TIMEOUT_S: Final = 3600.0
# Answers for the [y/n] rows: 1c, 1d, 1e, 1f and 1h; with `--pass2`
# 2b, 2c, 2d, 2f and 2h (a human reads them).
PASS1_ROWS: Final = ("1c", "1d", "1e", "1f", "1h")
PASS2_ROWS: Final = ("2b", "2c", "2d", "2f", "2h")
PROMPT_COUNT: Final = len(PASS1_ROWS)
HUMAN_ROWS: Final = frozenset(
    {"1c", "1d", "1e", "1f", "1h", "2b", "2c", "2d", "2f", "2h"}
)
PASS2_FLAG: Final = "--pass2"
SUBSTITUTIONS: Final = (
    "Headless mode. Substitutions in this run:",
    "  - the account email is an .invalid address (no inbox, nothing is mailed)",
    "  - sign-in uses the sandbox-test client, not managed login",
    f"  - the setup's {PROMPT_COUNT} [y/n] human rows are answered y from a file",
    "  - the saved sign-in is placed in the fresh HOME (managed login is not driven)",
)
ANSWERED: Final = "substitution: answered from a file"
KEPT_ENV: Final = ("LANG", "LC_ALL", "TERM", "SSL_CERT_FILE", "SSL_CERT_DIR")
STEP_WORDS: Final = ("PASS", "FAIL")


def fake_home(run: analysis_check.AnalysisRun) -> Path:
    """Return the temporary `HOME` `asteroidea-setup` runs in."""
    return run.base / "home"


def work_dir(run: analysis_check.AnalysisRun) -> Path:
    """Return the working directory of the child processes (outside the repo)."""
    return run.base / "work"


def clean_path(path: str, repo: Path) -> str:
    """Drop the entries of `path` inside `repo` (its venv is not the outsider's)."""
    kept = []
    for entry in path.split(os.pathsep):
        if not entry:
            continue
        resolved = Path(entry).resolve()
        if resolved == repo or repo in resolved.parents:
            continue
        kept.append(entry)
    return os.pathsep.join(kept)


def child_env(
    home: Path, repo: Path, extra: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Return the outsider's environment: a new `HOME`, repo off `PATH`."""
    env = {key: os.environ[key] for key in KEPT_ENV if key in os.environ}
    env["HOME"] = str(home)
    env["PATH"] = clean_path(os.environ.get("PATH", ""), repo)
    env.update(extra or {})
    return env


def parse_steps(output: str) -> list[StepResult]:
    """Turn the setup's PASS and FAIL lines into results."""
    results = []
    for line in output.splitlines():
        word, _, rest = line.partition(" ")
        if word not in STEP_WORDS:
            continue
        name, _, reason = rest.partition(": ")
        results.append(
            StepResult(
                f"Script {name}",
                passed=word == "PASS",
                detail=reason or "ok",
                seconds=0.0,
                note=ANSWERED if name in HUMAN_ROWS else "",
            )
        )
    return results


def get_started(run: analysis_check.AnalysisRun) -> list[StepResult]:
    """Call `GET /get-started` with the access token; keep the command it shows."""
    started = time.monotonic()
    try:
        body, _ = api_get(run, "/get-started")
    except StepFailureError as err:
        advice = "Run make web-publish after the deploy, then retry."
        raise StepFailureError(str(err), advice) from err
    command = shlex.split(str(body["install"]).splitlines()[-1])
    if len(command) != len(COMMAND_WORDS) or command[:2] != ["uvx", "--from"]:
        msg = f"the install text ends with an unexpected command: {command}"
        raise StepFailureError(msg, "Compare it with the get_started_api text.")
    run.outputs["setup_command"] = json.dumps(command)
    run.outputs["notebook"] = str(body["notebook"]["filename"])
    return [
        StepResult(
            "Get started",
            passed=True,
            detail=f"{len(body['pins'])} pins; the install command parses",
            seconds=time.monotonic() - started,
        )
    ]


def place_sign_in(run: analysis_check.AnalysisRun) -> None:
    """Copy the saved sign-in where `asteroidea-setup` reads a cached one."""
    TokenCache(fake_home(run) / ".config" / "asteroidea" / CACHE_NAME).write(
        TokenCache(run.config_dir / CACHE_NAME).read()
    )


def run_script(
    run: analysis_check.AnalysisRun, args: Sequence[str] = ()
) -> list[StepResult]:
    """Run the shown command in a fresh `HOME`, repo off `PATH`, cwd outside.

    With `--pass2` the web address is dropped (the pass-2 rows need none).
    """
    fake_home(run).mkdir(parents=True, exist_ok=True)
    work_dir(run).mkdir(parents=True, exist_ok=True)
    place_sign_in(run)
    answers = run.base / "answers.txt"
    rows = PASS2_ROWS if args else PASS1_ROWS
    answers.write_text("".join(f"{row}=y\n" for row in rows))
    command = json.loads(run.outputs["setup_command"])
    if args:
        command = command[:-1]
    env = child_env(fake_home(run), REPO)
    done = execute(
        [*command, *args, "--answers", str(answers)],
        cwd=work_dir(run),
        env=env,
        timeout=SETUP_TIMEOUT_S,
    )
    log = run.base / ("pass2.log" if args else "install.log")
    log.write_text(done.out + done.err)
    results = parse_steps(done.out)
    if done.code != 0 or not results or not all(r.passed for r in results):
        lines = (done.err or done.out).strip().splitlines()
        msg = (
            f"asteroidea-setup exit {done.code}: {lines[-1] if lines else 'no output'}"
        )
        raise StepFailureError(msg, f"Read {log}.")
    return results


def execute_notebook(run: analysis_check.AnalysisRun) -> list[StepResult]:
    """Run the downloaded notebook with nbconvert and the venv's interpreter."""
    home = fake_home(run)
    job = notebook.NotebookJob(
        python=home / "asteroidea-env" / "bin" / "python",
        notebook=home / "asteroidea-get-started" / run.outputs["notebook"],
        env=child_env(home, REPO),
        timeout=NOTEBOOK_TIMEOUT_S,
        hint="Open the executed copy if one was written.",
        cwd=work_dir(run),
    )
    return notebook.run_notebook(run, job)


def run_pass2(run: analysis_check.AnalysisRun) -> list[StepResult]:
    """Run the setup again with `--pass2` in the same `HOME` (the venv is there)."""
    return run_script(run, (PASS2_FLAG,))


COMMON_STEPS: Final = (
    ("Checking the AWS sign-in", first_hour.check_sso),
    ("Creating the researcher", analysis_check.create_researcher),
    ("Signing in", sign_in),
    ("Fetching the install command", get_started),
    ("Running asteroidea-setup", run_script),
)
STEPS: Final = (
    *COMMON_STEPS,
    ("Executing the downloaded notebook", execute_notebook),
)
STEPS_PASS2: Final = (
    *COMMON_STEPS,
    ("Running the pass-2 rows", run_pass2),
)


def write_report(run: analysis_check.AnalysisRun, seconds: float) -> Path:
    """Write `report.md` next to the executed notebook."""
    report = Report(
        run.run_id, "headless", commit_sha(), title="analysis_check.AnalysisRun run"
    )
    report.results = list(run.results)
    report.ids = {"email": run.email, "run": str(run.base), "total": f"{seconds:.0f} s"}
    return write_run_report(run, report)


def run_live(steps: Sequence[analysis_check.Step] = STEPS) -> int:
    """Run the LIVE steps; exit 0 only when every step passed."""
    profile, account = require_sandbox()
    console = Console(headless=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    home = Path(os.environ.get(HOME_ENV) or Path.home() / "asteroidea-outsider")
    run = analysis_check.AnalysisRun(console, stamp, home / stamp, profile, account)
    run.base.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    console.say(f"asteroidea outsider run {stamp}")
    for line in SUBSTITUTIONS:
        console.say(line)
    console.say()
    try:
        complete = analysis_check.run_all(run, steps)
    except EOFError, KeyboardInterrupt:
        console.say("Stopped before the end.")
        complete = False
    seconds = time.monotonic() - started
    console.say()
    for line in format_summary(run.results):
        console.say(line)
    console.say()
    console.say(f"Total time: {seconds:.0f} s")
    console.say(f"Report and executed notebook: {write_report(run, seconds)}")
    ok = all_passed(run.results, complete=complete)
    console.say("OUTSIDER PASS" if ok else "OUTSIDER FAIL")
    return 0 if ok else 1


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point: `python -m asteroidea_infra.hosted_setup_check run`."""
    args = list(sys.argv[1:] if argv is None else argv)
    if args in (["run"], ["run", "1"], ["run", "2"]):
        return run_live(STEPS_PASS2 if args[1:] == ["2"] else STEPS)
    sys.stderr.write("usage: python -m asteroidea_infra.hosted_setup_check run [1|2]\n")
    return 2


if __name__ == "__main__":
    sys.exit(main())
