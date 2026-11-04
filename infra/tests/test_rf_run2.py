"""`make rf-run PASS=2` (rows R9 to R13): offline parts.

The browser run, the venv and the AWS calls are LIVE and run by hand; here the row
table, the budgets, the judges, the worker's comparison, the clean-up and the glue
run against fakes.
"""

import ast
import dataclasses
import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, cast

import numpy as np
import pyarrow as pa
import pytest

from asteroidea import analysis as sdk_analysis
from asteroidea_infra import (
    analysis_check as analysis,
)
from asteroidea_infra import (
    first_hour as acceptance,
)
from asteroidea_infra import (
    research_app_check as rf_run,
)
from asteroidea_infra import (
    research_app_worker as rf_worker,
)
from asteroidea_infra.runner import Proc
from asteroidea_infra.steps import Console, StepFailureError

if TYPE_CHECKING:
    from collections.abc import Sequence

REPO: Final = Path(__file__).resolve().parents[2]
ROWS: Final = REPO / "infra" / "tests" / "fixtures" / "rf_acceptance_rows.md"
SPEC: Final = REPO / "web" / rf_run.SPEC2
MB: Final = 1024 * 1024


def good_rows() -> dict[str, dict[str, Any]]:
    """Every pass-2 browser row green and well inside its budget."""
    rows: dict[str, dict[str, Any]] = {}
    for spec in rf_run.ROWS2:
        parts = {
            lim.part: (1.0 if lim.unit == rf_run.UNIT_S else 1000.0)
            for lim in spec.limits
            if lim.part != "row"
        }
        rows[spec.row] = {
            "row": spec.row,
            "ok": True,
            "seconds": 1.0,
            "detail": "fine",
            "parts": parts,
        }
    return rows


def spec_of(row: str) -> rf_run.RowSpec:
    return next(s for s in (*rf_run.ROWS2, rf_run.SDK_SPEC) if s.row == row)


def sdk_facts(**over: Any) -> dict[str, Any]:
    """Return what the worker prints for a run that went well."""
    facts: dict[str, Any] = {
        "parts": {"labels": 0.6, "sql": 4.0, "rates": 0.1},
        "n_labels": 1,
        "label": {"label_id": "x", "name": "baseline-1", "mine": True},
        "n_onsets": 41,
        "n_trials": 41,
        "n_spikes": 1000,
        "unit": 0,
        "bins": 80,
        "edges_equal": True,
        "equal": True,
        "max_rel_diff": 0.0,
        "rtol": 1e-9,
    }
    return {**facts, **over}


# --- the row table and the budgets -------------------------------------------


def test_the_pass_two_rows_are_r9_to_r13_in_order() -> None:
    assert rf_run.PASS_TWO_ROWS == ("R9", "R9b", "R10", "R11", "R12", "R13")
    assert rf_run.ROW_IDS2 == ("R9", "R9b", "R10", "R11", "R12")


def test_every_pass_two_row_is_a_row_of_the_acceptance_table() -> None:
    table_rows = set(re.findall(r"^\| (R\d+) \|", ROWS.read_text(), re.MULTILINE))
    assert {"R9", "R10", "R11", "R12", "R13"} <= table_rows
    # R9b is this runner's second image (the ds004798 T1w); it shares R9's row.
    assert set(rf_run.PASS_TWO_ROWS) - {"R9b"} <= table_rows


@pytest.mark.parametrize(
    ("row", "want"),
    [
        ("R9", {"presign": 3.0, "volume": 5.0}),
        ("R9b", {"presign": 3.0, "volume": 5.0}),
        ("R10", {"post": 3.0}),
        ("R11", {"patch": 3.0}),
        ("R12", {"row": 8.0, "body": 5.0 * MB}),
        ("R13", {"labels": 5.0, "rates": 5.0, "sql": 10.0}),
    ],
)
def test_budgets_match_the_table(row: str, want: dict[str, float]) -> None:
    got = {lim.part: lim.ceiling for lim in spec_of(row).limits}
    assert got == want


def test_the_spec_file_exists_and_writes_every_row_the_judge_reads() -> None:
    text = SPEC.read_text()
    for row in rf_run.ROW_IDS2:
        assert f'"{row}"' in text
    for spec in rf_run.ROWS2:
        for lim in spec.limits:
            assert lim.part == "row" or f'"{lim.part}"' in text
    assert 'skip("R9b"' in text


