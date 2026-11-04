"""`make live-first-hour`: the first-hour rows R0 to R6 as one guided command.

Guided mode asks two things (an email address, and consent to stop a
process on the web port) and walks through the two sign-ins. `--headless`
(`make live-first-hour HEADLESS=1`) runs the same steps with no human: the sign-in
uses the `sandbox-test` client and the web step uses headless Playwright. Every
substitution is named on screen and in `report.md`. The temporary password never
passes through this program in guided mode; tokens and passwords are never printed.
"""

import argparse
import contextlib
import io
import json
import os
import signal
import socket
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final

from botocore.exceptions import BotoCoreError, ClientError

from asteroidea._auth import CACHE_NAME, Auth, TokenCache
from asteroidea._config import load_config
from asteroidea._http import send
from asteroidea_infra.admin import EMAIL_RE, add_researcher, sdk_config, session_for
from asteroidea_infra.runner import (
    REPO,
    SANDBOX_CLIENT_NOTE,
    Run,
    commit_sha,
    execute,
    last_json,
    must,
    sandbox_sign_in,
)
from asteroidea_infra.runner import write_report as write_run_report
from asteroidea_infra.sandbox_guard import require_sandbox
from asteroidea_infra.steps import (
    WEB_PORT,
    Console,
    Listener,
    Report,
    StepFailureError,
    StepResult,
    all_passed,
    format_summary,
    judge_query,
    judge_slice,
    judge_snapshot,
    judge_upload,
    node_hint,
    release_port,
    run_step,
    suggest_email,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence


HTTP_OK: Final = 200
INTERACTIVE_TIMEOUT_S: Final = 1800.0
HOME_ENV: Final = "ACCEPTANCE_HOME"
WEB_NOTE: Final = (
    "substitution: headless Playwright, token injected, /uploads forwarded"
)
WORKER: Final = Path(__file__).with_name("first_hour_worker.py")
HEADLESS_SUBSTITUTIONS: Final = (
    "Headless mode. Substitutions in this run:",
    "  - the account email is an .invalid address (no inbox, nothing is mailed)",
    "  - sign-in uses the sandbox-test client, not managed login",
    "  - the web page is checked by headless Playwright on a free port with the",
    "    token injected, and the page's /uploads call is forwarded from Node",
)


def _float(data: Mapping[str, object], key: str) -> float:
    value = data[key]
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    msg = f"worker value {key} is not a number"
    raise StepFailureError(msg, "Report this as a defect.")


def _flag(data: Mapping[str, object], key: str) -> bool:
    return data.get(key) is True


def _elapsed(started: float) -> float:
    return time.monotonic() - started


# --- pre-flight -----------------------------------------------------------------


def check_sso(run: Run) -> list[StepResult]:
    """Check the sandbox sign-in; when it fails, show the login line and wait."""
    started = time.monotonic()
    last = ""
    for attempt in range(2):
        try:
            run.session = session_for(run.profile, run.account)
        except SystemExit as err:
            raise StepFailureError(
                str(err.code), "Use the asteroidea-sandbox profile only."
            ) from err
        except (BotoCoreError, ClientError) as err:
            last = f"{type(err).__name__}: {err}"
        else:
            return [
                StepResult(
                    "Sandbox sign-in",
                    passed=True,
                    detail=f"AWS sign-in is valid (account {run.account})",
                    seconds=_elapsed(started),
                )
            ]
        if attempt == 0 and not run.console.headless:
            run.console.say("The AWS sign-in has expired. In another terminal, run:")
            run.console.say(f"    aws sso login --profile {run.profile}")
            run.console.pause("When the browser says you are signed in,")
    raise StepFailureError(
        last, f"Run: aws sso login --profile {run.profile}, then retry."
    )


# --- account ---------------------------------------------------------------------


def create_account(run: Run) -> list[StepResult]:
    """Ask for the email (one suggestion), create the researcher."""
    started = time.monotonic()
    suggestion = suggest_email(
        os.environ.get("ASTEROIDEA_BUDGET_EMAIL"),
        datetime.now(UTC),
        headless=run.console.headless,
    )
    email = run.console.ask("Email address for the new researcher", suggestion)
    if not EMAIL_RE.match(email):
        msg = f"that is not an email address: {email!r}"
        raise StepFailureError(msg, "Enter an address like name@example.com.")
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            add_researcher(run.need_session(), email)
    except SystemExit as err:
        raise StepFailureError(
            str(err.code), "Enter an address that has not been added before."
        ) from err
    run.email = email
    if run.console.headless:
        run.console.say(
            "Headless: no inbox; the sign-in step sets a password directly."
        )
    else:
        run.console.say(
            f"A temporary password is on its way to {email}; "
            "keep it for the sign-in step."
        )
    return [
        StepResult(
            "Account",
            passed=True,
            detail=f"Researcher account created for {email}",
            seconds=_elapsed(started),
            note="substitution: .invalid address" if run.console.headless else "",
        )
    ]


# --- install ---------------------------------------------------------------------


def install_sdk(run: Run) -> list[StepResult]:
    """Build the wheel, make a fresh venv, install it, write platform.json."""
    started = time.monotonic()
    retry = "Fix the error above, then retry."
    wheels = run.base / "wheel"
    must(
        ["uv", "build", "--package", "asteroidea", "--wheel", "--out-dir", str(wheels)],
        cwd=REPO,
        next_step=retry,
    )
    wheel = next(wheels.glob("asteroidea-*.whl"))
    venv = run.base / "venv"
    must(["uv", "venv", "--clear", "-p", "3.14", str(venv)], next_step=retry)
    must(
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(run.python),
            f"asteroidea[nifti] @ {wheel.as_uri()}",
        ],
        next_step=retry,
    )
    run.config_dir.mkdir(parents=True, exist_ok=True)
    platform = sdk_config(run.need_session())
    (run.config_dir / "platform.json").write_text(json.dumps(platform, indent=2) + "\n")
    run.outputs = {
        key: str(value) for key, value in platform.items() if key != "raw_bucket"
    }
    seconds = _elapsed(started)
    return [
        StepResult(
            "Install",
            passed=True,
            detail=f"SDK installed into a fresh environment in {seconds:.0f} s",
            seconds=seconds,
        )
    ]


