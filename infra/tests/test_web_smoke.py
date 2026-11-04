"""`make web-smoke`: offline parts.

The browser run and the AWS calls are LIVE and run by hand; here the step table, the
failure classification, the judge, the report writer, the guard and the runner's glue
run against fakes.
"""

import json
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
    web_smoke,
)
from asteroidea_infra.runner import Proc
from asteroidea_infra.sandbox_guard import EXIT_NO_SANDBOX
from asteroidea_infra.steps import Console, StepFailureError, StepResult

if TYPE_CHECKING:
    from collections.abc import Sequence

REPO: Final = Path(__file__).resolve().parents[2]
SPEC_TEXT: Final = (REPO / "web" / web_smoke.SPEC).read_text()


def record(
    step: str,
    viewport: str = "desktop",
    *,
    ok: bool = True,
    issues: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "viewport": viewport,
        "step": step,
        "ok": ok,
        "seconds": 1.5,
        "detail": "drawn",
        "screenshot": f"/out/{viewport}-{step}.png",
        "issues": issues or [],
    }


def full(viewport: str) -> list[dict[str, Any]]:
    """Every required step green, plus one dataset page."""
    names = [*web_smoke.STEP_NAMES, "Dataset page dandi-000623 Subjects"]
    return [record(n, viewport) for n in names]


def both() -> list[dict[str, Any]]:
    return [*full("desktop"), *full("phone")]


# --- the step table ------------------------------------------------------------------


def test_the_spec_exists_and_the_config_picks_it_up() -> None:
    assert (REPO / "web" / web_smoke.SPEC).is_file()
    assert (REPO / "web" / web_smoke.CONFIG).is_file()
    config = (REPO / "web" / web_smoke.CONFIG).read_text()
    assert 'testDir: "e2e"' in config
    assert r"/\/live\/.*\.spec\.ts$/" in config


def test_the_spec_records_every_step_in_the_table() -> None:
    for name in web_smoke.STEP_NAMES:
        assert f'step("{name}"' in SPEC_TEXT, name
    assert "Dataset page ${dataset}" in SPEC_TEXT


def test_the_spec_runs_both_viewports() -> None:
    assert "width: 1440, height: 900" in SPEC_TEXT
    assert "width: 375, height: 812" in SPEC_TEXT
    assert web_smoke.VIEWPORTS == ("desktop", "phone")


def test_the_spec_watches_every_failure_kind() -> None:
    for kind in web_smoke.KINDS:
        assert f'"{kind}"' in SPEC_TEXT, kind
    for hook in ('page.on("console"', 'page.on("pageerror"', 'page.on("requestfailed"'):
        assert hook in SPEC_TEXT
    assert 'page.on("response"' in SPEC_TEXT
    assert ".notice-error" in SPEC_TEXT


def test_the_session_page_steps_are_required_and_a9_only_with_the_result_file() -> None:
    session = [n for n in web_smoke.STEP_NAMES if n.startswith("Session")]
    assert session == [
        "Session page",
        "Session Units",
        "Session Tuning",
        "Session Events",
        "Session Files",
        "Session Results",
    ]
    assert "Second dataset session" in web_smoke.STEP_NAMES
    assert "Result page" not in web_smoke.steps_for({})
    on = web_smoke.steps_for({"RS_RESULT_FILE": "/x.json"})
    assert {"Result page", "Result file page"} <= set(on)
    for step in (*web_smoke.RESULT_STEPS, *web_smoke.STEPS):
        assert f'step("{step.name}"' in SPEC_TEXT, step.name
    assert "SMOKE_RS_RESULT_FILE" in SPEC_TEXT


def test_a_run_with_the_result_file_fails_a9_steps_it_never_reached() -> None:
    table = web_smoke.steps_for({"RS_RESULT_FILE": "/x.json"})
    failed = [r for r in web_smoke.judge_all(both(), table) if not r.passed]
    assert {r.name for r in failed} == {
        f"{v} {s}"
        for v in web_smoke.VIEWPORTS
        for s in ("Result page", "Result file page")
    }
    assert all(r.passed for r in web_smoke.judge_all(both()))


