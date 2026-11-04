"""`asteroidea-setup`: every step's PASS and FAIL, with all outside effects faked."""

import hashlib
import json
import stat
import time
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from asteroidea._artifacts import ArtifactInfo, ArtifactResult
from asteroidea._auth import TokenCache
from asteroidea._http import Reply
from asteroidea._query import FileRef, QueryResult
from asteroidea._setup import Done, Sdk, Setup, main, read_answers
from asteroidea._upload import UploadResult
from asteroidea.errors import DatasetNotYoursError

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

APP = "https://app.example.test"
API = "https://api.example.test"
WHEEL = b"wheel-bytes"
NOTEBOOK = b"notebook-v1"
PLATFORM = {
    "region": "us-east-1",
    "user_pool_id": "p",
    "client_id": "c",
    "login_base_url": "https://login.example.test",
    "redirect_uri": "http://localhost:8765/callback",
    "identity_pool_id": "i",
    "api_url": API,
    "raw_bucket": "raw",
}
DATASET = "dandi-000623"
PINS = ["numpy==2.0.0", "ipykernel==7.4.0"]


class World:
    """The fakes of one run, and what they saw."""

    def __init__(self, home: Path) -> None:
        self.home = home
        self.platform: dict[str, object] = dict(PLATFORM)
        self.get_started: dict[str, object] | None = None
        self.get_started_status = 200
        self.wheel_sha = hashlib.sha256(WHEEL).hexdigest()
        self.notebook = NOTEBOOK
        self.pins: list[str] | None = list(PINS)
        self.children: list[list[str]] = []
        self.child_code = 0
        self.uv: str | None = "/bin/uv"
        self.prompts: list[str] = []
        self.out: list[str] = []
        self.answer = "y"

    def body(self) -> dict[str, object]:
        if self.get_started is not None:
            return self.get_started
        body: dict[str, object] = {
            "wheel": {
                "url": "https://files.example.test/w",
                "filename": "asteroidea-0.1.0-py3-none-any.whl",
                "sha256": self.wheel_sha,
            },
            "notebook": {"url": "https://files.example.test/n", "filename": "ex.ipynb"},
        }
        if self.pins is not None:
            body["pins"] = self.pins
        return body

    def transport(self, method: str, url: str, headers: object, body: object) -> Reply:
        del method, headers, body
        if url == f"{APP}/platform.json":
            return Reply(200, json.dumps(self.platform).encode())
        if url == f"{API}/get-started":
            return Reply(self.get_started_status, json.dumps(self.body()).encode())
        return Reply(404, b"{}")

    def downloader(self, url: str, dest: Path) -> int:
        dest.write_bytes(WHEEL if url.endswith("/w") else self.notebook)
        return 200

    def run(self, args: Sequence[str], timeout: float) -> Done:
        del timeout
        self.children.append(list(args))
        return Done(self.child_code, "child said no" if self.child_code else "")

    def which(self, name: str) -> str | None:
        return self.uv if name == "uv" else None

    def prompt(self, text: str) -> str:
        self.prompts.append(text)
        if "Paste" in text:
            return ""
        return self.answer

    def setup(self, **changes: object) -> Setup:
        base = Setup(
            app=APP,
            home=self.home,
            transport=self.transport,
            downloader=self.downloader,
            run=self.run,
            which=self.which,
            prompt=self.prompt,
            echo=self.out.append,
            sdk=Sdk(),
        )
        return replace(base, **changes)


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("ASTEROIDEA_CONFIG_DIR", str(tmp_path / "cfg"))
    return World(home)


def sign_in_cached(tmp_path: Path) -> None:
    TokenCache(tmp_path / "cfg" / "tokens.json").write(
        {
            "tokens": {
                "id_token": "i",
                "access_token": "tok",
                "refresh_token": "r",
                "expires_at": time.time() + 3600,
            }
        }
    )


@pytest.fixture
def signed(world: World, tmp_path: Path) -> World:
    sign_in_cached(tmp_path)
    return world


def run_pass1(w: World) -> int:
    return w.setup().execute(pass2=False)


def report(w: World) -> str:
    return (w.home / "asteroidea-get-started" / "report.txt").read_text()


def cfg_file(tmp_path: Path) -> Path:
    return tmp_path / "cfg" / "platform.json"


def test_full_run_passes_every_step(signed: World, tmp_path: Path) -> None:
    assert run_pass1(signed) == 0
    text = report(signed)
    for step in (
        "PASS 1c",
        "PASS config",
        "PASS sign-in",
        "PASS get-started",
        "PASS uv",
        "PASS venv",
        "PASS install",
        "PASS kernel",
        "PASS notebook",
        "PASS 1h",
    ):
        assert step in text
    assert "FAIL" not in text
    assert (
        signed.home / "asteroidea-get-started" / "ex.ipynb"
    ).read_bytes() == NOTEBOOK
    assert any("jupyter lab" in line for line in signed.out)
    venv, install, kernel = signed.children
    assert venv[1:4] == ["venv", "--allow-existing", "--python"]
    assert venv[4] == "3.14"
    assert install[:3] == ["/bin/uv", "pip", "install"]
    assert install[-2:] == PINS
    assert "asteroidea[nwb,nifti] @ file://" in install[5]
    assert kernel[1:] == ["-m", "ipykernel", "install", "--sys-prefix"]
    assert cfg_file(tmp_path).exists()


