"""Acceptance run, pure logic: step results, console, retry loop, judges, report.

`make live-first-hour` runs the first-hour rows R0 to R6 as one guided
command. Everything here is free of AWS and of the filesystem layout so that tests
can drive it with a scripted console.
"""

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

from botocore.exceptions import BotoCoreError, ClientError

from asteroidea.errors import AsteroideaError
from asteroidea_infra.admin import EMAIL_RE

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from datetime import datetime

UPLOAD_LIMIT_S: Final = 60.0
QUERY_LIMIT_S: Final = 10.0
SLICE_LIMIT_S: Final = 5.0
SLICE_LIMIT_BYTES: Final = 64 * 1024
WEB_PORT: Final = 5173
HEADLESS_DOMAIN: Final = "example.invalid"
RETRY_WORDS: Final = frozenset({"r", "retry"})
YES_WORDS: Final = frozenset({"y", "yes"})
NO_WORDS: Final = frozenset({"n", "no"})


class StepFailureError(Exception):
    """A step failed; `next_step` is the one plain thing to do about it."""

    def __init__(self, message: str, next_step: str) -> None:
        """Keep the message and the advice."""
        super().__init__(message)
        self.next_step = next_step


STEP_ERRORS: Final = (
    StepFailureError,
    AsteroideaError,
    ClientError,
    BotoCoreError,
    OSError,
    ValueError,
)


@dataclass(frozen=True)
class StepResult:
    """One line of the run: what was checked, how it went, the key number."""

    name: str
    passed: bool
    detail: str
    seconds: float = 0.0
    note: str = ""

    def line(self) -> str:
        """Return the screen line: PASS or FAIL, the detail, the substitution label."""
        word = "PASS" if self.passed else "FAIL"
        tail = f"  [{self.note}]" if self.note else ""
        return f"{word}  {self.detail}{tail}"


@dataclass
class Console:
    """Plain-text terminal. Headless mode answers every question with its default."""

    headless: bool = False
    read: Callable[[str], str] = input
    write: Callable[[str], None] = print

    def say(self, line: str = "") -> None:
        """Print one line."""
        self.write(line)

    def ask(self, prompt: str, default: str = "") -> str:
        """Ask for text; an empty answer takes `default`."""
        if self.headless:
            self.say(f"{prompt} {default} (headless: default taken)")
            return default
        suffix = f" [{default}]" if default else ""
        return self.read(f"{prompt}{suffix}: ").strip() or default

    def confirm(self, prompt: str, *, default: bool) -> bool:
        """Ask yes or no; an empty or unclear answer takes `default`."""
        if self.headless:
            self.say(f"{prompt} {'yes' if default else 'no'} (headless: default taken)")
            return default
        hint = "[Y/n]" if default else "[y/N]"
        answer = self.read(f"{prompt} {hint}: ").strip().lower()
        if answer in YES_WORDS:
            return True
        if answer in NO_WORDS:
            return False
        return default

    def pause(self, prompt: str) -> None:
        """Wait for Enter (headless: continue at once)."""
        if not self.headless:
            self.read(f"{prompt} Press Enter to continue: ")

    def offer_retry(self) -> bool:
        """Ask whether to retry the failed step; headless never retries."""
        if self.headless:
            return False
        answer = self.read("Retry this step (r) or quit (q)? [r/q]: ").strip().lower()
        return answer in RETRY_WORDS


def suggest_email(
    base: str | None, now: datetime, *, headless: bool, tag: str = "acc"
) -> str:
    """Return the suggested researcher address, or "" when none can be derived.

    Guided: the profile's address with a plus tag, `local+<tag>-<yyyymmddhhmm>@domain`.
    Headless: an `.invalid` address, so a scripted run never mails anyone. `tag` is
    `acc` for `make live-first-hour` and `ana` for `make live-analysis`.
    """
    stamp = now.strftime("%Y%m%d%H%M")
    if headless:
        return f"{tag}-{stamp}@{HEADLESS_DOMAIN}"
    if base is None or not EMAIL_RE.match(base):
        return ""
    local, _, domain = base.partition("@")
    return f"{local.split('+', 1)[0]}+{tag}-{stamp}@{domain}"


def node_hint(version: str) -> str | None:
    """Return a hint to install Node 24 (the repo pin) when `version` is another major.

    The CDK "untested Node" warning is silenced in the guided output, so the
    summary says what the real fix is.
    """
    if version.strip().startswith("v24."):
        return None
    shown = version.strip() or "not found"
    return (
        f"Hint: this machine runs Node {shown}; the repo pins Node 24. "
        "Installing Node 24 removes the warning this command hides."
    )


def run_step(
    console: Console, title: str, action: Callable[[], Sequence[StepResult]]
) -> list[StepResult]:
    """Run a step, print each result, and offer a retry after a failure."""
    while True:
        console.say(f"... {title}")
        started = time.monotonic()
        advice = "Fix the cause named above, then retry."
        try:
            results = list(action())
        except STEP_ERRORS as err:
            if isinstance(err, StepFailureError):
                advice = err.next_step
            elapsed = time.monotonic() - started
            text = (
                str(err)
                if isinstance(err, StepFailureError)
                else f"{type(err).__name__}: {err}"
            )
            results = [StepResult(title, passed=False, detail=text, seconds=elapsed)]
        for result in results:
            console.say(result.line())
        if all(r.passed for r in results):
            return results
        console.say(f"Next step: {advice}")
        if not console.offer_retry():
            return results


