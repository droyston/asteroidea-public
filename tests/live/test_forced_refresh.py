"""Forced refresh: expire everything, read again.

A test researcher signs in without a browser: the `sandbox-test` client issues tokens
to an admin-created user, and those tokens are written to the SDK's cache file. The
refresh grant goes through `InitiateAuth` for this client, because `sandbox-test` has
OAuth disabled and so has no token endpoint (the one seam; the real `sdk` client's
refresh grant was proven by `asteroidea_infra.auth_check`). Everything else is the
SDK's own code.

The password lives only in `$ASTEROIDEA_LIVE_SCRATCH/live-user.json` (mode 0600).
"""

import json
import stat
from typing import TYPE_CHECKING

from asteroidea._creds import credentials, session

if TYPE_CHECKING:
    from pathlib import Path

    import boto3
    import pytest

    from asteroidea._auth import Auth

DATABASE = "asteroidea"
TABLE = "assets"
FILE_MODE = 0o600
DIR_MODE = 0o700
HOUR = 3600


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_forced_refresh_gets_new_credentials_and_reads(
    auth: Auth, capsys: pytest.CaptureFixture[str]
) -> None:
    cache_path = auth.cache.path
    config_dir = cache_path.parent
    assert _mode(cache_path) == FILE_MODE
    assert _mode(config_dir) == DIR_MODE

    first = credentials(auth)
    assert _read_table(session(auth)) == TABLE
    old_tokens = auth.tokens()

    # Force expiry: the credentials' and the tokens' expiry go to the past.
    saved = json.loads(cache_path.read_text())
    past = auth.clock() - HOUR
    saved["credentials"]["expires_at"] = past
    saved["tokens"]["expires_at"] = past
    cache_path.write_text(json.dumps(saved))
    cache_path.chmod(FILE_MODE)
    capsys.readouterr()  # drop earlier output

    # A data call must refresh by itself.
    refreshed_session = session(auth)
    assert _read_table(refreshed_session) == TABLE
    second = credentials(auth)

    printed = capsys.readouterr()
    assert second.access_key_id != first.access_key_id
    assert second.expires_at > auth.clock() + 300
    assert auth.tokens().access_token != old_tokens.access_token
    assert auth.tokens().refresh_token == old_tokens.refresh_token
    assert "oauth2/authorize" not in printed.out + printed.err
    assert "http" not in printed.out + printed.err
    assert _mode(cache_path) == FILE_MODE
    assert _mode(config_dir) == DIR_MODE


def _read_table(held: boto3.Session) -> str:
    """Return the name of the `assets` table, read as ResearcherRole may."""
    table = held.client("glue").get_table(DatabaseName=DATABASE, Name=TABLE)["Table"]
    return str(table["Name"])
