"""`make outsider-run`: offline parts.

The install itself is `asteroidea-setup` (tested in `sdk/tests/test_setup.py`); here
the runner's command, answers file and token placement run against a fake `execute`.
"""

import json
import re
import shlex
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import pytest

from asteroidea._auth import CACHE_NAME, TokenCache
from asteroidea_infra import (
    analysis_check as analysis,
)
from asteroidea_infra import (
    first_hour as acceptance,
)
from asteroidea_infra import (
    hosted_setup_check as outsider,
)
from asteroidea_infra.runner import Proc
from asteroidea_infra.steps import Console, StepFailureError
from asteroidea_ingest import get_started_api

if TYPE_CHECKING:
    from collections.abc import Sequence

REPO: Final = Path(__file__).resolve().parents[2]
NOTEBOOK_FILE: Final = REPO / "notebooks" / "exemplar.ipynb"
WHEEL_URL: Final = (
    "https://signed.example/handout/w.whl?X-Amz-Signature=ab&X-Amz-Expires=3600"
)
APP_URL: Final = "https://d1.cloudfront.net"
READ_BANNED: Final = ("curation/", "notebooks/")


# --- the notebook reads no repo-relative path ------------


def read_violations(source: str) -> list[str]:
    """Return the repo-relative reads in notebook code cells."""
    found = []
    for cell in json.loads(source)["cells"]:
        if cell["cell_type"] != "code":
            continue
        code = "".join(cell["source"])
        found.extend(f"mentions {bad}" for bad in READ_BANNED if bad in code)
        for match in re.finditer(
            r"\bopen\(\s*([rRbBuU]*)(['\"])([^'\"]*)\2([^)]*)\)", code
        ):
            path, rest = match.group(3), match.group(4)
            writing = re.search(r"['\"][wax]\+?b?['\"]", rest) is not None
            if not path.startswith("/") and not writing:
                found.append(f"relative open({path!r})")
    return found


def test_notebook_opens_no_repo_relative_path_for_reading() -> None:
    assert read_violations(NOTEBOOK_FILE.read_text()) == []


def test_red_proof_a_relative_open_fails_the_scan() -> None:
    scratch = json.loads(NOTEBOOK_FILE.read_text())
    scratch["cells"].append(
        {"cell_type": "code", "source": ['open("curation/x")'], "metadata": {}}
    )
    assert read_violations(json.dumps(scratch))


def test_writes_under_tempfile_pass_the_scan() -> None:
    cells = [
        {
            "cell_type": "code",
            "source": [
                "with open(path, 'wb') as f:\n    f.write(b'')\nopen('out.png', 'wb')"
            ],
        }
    ]
    assert read_violations(json.dumps({"cells": cells})) == []


# --- pins: one source --------------------------------------------------------------


def test_get_started_api_holds_no_pin_literals() -> None:
    source = Path(get_started_api.__file__).read_text()
    assert re.search(r"[A-Za-z0-9_.-]+==\d", source) is None


# --- the runner against a fake execute ---------------------------------------------


