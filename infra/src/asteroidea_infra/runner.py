"""The kit the LIVE runners share: subprocesses, sandbox sign-in, API calls, report.

`execute` is the one place the repo starts a child process (fixed argument list, no
shell, a timeout), so the ruff `S603` and `S607` exemption names this file alone.
The runners (`first_hour`, `analysis_check`, `hosted_setup_check`, `research_app_check`,
`notebook`, `web_smoke`, `route_timing`) call these helpers instead of keeping a
copy each.
"""

import json
import os
import secrets
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import urllib3

from asteroidea._auth import CACHE_NAME, TokenCache
from asteroidea_infra.admin import stack_outputs
from asteroidea_infra.steps import (
    WEB_PORT,
    Console,
    Listener,
    Report,
    StepFailureError,
    StepResult,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from boto3 import Session

REPO: Final = Path(__file__).resolve().parents[3]
HTTP_OK: Final = 200
API_TIMEOUT_S: Final = 120.0
DEFAULT_TIMEOUT_S: Final = 600.0
TIMEOUT_CODE: Final = 124
SANDBOX_CLIENT_NOTE: Final = "substitution: sandbox-test client, not managed login"


@dataclass
class Run:
    """State shared by the steps of one run."""

    console: Console
    run_id: str
    base: Path
    profile: str
    account: str
    session: Session | None = None
    email: str = ""
    outputs: dict[str, str] = field(default_factory=dict)
    web_port: int = WEB_PORT
    stopped: Listener | None = None
    web_started: bool = False
    results: list[StepResult] = field(default_factory=list)

    @property
    def config_dir(self) -> Path:
        """The SDK settings directory of this run."""
        return self.base / "config"

    @property
    def python(self) -> Path:
        """The fresh venv's interpreter."""
        return self.base / "venv" / "bin" / "python"

    @property
    def state(self) -> Path:
        """The JSON file the worker steps share."""
        return self.base / "state.json"

    @property
    def child_env(self) -> dict[str, str]:
        """The environment of every SDK subprocess."""
        return {
            **os.environ,
            "ASTEROIDEA_CONFIG_DIR": str(self.config_dir),
            "ASTEROIDEA_ACC_STATE": str(self.state),
        }

    def need_session(self) -> Session:
        """Return the sandbox session; a step before pre-flight is a bug."""
        if self.session is None:
            msg = "the sandbox session is not open"
            raise StepFailureError(msg, "Run the checks from the start.")
        return self.session


@dataclass(frozen=True)
class Proc:
    """A finished child process."""

    code: int
    out: str
    err: str


def execute(
    cmd: Sequence[str],
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    interactive: bool = False,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> Proc:
    """Run a fixed command line (no shell); `interactive` hands over the terminal.

    A command that outlives `timeout` seconds is killed and reported with code 124.
    """
    try:
        done = subprocess.run(
            list(cmd),
            cwd=cwd,
            env=env,
            stdin=None if interactive else subprocess.DEVNULL,
            capture_output=not interactive,
            text=True,
            errors="replace",
            timeout=timeout,
            check=False,
            shell=False,
        )
    except subprocess.TimeoutExpired:
        return Proc(TIMEOUT_CODE, "", f"{cmd[0]} timed out after {timeout:.0f} s")
    return Proc(done.returncode, done.stdout or "", done.stderr or "")


def must(
    cmd: Sequence[str],
    *,
    next_step: str,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> Proc:
    """Run `cmd`; a non-zero exit becomes a failure that quotes the last error line."""
    done = execute(cmd, cwd=cwd, env=env)
    if done.code != 0:
        lines = (done.err or done.out).strip().splitlines()
        msg = f"{cmd[0]} failed: {lines[-1] if lines else 'no output'}"
        raise StepFailureError(msg, next_step)
    return done


def last_json(text: str) -> dict[str, object]:
    """Return the last line of `text` that parses as a JSON object, else {}."""
    for line in reversed(text.strip().splitlines()):
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if isinstance(data, dict):
            return {str(k): v for k, v in data.items()}
    return {}


def sandbox_sign_in(
    session: Session,
    out: Mapping[str, str],
    email: str,
    config_dir: Path,
) -> Mapping[str, Any]:
    """Headless substitution: sign in, cache the tokens, return the result."""
    password = "Aa1!" + secrets.token_urlsafe(18)
    idp = session.client("cognito-idp")
    idp.admin_set_user_password(
        UserPoolId=out["UserPoolId"],
        Username=email,
        Password=password,
        Permanent=True,
    )
    result: Mapping[str, Any] = idp.admin_initiate_auth(
        UserPoolId=out["UserPoolId"],
        ClientId=out["SandboxTestClientId"],
        AuthFlow="ADMIN_USER_PASSWORD_AUTH",
        AuthParameters={"USERNAME": email, "PASSWORD": password},
    )["AuthenticationResult"]
    TokenCache(config_dir / CACHE_NAME).write(
        {
            "tokens": {
                "id_token": result["IdToken"],
                "access_token": result["AccessToken"],
                "refresh_token": result["RefreshToken"],
                "expires_at": time.time() + result["ExpiresIn"],
            }
        }
    )
    return result


def commit_sha() -> str:
    """Return the short SHA of the checkout the run reports on."""
    return execute(["git", "rev-parse", "--short", "HEAD"], cwd=REPO).out.strip()


def write_report(run: Run, report: Report, tail: Sequence[str] = ()) -> Path:
    """Write `report.md` under the run's directory and return its path.

    `tail` holds extra Markdown lines; they follow the report text, joined by newlines,
    and the file then ends with exactly one newline.
    """
    text = report.markdown()
    if tail:
        text = (text + "\n".join(tail)).rstrip("\n") + "\n"
    path = run.base / "report.md"
    path.write_text(text)
    return path


def sign_in(run: Run) -> list[StepResult]:
    """Sign the new researcher in (sandbox-test client); keep tokens and app URL."""
    started = time.monotonic()
    session = run.need_session()
    run.outputs.update(stack_outputs(session))
    run.outputs["WebUrl"] = stack_outputs(session, "DataStack")["WebUrl"]
    got = sandbox_sign_in(session, run.outputs, run.email, run.config_dir)
    run.outputs["access_token"] = str(got["AccessToken"])
    run.outputs["refresh_token"] = str(got["RefreshToken"])
    return [
        StepResult(
            "Sign in",
            passed=True,
            detail=f"signed in; app {run.outputs['WebUrl']}",
            seconds=time.monotonic() - started,
            note=SANDBOX_CLIENT_NOTE,
        )
    ]


def api_send(
    run: Run, method: str, path: str, body: object = None
) -> tuple[int, str, float]:
    """Send `method` `path` as the researcher; return status, text and wall seconds."""
    url = f"{run.outputs['ApiUrl'].rstrip('/')}{path}"
    headers = {"Authorization": f"Bearer {run.outputs['access_token']}"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    started = time.monotonic()
    try:
        resp = urllib3.request(
            method,
            url,
            headers=headers,
            body=None if body is None else json.dumps(body),
            timeout=API_TIMEOUT_S,
        )
    except urllib3.exceptions.HTTPError as err:
        msg = f"{method} {path.split('?', 1)[0]} failed: {err}"
        raise StepFailureError(msg, "Check the deploy, then retry.") from err
    return resp.status, resp.data.decode(errors="replace"), time.monotonic() - started


def api_get(run: Run, path: str) -> tuple[Any, float]:
    """GET `path` with the access token; return the JSON body and wall seconds."""
    status, text, seconds = api_send(run, "GET", path)
    if status != HTTP_OK:
        msg = f"GET {path.split('?', 1)[0]} answered {status}: {text[:200]}"
        raise StepFailureError(msg, "Read the answer; fix the cause, then retry.")
    return json.loads(text), seconds
