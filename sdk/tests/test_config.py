import json
from typing import TYPE_CHECKING

import pytest

from asteroidea._config import CONFIG_DIR_ENV, load_config
from asteroidea.errors import ConfigMissingError

if TYPE_CHECKING:
    from pathlib import Path

GOOD = {
    "region": "us-east-1",
    "user_pool_id": "p",
    "client_id": "c",
    "login_base_url": "https://login.example.test",
    "redirect_uri": "http://localhost:8765/callback",
    "identity_pool_id": "i",
    "api_url": "https://api.example.test",
    "raw_bucket": "raw-bucket",
}


def test_loads_from_explicit_path(tmp_path: Path) -> None:
    path = tmp_path / "platform.json"
    path.write_text(json.dumps(GOOD))
    assert load_config(path).client_id == "c"


def test_loads_from_config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "platform.json").write_text(json.dumps(GOOD))
    monkeypatch.setenv(CONFIG_DIR_ENV, str(tmp_path))
    assert load_config().user_pool_id == "p"


def test_default_dir_is_home_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(CONFIG_DIR_ENV, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    target = tmp_path / ".config" / "asteroidea"
    target.mkdir(parents=True)
    (target / "platform.json").write_text(json.dumps(GOOD))
    assert load_config().region == "us-east-1"


def test_missing_file_says_who_to_ask(tmp_path: Path) -> None:
    with pytest.raises(
        ConfigMissingError, match=r"ask your administrator for platform\.json"
    ):
        load_config(tmp_path / "nope.json")


@pytest.mark.parametrize("text", ["not json", "[]", json.dumps({"region": "x"})])
def test_unusable_file_is_config_missing(tmp_path: Path, text: str) -> None:
    path = tmp_path / "platform.json"
    path.write_text(text)
    with pytest.raises(ConfigMissingError, match="ask your administrator"):
        load_config(path)