# --- sign in ---------------------------------------------------------------------

SIGN_IN_SNIPPET: Final = (
    "import sys, asteroidea\n"
    "try:\n"
    "    asteroidea.sign_in()\n"
    "except asteroidea.AsteroideaError as err:\n"
    "    print(f'{type(err).__name__}: {err}')\n"
    "    sys.exit(1)\n"
)


def _sandbox_test_tokens(run: Run) -> None:
    """Headless substitution: tokens for the new researcher without managed login."""
    session = run.need_session()
    cfn = session.client("cloudformation")
    stack = cfn.describe_stacks(StackName="ServicesStack")["Stacks"][0]
    out = {o["OutputKey"]: o["OutputValue"] for o in stack["Outputs"]}
    sandbox_sign_in(session, out, run.email, run.config_dir)


def sign_in(run: Run) -> list[StepResult]:
    """Sign in by copy-paste (guided), or through the sandbox-test client (headless)."""
    console = run.console
    started = time.monotonic()
    if console.headless:
        _sandbox_test_tokens(run)
    else:
        console.say("Now you sign in as the new researcher. The program prints a web")
        console.say("address; open it, sign in with the email above and the temporary")
        console.say("password from the email, and choose a new password when asked.")
        console.say(
            "Your browser will then end on a page that says it 'can't be reached'."
        )
        console.say("That is expected. Copy the whole address from the address bar and")
        console.say("paste it at the prompt.")
        console.say()
    done = execute(
        [str(run.python), "-c", SIGN_IN_SNIPPET],
        env=run.child_env,
        interactive=not console.headless,
        timeout=INTERACTIVE_TIMEOUT_S,
    )
    if done.code != 0:
        raise StepFailureError(
            (done.out or "sign-in did not finish").strip().splitlines()[-1],
            "Retry the step and paste the address from the new sign-in page.",
        )
    return [
        StepResult(
            "Sign in",
            passed=True,
            detail="Signed in; the saved sign-in gives access to the data",
            seconds=_elapsed(started),
            note=SANDBOX_CLIENT_NOTE if console.headless else "",
        )
    ]


