"""In `make rf-run` and `make web-smoke` a FAIL does not stop the run.

Every step runs, then the prompts; a step whose need failed is marked "not
reached"; the exit status still reports every FAIL. `make analysis`, `acceptance` and
`outsider-run` keep `analysis.run_all` (stop at the first failure).
"""

import dataclasses
from typing import TYPE_CHECKING, Any

import pytest

from asteroidea_infra import (
    analysis_check as analysis,
)
from asteroidea_infra import (
    research_app_check as rf_run,
)
from asteroidea_infra import (
    web_smoke,
)
from asteroidea_infra.steps import Console, StepResult

if TYPE_CHECKING:
    from pathlib import Path

BROWSER2 = "Driving R9 to R12 in a headless browser"
SDK = "R13 in the fresh venv"
INSTALL = "Building and installing the SDK"


def ok(title: str) -> Any:
    return lambda _run: [StepResult(title, passed=True, detail="ok")]


def bad(title: str) -> Any:
    return lambda _run: [StepResult(title, passed=False, detail="broke")]


def pass_two_steps(**actions: Any) -> tuple[tuple[str, Any], ...]:
    """Return the pass-2 titles with a passing fake, except those in `actions`."""
    return tuple((title, actions.get(title, ok(title))) for title, _ in rf_run.STEPS2)


@pytest.fixture
def run(tmp_path: Path) -> analysis.AnalysisRun:
    return analysis.AnalysisRun(
        Console(headless=True), "20261026120000", tmp_path, "p", "1"
    )


def test_a_failing_browser_row_does_not_stop_r10_to_r13_or_the_prompts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ran: list[str] = []

    def rows(_run: object) -> list[Any]:
        ran.append("browser")
        return [
            StepResult("R9 Image", passed=False, detail="FAILED in the browser"),
            StepResult("R10 Label", passed=True, detail="ok"),
        ]

    def sdk(_run: object) -> list[Any]:
        ran.append("sdk")
        return [StepResult("R13 SDK", passed=True, detail="ok")]

    steps = pass_two_steps(**{BROWSER2: rows, SDK: sdk})
    pass_spec = dataclasses.replace(rf_run.PASS_TWO, steps=steps, finish=None)
    monkeypatch.setattr(rf_run, "PASSES", {1: rf_run.PASS_ONE, 2: pass_spec})
    monkeypatch.setenv("ASTEROIDEA_PROFILE", "p")
    monkeypatch.setenv("ASTEROIDEA_ACCOUNT", "1")
    monkeypatch.setenv(rf_run.HOME_ENV, str(tmp_path))
    monkeypatch.setattr(rf_run, "commit_sha", lambda: "abc1234")
    asked: list[str] = []

    def read(text: str) -> str:
        asked.append(text)
        return "y"

    assert rf_run.run_live(read=read, passes=2) == 1
    assert ran == ["browser", "sdk"]
    assert len(asked) == len(rf_run.HUMAN_ROWS2)
    out = capsys.readouterr().out
    assert "RF FAIL" in out
    assert "R13 SDK" in out


def test_a_failed_install_marks_r13_not_reached_and_runs_the_rest(
    run: analysis.AnalysisRun,
) -> None:
    ran: list[str] = []

    def browser(_run: object) -> list[Any]:
        ran.append("browser")
        return [StepResult("R9", passed=True, detail="ok")]

    steps = pass_two_steps(**{INSTALL: bad(INSTALL), BROWSER2: browser})
    assert analysis.run_continuing(run, steps, rf_run.NEEDS2) is True
    assert ran == ["browser"]
    by_name = {r.name: r for r in run.results}
    assert not by_name[SDK].passed
    assert f"not reached: {INSTALL}" in by_name[SDK].detail
    assert "R9" in by_name


def test_a_failed_sign_in_marks_everything_after_it_not_reached(
    run: analysis.AnalysisRun,
) -> None:
    steps = pass_two_steps(**{"Signing in": bad("Signing in")})
    analysis.run_continuing(run, steps, rf_run.NEEDS2)
    detail = {r.name: r.detail for r in run.results}
    for title in (
        "Creating the second researcher",
        "Reading the targets and warming the routes",
        BROWSER2,
        SDK,
    ):
        assert detail[title].startswith("not reached"), title
    assert detail["Creating the researcher"] == "ok"
    assert detail[INSTALL] == "ok"


def test_run_all_still_stops_at_the_first_failure(run: analysis.AnalysisRun) -> None:
    steps = pass_two_steps(**{"Signing in": bad("Signing in")})
    assert analysis.run_all(run, steps) is False
    assert [r.name for r in run.results][-1] == "Signing in"


def test_a_row_the_spec_marked_not_reached_is_a_fail_that_says_so() -> None:
    row = {"ok": False, "notReached": True, "detail": "R10 created no label"}
    got = rf_run.judge_row(rf_run.ROWS2[1], row)
    assert not got.passed
    assert got.detail == "not reached: R10 created no label"


def test_every_needs_key_and_value_is_a_step_title() -> None:
    for steps, needs in (
        (rf_run.STEPS, rf_run.NEEDS),
        (rf_run.STEPS2, rf_run.NEEDS2),
    ):
        titles = {t for t, _ in steps}
        assert set(needs) <= titles
        assert {n for v in needs.values() for n in v} <= titles


def test_web_smoke_needs_name_real_steps() -> None:
    titles = {t for t, _ in web_smoke.STEPS_LIVE}
    assert set(web_smoke.NEEDS) <= titles
    assert {n for v in web_smoke.NEEDS.values() for n in v} <= titles