@dataclass(frozen=True)
class Listener:
    """A process that holds the web port."""

    pid: int
    command: str
    cwd: str = ""


def release_port(
    console: Console,
    port: int,
    find: Callable[[int], Listener | None],
    stop: Callable[[int], None],
) -> Listener | None:
    """Free `port`, asking before any process is stopped.

    Returns the stopped listener, or None when the port was free. Raises
    `StepFailureError` when the tester declines (or in headless mode, which never
    stops a process).
    """
    held = find(port)
    if held is None:
        return None
    console.say(f"Port {port} is in use by pid {held.pid}: {held.command}")
    allowed = not console.headless and console.confirm(
        f"Stop it so the sign-in page can use port {port}?", default=False
    )
    if not allowed:
        msg = f"port {port} is in use and was left running"
        raise StepFailureError(
            msg,
            f"Sign-in only works on port {port}; stop that server yourself, "
            "or retry and answer y.",
        )
    stop(held.pid)
    return held


def judge_upload(seconds: float) -> StepResult:
    """R3: the row is visible within 60 s."""
    ok = seconds <= UPLOAD_LIMIT_S
    return StepResult(
        "Upload",
        ok,
        f"Upload visible after {seconds:.1f} s (limit {UPLOAD_LIMIT_S:.0f} s)",
        seconds,
    )


def judge_query(q1_seconds: float, q2_seconds: float) -> StepResult:
    """R4: each query answers within 10 s."""
    worst = max(q1_seconds, q2_seconds)
    return StepResult(
        "Query",
        worst <= QUERY_LIMIT_S,
        f"Queries answered in {q1_seconds:.1f} s and {q2_seconds:.1f} s "
        f"(limit {QUERY_LIMIT_S:.0f} s)",
        q1_seconds + q2_seconds,
    )


def judge_snapshot(
    *, same_rows: bool, new_visible: bool, new_hidden: bool
) -> StepResult:
    """Judge that the pinned snapshot repeats its rows and hides the later upload."""
    ok = same_rows and new_visible and new_hidden
    detail = (
        "Pinned snapshot repeats the old rows and hides the second upload"
        if ok
        else f"Pinned snapshot check failed (same rows {same_rows}, second upload "
        f"visible now {new_visible}, hidden at old snapshot {new_hidden})"
    )
    return StepResult("Pinned snapshot", ok, detail)


def judge_slice(seconds: float, fetched: int, *, same: bool) -> StepResult:
    """R5: one slice within 5 s and 64 KiB, with the right voxel values."""
    ok = seconds <= SLICE_LIMIT_S and fetched <= SLICE_LIMIT_BYTES and same
    kib = fetched / 1024
    return StepResult(
        "Slice read",
        ok,
        f"One slice read in {seconds:.1f} s, {kib:.0f} KiB fetched "
        f"(limits {SLICE_LIMIT_S:.0f} s, {SLICE_LIMIT_BYTES // 1024} KiB)"
        + ("" if same else "; voxel values differ from the sample"),
        seconds,
    )


def format_summary(results: Sequence[StepResult]) -> list[str]:
    """Return the closing table: step, result, key number."""
    width = max([len(r.name) for r in results] + [4])
    rows = [
        f"{'Step':<{width}}  Result  Key number",
        f"{'-' * width}  ------  ----------",
    ]
    rows.extend(
        f"{r.name:<{width}}  {'PASS' if r.passed else 'FAIL':<6}  {r.detail}"
        for r in results
    )
    return rows


def all_passed(results: Sequence[StepResult], *, complete: bool) -> bool:
    """Return True when every step ran (`complete`) and every result passed."""
    return complete and bool(results) and all(r.passed for r in results)


@dataclass
class Report:
    """What the run writes to `report.md`: results, substitutions and the ids."""

    run_id: str
    mode: str
    commit: str
    results: list[StepResult] = field(default_factory=list)
    ids: dict[str, object] = field(default_factory=dict)
    title: str = "Acceptance run"

    def markdown(self) -> str:
        """Return the report text."""
        lines = [
            f"# {self.title} {self.run_id}",
            "",
            f"- mode: {self.mode}",
            f"- commit: {self.commit}",
            "",
            "## Steps",
            "",
            "| Step | Result | Seconds | Detail | Substitution |",
            "|---|---|---|---|---|",
        ]
        lines.extend(
            f"| {r.name} | {'PASS' if r.passed else 'FAIL'} | {r.seconds:.1f} "
            f"| {r.detail} | {r.note} |"
            for r in self.results
        )
        lines.extend(["", "## Ids", ""])
        lines.extend(f"- {key}: {value}" for key, value in sorted(self.ids.items()))
        return "\n".join(lines) + "\n"