class Recorder:
    def __init__(
        self, code: int = 0, out: str = "PASS config\nPASS notebook\n"
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self.code = code
        self.out = out

    def __call__(self, cmd: Sequence[str], **kw: Any) -> Proc:
        answers = Path(cmd[cmd.index("--answers") + 1])
        self.calls.append({"cmd": list(cmd), "answers": answers.read_text(), **kw})
        return Proc(self.code, self.out, "")


@pytest.fixture
def run(tmp_path: Path) -> analysis.AnalysisRun:
    made = analysis.AnalysisRun(Console(headless=True), "id", tmp_path, "p", "1")
    TokenCache(made.config_dir / CACHE_NAME).write({"tokens": {"access_token": "t"}})
    command = ["uvx", "--from", WHEEL_URL, "asteroidea-setup", APP_URL]
    made.outputs["setup_command"] = json.dumps(command)
    return made


def test_pass1_runs_the_shown_command_with_an_answers_file(
    run: analysis.AnalysisRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    rec = Recorder()
    monkeypatch.setattr(outsider, "execute", rec)
    results = outsider.run_script(run)
    assert [r.name for r in results] == ["Script config", "Script notebook"]
    call = rec.calls[0]
    answers = run.base / "answers.txt"
    assert call["cmd"] == [
        "uvx",
        "--from",
        WHEEL_URL,
        "asteroidea-setup",
        APP_URL,
        "--answers",
        str(answers),
    ]
    assert call["answers"] == "1c=y\n1d=y\n1e=y\n1f=y\n1h=y\n"
    assert Path(call["cwd"]) == outsider.work_dir(run)
    assert call["env"]["HOME"] == str(outsider.fake_home(run))
    assert acceptance.REPO not in Path(call["cwd"]).resolve().parents
    assert str(acceptance.REPO) not in call["env"]["PATH"]
    assert "ASTEROIDEA_CONFIG_DIR" not in call["env"]


def test_the_sign_in_is_placed_where_setup_reads_a_cached_one(
    run: analysis.AnalysisRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(outsider, "execute", Recorder())
    outsider.run_script(run)
    placed = outsider.fake_home(run) / ".config" / "asteroidea" / CACHE_NAME
    assert json.loads(placed.read_text())["tokens"] == {"access_token": "t"}
    assert placed.stat().st_mode & 0o777 == 0o600


def test_pass2_adds_the_flag_drops_the_address_and_answers_the_pass2_rows(
    run: analysis.AnalysisRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    rec = Recorder(out="PASS 2e\n")
    monkeypatch.setattr(outsider, "execute", rec)
    outsider.run_pass2(run)
    call = rec.calls[0]
    assert call["cmd"][:4] == ["uvx", "--from", WHEEL_URL, "asteroidea-setup"]
    assert call["cmd"][4] == "--pass2"
    assert APP_URL not in call["cmd"]
    assert call["answers"] == "2b=y\n2c=y\n2d=y\n2f=y\n2h=y\n"


def test_a_failing_setup_stops_the_step_with_its_last_line(
    run: analysis.AnalysisRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    rec = Recorder(code=1, out="PASS config\nFAIL install: wheel sha256 is bad\n")
    monkeypatch.setattr(outsider, "execute", rec)
    with pytest.raises(StepFailureError, match="asteroidea-setup exit 1"):
        outsider.run_script(run)


def test_the_shown_install_text_parses_into_the_runner_command() -> None:
    text = get_started_api.install_command(wheel_filename="w.whl", web_url=APP_URL)
    command = shlex.split(text.splitlines()[-1])
    assert command == [
        "uvx",
        "--from",
        f"{APP_URL}/sdk/w.whl",
        "asteroidea-setup",
        APP_URL,
    ]
    assert len(command) == len(outsider.COMMAND_WORDS)


def test_human_rows_of_both_passes_carry_a_note_in_the_headless_run() -> None:
    pass2 = ("2b", "2c", "2d", "2e", "2f", "2g", "2h")
    out = "".join(f"PASS {row}\n" for row in (*pass2, "1c", "uv"))
    got = {r.name: bool(r.note) for r in outsider.parse_steps(out)}
    assert got["Script 2e"] is False
    assert got["Script 2g"] is False
    assert all(got[f"Script {row}"] for row in ("2b", "2c", "2d", "2f", "2h", "1c"))
    assert got["Script uv"] is False


# --- the runner's pure parts -----------------------------------------------------


def test_clean_path_drops_repo_entries(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / ".venv" / "bin").mkdir(parents=True)
    other = tmp_path / "other"
    other.mkdir()
    joined = f"{repo / '.venv' / 'bin'}:{other}:{repo}"
    assert outsider.clean_path(joined, repo.resolve()) == str(other)


def test_child_env_points_home_away_and_keeps_the_repo_off_path(tmp_path: Path) -> None:
    env = outsider.child_env(tmp_path, REPO)
    assert env["HOME"] == str(tmp_path)
    assert str(REPO) not in env["PATH"]
    assert "ASTEROIDEA_CONFIG_DIR" not in env


def test_parse_steps_reads_pass_and_fail_lines() -> None:
    out = "PASS uv\nNOTE x\nFAIL install: wheel sha256 is bad\nPASS 1h\n"
    got = outsider.parse_steps(out)
    assert [(r.name, r.passed) for r in got] == [
        ("Script uv", True),
        ("Script install", False),
        ("Script 1h", True),
    ]
    assert got[1].detail == "wheel sha256 is bad"
    assert got[2].note
    assert not got[0].note


def test_pass_selects_the_steps_and_rejects_other_values() -> None:
    assert outsider.main(["run", "3"]) == 2
    assert outsider.main(["bogus"]) == 2
    names = [title for title, _ in outsider.STEPS_PASS2]
    assert names[-1] == "Running the pass-2 rows"
    assert names[:-1] == [title for title, _ in outsider.STEPS][:-1]


def test_makefile_has_the_guarded_outsider_run_target() -> None:
    text = (REPO / "Makefile").read_text()
    recipe = text.split("\nlive-hosted-setup:", 1)[1].split("\n\n", 1)[0]
    assert "##" in recipe.splitlines()[0]
    assert "$(SANDBOX_GUARD)" in recipe
    assert "asteroidea_infra.hosted_setup_check run $(or $(PASS),1)" in recipe
    assert "PASS=2" in text.split("\nlive-hosted-setup:", 1)[1].splitlines()[0]


def test_main_selects_steps_by_argument(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: list[object] = []

    def fake(steps: object) -> int:
        seen.append(steps)
        return 0

    monkeypatch.setattr(outsider, "run_live", fake)
    assert outsider.main(["run", "1"]) == 0
    assert outsider.main(["run"]) == 0
    assert outsider.main(["run", "2"]) == 0
    assert seen == [outsider.STEPS, outsider.STEPS, outsider.STEPS_PASS2]
    assert outsider.main(["run", "3"]) == 2
    assert "usage: python -m asteroidea_infra.hosted_setup_check run [1|2]" in (
        capsys.readouterr().err
    )


def test_makefile_default_pass_is_accepted_by_main(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text = (REPO / "Makefile").read_text(encoding="utf-8")
    recipe = re.search(r"^live-hosted-setup:.*\n(?:\t.*\n?)+", text, re.MULTILINE)
    assert recipe
    found = re.search(
        r"asteroidea_infra\.hosted_setup_check run \$\(or \$\(PASS\),(\w+)\)",
        recipe.group(0),
    )
    assert found
    monkeypatch.setattr(outsider, "run_live", lambda _steps: 0)
    assert outsider.main(["run", found.group(1)]) == 0
