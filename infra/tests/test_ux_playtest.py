"""`make live-ux-playtest`: offline parts.

The browser run, the AWS calls and the human prompts are LIVE and run by hand; here the
readings, the miss lines, the judge, the prompts, the targets and the glue run against
fakes.
"""

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import pytest

from asteroidea_infra import analysis_check as analysis
from asteroidea_infra import ux_playtest
from asteroidea_infra.steps import Console, StepFailureError

if TYPE_CHECKING:
    from collections.abc import Sequence

REPO: Final = Path(__file__).resolve().parents[2]
SPEC_TEXT: Final = (REPO / "web" / ux_playtest.SPEC).read_text()


def record(
    page: str,
    viewport: str = "desktop",
    *,
    wait: float = 1.0,
    reached: bool = True,
    **more: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "viewport": viewport,
        "page": page,
        "ready": page,
        "reached": reached,
        "via": "click",
        "wait_s": wait,
        "screenshot": f"/out/{viewport}-{page}.png",
        "requests": [],
        "checks": [],
        **more,
    }


def full(viewport: str) -> list[dict[str, Any]]:
    return [record(p, viewport) for p in ux_playtest.PAGES]


def both() -> list[dict[str, Any]]:
    return [*full("desktop"), *full("phone")]


# --- the spec, the config and the target --------------------------------------------


def test_the_spec_waits_on_the_ready_mark_and_walks_both_sizes() -> None:
    assert 'main[data-ready="${ready}"]' in SPEC_TEXT
    assert "width: 1440, height: 900" in SPEC_TEXT
    assert "width: 375, height: 812" in SPEC_TEXT
    assert 'process.env.LIVE === "1"' in SPEC_TEXT
    for page in ux_playtest.PAGES:
        assert f'"{page}"' in SPEC_TEXT, page


def test_the_spec_reads_the_environment_the_module_sets(tmp_path: Path) -> None:
    run = analysis.AnalysisRun(Console(headless=True), "id", tmp_path, "p", "1")
    run.outputs.update(
        {
            "ApiUrl": "https://api",
            "WebUrl": "https://app",
            "access_token": "a",
            "refresh_token": "r",
        }
    )
    env = ux_playtest.browser_env(run)
    assert env["LIVE"] == "1"
    for name in (
        "UX_APP_URL",
        "UX_API_URL",
        "UX_ACCESS_TOKEN",
        "UX_REFRESH_TOKEN",
        "UX_TARGETS_FILE",
        "UX_OUT",
    ):
        assert name in env
        assert name in SPEC_TEXT, name


def test_the_live_config_includes_the_spec_and_the_fixture_config_runs_it() -> None:
    live = (REPO / "web" / ux_playtest.CONFIG).read_text()
    assert r"/ux-playtest\.spec\.ts$/" in live
    fixture = (REPO / "web" / "playwright.config.ts").read_text()
    assert "ux-playtest" not in fixture
    assert r"/\/live\//" in fixture


def test_the_make_target_is_live_guarded_and_takes_no_prompt() -> None:
    text = (REPO / "Makefile").read_text()
    start = text.index("live-ux-playtest: ## LIVE:")
    block = text[start : text.index("\n\n", start)]
    assert "$(SANDBOX_GUARD)" in block
    assert "NO_PROMPT" in block
    assert "asteroidea_infra.ux_playtest run" in block
    assert "live-ux-playtest" in text.split("\n", 5)[3]


# --- readings -----------------------------------------------------------------------


def test_a_slow_wait_and_a_slow_request_are_misses_not_failures() -> None:
    slow = record(
        "session",
        wait=8.2,
        requests=[
            {"method": "GET", "path": "/compute/rates?x=1", "status": 200, "ms": 6400},
            {"method": "GET", "path": "/catalog/studies", "status": 200, "ms": 120},
        ],
    )
    lines = ux_playtest.miss_lines([slow])
    assert len(lines) == 2
    assert all(line.startswith("miss") for line in lines)
    assert "8.2 s" in lines[0]
    assert "/compute/rates" in lines[1]
    assert "6.4 s" in lines[1]
    assert ux_playtest.judge_page(slow).passed