def test_the_spec_derives_names_and_ids_from_the_api_not_literals() -> None:
    """No `sub-` label form, no asset id, no unit index is typed in."""
    text = SPEC.read_text()
    assert '"sub-' not in text
    assert rf_run.CS62_R2 not in text
    assert re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-", text) is None
    assert "unit_index" in text
    assert "/catalog/datasets" in text


# --- the judges ---------------------------------------------------------------------


def test_every_row_inside_its_budget_passes() -> None:
    results = rf_run.judge_pass2(good_rows())
    assert [r.passed for r in results] == [True] * 5


def test_red_proof_an_image_over_its_budget_fails_and_names_the_part() -> None:
    rows = good_rows()
    rows["R9"]["parts"]["presign"] = 3.01
    rows["R9b"]["parts"]["presign"] = 3.01
    results = rf_run.judge_pass2(rows)
    assert not results[0].passed
    assert "presign over its limit" in results[0].detail
    assert not results[1].passed
    assert "presign over its limit" in results[1].detail
    assert all(r.passed for r in results[2:])


def test_red_proof_a_volume_of_9_s_is_reported_not_judged() -> None:
    """The volume is network-bound; the tester judges it, not the row."""
    rows = good_rows()
    rows["R9"]["parts"]["volume"] = 9.0
    rows["R9b"]["parts"]["volume"] = 9.0
    results = rf_run.judge_pass2(rows)
    for got in results[:2]:
        assert got.passed
        assert "volume 9.00 s (reference budget 5.00 s)" in got.detail
        assert "volume: network-bound, tester judges; " in got.detail
        assert "OVER" not in got.detail


def test_a_skipped_r9b_passes_and_says_so() -> None:
    rows = good_rows()
    rows["R9b"] = {
        "row": "R9b",
        "ok": True,
        "seconds": 0,
        "detail": "no T1w",
        "parts": {},
        "skipped": True,
    }
    got = rf_run.judge_pass2(rows)[1]
    assert got.passed
    assert got.detail == "skipped: no T1w"
    assert got.note == "skipped, not run"


def test_a_skipped_flag_never_rescues_a_failed_or_missing_row() -> None:
    rows = good_rows()
    del rows["R10"]
    assert "did not reach" in rf_run.judge_pass2(rows)[2].detail
    rows = good_rows()
    rows["R9"]["ok"] = False
    assert not rf_run.judge_pass2(rows)[0].passed


def test_a_post_or_refused_patch_over_3_s_fails() -> None:
    rows = good_rows()
    rows["R10"]["parts"]["post"] = 3.5
    rows["R11"]["parts"]["patch"] = 3.5
    results = rf_run.judge_pass2(rows)
    assert not results[2].passed
    assert not results[3].passed
    assert results[3].note == rf_run.SECOND_NOTE


def test_a_body_over_5_mb_fails_r12() -> None:
    rows = good_rows()
    rows["R12"]["parts"]["body"] = 5 * MB + 1
    got = rf_run.judge_pass2(rows)[4]
    assert not got.passed
    assert "body over its limit" in got.detail


def test_a_part_the_spec_did_not_measure_fails() -> None:
    rows = good_rows()
    del rows["R9"]["parts"]["volume"]
    got = rf_run.judge_pass2(rows)[0]
    assert not got.passed
    assert "volume not measured" in got.detail


# --- R13: the worker's facts and the judge -----------------------------------------


def judged(**over: Any) -> Any:
    return rf_run.judge_row(rf_run.SDK_SPEC, rf_run.sdk_measured(sdk_facts(**over)))


def test_r13_passes_when_the_label_is_mine_and_the_rates_equal() -> None:
    got = judged()
    assert got.passed
    assert "label baseline-1 is mine" in got.detail
    assert got.note == rf_run.RTOL_NOTE


