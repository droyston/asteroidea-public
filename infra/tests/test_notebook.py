"""`make notebook` and `make notebook-run`: the offline parts.

No AWS, no network, no real venv: `analysis.build_wheel`, `analysis.make_venv` and
`notebook.execute` are replaced by fakes.
"""

import re
import tomllib
from typing import TYPE_CHECKING, Any, Final

import pytest

from asteroidea_infra import analysis_check as analysis
from asteroidea_infra import notebook
from asteroidea_infra.runner import REPO, Proc
from asteroidea_infra.steps import Console, StepFailureError, StepResult

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

PINNED: Final = re.compile(r"^[A-Za-z0-9_.-]+==\d+(\.\d+)*$")


def test_every_notebook_pin_is_exact_and_polars_is_in_it() -> None:
    assert all(PINNED.match(pin) for pin in notebook.NOTEBOOK_PINS)
    names = {pin.split("==")[0] for pin in notebook.NOTEBOOK_PINS}
    assert {
        "numpy",
        "pandas",
        "matplotlib",
        "polars",
        "pynapple",
        "jupyterlab",
        "ipykernel",
        "nbconvert",
        "scipy",
        "nilearn",
    } <= names
    assert len(names) == len(notebook.NOTEBOOK_PINS)


def test_the_pynapple_pin_is_the_sdk_extra_pin() -> None:
    # `to_pynapple()` in the notebook needs the extra's exact version
    extra = tomllib.loads((REPO / "sdk" / "pyproject.toml").read_text())
    pins = extra["project"]["optional-dependencies"]["pynapple"]
    assert [p for p in notebook.NOTEBOOK_PINS if p.startswith("pynapple==")] == pins


def test_the_analysis_pins_do_not_gain_the_notebook_packages() -> None:
    assert analysis.EXTRA_PINS == ("numpy==2.5.4", "pandas==3.1.0")


class Builds:
    """Fakes for the wheel build and the venv build; count the calls."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.venvs = 0
        self.commands: list[list[str]] = []
        self.content = b"wheel-1"

    def wheel(self, out_dir: Path, _next_step: str) -> Path:
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / "asteroidea-0.1.0-py3-none-any.whl"
        path.write_bytes(self.content)
        return path

    def venv(self, venv: Path, _wheel: Path, _pins: Sequence[str], _next: str) -> None:
        self.venvs += 1
        (venv / "bin").mkdir(parents=True, exist_ok=True)
        (venv / "bin" / "python").write_text("")

    def must(self, cmd: Sequence[str], **_kwargs: object) -> Proc:
        self.commands.append(list(cmd))
        return Proc(0, "", "")


def install_fakes(builds: Builds, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(analysis, "build_wheel", builds.wheel)
    monkeypatch.setattr(analysis, "make_venv", builds.venv)
    monkeypatch.setattr(notebook, "must", builds.must)


def prepare(builds: Builds, pins: Sequence[str]) -> Path:
    return notebook.prepare(
        builds.tmp / "venv", builds.tmp / "stamp", builds.tmp / "wheel", pins
    )


def test_the_venv_is_rebuilt_only_when_the_wheel_or_the_pins_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    builds = Builds(tmp_path)
    install_fakes(builds, monkeypatch)
    pins = ("a==1", "b==2")
    python = prepare(builds, pins)
    assert python == tmp_path / "venv" / "bin" / "python"
    assert builds.venvs == 1
    prepare(builds, pins)
    assert builds.venvs == 1  # stamp hit
    prepare(builds, ("a==1", "b==3"))
    assert builds.venvs == 2  # a pin changed
    builds.content = b"wheel-2"
    prepare(builds, ("a==1", "b==3"))
    assert builds.venvs == 3  # the wheel changed


def test_a_missing_python_forces_a_rebuild(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    builds = Builds(tmp_path)
    install_fakes(builds, monkeypatch)
    python = prepare(builds, ("a==1",))
    python.unlink()
    prepare(builds, ("a==1",))
    assert builds.venvs == 2


def kernel_installs(builds: Builds) -> list[list[str]]:
    return [c for c in builds.commands if "ipykernel" in c and "install" in c]


def test_the_kernelspec_is_pinned_to_the_venv_interpreter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bare `python` in kernel.json ran another Python under `uv run`."""
    builds = Builds(tmp_path)
    install_fakes(builds, monkeypatch)
    python = prepare(builds, ("a==1",))
    assert kernel_installs(builds) == [
        [str(python), "-m", "ipykernel", "install", "--sys-prefix", "--name", "python3"]
    ]
    prepare(builds, ("a==1",))
    assert len(kernel_installs(builds)) == 1  # stamp hit: not again