def test_nothing_over_the_budget_gives_no_miss() -> None:
    assert ux_playtest.miss_lines(both()) == []
    edge = record("home", wait=ux_playtest.BUDGET_S)
    assert ux_playtest.miss_lines([edge]) == []


def test_a_page_never_reached_is_no_miss_but_a_failure() -> None:
    gone = record("study", reached=False, wait=90.0)
    assert ux_playtest.miss_lines([gone]) == []
    got = ux_playtest.judge_page(gone)
    assert not got.passed
    assert "never reached" in got.detail


def test_timing_lines_name_each_page_wait_and_slowest_request() -> None:
    rows = [
        record(
            "home",
            wait=1.234,
            requests=[{"method": "GET", "path": "/a", "status": 200, "ms": 900}],
        ),
        record("study", reached=False),
    ]
    lines = ux_playtest.timing_lines(rows)
    assert "1.23 s" in lines[0]
    assert "slowest 0.90 s" in lines[0]
    assert "not reached" in lines[1]


# --- judging ------------------------------------------------------------------------


def test_a_clean_walk_passes_every_page_at_both_sizes() -> None:
    results = ux_playtest.judge_all(both())
    assert len(results) == 2 * len(ux_playtest.PAGES)
    assert all(r.passed for r in results)
    assert results[0].name == "desktop home"


def test_a_failed_check_fails_its_page_and_names_the_check_and_screenshot() -> None:
    bad = record(
        "home",
        "phone",
        checks=[{"name": "no sideways scroll", "ok": False, "detail": "at phone"}],
    )
    got = ux_playtest.judge_page(bad)
    assert not got.passed
    assert got.name == "phone home"
    assert "no sideways scroll" in got.detail
    assert "/out/phone-home.png" in got.detail


def test_a_page_a_viewport_never_recorded_fails() -> None:
    records = [
        r for r in both() if not (r["viewport"] == "phone" and r["page"] == "file")
    ]
    failed = [r for r in ux_playtest.judge_all(records) if not r.passed]
    assert [r.name for r in failed] == ["phone file"]
    assert "did not reach" in failed[0].detail


def test_parse_records_reads_each_viewport_and_refuses_junk() -> None:
    got = ux_playtest.parse_records({"desktop": json.dumps([record("home")])})
    assert got[0]["page"] == "home"
    with pytest.raises(StepFailureError, match="not JSON"):
        ux_playtest.parse_records({"phone": "{"})
    with pytest.raises(StepFailureError, match="not a list"):
        ux_playtest.parse_records({"phone": "{}"})


# --- the human steps ----------------------------------------------------------------

STUDIES: Final = {
    "studies": [
        {
            "study": "bmovie",
            "title": "Movie watching",
            "datasets": [
                {"dataset": "dandi-000623", "role": "ephys"},
                {"dataset": "openneuro-ds004798", "role": "mri"},
            ],
        }
    ]
}
LINKS: Final = {
    "links": [
        {
            "dataset": "dandi-000623",
            "subject": "CS41",
            "subject_key": "bmovie-CS41",
            "status": "pending",
        },
        {
            "dataset": "openneuro-ds004798",
            "subject": "CS41",
            "subject_key": "bmovie-CS41",
            "status": "pending",
        },
    ]
}


def test_the_expected_answer_lists_studies_modalities_and_participants() -> None:
    text = "\n".join(ux_playtest.expected_answer(STUDIES, LINKS))
    assert "Movie watching" in text
    assert "dandi-000623 (ephys)" in text
    assert "openneuro-ds004798 (mri)" in text
    assert "participant bmovie-CS41" in text
    assert "pending" in text


def asking(answers: Sequence[str]) -> tuple[Console, list[str]]:
    said: list[str] = []
    queue = list(answers)
    return Console(
        headless=False, read=lambda _p: queue.pop(0), write=said.append
    ), said


def test_the_human_steps_are_rows_1_3_and_6() -> None:
    assert [s.row for s in ux_playtest.HUMAN_STEPS] == ["Row 1", "Row 3", "Row 6"]