# --- data steps ------------------------------------------------------------------


def _worker(run: Run, name: str) -> dict[str, object]:
    done = execute([str(run.python), str(WORKER), name], env=run.child_env)
    data = last_json(done.out)
    error = data.get("error")
    if isinstance(error, str):
        raise StepFailureError(
            error, "Run the step again; if it repeats, sign in again."
        )
    if done.code != 0 or not data:
        lines = (done.err or done.out).strip().splitlines()
        msg = f"the {name} step crashed: {lines[-1] if lines else 'no output'}"
        raise StepFailureError(msg, "Report this as a defect.")
    return data


def upload_step(run: Run) -> list[StepResult]:
    """R3: upload the synthetic sample and wait until it is visible."""
    return [judge_upload(_float(_worker(run, "upload"), "seconds"))]


def query_step(run: Run) -> list[StepResult]:
    """R4: query, upload a second sample, query again, then the pinned snapshot."""
    data = _worker(run, "query")
    if not _flag(data, "first_seen"):
        msg = "the first upload is missing from the query result"
        raise StepFailureError(msg, "Wait a minute and retry the step.")
    return [
        judge_query(_float(data, "q1_seconds"), _float(data, "q2_seconds")),
        judge_snapshot(
            same_rows=_flag(data, "same_rows"),
            new_visible=_flag(data, "new_visible"),
            new_hidden=_flag(data, "new_hidden"),
        ),
    ]


def slice_step(run: Run) -> list[StepResult]:
    """R5: read one slice lazily."""
    data = _worker(run, "slice")
    return [
        judge_slice(
            _float(data, "seconds"),
            int(_float(data, "fetched")),
            same=_flag(data, "same"),
        )
    ]


# --- web -------------------------------------------------------------------------