def test_rerun_is_idempotent(signed: World) -> None:
    assert run_pass1(signed) == 0
    assert run_pass1(signed) == 0
    folder = signed.home / "asteroidea-get-started"
    assert not (folder / "ex.ipynb.bak").exists()
    assert report(signed).count("# run") == 2


def test_config_written_0600(signed: World, tmp_path: Path) -> None:
    assert run_pass1(signed) == 0
    assert stat.S_IMODE(cfg_file(tmp_path).stat().st_mode) == 0o600
    assert json.loads(cfg_file(tmp_path).read_text())["api_url"] == API


def test_config_refuses_a_different_one_without_force(
    signed: World, tmp_path: Path
) -> None:
    cfg_file(tmp_path).write_text(json.dumps({**PLATFORM, "raw_bucket": "other"}))
    assert run_pass1(signed) == 1
    assert "FAIL config:" in report(signed)
    assert "--force" in report(signed)
    assert json.loads(cfg_file(tmp_path).read_text())["raw_bucket"] == "other"
    assert not signed.children


def test_force_replaces_a_different_config(signed: World, tmp_path: Path) -> None:
    cfg_file(tmp_path).write_text(json.dumps({**PLATFORM, "raw_bucket": "other"}))
    assert signed.setup(force=True).execute(pass2=False) == 0
    assert json.loads(cfg_file(tmp_path).read_text())["raw_bucket"] == "raw"
    assert stat.S_IMODE(cfg_file(tmp_path).stat().st_mode) == 0o600


def test_config_fails_when_not_https(signed: World) -> None:
    code = signed.setup(app="http://app.example.test").execute(pass2=False)
    assert code == 1
    assert "FAIL config: the web address must start with https://" in report(signed)


def test_config_fails_when_incomplete(signed: World, tmp_path: Path) -> None:
    del signed.platform["region"]
    assert run_pass1(signed) == 1
    assert "FAIL config:" in report(signed)
    assert "region" in report(signed)
    assert not cfg_file(tmp_path).exists()


def test_config_fails_on_a_404(signed: World) -> None:
    code = signed.setup(app="https://nowhere.example.test").execute(pass2=False)
    assert code == 1
    assert "FAIL config:" in report(signed)


def test_sign_in_fails_without_a_paste(world: World) -> None:
    assert run_pass1(world) == 1
    assert "FAIL sign-in:" in report(world)


def test_get_started_refusal(signed: World) -> None:
    signed.get_started_status = 503
    assert run_pass1(signed) == 1
    assert "FAIL get-started: the platform answered 503" in report(signed)


def test_get_started_without_pins_fails(signed: World) -> None:
    signed.pins = None
    assert run_pass1(signed) == 1
    assert "FAIL get-started: the platform did not send the package pins" in report(
        signed
    )


def test_get_started_rejects_a_path_in_a_filename(signed: World) -> None:
    signed.get_started = {
        "wheel": {"url": "u", "filename": "../x.whl", "sha256": "s"},
        "notebook": {"url": "u", "filename": "n.ipynb"},
        "pins": [],
    }
    assert run_pass1(signed) == 1
    assert "FAIL get-started: the platform sent an unsafe wheel name" in report(signed)


def test_uv_missing(signed: World) -> None:
    signed.uv = None
    assert run_pass1(signed) == 1
    assert "FAIL uv: uv is not installed" in report(signed)
    assert any("astral.sh/uv/install.sh" in line for line in signed.out)
    assert not signed.children


def test_venv_failure_shows_the_childs_last_line(signed: World) -> None:
    signed.child_code = 1
    assert run_pass1(signed) == 1
    assert "FAIL venv: child said no" in report(signed)


def test_sha_mismatch_stops_before_install(signed: World) -> None:
    signed.wheel_sha = "0" * 64
    assert run_pass1(signed) == 1
    got = hashlib.sha256(WHEEL).hexdigest()
    assert f"FAIL install: wheel sha256 is {got}, expected {'0' * 64}" in report(signed)
    assert len(signed.children) == 1  # the venv only; no pip install


def test_install_failure(signed: World) -> None:
    def run(args: Sequence[str], _timeout: float) -> Done:
        signed.children.append(list(args))
        return Done(1 if "pip" in args else 0, "no wheel")

    assert signed.setup(run=run).execute(pass2=False) == 1
    assert "FAIL install: no wheel" in report(signed)