def test_a_venv_stamped_before_the_kernelspec_fix_is_rebuilt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    builds = Builds(tmp_path)
    install_fakes(builds, monkeypatch)
    python = prepare(builds, ("a==1",))
    old = "\n".join(
        line
        for line in (tmp_path / "stamp").read_text().splitlines()
        if not line.startswith("kernelspec")
    )
    (tmp_path / "stamp").write_text(old + "\n")
    assert python.exists()
    prepare(builds, ("a==1",))
    assert builds.venvs == 2
    assert len(kernel_installs(builds)) == 2


def test_the_run_venv_gets_the_same_kernelspec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    builds = Builds(tmp_path)
    install_fakes(builds, monkeypatch)
    monkeypatch.setattr(
        analysis,
        "install",
        lambda _run, _pins: [StepResult("Install", passed=True, detail="")],
    )
    run = make_run(tmp_path)
    notebook.install_notebook(run)
    assert kernel_installs(builds) == [
        [
            str(run.python),
            "-m",
            "ipykernel",
            "install",
            "--sys-prefix",
            "--name",
            "python3",
        ]
    ]


def write_platform(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "platform.json").write_text("{}")


def test_make_notebook_refuses_without_platform_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    called: list[str] = []
    monkeypatch.setattr(notebook, "prepare", lambda: called.append("prepare"))
    monkeypatch.setenv("ASTEROIDEA_CONFIG_DIR", str(tmp_path / "empty"))
    assert notebook.main(["prepare"]) == 2
    assert called == []
    err = capsys.readouterr().err
    assert str(tmp_path / "empty" / "platform.json") in err
    assert "make researcher EMAIL=<your address>" in err


def test_make_notebook_proceeds_with_platform_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    called: list[str] = []
    monkeypatch.setattr(notebook, "prepare", lambda: called.append("prepare"))
    monkeypatch.setenv("ASTEROIDEA_CONFIG_DIR", str(tmp_path))
    write_platform(tmp_path)
    assert notebook.main(["prepare"]) == 0
    assert called == ["prepare"]


def test_the_default_platform_path_is_the_sdk_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ASTEROIDEA_CONFIG_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert (
        notebook.platform_path()
        == tmp_path / ".config" / "asteroidea" / "platform.json"
    )


def make_run(tmp_path: Path) -> analysis.AnalysisRun:
    console = Console(headless=True, read=lambda _p: "", write=lambda _l: None)
    return analysis.AnalysisRun(console, "r1", tmp_path, "profile", "000000000000")


def test_execute_notebook_runs_nbconvert_with_the_config_dir_and_the_cell_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    def fake(cmd: Sequence[str], **kwargs: object) -> Proc:
        # The first call executes; the second renders the executed copy
        seen.setdefault("cmd", list(cmd))
        seen.setdefault("env", kwargs["env"])
        return Proc(0, "", "")

    monkeypatch.setattr(notebook, "execute", fake)
    run = make_run(tmp_path)
    [result] = notebook.execute_notebook(run)
    assert result.passed
    cmd = seen["cmd"]
    # `python -m jupyter nbconvert` execs jupyter-nbconvert from PATH
    assert cmd[:4] == [str(run.python), "-m", "nbconvert", "--to"]
    assert "--execute" in cmd
    assert "--ExecutePreprocessor.timeout=600" in cmd
    env = seen["env"]
    assert env["ASTEROIDEA_CONFIG_DIR"] == str(run.config_dir)
    stem = notebook.NOTEBOOK.stem
    assert str(tmp_path / f"{stem}.executed.ipynb") in result.detail


def nbconvert_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: Path
) -> tuple[list[list[str]], StepResult]:
    calls: list[list[str]] = []

    def fake(cmd: Sequence[str], **_kwargs: object) -> Proc:
        calls.append(list(cmd))
        return Proc(0, "", "")

    monkeypatch.setattr(notebook, "execute", fake)
    [result] = notebook.execute_notebook(make_run(tmp_path), path)
    return calls, result