def find_listener(port: int) -> Listener | None:
    """Return the process listening on `port`, if any."""
    pids = execute(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"]).out.split()
    if not pids:
        return None
    pid = pids[0]
    command = execute(["ps", "-p", pid, "-o", "command="]).out.strip()
    where = execute(["lsof", "-a", "-p", pid, "-d", "cwd", "-Fn"]).out.splitlines()
    cwd = next((line[1:] for line in where if line.startswith("n")), "")
    return Listener(int(pid), command, cwd)


def _port_busy(port: int) -> bool:
    return find_listener(port) is not None


def stop_process(pid: int) -> None:
    """Stop a process we were allowed to stop, and wait for its port to clear."""
    os.kill(pid, signal.SIGTERM)
    time.sleep(1.0)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def web_env(run: Run) -> dict[str, str]:
    """Return the environment that starts the web server with the sign-in values."""
    session = run.need_session()
    stack = session.client("cloudformation").describe_stacks(StackName="ServicesStack")
    out = {o["OutputKey"]: o["OutputValue"] for o in stack["Stacks"][0]["Outputs"]}
    return {
        **os.environ,
        "WEB_PORT": str(run.web_port),
        "VITE_COGNITO_DOMAIN": out["ManagedLoginBaseUrl"].removeprefix("https://"),
        "VITE_COGNITO_CLIENT_ID": out["WebClientId"],
        "VITE_API_URL": out["ApiUrl"],
    }


def start_web(run: Run) -> list[StepResult]:
    """Free the port (asking first), start the web server, open the browser."""
    started = time.monotonic()
    console = run.console
    if console.headless:
        run.web_port = _free_port()
    else:
        run.stopped = release_port(console, run.web_port, find_listener, stop_process)
    must(
        ["make", "web"],
        cwd=REPO,
        env=web_env(run),
        next_step="Fix the error above, retry.",
    )
    run.web_started = True
    url = f"http://localhost:{run.web_port}/"
    if not console.headless:
        execute(["open", url])
    return [
        StepResult(
            "Web server",
            passed=True,
            detail=f"Web page is serving at {url}",
            seconds=_elapsed(started),
            note="" if not console.headless else f"free port {run.web_port}",
        )
    ]


def _state(run: Run) -> dict[str, object]:
    return last_json(run.state.read_text())


def verify_uploads(run: Run) -> StepResult:
    """Call GET /uploads with the researcher's own token; expect both datasets."""
    started = time.monotonic()
    state = _state(run)
    settings = load_config(run.config_dir / "platform.json")
    auth = Auth(settings, TokenCache(run.config_dir / CACHE_NAME))
    reply = send(
        "GET",
        f"{settings.api_url.rstrip('/')}/uploads",
        {"Authorization": f"Bearer {auth.tokens().access_token}"},
    )
    if reply.status != HTTP_OK:
        msg = f"GET /uploads answered HTTP {reply.status}"
        raise StepFailureError(msg, "Retry; if it repeats, report it as a defect.")
    rows = json.loads(reply.body)["uploads"]
    committed = {r["dataset"] for r in rows if r["state"] == "COMMITTED"}
    wanted = {str(state["dataset"]), str(state["dataset2"])}
    ok = wanted <= committed
    return StepResult(
        "Uploads API",
        passed=ok,
        detail=(
            "The platform lists both uploaded datasets as COMMITTED"
            if ok
            else f"The platform lists {len(wanted & committed)} of 2 uploaded datasets"
        ),
        seconds=_elapsed(started),
    )


def _headless_page(run: Run) -> StepResult:
    started = time.monotonic()
    browsers = Path(
        os.environ.get(
            "PLAYWRIGHT_BROWSERS_PATH", str(REPO / ".tools" / "ms-playwright")
        )
    )
    env = web_env(run)
    env["PLAYWRIGHT_BROWSERS_PATH"] = str(browsers)
    env["ACCEPTANCE_TOKENS"] = str(run.config_dir / CACHE_NAME)
    env["ACCEPTANCE_SHOT"] = str(run.base / "web.png")
    web = REPO / "web"
    if not list(browsers.glob("chromium*")):
        must(
            [str(web / "node_modules" / ".bin" / "playwright"), "install", "chromium"],
            cwd=web,
            env=env,
            next_step="Check the network, retry.",
        )
    done = must(
        ["node", "scripts/acceptance-headless.mjs"],
        cwd=web,
        env=env,
        next_step="Read the error above; retry.",
    )
    rows = last_json(done.out).get("rows")
    state = _state(run)
    text = "\n".join(str(r) for r in rows) if isinstance(rows, list) else ""
    ok = all(
        any(name in line and "COMMITTED" in line for line in text.splitlines())
        for name in (str(state["dataset"]), str(state["dataset2"]))
    )
    return StepResult(
        "Web page",
        passed=ok,
        detail="My uploads lists both datasets"
        if ok
        else "My uploads is missing a dataset",
        seconds=_elapsed(started),
        note=WEB_NOTE,
    )


def web_check(run: Run) -> list[StepResult]:
    """Check /uploads ourselves, then ask the tester (headless: Playwright)."""
    console = run.console
    if not console.headless:
        console.say("In the browser: click 'Sign in', use the same email and the NEW")
        console.say("password you chose at sign-in, and look for 'My uploads' below")
        console.say(
            "the header. The program then checks the same list through the API."
        )
        console.pause("When you are signed in on the page,")
    results = [verify_uploads(run)]
    if console.headless:
        results.append(_headless_page(run))
    else:
        seen = console.confirm("Do you see both datasets in My uploads?", default=True)
        results.append(
            StepResult(
                "Web page",
                passed=seen,
                detail="You see both datasets in My uploads"
                if seen
                else "You did not see both datasets in My uploads",
            )
        )
    return results


def cleanup_web(run: Run) -> None:
    """Stop our web server (guided: after asking) and restore the earlier one."""
    console = run.console
    if not run.web_started:
        return
    if console.confirm("Stop the web server this run started?", default=True):
        execute(["make", "web-kill"], cwd=REPO)
        run.web_started = False
        console.say("Web server stopped.")
        if run.stopped is not None and console.confirm(
            "Start your own make web again (without the sign-in values)?", default=True
        ):
            env = {k: v for k, v in os.environ.items() if not k.startswith("VITE_")}
            env["WEB_PORT"] = str(WEB_PORT)
            execute(["make", "web"], cwd=REPO, env=env)
            console.say(f"Your make web is back on port {WEB_PORT}.")


# --- the run ---------------------------------------------------------------------

Step = tuple[str, "Callable[[Run], Sequence[StepResult]]"]
STEPS: Final[tuple[Step, ...]] = (
    ("Checking the AWS sign-in", check_sso),
    ("Creating the researcher account", create_account),
    ("Building and installing the SDK", install_sdk),
    ("Signing in", sign_in),
    ("Uploading the sample", upload_step),
    ("Querying the catalog", query_step),
    ("Reading one slice", slice_step),
    ("Starting the web page", start_web),
    ("Checking the web page", web_check),
)


def write_report(run: Run, results: Sequence[StepResult]) -> Path:
    """Write `report.md` with the results, substitutions and every id."""
    report = Report(
        run.run_id, "headless" if run.console.headless else "guided", commit_sha()
    )
    report.results = list(results)
    report.ids = {"email": run.email, "web port": run.web_port}
    if run.state.exists():
        report.ids.update(_state(run))
    return write_run_report(run, report)


def run_all(run: Run) -> bool:
    """Run the steps in order into `run.results`; stop at the first failure.

    Returns True when every step ran.
    """
    for title, action in STEPS:
        done = run_step(run.console, title, lambda a=action: a(run))
        run.results.extend(done)
        if not all(r.passed for r in done):
            return False
    return True


def main(argv: Sequence[str] | None = None) -> int:
    """Run the acceptance steps; exit 0 only when every step passed."""
    parser = argparse.ArgumentParser(description="First-hour acceptance run (R0-R6)")
    parser.add_argument(
        "--headless", action="store_true", help="no human; substitutions"
    )
    args = parser.parse_args(argv)
    profile, account = require_sandbox()
    console = Console(headless=args.headless)
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    home = Path(os.environ.get(HOME_ENV) or Path.home() / "asteroidea-acc")
    run = Run(console, stamp, home / stamp, profile, account)
    run.base.mkdir(parents=True, exist_ok=True)
    console.say(f"asteroidea first-hour acceptance, run {stamp}")
    if args.headless:
        for line in HEADLESS_SUBSTITUTIONS:
            console.say(line)
    console.say()
    try:
        complete = run_all(run)
    except EOFError, KeyboardInterrupt:
        console.say()
        console.say("Stopped before the end; Enter now takes each default.")
        console.read = lambda _prompt: ""
        complete = False
    finally:
        cleanup_web(run)
    console.say()
    for line in format_summary(run.results):
        console.say(line)
    console.say()
    hint = node_hint(execute(["node", "--version"]).out.strip())
    if hint is not None:
        console.say(hint)
        console.say()
    console.say(f"Full report with ids and timings: {write_report(run, run.results)}")
    ok = all_passed(run.results, complete=complete)
    console.say("ACCEPTANCE PASS" if ok else "ACCEPTANCE FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
