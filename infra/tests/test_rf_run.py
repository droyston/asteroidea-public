"""`make rf-run`: offline parts.

The browser run and the AWS calls are LIVE and run by hand; here the row table, the
budgets, the judge, the report writer, the guard and the runner's glue run against
fakes.
"""

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import pytest

from asteroidea_infra import (
    analysis_check as analysis,
)
from asteroidea_infra import (
    first_hour as acceptance,
)
from asteroidea_infra import (
    research_app_check as rf_run,
)
from asteroidea_infra.runner import Proc
from asteroidea_infra.sandbox_guard import EXIT_NO_SANDBOX
from asteroidea_infra.steps import Console, StepFailureError

if TYPE_CHECKING:
    from collections.abc import Sequence

REPO: Final = Path(__file__).resolve().parents[2]
ROWS: Final = REPO / "infra" / "tests" / "fixtures" / "rf_acceptance_rows.md"
GOOD: Final = {"ok": True, "seconds": 1.0, "detail": "fine", "parts": {}}


def good_rows() -> dict[str, dict[str, Any]]:
    """Every row green and well inside its budget."""
    rows: dict[str, dict[str, Any]] = {}
    for spec in rf_run.ROWS:
        parts = {
            lim.part: (1.0 if lim.unit == rf_run.UNIT_S else 1000.0)
            for lim in spec.limits
            if lim.part != "row"
        }
        rows[spec.row] = {"row": spec.row, **GOOD, "parts": parts}
    return rows


def limits(row: str) -> dict[str, float]:
    spec = next(s for s in rf_run.ROWS if s.row == row)
    return {lim.part: lim.ceiling for lim in spec.limits}


# --- the row table and the budgets ------------------------------------------


def test_the_rows_are_r0_to_r8_in_order() -> None:
    assert tuple(f"R{n}" for n in range(9)) == rf_run.ROW_IDS
    assert rf_run.ROW_IDS == rf_run.PASS_ONE_ROWS


def test_the_human_pass_repeats_r1_to_r8() -> None:
    assert [s.row for s in rf_run.HUMAN_ROWS] == [f"R{n}" for n in range(1, 9)]


def test_every_row_id_is_a_row_of_the_acceptance_table() -> None:
    table_rows = set(re.findall(r"^\| (R\d+) \|", ROWS.read_text(), re.MULTILINE))
    assert set(rf_run.ROW_IDS) <= table_rows


@pytest.mark.parametrize(
    ("row", "want"),
    [
        ("R0", {"row": 5.0}),
        ("R1", {"row": 3.16}),
        ("R2", {"row": 4.0}),
        ("R3", {"subject": 4.0, "files": 4.0, "file": 4.0}),
        ("R4", {"row": 8.0, "body": 5.0 * 1024 * 1024}),
        ("R5", {"row": 10.0}),
        ("R6", {"row": 4.0}),
        ("R7", {"overview": 15.0, "window": 15.0, "pan": 15.0}),
        ("R8", {"row": 5.0 + 15.0 + 15.0}),
    ],
)
def test_budgets_match_the_table(row: str, want: dict[str, float]) -> None:
    assert limits(row) == want


def test_r7_reads_the_overview_then_a_60_s_window_then_one_pan() -> None:
    spec = next(s for s in rf_run.ROWS if s.row == "R7")
    assert [lim.part for lim in spec.limits] == ["overview", "window", "pan"]
    assert "60 s window at t=300" in spec.title


def test_the_spec_file_and_config_exist() -> None:
    assert (REPO / "web" / rf_run.SPEC).is_file()
    assert (REPO / "web" / rf_run.CONFIG).is_file()


def test_the_spec_writes_every_row_the_judge_reads() -> None:
    text = (REPO / "web" / rf_run.SPEC).read_text()
    for row in rf_run.ROW_IDS:
        assert f'run("{row}"' in text
    for spec in rf_run.ROWS:
        for lim in spec.limits:
            assert lim.part == "row" or f'"{lim.part}"' in text


