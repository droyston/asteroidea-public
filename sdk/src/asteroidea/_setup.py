"""`asteroidea-setup`: the guided researcher install, as one command.

Run as `uvx --from '<wheel link>' asteroidea-setup https://<hosted app>`.
Each step prints `PASS <step>` or `FAIL <step>: <reason>` and appends the same
line to `~/asteroidea-get-started/report.txt`. A re-run is safe; the first FAIL
stops it (exit 1).
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, NamedTuple

import asteroidea
from asteroidea._auth import Auth, Post, TokenCache, default_cache_path, post_form
from asteroidea._client import PlatformClient, json_of, refusal
from asteroidea._config import CONFIG_NAME, config_dir, load_config
from asteroidea._http import Downloader, Transport, download, send
from asteroidea.errors import AsteroideaError, DatasetNotYoursError

if TYPE_CHECKING:
    from asteroidea._artifacts import ArtifactInfo, ArtifactResult
    from asteroidea._query import QueryResult
    from asteroidea._upload import UploadResult

FILE_MODE: Final = 0o600
DIR_MODE: Final = 0o700
PYTHON: Final = "3.14"
EXTRAS: Final = "nwb,nifti"
OK: Final = 200
SHORT: Final = 300.0
LONG: Final = 1800.0
EXIT_FAIL: Final = 1
EXIT_USAGE: Final = 2
DATASET: Final = "dandi-000623"
UV_HINT: Final = "install uv first: curl -LsSf https://astral.sh/uv/install.sh | sh"

# The human rows of the guided install.
PASS1_BEFORE: Final = (
    ("1c", "Did the invitation email carry the web address?"),
    ("1d", "Did the web address show only the sign-in page before you signed in?"),
    ("1e", "Did sign-in (with the new password) work?"),
    ("1f", "Did the Get started page answer?"),
)
PASS1_AFTER: Final = (("1h", "Did the PSTH figure show after Run All?"),)
YES: Final = ("y", "yes")

Echo = Callable[[str], None]
Prompt = Callable[[str], str]


class Done(NamedTuple):
    """A finished child process: its exit code and the combined output."""

    returncode: int
    output: str


Run = Callable[[Sequence[str], float], Done]
Which = Callable[[str], str | None]


class StepError(Exception):
    """A step failed; the message is the reason."""


def run_process(args: Sequence[str], timeout: float) -> Done:
    """Run `args` (no shell), wait at most `timeout` seconds, return code and output."""
    try:
        done = subprocess.run(
            list(args),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            shell=False,
        )
    except subprocess.TimeoutExpired:
        return Done(124, f"timed out after {int(timeout)} seconds")
    return Done(done.returncode, (done.stdout + done.stderr).strip())


@dataclass(frozen=True)
class Sdk:
    """The SDK calls the pass-2 rows make (a seam for tests)."""

    query: Callable[..., QueryResult] = asteroidea.query
    save_artifact: Callable[..., ArtifactResult] = asteroidea.save_artifact
    artifacts: Callable[[], list[ArtifactInfo]] = asteroidea.artifacts
    download_artifact: Callable[..., Path] = asteroidea.download_artifact
    upload: Callable[..., UploadResult] = asteroidea.upload


def _stdout(message: str) -> None:
    """Write `message` and a newline to standard output."""
    sys.stdout.write(message + "\n")


def _tail(text: str) -> str:
    """Return the last line of `text`, or `no output` when it is empty."""
    lines = text.strip().splitlines()
    return lines[-1] if lines else "no output"


def _sha256(path: Path) -> str:
    """Return the SHA-256 of a file, read in 1 MiB chunks."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _plain_name(name: object, what: str) -> str:
    """Return `name` when it is a bare file name, else raise `StepError`."""
    text = str(name)
    if not text or text in {".", ".."} or text != Path(text).name or "\\" in text:
        msg = f"the platform sent an unsafe {what} name"
        raise StepError(msg)
    return text


def _plain_text(value: object) -> str:
    """Return `value` when it is a non-empty string, else raise `TypeError`."""
    if not isinstance(value, str) or not value:
        raise TypeError(value)
    return value


def read_answers(path: Path) -> dict[str, str]:
    """Read `row=y|n` lines (blank lines and `#` comments are skipped)."""
    answers: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            row, _, answer = line.partition("=")
            answers[row.strip()] = answer.strip().lower()
    return answers