def test_each_human_step_is_pass_on_yes_and_fail_otherwise() -> None:
    console, said = asking(["y", "n", ""])
    results = ux_playtest.human_pass(console, ["The routes answer:", "  study bmovie"])
    assert [r.passed for r in results] == [True, False, False]
    assert [r.line().split()[0] for r in results] == ["PASS", "FAIL", "FAIL"]
    assert any("study bmovie" in line for line in said)
    assert "squint test 2" in "\n".join(said)


# --- targets against fakes ----------------------------------------------------------


@pytest.fixture
def run(tmp_path: Path) -> analysis.AnalysisRun:
    made = analysis.AnalysisRun(Console(headless=True), "id", tmp_path, "p", "1")
    made.email = "ux@example.invalid"
    return made


def fake_api(asset: dict[str, Any]) -> Any:
    def fake_get(_run: object, path: str) -> tuple[Any, float]:
        if path == "/catalog/studies":
            return STUDIES, 0.1
        if path == "/catalog/links":
            return LINKS, 0.1
        assert path == f"/catalog/assets/{ux_playtest.CS62_R2}"
        return {"asset": asset}, 0.1

    return fake_get


def test_find_targets_writes_the_study_session_and_expected_answer(
    run: analysis.AnalysisRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    asset = {"dataset": "dandi-000623", "subject": "sub-CS62", "bids_session": "ses-1"}
    monkeypatch.setattr(ux_playtest, "api_get", fake_api(asset))
    (result,) = ux_playtest.find_targets(run)
    assert result.passed
    saved = json.loads((run.base / ux_playtest.TARGETS_FILE).read_text())
    assert saved["study"] == "bmovie"
    assert saved["titles"] == ["Movie watching"]
    assert saved["session"] == "ses-1"
    assert saved["asset"] == ux_playtest.CS62_R2
    assert any("participant" in line for line in saved["expected"])


def test_find_targets_uses_the_no_session_segment(
    run: analysis.AnalysisRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    asset = {"dataset": "dandi-000623", "subject": "s", "bids_session": None}
    monkeypatch.setattr(ux_playtest, "api_get", fake_api(asset))
    ux_playtest.find_targets(run)
    saved = json.loads((run.base / ux_playtest.TARGETS_FILE).read_text())
    assert saved["session"] == "_"


def test_find_targets_fails_when_no_study_holds_the_dataset(
    run: analysis.AnalysisRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    asset = {"dataset": "elsewhere", "subject": "s", "bids_session": "1"}
    monkeypatch.setattr(ux_playtest, "api_get", fake_api(asset))
    with pytest.raises(StepFailureError, match="no study contains"):
        ux_playtest.find_targets(run)


# --- report and entry point ---------------------------------------------------------


def test_the_report_lists_misses_apart_from_failures_and_flags_skipped_prompts(
    run: analysis.AnalysisRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ux_playtest, "commit_sha", lambda: "abc")
    (run.base / "misses.txt").write_text("miss  desktop session: 8.2 s\n")
    run.results.extend(ux_playtest.judge_all(both()))
    text = ux_playtest.write_report(run, 12.0, asked=False).read_text()
    assert "## Misses" in text
    assert "- miss  desktop session: 8.2 s" in text
    assert "## Failures\n\nNone." in text
    assert "not a pass" in text


def test_the_log_tee_prints_and_appends(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write = ux_playtest.tee(tmp_path / "log.txt")
    write("one")
    write("two")
    assert (tmp_path / "log.txt").read_text() == "one\ntwo\n"
    assert capsys.readouterr().out == "one\ntwo\n"


def test_main_refuses_an_unknown_command(capsys: pytest.CaptureFixture[str]) -> None:
    assert ux_playtest.main(["nope"]) == 2
    assert "usage" in capsys.readouterr().err


def test_main_passes_no_prompt_through(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[bool] = []

    def fake(*, prompt: bool = True) -> int:
        seen.append(prompt)
        return 0

    monkeypatch.setattr(ux_playtest, "run_live", fake)
    assert ux_playtest.main(["run", "--no-prompt"]) == 0
    assert ux_playtest.main(["run"]) == 0
    assert seen == [False, True]
