"""`make notebook` and `make live-notebook`: the exemplar and showcase notebooks.

`prepare` builds the researcher venv `.tools/notebook-venv` (the SDK wheel plus
`NOTEBOOK_PINS`) and rebuilds it only when the wheel hash or the pins change.
`run` is LIVE: it creates a researcher, installs the same pins into a fresh venv,
signs in headless, executes one notebook of `NOTEBOOKS` (default the exemplar)
with nbconvert and writes the executed copy `<stem>.executed.ipynb`, its HTML
`<stem>.executed.html` and `report.md` under `~/asteroidea-notebook/<run-id>/`.
`run --as me` creates no researcher and does not sign in: it checks the
user's own saved SDK sign-in (`config_dir()`) and executes the notebook with it.
"""

import argparse
import hashlib
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Final, override

from asteroidea._auth import CACHE_NAME
from asteroidea._config import CONFIG_NAME, config_dir
from asteroidea_infra import analysis_check, first_hour
from asteroidea_infra.runner import REPO, commit_sha, execute, must
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

NOTEBOOKS: Final = {
    "exemplar": REPO / "notebooks" / "exemplar.ipynb",
    "showcase": REPO / "notebooks" / "showcase.ipynb",
}
DEFAULT_NOTEBOOK: Final = "exemplar"
# The handout (`publish.upload_handout`) and the outsider run stay the exemplar.
NOTEBOOK: Final = NOTEBOOKS[DEFAULT_NOTEBOOK]
VENV: Final = REPO / ".tools" / "notebook-venv"
WHEEL_DIR: Final = REPO / ".tools" / "notebook-wheel"
STAMP: Final = REPO / ".tools" / "notebook-venv.stamp"
HOME_ENV: Final = "NOTEBOOK_HOME"
CELL_TIMEOUT_S: Final = 600
RUN_TIMEOUT_S: Final = 3600.0
# Exact pins (each the newest release at least 7 days old when pinned).
NOTEBOOK_PINS: Final = (
    *analysis_check.EXTRA_PINS,
    "matplotlib==3.11.2",  # 2026-09-11
    "polars==2.0.0",  # 2026-10-06
    "pynapple==0.11.4",  # the SDK extra `pynapple`, for `to_pynapple()`
    "ipython==9.17.1",  # 2026-09-01, same pin as the root dev group
    "jupyterlab==4.5.11",  # 2026-09-21
    "ipykernel==7.4.0",  # 2026-09-29
    "nbconvert==7.17.2",  # 2026-10-12
    "scipy==1.18.1",  # 2026-08-21, same as uv.lock; the showcase's signal work
    "nilearn==0.14.1",  # 2026-09-04; the showcase's MNI152 plots, bundled template
)
HEADLESS_SUBSTITUTIONS: Final = (
    "Headless mode. Substitutions in this run:",
    "  - the account email is an .invalid address (no inbox, nothing is mailed)",
    "  - sign-in uses the sandbox-test client, not managed login",
)


def platform_path() -> Path:
    """Return the platform.json path the SDK reads (see `config_dir`)."""
    return config_dir() / CONFIG_NAME


def install_kernelspec(venv: Path, next_step: str) -> None:
    """Write the venv's kernelspec with the venv interpreter as an absolute path.

    The ipykernel wheel ships a kernel.json whose argv starts with a bare `python`;
    under `uv run` that resolved to another Python.
    """
    must(
        [
            str(venv / "bin" / "python"),
            "-m",
            "ipykernel",
            "install",
            "--sys-prefix",
            "--name",
            "python3",
        ],
        next_step=next_step,
    )


def stamp_text(wheel: Path, pins: Sequence[str]) -> str:
    """Return the text that says which wheel and pins a venv was built from."""
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    return "\n".join([f"wheel {digest}", "kernelspec python3 sys-prefix", *pins]) + "\n"


def prepare(
    venv: Path = VENV,
    stamp: Path = STAMP,
    wheel_dir: Path = WHEEL_DIR,
    pins: Sequence[str] = NOTEBOOK_PINS,
) -> Path:
    """Build or reuse the notebook venv; return its Python."""
    retry = "Fix the error above, then run make notebook again."
    wheel = analysis_check.build_wheel(wheel_dir, retry)
    want = stamp_text(wheel, pins)
    python = venv / "bin" / "python"
    if python.exists() and stamp.exists() and stamp.read_text() == want:
        sys.stdout.write(f"notebook venv up to date: {venv}\n")
        return python
    sys.stdout.write(f"building the notebook venv {venv} (about 2 minutes)\n")
    stamp.unlink(missing_ok=True)
    analysis_check.make_venv(venv, wheel, pins, retry)
    install_kernelspec(venv, retry)
    stamp.write_text(want)
    return python


