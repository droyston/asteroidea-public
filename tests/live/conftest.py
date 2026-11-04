"""Live tests touch the sandbox account; they run only through `make live`.

Without `ASTEROIDEA_LIVE=1` the live tests are not collected. With it set but the
sandbox env missing, the run exits 4: a failure, never a skip.
"""

import contextlib
import importlib.util
import json
import os
import secrets
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import boto3
import pytest
from botocore import UNSIGNED
from botocore.config import Config
from botocore.exceptions import ClientError

from asteroidea._auth import CACHE_NAME, Auth, Post, TokenCache
from asteroidea._config import PlatformConfig
from asteroidea_infra.admin import sdk_config, session_for, stack_outputs
from asteroidea_infra.sandbox_guard import require_sandbox

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping

EXIT_NO_SANDBOX: Final = 4
REQUIRED: Final = ("ASTEROIDEA_PROFILE", "ASTEROIDEA_ACCOUNT")

LIVE = os.environ.get("ASTEROIDEA_LIVE") == "1"
collect_ignore_glob = [] if LIVE else ["test_*.py"]

if LIVE:
    _missing = [name for name in REQUIRED if os.environ.get(name, "") == ""]
    if _missing:
        pytest.exit(
            f"refusing: {', '.join(_missing)} not set; put them in "
            "~/.config/asteroidea/sandbox.env",
            returncode=EXIT_NO_SANDBOX,
        )


