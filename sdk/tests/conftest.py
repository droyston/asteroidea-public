import time
from typing import TYPE_CHECKING

import pytest

from asteroidea._auth import Auth, TokenCache
from asteroidea._config import PlatformConfig

if TYPE_CHECKING:
    from pathlib import Path

API = "https://api.example.test"
ACCESS_TOKEN = "the-access-token"


@pytest.fixture
def auth(tmp_path: Path) -> Auth:
    """Return a signed-in `Auth` whose platform API is `API`."""
    config = PlatformConfig(
        region="us-east-1",
        user_pool_id="p",
        client_id="c",
        login_base_url="https://login.example.test",
        redirect_uri="http://localhost:8765/callback",
        identity_pool_id="i",
        api_url=API,
        raw_bucket="raw",
    )
    signed = Auth(config, TokenCache(tmp_path / "tokens.json"))
    signed.cache.write(
        {
            "tokens": {
                "id_token": "i",
                "access_token": ACCESS_TOKEN,
                "refresh_token": "r",
                "expires_at": time.time() + 3600,
            }
        }
    )
    return signed