def test_the_step_names_are_unique() -> None:
    assert len(set(web_smoke.STEP_NAMES)) == len(web_smoke.STEP_NAMES)


def test_the_fixture_e2e_config_leaves_the_live_spec_out() -> None:
    fixture = (REPO / "web" / "playwright.config.ts").read_text()
    assert r"testIgnore: [/auth\.spec\.ts/, /\/live\//, /\/capture\//]" in fixture


# --- classification ------------------------------------------------------------------


def issue(
    kind: str, text: str = "boom", url: str = "", status: int = 0
) -> dict[str, Any]:
    return {"kind": kind, "text": text, "url": url, "status": status}


@pytest.mark.parametrize("kind", ["console", "pageerror", "notice", "empty"])
def test_these_kinds_always_fail(kind: str) -> None:
    got = web_smoke.classify(issue(kind))
    assert got is not None
    assert got.kind == kind


def test_a_failed_request_fails_but_a_page_cancelled_one_does_not() -> None:
    assert web_smoke.classify(issue("requestfailed", "net::ERR_CONNECTION_RESET"))
    assert web_smoke.classify(issue("requestfailed", "net::ERR_ABORTED")) is None


@pytest.mark.parametrize("status", [400, 401, 403, 404, 500, 503])
def test_an_http_error_fails(status: int) -> None:
    got = web_smoke.classify(issue("http", f"{status} x", "https://a/b", status))
    assert got is not None
    assert got.url == "https://a/b"


def test_a_404_passes_only_where_the_step_lists_its_url() -> None:
    bad = issue("http", "404 Not Found", "https://api/compute/overview?x=1", 404)
    assert web_smoke.classify(bad)
    assert web_smoke.classify(bad, [r"/compute/overview"]) is None
    assert web_smoke.classify(bad, [r"/other"])
    server = issue("http", "500 x", "https://api/compute/overview", 500)
    assert web_smoke.classify(server, [r"/compute/overview"])


def test_an_unknown_issue_kind_fails() -> None:
    got = web_smoke.classify(issue("mystery"))
    assert got is not None
    assert got.kind == "unknown-issue"


# --- judging -------------------------------------------------------------------------


def test_a_clean_run_passes_every_step() -> None:
    results = web_smoke.judge_all(both())
    assert all(r.passed for r in results)
    assert len(results) == 2 * (len(web_smoke.STEP_NAMES) + 1)
    assert results[0].name == "desktop Discover"


def test_an_issue_fails_its_step_and_names_page_error_and_screenshot() -> None:
    rec = record(
        "File LFP overview",
        "phone",
        issues=[issue("console", "TypeError: x is undefined")],
    )
    got = web_smoke.judge_record(rec)
    assert not got.passed
    assert got.name == "phone File LFP overview"
    assert "page File LFP overview" in got.detail
    assert "viewport phone" in got.detail
    assert "TypeError: x is undefined" in got.detail
    assert "/out/phone-File LFP overview.png" in got.detail


def test_a_step_that_threw_fails_with_its_message() -> None:
    rec = record("Discover", ok=False)
    rec["detail"] = "Error: locator timed out"
    got = web_smoke.judge_record(rec)
    assert not got.passed
    assert "locator timed out" in got.detail


def test_the_index_error_notice_fails_the_datasets_step() -> None:
    notice = issue("notice", "Index error SchemaError: index.json: unsupported schema")
    got = web_smoke.judge_record(record("Discover", issues=[notice]))
    assert not got.passed
    assert "unsupported schema" in got.detail


def test_an_empty_lfp_picker_is_a_failure() -> None:
    empty = issue("empty", "the LFP picker has no options")
    got = web_smoke.judge_record(record("File LFP overview", issues=[empty]))
    assert not got.passed
    assert "no options" in got.detail