@dataclass
class Setup:
    """One run of the guided install; every outside effect goes through a seam."""

    app: str = ""
    force: bool = False
    home: Path = field(default_factory=Path.home)
    config_path: Path = field(default_factory=lambda: config_dir() / CONFIG_NAME)
    answers: dict[str, str] | None = None
    transport: Transport = send
    downloader: Downloader = download
    run: Run = run_process
    which: Which = shutil.which
    prompt: Prompt = input
    echo: Echo = _stdout
    post: Post = post_form
    sdk: Sdk = field(default_factory=Sdk)
    current: str = ""

    @property
    def venv(self) -> Path:
        """The private environment."""
        return self.home / "asteroidea-env"

    @property
    def python(self) -> Path:
        """The environment's interpreter."""
        return self.venv / "bin" / "python"

    @property
    def folder(self) -> Path:
        """The get-started folder: notebook and report."""
        return self.home / "asteroidea-get-started"

    @property
    def report(self) -> Path:
        """The report file."""
        return self.folder / "report.txt"

    def say(self, line: str) -> None:
        """Print a line and append it to the report."""
        self.echo(line)
        with self.report.open("a") as handle:
            handle.write(line + "\n")

    def passed(self, step: str) -> None:
        """Record a PASS."""
        self.say(f"PASS {step}")

    def note(self, text: str) -> None:
        """Record a NOTE."""
        self.say(f"NOTE {text}")

    def ask(self, row: str, question: str) -> None:
        """Ask a human `[y/n]` row; anything but yes is a FAIL."""
        self.current = row
        if self.answers is not None:
            answer = self.answers.get(row, "n")
            self.note(f"{row} answered from a file: {answer}")
        else:
            try:
                answer = self.prompt(f"{question} [y/n] ").strip().lower()
            except EOFError:
                answer = "n"
        if answer not in YES and answer != "y":
            msg = "answered no"
            raise StepError(msg)
        self.passed(row)

    def child(self, args: Sequence[str], timeout: float = SHORT) -> str:
        """Run a child process; return its output, or raise `StepError` on failure."""
        done = self.run(args, timeout)
        if done.returncode != 0:
            raise StepError(_tail(done.output))
        return done.output

    # -- steps ---------------------------------------------------------------

    def step_config(self) -> None:
        """Fetch the hosted `platform.json`, validate it, and save it mode 0600."""
        self.current = "config"
        parts = urllib.parse.urlsplit(self.app)
        if parts.scheme != "https" or parts.hostname is None:
            msg = "the web address must start with https://"
            raise StepError(msg)
        url = f"{self.app.rstrip('/')}/{CONFIG_NAME}"
        try:
            reply = self.transport("GET", url, {}, None)
        except AsteroideaError as err:
            raise StepError(str(err)) from err
        if reply.status != OK:
            msg = f"{url} answered {reply.status}"
            raise StepError(msg)
        fields = json_of(reply)
        directory = self.config_path.parent
        directory.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
        directory.chmod(DIR_MODE)
        scratch = self.config_path.with_name(self.config_path.name + ".new")
        fd = os.open(scratch, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, FILE_MODE)
        try:
            os.fchmod(fd, FILE_MODE)
            with os.fdopen(fd, "w") as handle:
                handle.write(json.dumps(fields, indent=2, sort_keys=True) + "\n")
            try:
                load_config(scratch)
            except AsteroideaError as err:
                raise StepError(str(err)) from err
            if (
                self.config_path.exists()
                and self.config_path.read_bytes() != scratch.read_bytes()
                and not self.force
            ):
                msg = (
                    f"{self.config_path} differs; run again with --force to replace it"
                )
                raise StepError(msg)
            scratch.replace(self.config_path)
        finally:
            scratch.unlink(missing_ok=True)
        self.config_path.chmod(FILE_MODE)
        self.passed("config")

    def step_sign_in(self) -> Auth:
        """Reuse a valid cached sign-in, else sign in by copy-paste."""
        self.current = "sign-in"
        try:
            auth = Auth(
                load_config(self.config_path),
                TokenCache(default_cache_path()),
                post=self.post,
            )
            auth.sign_in(prompt=self.prompt, echo=self.echo)
        except AsteroideaError as err:
            raise StepError(str(err)) from err
        self.passed("sign-in")
        return auth

    def step_get_started(self, auth: Auth) -> dict[str, Any]:
        """Ask the platform for the wheel, the notebook and the pins."""
        self.current = "get-started"
        try:
            token = auth.tokens().access_token
            reply = PlatformClient(auth.config.api_url, token, self.transport).call(
                "GET", "/get-started"
            )
        except AsteroideaError as err:
            raise StepError(str(err)) from err
        if reply.status != OK:
            msg = f"the platform answered {reply.status}: {refusal(reply)}"
            raise StepError(msg)
        body = json_of(reply)
        try:
            wheel, notebook = dict(body["wheel"]), dict(body["notebook"])
            wheel_name = _plain_name(wheel["filename"], "wheel")
            _plain_name(notebook["filename"], "notebook")
            for value in (wheel["url"], wheel["sha256"], notebook["url"]):
                _plain_text(value)
        except (KeyError, TypeError, ValueError) as err:
            msg = f"the platform's answer lacks the wheel or the notebook ({err!r})"
            raise StepError(msg) from err
        pins = body.get("pins")
        if not isinstance(pins, list) or not all(isinstance(p, str) for p in pins):
            msg = (
                "the platform did not send the package pins; "
                "ask your administrator to update the platform"
            )
            raise StepError(msg)
        if any(p.startswith("-") for p in pins):
            msg = "the platform sent an unsafe package pin"
            raise StepError(msg)
        self.passed("get-started")
        return {
            "wheel": wheel,
            "wheel_name": wheel_name,
            "notebook": notebook,
            "pins": pins,
        }

    def step_venv(self) -> str:
        """Create the private environment with Python 3.14."""
        self.current = "uv"
        uv = self.which("uv")
        if uv is None:
            self.echo(UV_HINT)
            msg = "uv is not installed"
            raise StepError(msg)
        self.passed("uv")
        self.current = "venv"
        self.child([uv, "venv", "--allow-existing", "--python", PYTHON, str(self.venv)])
        self.passed("venv")
        return uv

    def step_install(self, uv: str, found: dict[str, Any]) -> None:
        """Download the wheel, check its sha256, install it with the pins."""
        self.current = "install"
        wheel = found["wheel"]
        with tempfile.TemporaryDirectory(prefix="asteroidea-wheel-") as scratch:
            dest = Path(scratch) / found["wheel_name"]
            try:
                status = self.downloader(str(wheel["url"]), dest)
            except AsteroideaError as err:
                raise StepError(str(err)) from err
            if status != OK:
                msg = f"wheel download answered {status}"
                raise StepError(msg)
            got = _sha256(dest)
            if got != str(wheel["sha256"]):
                msg = f"wheel sha256 is {got}, expected {wheel['sha256']}"
                raise StepError(msg)
            self.child(
                [
                    uv,
                    "pip",
                    "install",
                    "--python",
                    str(self.python),
                    f"asteroidea[{EXTRAS}] @ {dest.as_uri()}",
                    *found["pins"],
                ],
                LONG,
            )
        self.passed("install")

    def step_kernel(self) -> None:
        """Register the environment as a notebook kernel."""
        self.current = "kernel"
        self.child([str(self.python), "-m", "ipykernel", "install", "--sys-prefix"])
        self.passed("kernel")

    def step_notebook(self, found: dict[str, Any]) -> Path:
        """Download the notebook; keep a differing old copy as `.bak`."""
        self.current = "notebook"
        notebook = found["notebook"]
        target = self.folder / _plain_name(notebook["filename"], "notebook")
        part = target.with_name(target.name + ".part")
        try:
            status = self.downloader(str(notebook["url"]), part)
        except AsteroideaError as err:
            raise StepError(str(err)) from err
        if status != OK:
            part.unlink(missing_ok=True)
            msg = f"notebook download answered {status}"
            raise StepError(msg)
        if target.exists() and target.read_bytes() != part.read_bytes():
            backup = target.with_name(target.name + ".bak")
            shutil.copy2(target, backup)
            self.note(f"the old notebook copy is kept as {backup}")
        part.replace(target)
        self.passed("notebook")
        return target

    # -- runs ----------------------------------------------------------------

    def pass1(self) -> None:
        """Run the install: human rows, steps 1 to 7, then print the next command."""
        for row, question in PASS1_BEFORE:
            self.ask(row, question)
        self.step_config()
        auth = self.step_sign_in()
        found = self.step_get_started(auth)
        uv = self.step_venv()
        self.step_install(uv, found)
        self.step_kernel()
        target = self.step_notebook(found)
        self.echo("Open the notebook with:")
        self.echo(f"  {self.venv}/bin/jupyter lab {target}")
        for row, question in PASS1_AFTER:
            self.ask(row, question)

    def pass2(self) -> None:
        """Run the pass-2 rows: human prompts, and SDK rows in this process."""
        self.current = "config"
        try:
            load_config(self.config_path)
        except AsteroideaError as err:
            msg = f"{err} Run the setup first (without --pass2)."
            raise StepError(msg) from err
        self.ask("2b", "Did the hosted web address open and sign you in?")
        self.ask(
            "2c", "Did Catalog list dandi-000623 and openneuro-ds004798 with counts?"
        )
        self.ask(
            "2d",
            "Did opening dandi-000623, then one file, show the file list and headers?",
        )
        self.sdk_row("2e", self.row_save)
        self.ask("2f", "Did My results list the saved result, and did its file open?")
        self.sdk_row("2g", self.row_refuse)
        self.ask("2h", "Did sign-out return you to managed login?")

    def sdk_row(self, row: str, body: Callable[[Path], str]) -> None:
        """Run one SDK row in a scratch folder; any SDK or file error is its FAIL."""
        self.current = row
        with tempfile.TemporaryDirectory(prefix="asteroidea-pass2-") as scratch:
            try:
                body(Path(scratch))
            except (AsteroideaError, OSError) as err:
                msg = f"{type(err).__name__}: {err}"
                raise StepError(msg) from err
        self.passed(row)

    def row_save(self, work: Path) -> str:
        """Query, save a result, list it, download it, compare the bytes."""
        found = self.sdk.query(dataset=DATASET)
        if not found.files or found.snapshot_id is None:
            msg = f"the catalog returned no files for {DATASET}"
            raise StepError(msg)
        sent = work / "pass2-result.txt"
        data = ("pass 2 result " + os.urandom(8).hex()).encode()
        sent.write_bytes(data)
        saved = self.sdk.save_artifact(
            sent, sources=found.files[:1], snapshot_id=found.snapshot_id
        )
        if saved.artifact_id not in [a.artifact_id for a in self.sdk.artifacts()]:
            msg = f"artifacts() does not list {saved.artifact_id}"
            raise StepError(msg)
        back = self.sdk.download_artifact(saved.artifact_id, work / "back.txt")
        if back.read_bytes() != data:
            msg = "the downloaded bytes differ from the saved bytes"
            raise StepError(msg)
        return saved.artifact_id

    def row_refuse(self, work: Path) -> str:
        """Check that an upload into another researcher's dataset is refused."""
        small = work / "pass2-refused.txt"
        small.write_text("must not arrive")
        try:
            self.sdk.upload(small, dataset=DATASET)
        except DatasetNotYoursError as err:
            return str(err)
        msg = f"the upload into {DATASET} was accepted"
        raise StepError(msg)

    def execute(self, *, pass2: bool) -> int:
        """Run pass 1 or pass 2; return the exit code."""
        self.folder.mkdir(parents=True, exist_ok=True)
        with self.report.open("a") as handle:
            stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            handle.write(f"# run {stamp}\n")
        try:
            (self.pass2 if pass2 else self.pass1)()
        except StepError as err:
            self.say(f"FAIL {self.current}: {err}")
            return EXIT_FAIL
        self.echo(f"report: {self.report}")
        return 0


def main(
    argv: Sequence[str] | None = None,
    *,
    configure: Callable[[Setup], Setup] | None = None,
) -> int:
    """Entry point of `asteroidea-setup`; `configure` swaps seams in tests."""
    parser = argparse.ArgumentParser(
        prog="asteroidea-setup", description="Guided asteroidea install."
    )
    parser.add_argument("app", nargs="?", help="the hosted web address (https://...)")
    parser.add_argument(
        "--force", action="store_true", help="replace a different platform.json"
    )
    parser.add_argument("--pass2", action="store_true", help="run the pass-2 rows")
    parser.add_argument(
        "--answers", type=Path, help="read each [y/n] answer from this file"
    )
    args = parser.parse_args(argv)
    if not args.pass2 and not args.app:
        parser.error("the hosted web address is required")
    answers = read_answers(args.answers) if args.answers else None
    setup = Setup(app=args.app or "", force=args.force, answers=answers)
    if configure is not None:
        setup = configure(setup)
    return setup.execute(pass2=args.pass2)


if __name__ == "__main__":
    sys.exit(main())