def test_kernel_failure(signed: World) -> None:
    def run(args: Sequence[str], _timeout: float) -> Done:
        return Done(1 if "ipykernel" in args else 0, "kernel broke")

    assert signed.setup(run=run).execute(pass2=False) == 1
    assert "FAIL kernel: kernel broke" in report(signed)


def test_changed_notebook_keeps_a_bak(signed: World) -> None:
    assert run_pass1(signed) == 0
    signed.notebook = b"notebook-v2"
    assert run_pass1(signed) == 0
    folder = signed.home / "asteroidea-get-started"
    assert (folder / "ex.ipynb").read_bytes() == b"notebook-v2"
    assert (folder / "ex.ipynb.bak").read_bytes() == NOTEBOOK
    assert "NOTE the old notebook copy is kept as" in report(signed)


def test_a_no_answer_fails_the_row(signed: World) -> None:
    signed.answer = "n"
    assert run_pass1(signed) == 1
    assert "FAIL 1c: answered no" in report(signed)


def test_answers_file_replaces_prompts(signed: World, tmp_path: Path) -> None:
    answers = tmp_path / "answers.txt"
    answers.write_text("# rows\n1c=y\n1d=y\n1e=y\n1f=y\n1h=y\n")
    assert read_answers(answers)["1c"] == "y"
    seen: list[str] = []

    def configure(setup: Setup) -> Setup:
        return replace(
            signed.setup(answers=setup.answers), prompt=lambda t: seen.append(t) or "n"
        )

    assert main([APP, "--answers", str(answers)], configure=configure) == 0
    assert seen == []
    assert "NOTE 1c answered from a file: y" in report(signed)


def test_answers_file_missing_row_is_no(signed: World, tmp_path: Path) -> None:
    answers = tmp_path / "answers.txt"
    answers.write_text("1c=y\n")
    code = main(
        [APP, "--answers", str(answers)],
        configure=lambda s: signed.setup(answers=s.answers),
    )
    assert code == 1
    assert "FAIL 1d: answered no" in report(signed)


def test_usage_needs_an_address() -> None:
    with pytest.raises(SystemExit) as caught:
        main([])
    assert caught.value.code == 2


REF = FileRef("asset", DATASET, "p", "k", "v", 1)


class Pass2Fakes:
    def __init__(self, *, refuse: bool = True, listed: bool = True) -> None:
        self.refuse = refuse
        self.listed = listed
        self.saved: dict[str, bytes] = {}

    def query(self, **_: str) -> QueryResult:
        return QueryResult(rows=[], files=[REF], snapshot_id=7)

    def save_artifact(self, path: Path, **_: object) -> ArtifactResult:
        self.saved["a1"] = path.read_bytes()
        return ArtifactResult("a1", path.name, 1, 7)

    def artifacts(self) -> list[ArtifactInfo]:
        info = ArtifactInfo("a1", "n", 1, "text/plain", 7, "COMMITTED", 0, 0)
        return [info] if self.listed else []

    def download_artifact(self, artifact_id: str, dest: Path) -> Path:
        dest.write_bytes(self.saved[artifact_id])
        return dest

    def upload(self, _path: Path, **_: object) -> UploadResult:
        if self.refuse:
            raise DatasetNotYoursError
        return UploadResult("s", DATASET, "p", "a", 1)

    def sdk(self) -> Sdk:
        return Sdk(
            query=self.query,
            save_artifact=self.save_artifact,
            artifacts=self.artifacts,
            download_artifact=self.download_artifact,
            upload=self.upload,
        )


def run_pass2(w: World, fakes: Pass2Fakes) -> int:
    return w.setup(sdk=fakes.sdk()).execute(pass2=True)


def test_pass2_all_rows(signed: World, tmp_path: Path) -> None:
    cfg_file(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    cfg_file(tmp_path).write_text(json.dumps(PLATFORM))
    assert run_pass2(signed, Pass2Fakes()) == 0
    text = report(signed)
    for row in ("2b", "2c", "2d", "2e", "2f", "2g", "2h"):
        assert f"PASS {row}" in text
    assert len(signed.prompts) == 5


def test_pass2_needs_the_config(signed: World) -> None:
    assert run_pass2(signed, Pass2Fakes()) == 1
    assert "FAIL config:" in report(signed)
    assert "Run the setup first" in report(signed)


@pytest.mark.parametrize(
    ("fakes", "row", "reason"),
    [
        (Pass2Fakes(listed=False), "2e", "artifacts() does not list a1"),
        (Pass2Fakes(refuse=False), "2g", "was accepted"),
    ],
)
def test_pass2_sdk_row_failures(
    signed: World, tmp_path: Path, fakes: Pass2Fakes, row: str, reason: str
) -> None:
    cfg_file(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    cfg_file(tmp_path).write_text(json.dumps(PLATFORM))
    assert run_pass2(signed, fakes) == 1
    assert f"FAIL {row}:" in report(signed)
    assert reason in report(signed)