def test_red_proof_r13_fails_on_each_wrong_fact() -> None:
    no_label = judged(n_labels=0, label=None)
    assert not no_label.passed
    assert "0 labels carry the run's name" in no_label.detail
    other = judged(label={"name": "baseline-1", "mine": False})
    assert not other.passed
    assert "not marked mine" in other.detail
    onsets = judged(n_onsets=40)
    assert not onsets.passed
    assert "40 trial onsets" in onsets.detail
    differ = judged(equal=False, max_rel_diff=0.25)
    assert not differ.passed
    assert "max relative difference 0.25" in differ.detail
    slow = judged(parts={"labels": 5.1, "sql": 4.0, "rates": 0.1})
    assert not slow.passed
    assert "labels over its limit" in slow.detail
    slow_sql = judged(parts={"labels": 0.1, "sql": 10.1, "rates": 0.1})
    assert "sql over its limit" in slow_sql.detail


# --- the worker (R13 inside the venv), run here against fakes -------------------------


def test_the_worker_reads_the_same_file_as_the_runner() -> None:
    assert rf_worker.ASSET == rf_run.CS62_R2
    assert rf_worker.ASSET in rf_worker.TRIALS_SQL
    assert rf_worker.SPIKES_SQL.count(rf_worker.ASSET) == 2


def test_the_worker_imports_only_the_sdk_numpy_and_the_standard_library() -> None:
    tree = ast.parse(Path(rf_worker.__file__).read_text())
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            roots.add(node.module.split(".")[0])
    assert roots <= {
        "collections",
        "json",
        "sys",
        "time",
        "pathlib",
        "typing",
        "numpy",
        "asteroidea",
    }


class FakeAuth:
    def tokens(self) -> None:
        return None


class FakeSdk:
    """`asteroidea.labels` and `asteroidea.sql` over a small recording."""

    def __init__(self, onsets: list[float], spikes: list[float], *, mine: bool) -> None:
        self.onsets = onsets
        self.spikes = spikes
        self.mine = mine

    def labels(self, asset_id: str) -> pa.Table:
        assert asset_id == rf_run.CS62_R2
        return pa.Table.from_pylist(
            [
                {
                    "label_id": "id1",
                    "name": "baseline-1",
                    "mine": self.mine,
                    "start_s": 5.0,
                    "stop_s": 6.0,
                },
                {
                    "label_id": "id2",
                    "name": "other",
                    "mine": True,
                    "start_s": 1.0,
                    "stop_s": 2.0,
                },
            ]
        )

    def sql(self, query: str) -> pa.Table:
        if "FROM intervals" in query:
            return pa.table({"start_time": self.onsets})
        assert "FROM unit_spikes" in query
        return pa.table({"unit_index": [3], "spike_times": [self.spikes]})


def expected_file(tmp_path: Path, sdk: FakeSdk, *, nudge: float = 0.0) -> Path:
    """Write the route's answer: the same rates, rounded to 6 significant digits."""
    fit = sdk_analysis.rates(
        {3: np.asarray(sdk.spikes)}, np.asarray(sdk.onsets), 2.0, 2.0, 0.05
    )
    sig = [float(f"{v:.6g}") for v in fit.rate_hz[0].tolist()]
    edges = [float(f"{v:.6g}") for v in fit.edges.tolist()]
    sig[10] = sig[10] * (1 + nudge)
    path = tmp_path / "expected.json"
    path.write_text(
        json.dumps(
            {"unit": 3, "n_trials": len(sdk.onsets), "edges": edges, "rate_hz": sig}
        )
    )
    return path


def recording() -> FakeSdk:
    rng = np.random.default_rng(1)
    spikes = np.sort(rng.uniform(0, 100, 4000)).tolist()
    return FakeSdk([10.0 * k for k in range(1, 10)], spikes, mine=True)


def worker_facts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, nudge: float, mine: bool = True
) -> dict[str, Any]:
    sdk = recording()
    sdk.mine = mine

    def fake_labels(_auth: object, asset_id: str, transport: Any) -> pa.Table:
        transport("GET", "https://api.test/x", {}, None)
        return sdk.labels(asset_id)

    monkeypatch.setattr(rf_worker, "default_auth", FakeAuth)
    monkeypatch.setattr(rf_worker, "send", lambda *_a: rf_worker.Reply(200, b"{}"))
    monkeypatch.setattr(rf_worker, "read_labels", fake_labels)
    monkeypatch.setattr(rf_worker.asteroidea, "sql", sdk.sql)
    return rf_worker.measure(
        rf_run.CS62_R2, 3, "baseline-1", expected_file(tmp_path, sdk, nudge=nudge)
    )