def sibling(name: str) -> Any:
    """Load a sibling module by path (the suite runs with `--import-mode=importlib`)."""
    path = Path(__file__).with_name(f"{name}.py")
    spec = importlib.util.spec_from_file_location(f"_oa_{name}", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def load_sibling() -> Callable[[str], Any]:
    """Return `sibling`, so a test module loads a sibling lazily."""
    return sibling


def _test_user(
    admin_session: boto3.Session, pool: str, scratch: Path
) -> tuple[str, str]:
    """Return (email, password) of the live test researcher, creating it once."""
    admin = admin_session.client("cognito-idp")
    store = scratch / "live-user.json"
    if store.exists():
        saved = json.loads(store.read_text())
        return str(saved["email"]), str(saved["password"])
    email = f"sdk-{secrets.token_hex(3)}@example.invalid"
    password = "Aa1!" + secrets.token_urlsafe(18)
    fd = os.open(store, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(json.dumps({"email": email, "password": password}))
    admin.admin_create_user(
        UserPoolId=pool,
        Username=email,
        UserAttributes=[
            {"Name": "email", "Value": email},
            {"Name": "email_verified", "Value": "true"},
        ],
        MessageAction="SUPPRESS",
    )
    admin.admin_set_user_password(
        UserPoolId=pool, Username=email, Password=password, Permanent=True
    )
    admin.admin_add_user_to_group(
        UserPoolId=pool, Username=email, GroupName="researchers"
    )
    return email, password


def _refresh_via_initiate_auth(client_id: str, region: str) -> Post:
    """Return a token-endpoint stand-in for the `sandbox-test` client (no OAuth)."""
    idp = boto3.client(
        "cognito-idp", region_name=region, config=Config(signature_version=UNSIGNED)
    )

    def post(_url: str, form: Mapping[str, str]) -> dict[str, Any]:
        assert form["grant_type"] == "refresh_token"
        assert form["client_id"] == client_id
        result = idp.initiate_auth(
            ClientId=client_id,
            AuthFlow="REFRESH_TOKEN_AUTH",
            AuthParameters={"REFRESH_TOKEN": form["refresh_token"]},
        )["AuthenticationResult"]
        return {
            "id_token": result["IdToken"],
            "access_token": result["AccessToken"],
            "expires_in": result["ExpiresIn"],
        }

    return post


@pytest.fixture(scope="module")
def admin_session() -> boto3.Session:
    """Return a session on the sandbox profile."""
    profile, account = require_sandbox()
    return session_for(profile, account)


def _groupless_user(admin: boto3.Session, pool: str, scratch: Path) -> tuple[str, str]:
    """Return (email, password) of a user in no group, creating it once."""
    store = scratch / "live-groupless.json"
    if store.exists():
        saved = json.loads(store.read_text())
        return str(saved["email"]), str(saved["password"])
    email = f"groupless-{secrets.token_hex(3)}@example.invalid"
    password = "Aa1!" + secrets.token_urlsafe(18)
    fd = os.open(store, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(json.dumps({"email": email, "password": password}))
    idp = admin.client("cognito-idp")
    idp.admin_create_user(
        UserPoolId=pool,
        Username=email,
        UserAttributes=[
            {"Name": "email", "Value": email},
            {"Name": "email_verified", "Value": "true"},
        ],
        MessageAction="SUPPRESS",
    )
    idp.admin_set_user_password(
        UserPoolId=pool, Username=email, Password=password, Permanent=True
    )
    return email, password


def _password_sign_in(
    admin_session: boto3.Session,
    outputs: Mapping[str, str],
    credentials: tuple[str, str],
) -> Mapping[str, Any]:
    """Return the `AuthenticationResult` of `credentials` (`sandbox-test` client)."""
    email, password = credentials
    result: Mapping[str, Any] = admin_session.client("cognito-idp").admin_initiate_auth(
        UserPoolId=outputs["UserPoolId"],
        ClientId=outputs["SandboxTestClientId"],
        AuthFlow="ADMIN_USER_PASSWORD_AUTH",
        AuthParameters={"USERNAME": email, "PASSWORD": password},
    )["AuthenticationResult"]
    return result


def _sign_in(
    admin_session: boto3.Session,
    outputs: Mapping[str, str],
    credentials: tuple[str, str],
    base: Path,
) -> Auth:
    """Sign `credentials` in on the `sandbox-test` client and return a cached `Auth`.

    Not `acceptance.sandbox_sign_in`: that sets a new password (which would break the
    stored `live-user.json` one) and stamps expiry with `time.time()`, not the `Auth`
    clock.
    """
    client_id = outputs["SandboxTestClientId"]
    result = _password_sign_in(admin_session, outputs, credentials)
    platform = {**sdk_config(admin_session), "client_id": client_id}
    config = PlatformConfig(**{k: str(v) for k, v in platform.items()})
    signed_in = Auth(
        config,
        TokenCache(base / "asteroidea" / CACHE_NAME),
        post=_refresh_via_initiate_auth(client_id, config.region),
    )
    signed_in.cache.write(
        {
            "tokens": {
                "id_token": result["IdToken"],
                "access_token": result["AccessToken"],
                "refresh_token": result["RefreshToken"],
                "expires_at": signed_in.clock() + result["ExpiresIn"],
            }
        }
    )
    return signed_in


@pytest.fixture(scope="module")
def auth(
    admin_session: boto3.Session, tmp_path_factory: pytest.TempPathFactory
) -> Auth:
    """Sign a test researcher in without a browser (`sandbox-test` client)."""
    outputs = stack_outputs(admin_session)
    base = tmp_path_factory.mktemp("researcher")
    scratch = Path(os.environ.get("ASTEROIDEA_LIVE_SCRATCH") or base)
    scratch.mkdir(parents=True, exist_ok=True)
    user = _test_user(admin_session, outputs["UserPoolId"], scratch)
    return _sign_in(admin_session, outputs, user, base)


@pytest.fixture(scope="module")
def researcher_b(
    admin_session: boto3.Session, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[Auth]:
    """Sign in a second, new researcher (`sandbox-test`); delete it at the end."""
    outputs = stack_outputs(admin_session)
    base = tmp_path_factory.mktemp("researcher-b")
    user = _test_user(admin_session, outputs["UserPoolId"], base)
    yield _sign_in(admin_session, outputs, user, base)
    with contextlib.suppress(ClientError):
        admin_session.client("cognito-idp").admin_delete_user(
            UserPoolId=outputs["UserPoolId"], Username=user[0]
        )


@pytest.fixture
def groupless_tokens(
    admin_session: boto3.Session, tmp_path_factory: pytest.TempPathFactory
) -> Mapping[str, Any]:
    """Return the sign-in result of a user in no group (`sandbox-test` client)."""
    outputs = stack_outputs(admin_session)
    scratch = Path(
        os.environ.get("ASTEROIDEA_LIVE_SCRATCH")
        or tmp_path_factory.mktemp("groupless")
    )
    scratch.mkdir(parents=True, exist_ok=True)
    user = _groupless_user(admin_session, outputs["UserPoolId"], scratch)
    return _password_sign_in(admin_session, outputs, user)