# --- the judge ------------------------------------------------------------------------


def test_every_row_inside_its_budget_passes() -> None:
    results = rf_run.judge_all(good_rows())
    assert [r.passed for r in results] == [True] * 9
    assert results[0].note == rf_run.TOKENS_NOTE


def test_red_proof_a_row_over_its_budget_fails_and_names_the_part() -> None:
    rows = good_rows()
    rows["R7"]["parts"]["window"] = 15.5
    results = rf_run.judge_all(rows)
    r7 = results[7]
    assert not r7.passed
    assert "window over its limit" in r7.detail
    assert all(r.passed for i, r in enumerate(results) if i != 7)


def test_a_row_is_judged_on_its_row_part_when_the_spec_sets_one() -> None:
    spec = next(s for s in rf_run.ROWS if s.row == "R4")
    slow_setup = {
        "ok": True,
        "seconds": 30.0,
        "detail": "",
        "parts": {"row": 7.9, "body": 100.0},
    }
    assert rf_run.judge_row(spec, slow_setup).passed
    slow_call = {
        "ok": True,
        "seconds": 9.0,
        "detail": "",
        "parts": {"row": 8.1, "body": 100.0},
    }
    assert not rf_run.judge_row(spec, slow_call).passed


def test_a_body_over_5_mb_fails_r4() -> None:
    spec = next(s for s in rf_run.ROWS if s.row == "R4")
    big = {
        "ok": True,
        "seconds": 1.0,
        "detail": "",
        "parts": {"row": 1.0, "body": 5 * 1024 * 1024 + 1},
    }
    got = rf_run.judge_row(spec, big)
    assert not got.passed
    assert "body over its limit" in got.detail


def test_a_row_the_browser_never_reached_fails() -> None:
    rows = good_rows()
    del rows["R5"]
    results = rf_run.judge_all(rows)
    assert not results[5].passed
    assert "did not reach" in results[5].detail


def test_a_row_that_failed_in_the_browser_fails_with_its_message() -> None:
    rows = good_rows()
    rows["R4"] = {
        "row": "R4",
        "ok": False,
        "seconds": 90.0,
        "detail": "Error: 41 trials",
        "parts": {},
    }
    got = rf_run.judge_all(rows)[4]
    assert not got.passed
    assert "Error: 41 trials" in got.detail


def test_a_part_the_spec_did_not_measure_fails() -> None:
    rows = good_rows()
    del rows["R7"]["parts"]["pan"]
    got = rf_run.judge_all(rows)[7]
    assert not got.passed
    assert "pan not measured" in got.detail


def test_parse_rows_keys_by_row_and_rejects_bad_files() -> None:
    parsed = rf_run.parse_rows(json.dumps([{"row": "R0", "ok": True}]))
    assert set(parsed) == {"R0"}
    with pytest.raises(StepFailureError, match="not JSON"):
        rf_run.parse_rows("{")
    with pytest.raises(StepFailureError, match="not a list"):
        rf_run.parse_rows("{}")


def test_micro_series_picks_the_two_dimensional_micro_series_only() -> None:
    rows = [
        {"path": "/acquisition/LFP_macro/LFP_macro", "shape": [10, 2]},
        {"path": "/processing/ecephys/LFP_micro/LFP_micro", "shape": [10, 56]},
        {"path": "/processing/ecephys/LFP_micro/times", "shape": [10]},
    ]
    assert rf_run.micro_series(rows) == "/processing/ecephys/LFP_micro/LFP_micro"
    with pytest.raises(StepFailureError, match="0 two-dimensional LFP_micro"):
        rf_run.micro_series(rows[:1])


# --- steps against fakes ---------------------------------------------------------


@pytest.fixture
def run(tmp_path: Path) -> analysis.AnalysisRun:
    made = analysis.AnalysisRun(Console(headless=True), "id", tmp_path, "p", "1")
    made.email = "rf@example.invalid"
    made.outputs.update(
        {
            "ApiUrl": "https://api.example",
            "WebUrl": "https://d1.cloudfront.net",
            "access_token": "acc",
            "refresh_token": "ref",
            "series": "/processing/ecephys/LFP_micro/LFP_micro",
        }
    )
    return made