def test_the_worker_finds_the_label_and_its_rates_equal_the_routes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    facts = worker_facts(monkeypatch, tmp_path, nudge=0.0)
    assert facts["n_labels"] == 1
    assert facts["label"]["mine"] is True
    assert facts["n_onsets"] == facts["n_trials"] == 9
    assert facts["equal"] is True
    assert facts["edges_equal"] is True
    assert facts["max_rel_diff"] == 0.0
    assert rf_run.sdk_measured(facts)["ok"] is True


def test_red_proof_one_rate_off_by_1e_6_makes_the_comparison_fail(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    facts = worker_facts(monkeypatch, tmp_path, nudge=1e-6)
    assert facts["equal"] is False
    assert 0 < facts["max_rel_diff"] < 1e-5
    judged_ = rf_run.judge_row(rf_run.SDK_SPEC, rf_run.sdk_measured(facts))
    assert not judged_.passed
    assert "rates differ" in judged_.detail


def test_a_label_of_another_researcher_is_not_mine(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    facts = worker_facts(monkeypatch, tmp_path, nudge=0.0, mine=False)
    got = rf_run.judge_row(rf_run.SDK_SPEC, rf_run.sdk_measured(facts))
    assert not got.passed
    assert "not marked mine" in got.detail


def test_the_worker_refuses_another_file(tmp_path: Path) -> None:
    with pytest.raises(rf_worker.InputError, match="reads 1d50b507"):
        rf_worker.measure("other", 0, "x", tmp_path / "none.json")


def test_the_worker_prints_the_sdk_error_as_json(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def boom() -> None:
        raise rf_worker.asteroidea.PlatformUnreachableError

    monkeypatch.setattr(rf_worker, "default_auth", boom)
    (tmp_path / "e.json").write_text("{}")
    code = rf_worker.main(["w", rf_run.CS62_R2, "0", "n", str(tmp_path / "e.json")])
    assert code == 1
    assert json.loads(capsys.readouterr().out)["error"].startswith(
        "PlatformUnreachableError"
    )


# --- the runner's steps against fakes -------------------------------------------------


@pytest.fixture
def run(tmp_path: Path) -> analysis.AnalysisRun:
    made = analysis.AnalysisRun(
        Console(headless=True), "20261025120000", tmp_path, "p", "1"
    )
    made.email = "rf@example.invalid"
    made.outputs.update(
        {
            "ApiUrl": "https://api.example/",
            "WebUrl": "https://d1.cloudfront.net",
            "access_token": "acc",
            "refresh_token": "ref",
            "access_token_2": "acc2",
            "refresh_token_2": "ref2",
        }
    )
    return made


def test_the_label_name_carries_the_run_id(run: analysis.AnalysisRun) -> None:
    assert rf_run.label_name(run) == "baseline-20261025120000"
    assert len(rf_run.label_name(run)) <= 64


def test_the_second_researcher_is_created_labelled_and_signed_in(
    run: analysis.AnalysisRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    made: list[str] = []
    monkeypatch.setattr(rf_run, "add_researcher", lambda _s, email: made.append(email))
    seen: dict[str, Any] = {}

    def fake_sign_in(_s: object, out: Any, email: str, config: Path) -> dict[str, str]:
        seen.update(out=dict(out), email=email, config=config)
        return {"AccessToken": "A2", "RefreshToken": "R2"}

    monkeypatch.setattr(rf_run, "sandbox_sign_in", fake_sign_in)
    run.session = cast("Any", object())
    (result,) = rf_run.second_researcher(run)
    assert made == ["rf2-20261025120000@example.invalid"]
    assert result.passed
    assert result.note == rf_run.SECOND_NOTE
    assert seen["config"] == run.base / "config2"
    assert run.outputs["access_token_2"] == "A2"
    assert run.outputs["refresh_token_2"] == "R2"


def test_targets_come_from_the_api_and_the_rates_are_kept(
    run: analysis.AnalysisRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []

    def fake_get(_run: object, path: str) -> tuple[Any, float]:
        seen.append(path)
        if "/tables/units" in path:
            return {"rows": [{"unit_index": "7"}, {"unit_index": "3"}]}, 0.5
        if path.startswith("/files/"):
            return {"labels": []}, 0.25
        return {
            "n_trials": 41,
            "edges": [-2.0, -1.95],
            "units": [{"unit_index": 3, "rate_hz": [1.5, 2.5]}],
        }, 2.0

    monkeypatch.setattr(rf_run, "_get", fake_get)
    (result,) = rf_run.pass_two_targets(run)
    assert result.passed
    assert "unit 3" in result.detail
    assert "units=3&" in seen[2]
    assert seen[2] == rf_run.rates_path(3)
    saved = json.loads((run.base / rf_run.EXPECTED_FILE).read_text())
    assert saved == {
        "unit": 3,
        "n_trials": 41,
        "edges": [-2.0, -1.95],
        "rate_hz": [1.5, 2.5],
    }
    warm = json.loads((run.base / rf_run.WARM2_FILE).read_text())
    assert warm["cold_s"] == {"units": 0.5, "labels": 0.25, "rates": 2.0}


def test_the_pass_two_env_carries_both_token_pairs_the_file_and_the_label(
    run: analysis.AnalysisRun,
) -> None:
    env = rf_run.browser2_env(run)
    assert env["RF_ACCESS_TOKEN"] == "acc"
    assert env["RF_ACCESS_TOKEN_2"] == "acc2"
    assert env["RF_REFRESH_TOKEN_2"] == "ref2"
    assert env["RF_ASSET"] == rf_run.CS62_R2
    assert env["RF_LABEL"] == "baseline-20261025120000"
    assert env["RF_OUT"] == str(run.base / "browser2")


class Browser:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.calls: list[dict[str, Any]] = []

    def __call__(self, cmd: Sequence[str], **kw: Any) -> Proc:
        self.calls.append({"cmd": list(cmd), **kw})
        out = Path(kw["env"]["RF_OUT"])
        (out / rf_run.ROWS_FILE).write_text(json.dumps(self.rows))
        return Proc(0, "list reporter\n", "")


def test_browser2_rows_runs_the_pass_two_spec_and_judges_r9_to_r12(
    run: analysis.AnalysisRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = Browser(list(good_rows().values()))
    monkeypatch.setattr(rf_run, "execute", fake)
    monkeypatch.setattr(rf_run, "ensure_browser", lambda _env: None)
    results = rf_run.browser2_rows(run)
    assert [r.passed for r in results] == [True] * 5
    assert fake.calls[0]["cmd"][-3:] == ["-c", rf_run.CONFIG, rf_run.SPEC2]
    assert Path(fake.calls[0]["cwd"]) == acceptance.REPO / "web"
    assert (rf_run.browser2_dir(run) / "browser.log").is_file()


def test_the_sdk_step_passes_the_worker_its_arguments_and_judges_the_facts(
    run: analysis.AnalysisRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    (run.base / rf_run.EXPECTED_FILE).write_text(json.dumps({"unit": 3}))
    calls: list[dict[str, Any]] = []

    def fake(cmd: Sequence[str], **kw: Any) -> Proc:
        calls.append({"cmd": list(cmd), **kw})
        return Proc(0, "noise\n" + json.dumps(sdk_facts()) + "\n", "")

    monkeypatch.setattr(rf_run, "execute", fake)
    (result,) = rf_run.sdk_rows(run)
    assert result.passed
    cmd = calls[0]["cmd"]
    assert cmd[1:4] == [str(rf_run.WORKER), rf_run.CS62_R2, "3"]
    assert cmd[4] == "baseline-20261025120000"
    assert calls[0]["env"]["ASTEROIDEA_CONFIG_DIR"] == str(run.config_dir)


SPLIT = {"auth_s": 0.31, "http_s": 4.12, "table_s": 0.77, "warm_labels_s": 1.43}


def test_red_proof_r13_detail_prints_the_labels_split() -> None:
    got = judged(labels_split=SPLIT)
    want = (
        "labels split: auth 0.31 s, http 4.12 s, table 0.77 s, warm second call 1.43 s"
    )
    assert want in got.detail
    failing = judged(labels_split=SPLIT, equal=False, max_rel_diff=0.25)
    assert "labels split: auth 0.31 s" in failing.detail


def test_r13_detail_says_when_the_split_is_not_reported() -> None:
    assert "labels split: not reported" in judged().detail
    assert "labels split: not reported" in judged(labels_split={"auth_s": 1}).detail


def test_the_sdk_step_keeps_the_workers_json_in_the_run_dir(
    run: analysis.AnalysisRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    (run.base / rf_run.EXPECTED_FILE).write_text(json.dumps({"unit": 3}))
    facts = sdk_facts(labels_split=SPLIT)
    monkeypatch.setattr(
        rf_run, "execute", lambda *_a, **_k: Proc(0, json.dumps(facts) + "\n", "")
    )
    (result,) = rf_run.sdk_rows(run)
    assert "http 4.12 s" in result.detail
    assert json.loads((run.base / "r13.json").read_text()) == facts


def test_the_sdk_step_surfaces_the_sdk_error_and_a_crash(
    run: analysis.AnalysisRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    (run.base / rf_run.EXPECTED_FILE).write_text(json.dumps({"unit": 3}))
    monkeypatch.setattr(
        rf_run,
        "execute",
        lambda *_a, **_k: Proc(1, '{"error": "QueryError: bad sql"}\n', ""),
    )
    with pytest.raises(StepFailureError, match="QueryError: bad sql"):
        rf_run.sdk_rows(run)
    monkeypatch.setattr(
        rf_run, "execute", lambda *_a, **_k: Proc(1, "", "Traceback\nValueError: x")
    )
    with pytest.raises(StepFailureError, match="worker crashed: ValueError: x"):
        rf_run.sdk_rows(run)


def test_the_sdk_install_uses_the_analysis_extras_pins(
    run: analysis.AnalysisRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[Any] = []
    monkeypatch.setattr(analysis, "install", lambda _r, pins: seen.append(pins) or [])
    rf_run.install_sdk(run)
    assert seen == [rf_run.SDK_PINS]
    assert set(rf_run.SDK_PINS) == {"numpy==2.5.4", "polars==2.0.0"}


# --- the clean-up ----------------------------------------------------------------


class Labels:
    """A labels route: GET lists, DELETE removes (as the author)."""

    def __init__(self, names: list[str], *, delete_status: int = 204) -> None:
        self.items = [{"label_id": f"id{i}", "name": n} for i, n in enumerate(names)]
        self.deleted: list[str] = []
        self.delete_status = delete_status

    def get(self, _run: object, _path: str) -> tuple[Any, float]:
        return {"labels": list(self.items)}, 0.1

    def send(self, _run: object, method: str, path: str) -> tuple[int, str, float]:
        assert method == "DELETE"
        label_id = path.rsplit("/", 1)[1]
        self.deleted.append(label_id)
        if self.delete_status == rf_run.HTTP_NO_CONTENT:
            self.items = [x for x in self.items if x["label_id"] != label_id]
        return self.delete_status, "refused", 0.1


def test_cleanup_deletes_only_the_labels_this_run_named(
    run: analysis.AnalysisRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = Labels(["keep", rf_run.label_name(run), "keep-too"])
    monkeypatch.setattr(rf_run, "_get", store.get)
    monkeypatch.setattr(rf_run, "api_send", store.send)
    (result,) = rf_run.cleanup_labels(run)
    assert result.passed
    assert store.deleted == ["id1"]
    assert [x["name"] for x in store.items] == ["keep", "keep-too"]
    assert "deleted 1 label(s)" in result.detail


def test_red_proof_cleanup_fails_when_a_delete_is_refused_or_a_label_remains(
    run: analysis.AnalysisRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = Labels([rf_run.label_name(run)], delete_status=403)
    monkeypatch.setattr(rf_run, "_get", store.get)
    monkeypatch.setattr(rf_run, "api_send", store.send)
    with pytest.raises(StepFailureError, match="answered 403"):
        rf_run.cleanup_labels(run)
    store = Labels([rf_run.label_name(run)])
    monkeypatch.setattr(rf_run, "_get", store.get)
    monkeypatch.setattr(rf_run, "api_send", lambda *_a: (204, "", 0.1))
    with pytest.raises(StepFailureError, match=r"1 label\(s\) named .* remain"):
        rf_run.cleanup_labels(run)


def test_cleanup_with_no_sign_in_has_nothing_to_delete(
    run: analysis.AnalysisRun,
) -> None:
    run.outputs.pop("access_token")
    (result,) = rf_run.cleanup_labels(run)
    assert result.passed
    assert "never signed in" in result.detail


# --- the run: clean-up always happens, the manual rows, the report ----------------


def fake_pass(monkeypatch: pytest.MonkeyPatch, finish: Any, *, steps_ok: bool) -> None:
    pass_spec = dataclasses.replace(rf_run.PASS_TWO, finish=finish)
    monkeypatch.setattr(rf_run, "PASSES", {1: rf_run.PASS_ONE, 2: pass_spec})
    monkeypatch.setattr(
        analysis, "run_continuing", lambda _run, _steps, _needs: steps_ok
    )
    monkeypatch.setenv("ASTEROIDEA_PROFILE", "p")
    monkeypatch.setenv("ASTEROIDEA_ACCOUNT", "1")


def test_the_cleanup_runs_even_when_the_steps_stopped_early(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    called: list[str] = []

    def finish(_run: object) -> list[Any]:
        called.append("finish")
        return [rf_run.StepResult("Cleanup", passed=True, detail="ok")]

    fake_pass(monkeypatch, finish, steps_ok=False)
    monkeypatch.setenv(rf_run.HOME_ENV, str(tmp_path))
    monkeypatch.setattr(rf_run, "commit_sha", lambda: "abc1234")
    assert rf_run.run_live(prompt=False, passes=2) == 1
    assert called == ["finish"]
    out = capsys.readouterr().out
    assert "(pass 2)" in out
    assert "RF FAIL" in out


def test_a_green_pass_two_exits_0_and_a_failed_cleanup_exits_1(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake_pass(
        monkeypatch,
        lambda _r: [rf_run.StepResult("Cleanup", passed=True, detail="ok")],
        steps_ok=True,
    )
    monkeypatch.setenv(rf_run.HOME_ENV, str(tmp_path))
    monkeypatch.setattr(rf_run, "commit_sha", lambda: "abc1234")
    assert rf_run.run_live(prompt=False, passes=2) == 0
    fake_pass(
        monkeypatch,
        lambda _r: [rf_run.StepResult("Cleanup", passed=False, detail="left over")],
        steps_ok=True,
    )
    assert rf_run.run_live(prompt=False, passes=2) == 1


def test_pass_one_still_has_no_cleanup_and_its_own_rows() -> None:
    assert rf_run.PASS_ONE.finish is None
    assert rf_run.PASS_ONE.shot_ids == rf_run.ROW_IDS
    assert rf_run.PASS_TWO.finish is rf_run.cleanup_labels
    assert rf_run.PASS_TWO.steps == rf_run.STEPS2


def test_the_human_pass_repeats_r9_r10_and_r12_only() -> None:
    assert [s.row for s in rf_run.HUMAN_ROWS2] == ["R9", "R10", "R12"]
    said: list[str] = []
    feed = iter(["y", "n", "y"])
    console = Console(headless=False, read=lambda _p: next(feed), write=said.append)
    results = rf_run.human_pass(
        console, "https://app.example", rf_run.HUMAN_ROWS2, rf_run.PASS_TWO.human_hint
    )
    assert [r.name.split()[0] for r in results] == ["R9", "R10", "R12"]
    assert [r.passed for r in results] == [True, False, True]
    assert any("delete it yourself" in line for line in said)
    assert any("R9 to R12" in line for line in said)


def test_the_report_names_pass_two_screenshots_and_seams(
    run: analysis.AnalysisRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rf_run, "commit_sha", lambda: "abc1234")
    shots = rf_run.browser2_dir(run)
    shots.mkdir(parents=True)
    (shots / "R9.png").write_bytes(b"png")
    run.results.extend(rf_run.judge_pass2(good_rows()))
    text = rf_run.write_report(run, 9.0, pass_spec=rf_run.PASS_TWO).read_text()
    assert text.startswith("# Research frontend run (pass 2) 20261025120000")
    assert f"- R9: {shots / 'R9.png'}" in text
    assert "- R13: none" in text
    assert "- R1: none" not in text
    assert "| R10 Events tab" in text
    assert "R11: the refusal is read" in text
    assert "R5-rates.json" not in text


def test_the_substitutions_name_the_rounding_the_second_account_and_the_warm_up() -> (
    None
):
    text = "\n".join(rf_run.SUBSTITUTIONS2)
    assert "6 significant digits" in text
    assert "second researcher" in text
    assert "warm-up" in text


def test_makefile_help_names_pass_2() -> None:
    text = (REPO / "Makefile").read_text()
    line = next(x for x in text.splitlines() if x.startswith("live-research-app:"))
    assert "PASS=2 is R9-R13" in line
