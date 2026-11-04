"""Load `platform.json`, the settings an administrator hands each researcher."""

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from asteroidea.errors import ConfigMissingError

CONFIG_DIR_ENV: Final = "ASTEROIDEA_CONFIG_DIR"
CONFIG_NAME: Final = "platform.json"
_KEYS: Final = (
    "region",
    "user_pool_id",
    "client_id",
    "login_base_url",
    "redirect_uri",
    "identity_pool_id",
    "api_url",
    "raw_bucket",
)


@dataclass(frozen=True)
class PlatformConfig:
    """The values `make sdk-config` prints."""

    region: str
    user_pool_id: str
    client_id: str
    login_base_url: str
    redirect_uri: str
    identity_pool_id: str
    api_url: str
    raw_bucket: str


def config_dir() -> Path:
    """Return `ASTEROIDEA_CONFIG_DIR`, else `~/.config/asteroidea`."""
    override = os.environ.get(CONFIG_DIR_ENV, "")
    return Path(override) if override else Path.home() / ".config" / "asteroidea"


def load_config(path: Path | str | None = None) -> PlatformConfig:
    """Read `platform.json` from `path`, or from the settings directory.

    Raises `ConfigMissingError` when the file is absent, unreadable or incomplete.
    """
    target = Path(path) if path is not None else config_dir() / CONFIG_NAME
    try:
        raw = json.loads(target.read_text())
    except FileNotFoundError as err:
        msg = (
            f"No platform settings at {target}; ask your administrator for "
            f"platform.json and save it there."
        )
        raise ConfigMissingError(msg) from err
    except (OSError, ValueError) as err:
        msg = (
            f"The platform settings at {target} cannot be read; ask your "
            f"administrator for a new platform.json."
        )
        raise ConfigMissingError(msg) from err
    missing = [k for k in _KEYS if not isinstance(raw, dict) or not raw.get(k)]
    if missing:
        msg = (
            f"The platform settings at {target} lack {', '.join(missing)}; "
            f"ask your administrator for a new platform.json."
        )
        raise ConfigMissingError(msg)
    return PlatformConfig(**{k: str(raw[k]) for k in _KEYS})