def test_each_notebook_gets_its_own_executed_copy_and_html(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    showcase = notebook.NOTEBOOKS["showcase"]
    calls, result = nbconvert_calls(tmp_path, monkeypatch, showcase)
    executed = tmp_path / "showcase.executed.ipynb"
    assert [c[c.index("--to") + 1] for c in calls] == ["notebook", "html"]
    assert calls[0][-1] == str(showcase)
    assert calls[0][calls[0].index("--output") + 1] == executed.name
    assert calls[1][-1] == str(executed)
    assert calls[1][calls[1].index("--output") + 1] == "showcase.executed.html"
    assert "--execute" not in calls[1]
    assert str(executed) in result.detail
    assert str(tmp_path / "showcase.executed.html") in result.detail


def test_a_failing_html_render_is_a_step_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    codes = iter([0, 1])

    def fake(_cmd: Sequence[str], **_kwargs: object) -> Proc:
        return Proc(next(codes), "", "line\nTemplateNotFound: lab")

    monkeypatch.setattr(notebook, "execute", fake)
    with pytest.raises(StepFailureError, match="to HTML failed: TemplateNotFound"):
        notebook.execute_notebook(make_run(tmp_path))


def test_the_registry_keeps_the_exemplar_as_the_default_and_the_handout() -> None:
    assert notebook.NOTEBOOK == REPO / "notebooks" / "exemplar.ipynb"
    assert notebook.NOTEBOOKS[notebook.DEFAULT_NOTEBOOK] == notebook.NOTEBOOK
    assert set(notebook.NOTEBOOKS) == {"exemplar", "showcase"}
    assert all(path.exists() for path in notebook.NOTEBOOKS.values())


def test_the_run_steps_execute_the_chosen_notebook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[Path] = []
    monkeypatch.setattr(
        notebook,
        "run_notebook",
        lambda _run, job: seen.append(job.notebook) or [],
    )
    for name in ("exemplar", "showcase"):
        steps = notebook.steps_for(name)
        assert [t for t, _ in steps] == [t for t, _ in notebook.STEPS]
        steps[-1][1](make_run(tmp_path))
    assert seen == [notebook.NOTEBOOKS["exemplar"], notebook.NOTEBOOKS["showcase"]]


def test_make_notebook_run_takes_a_notebook_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ran: list[str] = []
    monkeypatch.setattr(notebook, "run_live", ran.append)
    notebook.main(["run", "--notebook", "showcase"])
    notebook.main(["run"])
    assert ran == ["showcase", "exemplar"]
    with pytest.raises(SystemExit):
        notebook.main(["run", "--notebook", "nope"])


def test_a_failing_notebook_becomes_a_step_failure_naming_the_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake(_cmd: Sequence[str], **_kwargs: object) -> Proc:
        return Proc(1, "", "line\nCellExecutionError: boom")

    monkeypatch.setattr(notebook, "execute", fake)
    with pytest.raises(StepFailureError, match="CellExecutionError: boom"):
        notebook.execute_notebook(make_run(tmp_path))


def test_the_run_steps_reuse_the_analysis_steps_and_end_with_the_notebook() -> None:
    titles = [title for title, _ in notebook.STEPS]
    assert titles[0] == "Checking the AWS sign-in"
    assert titles[-1] == "Executing the notebook"
    assert notebook.STEPS[1][1] is analysis.create_researcher


def test_the_run_stops_at_the_first_failing_step(tmp_path: Path) -> None:
    ran: list[str] = []

    def ok(_run: analysis.AnalysisRun) -> list[StepResult]:
        ran.append("ok")
        return []

    def bad(_run: analysis.AnalysisRun) -> list[StepResult]:
        ran.append("bad")
        msg = "x"
        raise StepFailureError(msg, "y")

    steps: tuple[analysis.Step, ...] = (("one", ok), ("two", bad), ("three", ok))
    assert analysis.run_all(make_run(tmp_path), steps) is False
    assert ran == ["ok", "bad"]


# --- AS=me: the user's own sign-in ---

DEFAULT_TITLES: Final = (
    "Checking the AWS sign-in",
    "Creating the researcher",
    "Building and installing the SDK and notebook packages",
    "Signing in",
    "Executing the notebook",
)


def owner_run(tmp_path: Path, owner: Path) -> notebook.OwnerRun:
    console = Console(headless=True, read=lambda _p: "", write=lambda _l: None)
    return notebook.OwnerRun(
        console, "r1", tmp_path / "run", "profile", "000000000000", owner_config=owner
    )


def test_the_default_steps_are_unchanged() -> None:
    for name in notebook.NOTEBOOKS:
        assert tuple(t for t, _ in notebook.steps_for(name)) == DEFAULT_TITLES
    assert notebook.STEPS[3][1] is notebook.first_hour.sign_in


def test_as_me_skips_creating_the_researcher_and_signing_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    steps = notebook.steps_for("showcase", as_owner=True)
    assert [t for t, _ in steps] == [
        "Checking the AWS sign-in",
        "Building and installing the SDK and notebook packages",
        "Checking your own sign-in",
        "Executing the notebook",
    ]
    actions = [a for _, a in steps]
    assert analysis.create_researcher not in actions
    assert notebook.first_hour.sign_in not in actions
    seen: list[Path] = []
    monkeypatch.setattr(
        notebook, "run_notebook", lambda _run, job: seen.append(job.notebook) or []
    )
    steps[-1][1](make_run(tmp_path))
    assert seen == [notebook.NOTEBOOKS["showcase"]]


def test_the_owner_run_uses_the_sdk_settings_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = tmp_path / "owner-config"
    monkeypatch.setenv("ASTEROIDEA_CONFIG_DIR", str(owner))
    console = Console(headless=True, read=lambda _p: "", write=lambda _l: None)
    run = notebook.OwnerRun(console, "r1", tmp_path / "run", "p", "000000000000")
    assert run.config_dir == owner
    assert run.child_env["ASTEROIDEA_CONFIG_DIR"] == str(owner)
    monkeypatch.delenv("ASTEROIDEA_CONFIG_DIR")
    home = notebook.OwnerRun(console, "r1", tmp_path / "run", "p", "000000000000")
    assert home.config_dir == notebook.Path.home() / ".config" / "asteroidea"


def test_as_me_executes_the_notebook_with_the_owners_config_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    envs: list[dict[str, str]] = []

    def fake(_cmd: Sequence[str], **kwargs: Any) -> Proc:
        envs.append(dict(kwargs["env"]))
        return Proc(0, "", "")

    monkeypatch.setattr(notebook, "execute", fake)
    owner = tmp_path / "owner-config"
    run = owner_run(tmp_path, owner)
    notebook.execute_notebook(run, notebook.NOTEBOOKS["showcase"])
    assert envs
    assert {e["ASTEROIDEA_CONFIG_DIR"] for e in envs} == {str(owner)}
    assert str(run.base / "config") not in {e["ASTEROIDEA_CONFIG_DIR"] for e in envs}


def signed_in(owner: Path) -> dict[str, bytes]:
    owner.mkdir(parents=True)
    files = {"platform.json": b'{"client_id": "c"}', "tokens.json": b'{"tokens": {}}'}
    for name, body in files.items():
        (owner / name).write_bytes(body)
    return files


def test_the_sign_in_check_calls_the_sdk_and_leaves_the_tokens_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[list[str], dict[str, str]]] = []

    def fake(cmd: Sequence[str], **kwargs: Any) -> Proc:
        calls.append((list(cmd), dict(kwargs["env"])))
        return Proc(0, "", "")

    monkeypatch.setattr(notebook, "execute", fake)
    owner = tmp_path / "owner-config"
    files = signed_in(owner)
    run = owner_run(tmp_path, owner)
    [result] = notebook.check_owner_sign_in(run)
    assert result.passed
    [(cmd, env)] = calls
    assert cmd[:2] == [str(run.python), "-c"]
    assert "asteroidea.studies()" in cmd[2]
    assert env["ASTEROIDEA_CONFIG_DIR"] == str(owner)
    assert {p.name: p.read_bytes() for p in owner.iterdir()} == files
    assert not (run.base / "config").exists()