def test_an_expected_404_is_tolerated_only_in_its_own_step() -> None:
    table = {
        **web_smoke.BY_NAME,
        "File Events": web_smoke.SmokeStep("File Events", (r"/events$",)),
    }
    miss = issue("http", "404 Not Found", "https://api/catalog/assets/x/events", 404)
    assert web_smoke.judge_record(record("File Events", issues=[miss]), table).passed
    assert not web_smoke.judge_record(
        record("File Series", issues=[miss]), table
    ).passed


def test_a_step_the_spec_never_reached_fails_in_that_viewport() -> None:
    records = [
        r for r in both() if not (r["viewport"] == "phone" and r["step"] == "Discover")
    ]
    results = web_smoke.judge_all(records)
    failed = [r for r in results if not r.passed]
    assert [r.name for r in failed] == ["phone Discover"]
    assert "did not reach" in failed[0].detail


def test_no_dataset_page_at_all_fails() -> None:
    records = [r for r in both() if not r["step"].startswith(web_smoke.DATASET_PAGE)]
    failed = [r for r in web_smoke.judge_all(records) if not r.passed]
    assert len(failed) == 2
    assert all("no catalog dataset page" in r.detail for r in failed)


def test_a_viewport_with_no_records_fails_all_its_steps() -> None:
    results = web_smoke.judge_all(full("desktop"))
    failed = [r for r in results if not r.passed]
    assert len(failed) == len(web_smoke.STEP_NAMES) + 1
    assert all(r.name.startswith("phone ") for r in failed)


def test_parse_records_reads_each_viewport_and_refuses_junk() -> None:
    got = web_smoke.parse_records({"desktop": json.dumps([record("Discover")])})
    assert got[0]["step"] == "Discover"
    with pytest.raises(StepFailureError, match="not JSON"):
        web_smoke.parse_records({"phone": "{"})
    with pytest.raises(StepFailureError, match="not a list"):
        web_smoke.parse_records({"phone": "{}"})


# --- steps against fakes -------------------------------------------------------------


@pytest.fixture
def run(tmp_path: Path) -> analysis.AnalysisRun:
    made = analysis.AnalysisRun(Console(headless=True), "id", tmp_path, "p", "1")
    made.email = "smoke@example.invalid"
    made.outputs.update(
        {
            "ApiUrl": "https://api.example",
            "WebUrl": "https://d1.cloudfront.net",
            "access_token": "acc",
            "refresh_token": "ref",
        }
    )
    return made