def install_notebook(run: analysis_check.AnalysisRun) -> list[StepResult]:
    """Install the SDK and `NOTEBOOK_PINS` into the run's fresh venv."""
    results = analysis_check.install(run, NOTEBOOK_PINS)
    install_kernelspec(run.base / "venv", "Fix the error above, then retry.")
    return results


@dataclass(frozen=True)
class NotebookJob:
    """What differs between the researcher run and the outsider run."""

    python: Path
    notebook: Path
    env: Mapping[str, str]
    timeout: float
    hint: str
    cwd: Path | None = None


def run_notebook(run: analysis_check.AnalysisRun, job: NotebookJob) -> list[StepResult]:
    """Execute the notebook with nbconvert; keep the executed copy and its HTML.

    Both are named after the notebook, `<stem>.executed.ipynb` and
    `<stem>.executed.html`, in the run folder.
    """
    started = time.monotonic()
    out = run.base / f"{job.notebook.stem}.executed.ipynb"
    html = out.with_suffix(".html")
    done = execute(
        [
            str(job.python),
            "-m",
            "nbconvert",
            "--to",
            "notebook",
            "--execute",
            f"--ExecutePreprocessor.timeout={CELL_TIMEOUT_S}",
            "--output",
            out.name,
            "--output-dir",
            str(run.base),
            str(job.notebook),
        ],
        cwd=job.cwd,
        env=job.env,
        timeout=job.timeout,
    )
    if done.code != 0:
        lines = (done.err or done.out).strip().splitlines()
        msg = f"nbconvert failed: {lines[-1] if lines else 'no output'}"
        raise StepFailureError(msg, job.hint)
    page = execute(
        [
            str(job.python),
            "-m",
            "nbconvert",
            "--to",
            "html",
            "--output",
            html.name,
            "--output-dir",
            str(run.base),
            str(out),
        ],
        cwd=job.cwd,
        env=job.env,
        timeout=job.timeout,
    )
    if page.code != 0:
        lines = (page.err or page.out).strip().splitlines()
        msg = f"nbconvert to HTML failed: {lines[-1] if lines else 'no output'}"
        raise StepFailureError(msg, f"The executed copy is at {out}.")
    return [
        StepResult(
            "Execute notebook",
            passed=True,
            detail=f"all cells ran (limit {CELL_TIMEOUT_S} s each); copy at {out}, "
            f"HTML at {html}",
            seconds=time.monotonic() - started,
        )
    ]


def execute_notebook(
    run: analysis_check.AnalysisRun, notebook: Path = NOTEBOOK
) -> list[StepResult]:
    """Execute the notebook with nbconvert; keep the executed copy in the run folder."""
    hint = "Open the executed copy if one was written; the error names the cell."
    job = NotebookJob(run.python, notebook, run.child_env, RUN_TIMEOUT_S, hint)
    return run_notebook(run, job)


@dataclass
class OwnerRun(analysis_check.AnalysisRun):
    """A run as the user's own sign-in: the SDK settings are the user's directory.

    The notebook's results are then saved under the user's name. The run reads that
    directory and never copies or rewrites the tokens in it (the SDK itself may
    refresh them, as in any session of the user's).
    """

    owner_config: Path = field(default_factory=config_dir)

    @property
    @override
    def config_dir(self) -> Path:
        """The user's SDK settings directory (`config_dir()`), not the run folder."""
        return self.owner_config


OWNER_MODE: Final = "as the user's own sign-in"
OWNER_SIGN_IN_HINT: Final = (
    "Sign in once as yourself: in a Python with the SDK installed (make notebook "
    "opens one), run asteroidea.sign_in(), then run make notebook-run "
    "NOTEBOOK=<name> AS=me again."
)
# One cheap call that needs a valid sign-in; it never prompts (`Auth.tokens`).
OWNER_CHECK_SNIPPET: Final = (
    "import sys, asteroidea\n"
    "try:\n"
    "    asteroidea.studies()\n"
    "except asteroidea.AsteroideaError as err:\n"
    "    print(f'{type(err).__name__}: {err}')\n"
    "    sys.exit(1)\n"
)


def check_owner_sign_in(run: analysis_check.AnalysisRun) -> list[StepResult]:
    """Check that the user's saved sign-in reads the platform, before executing.

    Refuses when `platform.json` or `tokens.json` is missing from the user's
    settings directory, or when `asteroidea.studies()` is refused with it.
    """
    started = time.monotonic()
    settings = run.config_dir
    for name in (CONFIG_NAME, CACHE_NAME):
        if not (settings / name).exists():
            msg = f"no saved sign-in: {settings / name} is missing"
            raise StepFailureError(msg, OWNER_SIGN_IN_HINT)
    done = execute([str(run.python), "-c", OWNER_CHECK_SNIPPET], env=run.child_env)
    if done.code != 0:
        lines = (done.out or done.err).strip().splitlines()
        why = lines[-1] if lines else "no output"
        msg = f"your saved sign-in in {settings} was refused: {why}"
        raise StepFailureError(msg, OWNER_SIGN_IN_HINT)
    return [
        StepResult(
            "Your own sign-in",
            passed=True,
            detail=f"asteroidea.studies() answered with the sign-in in {settings}",
            seconds=time.monotonic() - started,
        )
    ]