@pytest.mark.parametrize("missing", ["tokens.json", "platform.json"])
def test_the_sign_in_check_refuses_a_missing_file_naming_sign_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    monkeypatch.setattr(notebook, "execute", lambda *_a, **_k: Proc(0, "", ""))
    owner = tmp_path / "owner-config"
    signed_in(owner)
    (owner / missing).unlink()
    with pytest.raises(StepFailureError, match=f"{missing} is missing") as err:
        notebook.check_owner_sign_in(owner_run(tmp_path, owner))
    assert "asteroidea.sign_in()" in err.value.next_step


def test_the_sign_in_check_refuses_a_refused_sign_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    refused = "SignInRequiredError: You are not signed in, or your session has ended"
    monkeypatch.setattr(notebook, "execute", lambda *_a, **_k: Proc(1, refused, ""))
    owner = tmp_path / "owner-config"
    signed_in(owner)
    with pytest.raises(
        StepFailureError, match="was refused: SignInRequiredError"
    ) as err:
        notebook.check_owner_sign_in(owner_run(tmp_path, owner))
    assert "asteroidea.sign_in()" in err.value.next_step


def test_make_notebook_run_as_me_runs_the_owner_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ran: list[tuple[str, str]] = []
    monkeypatch.setattr(notebook, "run_live", lambda n: ran.append(("default", n)))
    monkeypatch.setattr(notebook, "run_as_owner", lambda n: ran.append(("me", n)))
    notebook.main(["run", "--notebook", "showcase", "--as", "me"])
    notebook.main(["run", "--notebook", "showcase"])
    assert ran == [("me", "showcase"), ("default", "showcase")]
    with pytest.raises(SystemExit):
        notebook.main(["run", "--as", "someone"])


def test_the_report_labels_the_owner_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(notebook, "commit_sha", lambda: "abc")
    owner = tmp_path / "owner-config"
    run = owner_run(tmp_path, owner)
    run.base.mkdir(parents=True)
    text = notebook.write_report(run, "showcase", as_owner=True).read_text()
    assert "- mode: headless, as the user's own sign-in" in text
    assert f"as the user's own sign-in ({owner})" in text
    plain = make_run(tmp_path / "plain")
    plain.base.mkdir(parents=True)
    assert "- mode: headless\n" in notebook.write_report(plain, "showcase").read_text()