def test_find_targets_picks_nwb_and_the_first_nifti(
    run: analysis.AnalysisRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []

    def fake_get(_run: object, path: str) -> tuple[Any, float]:
        seen.append(path)
        if path == "/catalog/datasets":
            return {"datasets": [{"dataset": "a"}, {"dataset": "b"}]}, 0.1
        if path.endswith("format=nifti"):
            hit = path.startswith("/catalog/datasets/b/")
            return {
                "assets": [{"asset_id": "N1", "path": "sub-1/anat/x_T1w.nii.gz"}]
                if hit
                else []
            }, 0.1
        asset = path.rsplit("/", 1)[1]
        return {"asset": {"dataset": "d-" + asset, "subject": "s-" + asset}}, 0.1

    monkeypatch.setattr(web_smoke, "api_get", fake_get)
    (result,) = web_smoke.find_targets(run)
    assert result.passed
    saved = json.loads((run.base / web_smoke.TARGETS_FILE).read_text())
    assert saved["datasets"] == ["a", "b"]
    assert saved["nwb"] == {
        "asset": web_smoke.CS62_R2,
        "dataset": "d-" + web_smoke.CS62_R2,
        "subject": "s-" + web_smoke.CS62_R2,
    }
    assert saved["nifti"] == {"asset": "N1", "dataset": "d-N1", "subject": "s-N1"}


def test_nifti_order_skips_test_datasets_and_prefers_the_named_ones() -> None:
    names = [
        "artifact-1",
        "sample-x",
        "sdk-live-2",
        "zz",
        "openneuro-ds004798",
        "openneuro-ds004473",
    ]
    assert web_smoke.nifti_order(names) == [
        "openneuro-ds004473",
        "openneuro-ds004798",
        "zz",
    ]


def test_anatomical_needs_an_anat_folder_in_the_path() -> None:
    func = {"asset_id": "F", "path": "sub-1/func/bold.nii.gz"}
    anat = {"asset_id": "A", "path": "sub-1/anat/sub-1_T1w.nii.gz"}
    assert web_smoke.anatomical([func, anat]) == anat
    assert web_smoke.anatomical([func]) is None


def test_find_targets_never_picks_a_test_dataset_nifti(
    run: analysis.AnalysisRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[str] = []

    def fake_get(_run: object, path: str) -> tuple[Any, float]:
        if path == "/catalog/datasets":
            return {
                "datasets": [
                    {"dataset": "artifact-1"},
                    {"dataset": "openneuro-ds004473"},
                ]
            }, 0.1
        if path.endswith("format=nifti"):
            asked.append(path)
            return {
                "assets": [{"asset_id": "N2", "path": "sub-1/anat/a_T1w.nii.gz"}]
            }, 0.1
        return {"asset": {"dataset": "openneuro-ds004473", "subject": "sub-1"}}, 0.1

    monkeypatch.setattr(web_smoke, "api_get", fake_get)
    web_smoke.find_targets(run)
    assert asked == ["/catalog/datasets/openneuro-ds004473/assets?format=nifti"]


def test_the_spec_flags_an_empty_table_only_when_the_api_has_rows() -> None:
    assert "if (expected > 0 && drawn === 0)" in SPEC_TEXT


def test_find_targets_records_no_nifti_as_null(
    run: analysis.AnalysisRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_get(_run: object, path: str) -> tuple[Any, float]:
        if path == "/catalog/datasets":
            return {"datasets": [{"dataset": "a"}]}, 0.1
        if path.endswith("format=nifti"):
            return {"assets": []}, 0.1
        return {"asset": {"dataset": "a", "subject": "s"}}, 0.1

    monkeypatch.setattr(web_smoke, "api_get", fake_get)
    web_smoke.find_targets(run)
    assert json.loads((run.base / web_smoke.TARGETS_FILE).read_text())["nifti"] is None


def test_the_browser_env_carries_urls_tokens_and_files(
    run: analysis.AnalysisRun,
) -> None:
    env = web_smoke.browser_env(run)
    assert env["SMOKE_APP_URL"] == "https://d1.cloudfront.net"
    assert env["SMOKE_API_URL"] == "https://api.example"
    assert env["SMOKE_ACCESS_TOKEN"] == "acc"
    assert env["SMOKE_REFRESH_TOKEN"] == "ref"
    assert env["SMOKE_TARGETS_FILE"] == str(run.base / web_smoke.TARGETS_FILE)
    assert env["SMOKE_OUT"] == str(web_smoke.browser_dir(run))
    assert env["PLAYWRIGHT_BROWSERS_PATH"].endswith(".tools/ms-playwright")


def test_the_result_file_is_made_absolute_for_the_spec(
    run: analysis.AnalysisRun, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("RS_RESULT_FILE", raising=False)
    assert web_smoke.browser_env(run)["SMOKE_RS_RESULT_FILE"] == ""
    monkeypatch.setenv("RS_RESULT_FILE", str(tmp_path / "ids.json"))
    assert web_smoke.browser_env(run)["SMOKE_RS_RESULT_FILE"] == str(
        tmp_path / "ids.json"
    )


def test_a_named_result_file_that_is_missing_fails_before_the_browser_runs(
    run: analysis.AnalysisRun, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("RS_RESULT_FILE", str(tmp_path / "absent.json"))
    monkeypatch.setattr(web_smoke, "ensure_browser", lambda _env: None)
    with pytest.raises(StepFailureError, match="names no file"):
        web_smoke.browser_steps(run)


class Browser:
    def __init__(self, records: list[dict[str, Any]] | None, code: int = 0) -> None:
        self.records = records
        self.code = code
        self.calls: list[dict[str, Any]] = []

    def __call__(self, cmd: Sequence[str], **kw: Any) -> Proc:
        self.calls.append({"cmd": list(cmd), **kw})
        out = Path(kw["env"]["SMOKE_OUT"])
        for view in web_smoke.VIEWPORTS:
            mine = [r for r in self.records or [] if r["viewport"] == view]
            if self.records is not None and mine:
                (out / f"smoke-{view}.json").write_text(json.dumps(mine))
        return Proc(self.code, "list reporter\n", "")


def test_browser_steps_runs_the_live_config_in_web_and_judges_the_records(
    run: analysis.AnalysisRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = Browser(both())
    monkeypatch.setattr(web_smoke, "execute", fake)
    monkeypatch.setattr(web_smoke, "ensure_browser", lambda _env: None)
    results = web_smoke.browser_steps(run)
    assert all(r.passed for r in results)
    call = fake.calls[0]
    assert call["cmd"][-3:] == ["-c", web_smoke.CONFIG, web_smoke.SPEC]
    assert Path(call["cwd"]) == acceptance.REPO / "web"
    assert (web_smoke.browser_dir(run) / "browser.log").read_text() == "list reporter\n"
    assert len(json.loads((run.base / "smoke.json").read_text())) == len(both())


def test_a_browser_run_without_records_is_a_failed_step(
    run: analysis.AnalysisRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(web_smoke, "execute", Browser(None, code=1))
    monkeypatch.setattr(web_smoke, "ensure_browser", lambda _env: None)
    with pytest.raises(StepFailureError, match=r"wrote no smoke-\*\.json"):
        web_smoke.browser_steps(run)


# --- the report and the guard --------------------------------------------------------


def test_the_report_has_pass_fail_per_step_the_failures_and_the_wall_time(
    run: analysis.AnalysisRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(web_smoke, "commit_sha", lambda: "abc1234")
    bad = record("Get started", issues=[issue("console", "Failed to load")])
    run.results = web_smoke.judge_all([bad, *both()[1:]])
    path = web_smoke.write_report(run, 321.0)
    text = path.read_text()
    assert path == run.base / "report.md"
    assert "| desktop Get started | FAIL |" in text
    assert "| desktop Discover | FAIL |" in text
    assert "## Failures" in text
    assert "Failed to load" in text
    assert "- total: 321 s" in text
    assert "abc1234" in text


def test_a_clean_report_says_none(
    run: analysis.AnalysisRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(web_smoke, "commit_sha", lambda: "abc")
    run.results = [StepResult("desktop Discover", passed=True, detail="ok")]
    assert "None." in web_smoke.write_report(run, 1.0).read_text()


def test_the_guard_refuses_without_the_sandbox_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ASTEROIDEA_PROFILE", raising=False)
    monkeypatch.delenv("ASTEROIDEA_ACCOUNT", raising=False)
    with pytest.raises(SystemExit) as exc:
        web_smoke.run_live()
    assert exc.value.code == EXIT_NO_SANDBOX
    make = (REPO / "Makefile").read_text()
    recipe = make.split("\nlive-web-smoke:", 1)[1].split("\n\n", 1)[0]
    assert "$(SANDBOX_GUARD)" in recipe
    assert "asteroidea_infra.web_smoke run" in recipe
    assert "web-smoke" in make.split("MDBOOK_VERSION")[0]


def test_main_refuses_unknown_arguments() -> None:
    assert web_smoke.main(["nope"]) == 2


def test_developer_guide_lists_the_target() -> None:
    assert "`make live-web-smoke`" in (REPO / "docs/src/developers.md").read_text()


def test_a_failed_step_does_not_stop_the_smoke_run_but_marks_its_dependents() -> None:
    assert web_smoke.NEEDS["Visiting every page and tab in a headless browser"] == (
        "Finding the files to visit",
    )
    assert set(web_smoke.NEEDS) <= {title for title, _ in web_smoke.STEPS_LIVE}