Step = analysis_check.Step
STEPS: Final[tuple[Step, ...]] = (
    ("Checking the AWS sign-in", first_hour.check_sso),
    ("Creating the researcher", analysis_check.create_researcher),
    ("Building and installing the SDK and notebook packages", install_notebook),
    ("Signing in", first_hour.sign_in),
    ("Executing the notebook", execute_notebook),
)
# AS=me: no test researcher and no headless sign-in; the user's own sign-in instead.
OWNER_STEPS: Final[tuple[Step, ...]] = (
    ("Checking the AWS sign-in", first_hour.check_sso),
    ("Building and installing the SDK and notebook packages", install_notebook),
    ("Checking your own sign-in", check_owner_sign_in),
    ("Executing the notebook", execute_notebook),
)


def steps_for(name: str, *, as_owner: bool = False) -> tuple[Step, ...]:
    """Return `STEPS` (or `OWNER_STEPS`) with the last step executing `name`."""
    *head, (title, _) = OWNER_STEPS if as_owner else STEPS
    return (*head, (title, partial(execute_notebook, notebook=NOTEBOOKS[name])))


def write_report(
    run: analysis_check.AnalysisRun, name: str, *, as_owner: bool = False
) -> Path:
    """Write `report.md` next to the executed notebook."""
    report = Report(
        run.run_id,
        f"headless, {OWNER_MODE}" if as_owner else "headless",
        commit_sha(),
        title="Notebook run",
    )
    report.results = list(run.results)
    who = (
        {"sign-in": f"{OWNER_MODE} ({run.config_dir})"}
        if as_owner
        else {"email": run.email}
    )
    report.ids = {**who, "notebook": name, "folder": str(run.base)}
    return write_run_report(run, report)


def run_live(name: str = DEFAULT_NOTEBOOK) -> int:
    """Run the LIVE steps on the notebook `name`; exit 0 only when all passed."""
    return _run(name, as_owner=False)


def run_as_owner(name: str = DEFAULT_NOTEBOOK) -> int:
    """Run the notebook `name` LIVE as the user's own sign-in (`AS=me`)."""
    return _run(name, as_owner=True)


def _run(name: str, *, as_owner: bool) -> int:
    profile, account = require_sandbox()
    console = Console(headless=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    home = Path(os.environ.get(HOME_ENV) or Path.home() / "asteroidea-notebook")
    kind = OwnerRun if as_owner else analysis_check.AnalysisRun
    run = kind(console, stamp, home / stamp, profile, account)
    run.base.mkdir(parents=True, exist_ok=True)
    console.say(f"asteroidea {name} notebook run {stamp}: {NOTEBOOKS[name]}")
    if as_owner:
        console.say(f"Headless, {OWNER_MODE}: the SDK settings in {run.config_dir}")
        console.say(
            "  - no test researcher is created; results are saved under your name"
        )
    else:
        for line in HEADLESS_SUBSTITUTIONS:
            console.say(line)
    console.say()
    try:
        complete = analysis_check.run_all(run, steps_for(name, as_owner=as_owner))
    except EOFError, KeyboardInterrupt:
        console.say()
        console.say("Stopped before the end.")
        complete = False
    console.say()
    for line in format_summary(run.results):
        console.say(line)
    console.say()
    report = write_report(run, name, as_owner=as_owner)
    console.say(f"Report and executed notebook: {report}")
    ok = all_passed(run.results, complete=complete)
    console.say("NOTEBOOK PASS" if ok else "NOTEBOOK FAIL")
    return 0 if ok else 1


def main(argv: Sequence[str] | None = None) -> int:
    """Dispatch `prepare` (offline) or `run` (LIVE)."""
    parser = argparse.ArgumentParser(description="The exemplar and showcase notebooks")
    parser.add_argument("command", choices=("prepare", "run"))
    parser.add_argument(
        "--notebook", choices=tuple(NOTEBOOKS), default=DEFAULT_NOTEBOOK
    )
    parser.add_argument(
        "--as",
        dest="as_who",
        choices=("me",),
        default=None,
        help="me: run as your own saved SDK sign-in, with no test researcher",
    )
    args = parser.parse_args(argv)
    if args.command == "prepare":
        target = platform_path()
        if not target.exists():
            sys.stderr.write(
                f"refusing: no platform.json at {target}\n"
                "Create a researcher first (it writes the file): "
                "make researcher EMAIL=<your address>\n"
            )
            return 2
        prepare()
        return 0
    if args.as_who == "me":
        return run_as_owner(args.notebook)
    return run_live(args.notebook)


if __name__ == "__main__":
    sys.exit(main())