def test_warm_up_calls_the_three_compute_routes_once_each(
    run: analysis.AnalysisRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []

    def fake_get(_run: object, path: str) -> tuple[Any, float]:
        seen.append(path)
        if "/tables/series" in path:
            return {"rows": [{"path": run.outputs["series"], "shape": [1, 2]}]}, 0.1
        return {}, 2.5

    monkeypatch.setattr(rf_run, "_get", fake_get)
    (result,) = rf_run.warm_up(run)
    routes = [p.split("?", 1)[0] for p in seen]
    assert routes == [
        f"/catalog/assets/{rf_run.CS62_R2}/tables/series",
        "/compute/rates",
        "/compute/overview",
        "/compute/spectrogram",
    ]
    assert "start_s=300&duration_s=60" in seen[3]
    assert result.passed
    assert result.note == rf_run.WARMUP_NOTE
    assert "cold walls, not judged" in result.detail
    saved = json.loads((run.base / rf_run.WARM_FILE).read_text())
    assert saved["cold_s"] == {"rates": 2.5, "overview": 2.5, "spectrogram": 2.5}


def test_the_browser_env_carries_the_url_tokens_file_and_series(
    run: analysis.AnalysisRun,
) -> None:
    env = rf_run.browser_env(run)
    assert env["RF_APP_URL"] == "https://d1.cloudfront.net"
    assert env["RF_API_URL"] == "https://api.example"
    assert env["RF_ACCESS_TOKEN"] == "acc"
    assert env["RF_REFRESH_TOKEN"] == "ref"
    assert env["RF_ASSET"] == rf_run.CS62_R2
    assert env["RF_SERIES"] == run.outputs["series"]
    assert env["RF_OUT"] == str(rf_run.browser_dir(run))
    assert env["PLAYWRIGHT_BROWSERS_PATH"].endswith(".tools/ms-playwright")


class Browser:
    def __init__(self, rows: list[dict[str, Any]] | None, code: int = 0) -> None:
        self.rows = rows
        self.code = code
        self.calls: list[dict[str, Any]] = []

    def __call__(self, cmd: Sequence[str], **kw: Any) -> Proc:
        self.calls.append({"cmd": list(cmd), **kw})
        out = Path(kw["env"]["RF_OUT"])
        if self.rows is not None:
            (out / rf_run.ROWS_FILE).write_text(json.dumps(self.rows))
        return Proc(self.code, "list reporter\n", "")


def test_browser_rows_runs_the_live_config_in_web_and_judges_the_rows(
    run: analysis.AnalysisRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = Browser(list(good_rows().values()))
    monkeypatch.setattr(rf_run, "execute", fake)
    monkeypatch.setattr(rf_run, "ensure_browser", lambda _env: None)
    results = rf_run.browser_rows(run)
    assert [r.passed for r in results] == [True] * 9
    call = fake.calls[0]
    assert call["cmd"][-3:] == ["-c", rf_run.CONFIG, rf_run.SPEC]
    assert Path(call["cwd"]) == acceptance.REPO / "web"
    assert (rf_run.browser_dir(run) / "browser.log").read_text() == "list reporter\n"


def test_a_browser_run_without_rows_json_is_a_failed_step(
    run: analysis.AnalysisRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rf_run, "execute", Browser(None, code=1))
    monkeypatch.setattr(rf_run, "ensure_browser", lambda _env: None)
    with pytest.raises(StepFailureError, match=r"wrote no rows\.json"):
        rf_run.browser_rows(run)


# --- the own-browser pass and the report ----------------------------------------------


def scripted(answers: list[str]) -> tuple[Console, list[str]]:
    said: list[str] = []
    feed = iter(answers)
    return Console(headless=False, read=lambda _p: next(feed), write=said.append), said


def test_the_human_answers_y_or_n_for_each_of_r1_to_r8() -> None:
    console, said = scripted(["y", "y", "n", "", "y", "y", "y", "y"])
    results = rf_run.human_pass(console, "https://app.example")
    assert [r.name.split()[0] for r in results] == [f"R{n}" for n in range(1, 9)]
    assert [r.passed for r in results] == [
        True,
        True,
        False,
        False,
        True,
        True,
        True,
        True,
    ]
    assert all(r.note == "tester's own browser" for r in results)
    assert any("https://app.example" in line for line in said)


def test_the_report_lists_steps_screenshots_and_the_human_rows(
    run: analysis.AnalysisRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rf_run, "commit_sha", lambda: "abc1234")
    shots = rf_run.browser_dir(run)
    shots.mkdir(parents=True)
    for row in ("R0", "R3"):
        (shots / f"{row}.png").write_bytes(b"png")
    run.results.extend(rf_run.judge_all(good_rows()))
    console, _ = scripted(["y"] * 8)
    human = rf_run.human_pass(console, "https://app.example")
    text = rf_run.write_report(run, 12.0, human=human).read_text()
    assert text.startswith("# Research frontend run id")
    assert "| R7 LFP tab: overview, a 60 s window at t=300, one pan | PASS |" in text
    assert "| R3 (tester) " in text
    assert f"- R0: {shots / 'R0.png'}" in text
    assert "- R1: none" in text
    assert "abc1234" in text
    assert "## Unverified seams" in text


# --- the guard, the entry point and the Makefile ----------------------------------


def test_the_run_refuses_without_the_sandbox_env(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("ASTEROIDEA_PROFILE", raising=False)
    monkeypatch.delenv("ASTEROIDEA_ACCOUNT", raising=False)
    with pytest.raises(SystemExit) as exc:
        rf_run.run_live()
    assert exc.value.code == EXIT_NO_SANDBOX
    assert "ASTEROIDEA_PROFILE" in capsys.readouterr().err


def test_main_selects_the_prompt_and_rejects_other_arguments(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: list[tuple[bool, int]] = []

    def fake(*, prompt: bool = True, passes: int = 1, **_k: object) -> int:
        seen.append((prompt, passes))
        return 0

    monkeypatch.setattr(rf_run, "run_live", fake)
    assert rf_run.main(["run"]) == 0
    assert rf_run.main(["run", "1"]) == 0
    assert rf_run.main(["run", "1", "--no-prompt"]) == 0
    assert rf_run.main(["run", "2"]) == 0
    assert rf_run.main(["run", "2", "--no-prompt"]) == 0
    assert seen == [(True, 1), (True, 1), (False, 1), (True, 2), (False, 2)]
    assert rf_run.main(["run", "3"]) == 2
    assert rf_run.main(["bogus"]) == 2
    assert (
        "usage: python -m asteroidea_infra.research_app_check run [1|2] [--no-prompt]"
        in (capsys.readouterr().err)
    )


def test_makefile_has_the_guarded_rf_run_target() -> None:
    text = (REPO / "Makefile").read_text()
    recipe = text.split("\nlive-research-app:", 1)[1].split("\n\n", 1)[0]
    assert "##" in recipe.splitlines()[0]
    assert "$(SANDBOX_GUARD)" in recipe
    assert "asteroidea_infra.research_app_check run $(or $(PASS),1)" in recipe
    assert "--no-prompt" in recipe
    assert "outsider-run: live-hosted-setup" in text
    assert "rf-run: live-research-app" in text


def test_makefile_default_pass_is_accepted_by_main(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text = (REPO / "Makefile").read_text(encoding="utf-8")
    recipe = re.search(r"^live-research-app:.*\n(?:\t.*\n?)+", text, re.MULTILINE)
    assert recipe
    found = re.search(
        r"research_app_check run \$\(or \$\(PASS\),(\w+)\)", recipe.group(0)
    )
    assert found
    monkeypatch.setattr(rf_run, "run_live", lambda **_k: 0)
    assert rf_run.main(["run", found.group(1)]) == 0
