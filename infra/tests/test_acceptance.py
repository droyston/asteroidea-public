"""`make acceptance`: the pure logic. No AWS, no browser."""

import os
import socket
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from asteroidea.errors import SignInRequiredError
from asteroidea_infra import first_hour
from asteroidea_infra.first_hour import find_listener
from asteroidea_infra.runner import TIMEOUT_CODE, execute, must
from asteroidea_infra.steps import (
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
    from collections.abc import Callable

NOW = datetime(2026, 10, 23, 20, 5, tzinfo=UTC)


def scripted(
    *answers: str, headless: bool = False
) -> tuple[Console, list[str], list[str]]:
    """Return a console fed `answers`, the prompts it asked, the lines it said."""
    queue = list(answers)
    asked: list[str] = []
    said: list[str] = []

    def read(prompt: str) -> str:
        asked.append(prompt)
        return queue.pop(0)

    return Console(headless=headless, read=read, write=said.append), asked, said


def test_result_line_says_pass_or_fail_and_labels_a_substitution() -> None:
    ok = StepResult("Upload", passed=True, detail="Upload visible after 9.9 s")
    bad = StepResult("Sign in", passed=False, detail="no", note="substitution: x")
    assert ok.line() == "PASS  Upload visible after 9.9 s"
    assert bad.line() == "FAIL  no  [substitution: x]"


def test_summary_table_lists_every_step_with_its_number() -> None:
    rows = format_summary(
        [
            StepResult("Upload", passed=True, detail="9.9 s (limit 60 s)"),
            StepResult("Query", passed=False, detail="12.0 s (limit 10 s)"),
        ]
    )
    assert rows[0].split() == ["Step", "Result", "Key", "number"]
    assert rows[2].startswith("Upload")
    assert "PASS" in rows[2]
    assert "9.9 s" in rows[2]
    assert rows[3].startswith("Query")
    assert "FAIL" in rows[3]
    assert all(row.isascii() for row in rows)


def test_exit_is_zero_only_when_every_step_ran_and_passed() -> None:
    good = [StepResult("a", passed=True, detail="")]
    bad = [*good, StepResult("b", passed=False, detail="")]
    assert all_passed(good, complete=True)
    assert not all_passed(good, complete=False)
    assert not all_passed(bad, complete=True)
    assert not all_passed([], complete=True)


def test_email_suggestion_tags_the_profile_address_with_the_minute() -> None:
    assert (
        suggest_email("own.er.name@example.org", NOW, headless=False)
        == "own.er.name+acc-202610232005@example.org"
    )
    assert (
        suggest_email("owner+budget@example.com", NOW, headless=False)
        == "owner+acc-202610232005@example.com"
    )


def test_email_suggestion_is_empty_without_a_usable_profile_address() -> None:
    assert suggest_email(None, NOW, headless=False) == ""
    assert suggest_email("not-an-address", NOW, headless=False) == ""


def test_headless_email_is_an_invalid_address_so_nothing_is_mailed() -> None:
    email = suggest_email("own.er.name@example.org", NOW, headless=True)
    assert email == "acc-202610232005@example.invalid"


def test_ask_takes_the_default_on_enter_and_the_answer_otherwise() -> None:
    console, asked, _ = scripted("", "me@x.org")
    assert console.ask("Email", "a@b.co") == "a@b.co"
    assert console.ask("Email", "a@b.co") == "me@x.org"
    assert asked[0] == "Email [a@b.co]: "


def test_headless_console_never_reads_the_keyboard() -> None:
    console, asked, said = scripted(headless=True)
    assert console.ask("Email", "d@x.io") == "d@x.io"
    assert console.confirm("Stop it?", default=False) is False
    console.pause("Go on.")
    assert asked == []
    assert said


def listener(_port: int) -> Listener:
    return Listener(4242, "node ./node_modules/.bin/vite")


def test_free_port_stops_nothing() -> None:
    console, asked, _ = scripted()
    stopped: list[int] = []
    assert release_port(console, 5173, lambda _p: None, stopped.append) is None
    assert stopped == []
    assert asked == []


@pytest.mark.parametrize("answer", ["n", "", "no", "maybe"])
def test_a_process_is_never_stopped_without_a_yes(answer: str) -> None:
    console, asked, said = scripted(answer)
    stopped: list[int] = []
    with pytest.raises(StepFailureError) as err:
        release_port(console, 5173, listener, stopped.append)
    assert stopped == []
    assert "5173" in str(err.value)
    assert any("4242" in line and "vite" in line for line in said)
    assert len(asked) == 1


@pytest.mark.parametrize("answer", ["y", "YES"])
def test_a_yes_stops_exactly_the_shown_process(answer: str) -> None:
    console, _, _ = scripted(answer)
    stopped: list[int] = []
    held = release_port(console, 5173, listener, stopped.append)
    assert stopped == [4242]
    assert held == listener(5173)


def test_headless_never_stops_a_process() -> None:
    console, _, _ = scripted(headless=True)
    stopped: list[int] = []
    with pytest.raises(StepFailureError):
        release_port(console, 5173, listener, stopped.append)
    assert stopped == []


def flaky(fails: int) -> Callable[[], list[StepResult]]:
    calls = {"n": 0}

    def action() -> list[StepResult]:
        calls["n"] += 1
        if calls["n"] <= fails:
            msg = "boom"
            raise StepFailureError(msg, "Fix the thing.")
        return [StepResult("x", passed=True, detail="fine")]

    return action


def test_a_failed_step_prints_the_next_step_and_retries_on_r() -> None:
    console, _, said = scripted("r")
    results = run_step(console, "Doing x", flaky(1))
    assert [r.passed for r in results] == [True]
    assert "FAIL  boom" in said
    assert "Next step: Fix the thing." in said


def test_quit_stops_after_the_failure() -> None:
    console, _, said = scripted("q")
    results = run_step(console, "Doing x", flaky(5))
    assert [r.passed for r in results] == [False]
    assert said.count("FAIL  boom") == 1


def test_headless_failure_is_not_retried() -> None:
    console, asked, _ = scripted(headless=True)
    assert not run_step(console, "Doing x", flaky(5))[0].passed
    assert asked == []


def test_a_library_exception_keeps_its_class_and_message() -> None:
    def action() -> list[StepResult]:
        raise SignInRequiredError

    console, _, said = scripted(headless=True)
    run_step(console, "Doing x", action)
    assert any(
        line.startswith("FAIL  SignInRequiredError: You are not signed in")
        for line in said
    )


def test_limits_are_inclusive_and_the_slice_needs_the_right_voxels() -> None:
    assert judge_upload(60.0).passed
    assert not judge_upload(60.1).passed
    assert judge_query(10.0, 1.0).passed
    assert not judge_query(1.0, 10.1).passed
    assert judge_slice(5.0, 64 * 1024, same=True).passed
    assert not judge_slice(5.0, 64 * 1024 + 1, same=True).passed
    assert not judge_slice(1.0, 1, same=False).passed
    assert "(limit 60 s)" in judge_upload(9.94).detail
    assert "9.9 s" in judge_upload(9.94).detail


def test_snapshot_check_needs_all_three_conditions() -> None:
    assert judge_snapshot(same_rows=True, new_visible=True, new_hidden=True).passed
    assert not judge_snapshot(same_rows=False, new_visible=True, new_hidden=True).passed
    assert not judge_snapshot(same_rows=True, new_visible=False, new_hidden=True).passed
    assert not judge_snapshot(same_rows=True, new_visible=True, new_hidden=False).passed


def test_the_first_hour_run_checks_no_commit_of_this_repository() -> None:
    """No step tests the checkout for a fixed commit; a clone may lack it."""
    assert not hasattr(first_hour, "WRITTEN_FOR")
    assert "Checking this checkout" not in [title for title, _ in first_hour.STEPS]


def test_report_names_substitutions_and_ids() -> None:
    report = Report("20261023200500", "headless", "abc1234")
    report.results = [
        StepResult("Sign in", passed=True, detail="ok", note="substitution: s")
    ]
    report.ids = {"dataset": "sample-1", "snapshot_id": 7}
    text = report.markdown()
    assert "substitution: s" in text
    assert "- dataset: sample-1" in text
    assert "- snapshot_id: 7" in text


def test_must_quotes_the_last_error_line_of_a_failed_command() -> None:
    with pytest.raises(StepFailureError) as err:
        must(["sh", "-c", "echo first >&2; echo last >&2; exit 3"], next_step="Fix it.")
    assert str(err.value) == "sh failed: last"
    assert err.value.next_step == "Fix it."


def test_execute_captures_output() -> None:
    done = execute(["sh", "-c", "echo out; echo err >&2; exit 2"])
    assert (done.code, done.out.strip(), done.err.strip()) == (2, "out", "err")


def test_find_listener_reports_the_process_holding_a_port() -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        held = find_listener(int(sock.getsockname()[1]))
    assert held is not None
    assert held.pid == os.getpid()


def test_node_hint_appears_unless_node_is_24() -> None:
    assert node_hint("v24.1.0") is None
    assert "Node 24" in (node_hint("v26.8.2") or "")
    assert "v26.8.2" in (node_hint("v26.8.2") or "")
    assert "not found" in (node_hint("") or "")


def test_a_command_that_outlives_its_timeout_is_killed_and_reported() -> None:
    done = execute(["sleep", "5"], timeout=0.2)
    assert done.code == TIMEOUT_CODE
    assert "timed out" in done.err
