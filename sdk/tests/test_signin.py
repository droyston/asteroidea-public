"""Sign-in, token cache, refresh and identity-pool credentials, endpoints stubbed.

The OAuth token endpoint is a recording stub; the identity pool is a botocore Stubber.
"""

import base64
import datetime as dt
import hashlib
import json
import stat
import time
import urllib.parse
from dataclasses import asdict
from typing import TYPE_CHECKING, Any, NoReturn

import boto3
import pytest
from botocore import UNSIGNED
from botocore.config import Config
from botocore.exceptions import EndpointConnectionError
from botocore.stub import Stubber

import asteroidea
from asteroidea._auth import (
    Auth,
    GrantRefusedError,
    TokenCache,
    Tokens,
    authorize_url,
    claims,
    new_pkce,
    parse_paste,
)
from asteroidea._config import PlatformConfig
from asteroidea._creds import credentials, session
from asteroidea.errors import (
    CredentialsUnavailableError,
    NotAResearcherError,
    PlatformUnreachableError,
    SignInFailedError,
    SignInRequiredError,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping
    from pathlib import Path

LOGIN = "https://login.example.test"
HOUR = 3600.0
FIVE_MINUTES = 300.0  # literal on purpose: the tests must not read the SDK's constant
PROVIDER = "cognito-idp.us-east-1.amazonaws.com/us-east-1_pool"


def make_jwt(payload: Mapping[str, Any]) -> str:
    def part(obj: Mapping[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    return f"{part({'alg': 'none'})}.{part(payload)}.sig"


class Clock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class Endpoint:
    """A stub OAuth token endpoint that records each call."""

    def __init__(self) -> None:
        self.groups: list[str] = ["researchers"]
        self.calls: list[dict[str, str]] = []
        self.refused: dict[str, str] = {}  # code or refresh token -> error code
        self.counter = 0

    def reply(self) -> dict[str, Any]:
        self.counter += 1
        return {
            "id_token": make_jwt({"cognito:groups": self.groups, "n": self.counter}),
            "access_token": f"access-{self.counter}",
            "refresh_token": f"refresh-{self.counter}",
            "expires_in": HOUR,
        }

    def __call__(self, url: str, form: Mapping[str, str]) -> dict[str, Any]:
        assert url == f"{LOGIN}/oauth2/token"
        self.calls.append(dict(form))
        key = form.get("code") or form.get("refresh_token") or ""
        if key in self.refused:
            raise GrantRefusedError(self.refused[key])
        reply = self.reply()
        if form["grant_type"] == "refresh_token":
            del reply["refresh_token"]  # the endpoint keeps the old refresh token
        return reply


@pytest.fixture
def config() -> PlatformConfig:
    return PlatformConfig(
        region="us-east-1",
        user_pool_id="us-east-1_pool",
        client_id="client-sdk",
        login_base_url=LOGIN,
        redirect_uri="http://localhost:8765/callback",
        identity_pool_id="us-east-1:identity-pool",
        api_url="https://api.example.test",
        raw_bucket="raw-bucket",
    )


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def endpoint() -> Endpoint:
    return Endpoint()


@pytest.fixture
def cache_path(tmp_path: Path) -> Path:
    return tmp_path / "config" / "tokens.json"


@pytest.fixture
def make_auth(
    config: PlatformConfig, cache_path: Path, clock: Clock, endpoint: Endpoint
) -> Callable[[], Auth]:
    def build() -> Auth:
        return Auth(config, TokenCache(cache_path), post=endpoint, clock=clock)

    return build


@pytest.fixture
def auth(make_auth: Callable[[], Auth]) -> Auth:
    return make_auth()


class Browser:
    """Plays the researcher: reads each printed login address, returns a paste."""

    def __init__(self, pastes: list[str | Callable[[dict[str, str]], str]]) -> None:
        self.pastes: Iterator[str | Callable[[dict[str, str]], str]] = iter(pastes)
        self.printed: list[str] = []

    def echo(self, message: str) -> None:
        self.printed.append(message)

    @property
    def urls(self) -> list[str]:
        return [m for m in self.printed if "/oauth2/authorize" in m]

    def query(self, index: int = -1) -> dict[str, str]:
        parts = urllib.parse.urlsplit(self.urls[index])
        return {k: v[0] for k, v in urllib.parse.parse_qs(parts.query).items()}

    def prompt(self, _text: str) -> str:
        paste = next(self.pastes)
        return paste if isinstance(paste, str) else paste(self.query())


def redirect(code: str, state: str | None = None) -> Callable[[dict[str, str]], str]:
    def build(query: dict[str, str]) -> str:
        chosen = query["state"] if state is None else state
        return f"http://localhost:8765/callback?code={code}&state={chosen}"

    return build


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def sign_in_once(auth: Auth, code: str = "c1") -> None:
    browser = Browser([redirect(code)])
    auth.sign_in(prompt=browser.prompt, echo=browser.echo)


# PKCE and the login address


def test_pkce_pair_is_s256_of_the_verifier() -> None:
    verifier, challenge = new_pkce()
    digest = hashlib.sha256(verifier.encode()).digest()
    assert challenge == base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    assert 43 <= len(verifier) <= 128
    assert new_pkce()[0] != verifier


def test_authorize_url_carries_pkce_state_and_redirect(config: PlatformConfig) -> None:
    parts = urllib.parse.urlsplit(authorize_url(config, "st", "chal"))
    query = {k: v[0] for k, v in urllib.parse.parse_qs(parts.query).items()}
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == f"{LOGIN}/oauth2/authorize"
    assert query["code_challenge_method"] == "S256"
    assert query["code_challenge"] == "chal"
    assert query["state"] == "st"
    assert query["redirect_uri"] == config.redirect_uri
    assert query["response_type"] == "code"
    assert query["client_id"] == config.client_id


def test_sign_in_sends_the_verifier_matching_the_printed_challenge(
    auth: Auth, endpoint: Endpoint
) -> None:
    browser = Browser([redirect("c1")])
    auth.sign_in(prompt=browser.prompt, echo=browser.echo)
    sent = endpoint.calls[-1]
    digest = hashlib.sha256(sent["code_verifier"].encode()).digest()
    assert (
        base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
        == browser.query()["code_challenge"]
    )
    assert sent["grant_type"] == "authorization_code"
    assert sent["redirect_uri"] == "http://localhost:8765/callback"


# The paste


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("abc-123", ("abc-123", None)),
        ("  abc-123\n", ("abc-123", None)),
        ("http://localhost:8765/callback?code=abc&state=s1", ("abc", "s1")),
        ("code=abc&state=s1", ("abc", "s1")),
        ("http://localhost:8765/callback?state=s1", (None, "s1")),
        ("http://localhost:8765/callback", (None, None)),
        ("", (None, None)),
        ("two words", (None, None)),
    ],
)
def test_parse_paste(text: str, expected: tuple[str | None, str | None]) -> None:
    assert parse_paste(text) == expected


def test_bare_code_and_full_url_both_sign_in(
    make_auth: Callable[[], Auth], endpoint: Endpoint, cache_path: Path
) -> None:
    for paste in ("bare-code", redirect("url-code")):
        cache_path.unlink(missing_ok=True)
        browser = Browser([paste])
        make_auth().sign_in(prompt=browser.prompt, echo=browser.echo)
    assert [c["code"] for c in endpoint.calls] == ["bare-code", "url-code"]


def test_state_mismatch_restarts_with_a_new_login_address(auth: Auth) -> None:
    browser = Browser([redirect("evil", state="wrong"), redirect("good")])
    tokens = auth.sign_in(prompt=browser.prompt, echo=browser.echo)
    assert len(browser.urls) == 2
    assert browser.query(0)["state"] != browser.query(1)["state"]
    assert browser.query(0)["code_challenge"] != browser.query(1)["code_challenge"]
    assert tokens.refresh_token


def test_state_mismatch_never_sends_the_code(auth: Auth, endpoint: Endpoint) -> None:
    browser = Browser([redirect("evil", state="wrong"), redirect("good")])
    auth.sign_in(prompt=browser.prompt, echo=browser.echo)
    assert [c["code"] for c in endpoint.calls] == ["good"]


def test_paste_without_a_code_retries_up_to_three_times(auth: Auth) -> None:
    browser = Browser(["", "http://localhost:8765/callback?x=1", redirect("c1")])
    auth.sign_in(prompt=browser.prompt, echo=browser.echo)
    assert len(browser.urls) == 1


def test_three_pastes_without_a_code_fail_with_a_next_step(auth: Auth) -> None:
    browser = Browser(["", "nope nope", "?x=1", redirect("c1")])
    with pytest.raises(SignInFailedError, match=r"run sign_in\(\) again"):
        auth.sign_in(prompt=browser.prompt, echo=browser.echo)


def test_expired_code_restarts(auth: Auth, endpoint: Endpoint) -> None:
    endpoint.refused["old"] = "invalid_grant"
    browser = Browser([redirect("old"), redirect("fresh")])
    auth.sign_in(prompt=browser.prompt, echo=browser.echo)
    assert len(browser.urls) == 2
    assert [c["code"] for c in endpoint.calls] == ["old", "fresh"]


def test_restarts_are_bounded(auth: Auth, endpoint: Endpoint) -> None:
    endpoint.refused["old"] = "invalid_grant"
    browser = Browser([redirect("old")] * 5)
    with pytest.raises(SignInFailedError, match=r"run sign_in\(\) again"):
        auth.sign_in(prompt=browser.prompt, echo=browser.echo)
    assert len(browser.urls) == 3


def test_any_other_refusal_of_the_code_is_a_failure_not_a_loop(
    auth: Auth, endpoint: Endpoint
) -> None:
    endpoint.refused["bad"] = "invalid_client"
    browser = Browser([redirect("bad")])
    with pytest.raises(SignInFailedError):
        auth.sign_in(prompt=browser.prompt, echo=browser.echo)
    assert len(browser.urls) == 1


# Groups


def test_not_a_researcher_is_refused_and_nothing_is_cached(
    auth: Auth, endpoint: Endpoint, cache_path: Path
) -> None:
    endpoint.groups = ["other"]
    with pytest.raises(NotAResearcherError, match="administrator"):
        sign_in_once(auth)
    assert not cache_path.exists()


def test_user_without_any_group_is_not_a_researcher(
    auth: Auth, endpoint: Endpoint
) -> None:
    endpoint.groups = []
    with pytest.raises(NotAResearcherError):
        sign_in_once(auth)


def test_a_refreshed_account_that_left_the_group_is_refused(
    auth: Auth, clock: Clock, endpoint: Endpoint
) -> None:
    sign_in_once(auth)
    endpoint.groups = []
    clock.now += HOUR
    with pytest.raises(NotAResearcherError):
        auth.tokens()


# The cache


def test_cache_modes_after_sign_in(auth: Auth, cache_path: Path) -> None:
    sign_in_once(auth)
    assert mode(cache_path) == 0o600
    assert mode(cache_path.parent) == 0o700
    assert not list(cache_path.parent.glob("*.new"))


def test_cache_modes_are_tightened_when_wider(
    auth: Auth, cache_path: Path, clock: Clock
) -> None:
    sign_in_once(auth)
    cache_path.chmod(0o644)
    cache_path.parent.chmod(0o755)
    clock.now += HOUR  # forces a refresh, which rewrites the cache
    auth.tokens()
    assert mode(cache_path) == 0o600
    assert mode(cache_path.parent) == 0o700


def test_valid_cache_prints_no_address_and_calls_nothing(
    make_auth: Callable[[], Auth], endpoint: Endpoint
) -> None:
    sign_in_once(make_auth())
    calls = len(endpoint.calls)
    second = Browser([])
    make_auth().sign_in(prompt=second.prompt, echo=second.echo)
    assert second.printed == []
    assert len(endpoint.calls) == calls


def test_no_cache_means_sign_in_required(auth: Auth) -> None:
    with pytest.raises(SignInRequiredError, match=r"run sign_in\(\)"):
        auth.tokens()


def test_damaged_cache_means_sign_in_required(auth: Auth, cache_path: Path) -> None:
    cache_path.parent.mkdir(parents=True)
    cache_path.write_text("{not json")
    with pytest.raises(SignInRequiredError):
        auth.tokens()


# Refresh


def test_no_refresh_while_more_than_five_minutes_remain(
    auth: Auth, clock: Clock, endpoint: Endpoint
) -> None:
    sign_in_once(auth)
    calls = len(endpoint.calls)
    clock.now += HOUR - FIVE_MINUTES - 1
    auth.tokens()
    assert len(endpoint.calls) == calls


def test_refresh_happens_five_minutes_before_expiry(
    auth: Auth, clock: Clock, endpoint: Endpoint
) -> None:
    sign_in_once(auth)
    old = auth.tokens()
    clock.now += HOUR - FIVE_MINUTES + 1
    new = auth.tokens()
    assert endpoint.calls[-1]["grant_type"] == "refresh_token"
    assert endpoint.calls[-1]["refresh_token"] == old.refresh_token
    assert new.access_token != old.access_token
    assert new.refresh_token == old.refresh_token
    assert new.expires_at > clock.now + FIVE_MINUTES


def test_refresh_keeps_the_saved_credentials(
    auth: Auth, clock: Clock, cache_path: Path
) -> None:
    sign_in_once(auth)
    saved = json.loads(cache_path.read_text())
    saved["credentials"] = {"marker": 1}
    cache_path.write_text(json.dumps(saved))
    clock.now += HOUR
    auth.tokens()
    assert json.loads(cache_path.read_text())["credentials"] == {"marker": 1}


def test_gone_refresh_token_means_sign_in_required_and_clears_the_cache(
    auth: Auth, clock: Clock, endpoint: Endpoint, cache_path: Path
) -> None:
    sign_in_once(auth)
    endpoint.refused[auth.tokens().refresh_token] = "invalid_grant"
    clock.now += HOUR
    with pytest.raises(SignInRequiredError):
        auth.tokens()
    assert not cache_path.exists()


def test_sign_in_after_a_gone_refresh_token_prompts_again(
    auth: Auth, clock: Clock, endpoint: Endpoint
) -> None:
    sign_in_once(auth)
    endpoint.refused[auth.tokens().refresh_token] = "invalid_grant"
    clock.now += HOUR
    browser = Browser([redirect("c2")])
    auth.sign_in(prompt=browser.prompt, echo=browser.echo)
    assert len(browser.urls) == 1


def test_claims_round_trip() -> None:
    assert claims(make_jwt({"a": [1]})) == {"a": [1]}


# Identity-pool credentials


def identity() -> tuple[Any, Stubber]:
    client = boto3.client(
        "cognito-identity",
        region_name="us-east-1",
        config=Config(signature_version=UNSIGNED),
    )
    return client, Stubber(client)


def expect(stub: Stubber, token: str, key: str, expires: float) -> None:
    logins = {PROVIDER: token}
    stub.add_response(
        "get_id",
        {"IdentityId": "us-east-1:ident"},
        {"IdentityPoolId": "us-east-1:identity-pool", "Logins": logins},
    )
    stub.add_response(
        "get_credentials_for_identity",
        {
            "IdentityId": "us-east-1:ident",
            "Credentials": {
                "AccessKeyId": key,
                "SecretKey": f"secret-{key}",
                "SessionToken": f"token-{key}",
                "Expiration": dt.datetime.fromtimestamp(expires, dt.UTC),
            },
        },
        {"IdentityId": "us-east-1:ident", "Logins": logins},
    )


@pytest.fixture
def signed_in(auth: Auth, clock: Clock) -> Tokens:
    token = make_jwt({"cognito:groups": ["researchers"]})
    tokens = Tokens(token, "access", "refresh", clock.now + HOUR)
    auth.cache.write(
        {
            "tokens": {
                "id_token": tokens.id_token,
                "access_token": tokens.access_token,
                "refresh_token": tokens.refresh_token,
                "expires_at": tokens.expires_at,
            }
        }
    )
    return tokens


def test_exchanges_the_id_token_and_caches_the_credentials(
    auth: Auth, clock: Clock, signed_in: Tokens, cache_path: Path
) -> None:
    client, stub = identity()
    expect(stub, signed_in.id_token, "AKIA1", clock.now + HOUR)
    with stub:
        got = credentials(auth, client)
    assert got.access_key_id == "AKIA1"
    assert (got.secret_key, got.session_token) == ("secret-AKIA1", "token-AKIA1")
    assert "secret" not in repr(got)
    assert mode(cache_path) == 0o600
    assert auth.cache.read()["tokens"]["refresh_token"] == signed_in.refresh_token


def test_fresh_cached_credentials_make_no_exchange(
    auth: Auth, clock: Clock, signed_in: Tokens
) -> None:
    client, stub = identity()
    expect(stub, signed_in.id_token, "AKIA1", clock.now + HOUR)
    with stub:
        first = credentials(auth, client)
        stub.assert_no_pending_responses()
        assert credentials(auth, client) == first


def test_credentials_are_renewed_five_minutes_before_expiry(
    auth: Auth, clock: Clock, signed_in: Tokens
) -> None:
    client, stub = identity()
    expect(stub, signed_in.id_token, "AKIA1", clock.now + HOUR)
    with stub:
        credentials(auth, client)
    clock.now += HOUR - FIVE_MINUTES - 1
    with stub:  # nothing queued: an exchange here would raise
        assert credentials(auth, client).access_key_id == "AKIA1"
    clock.now += 2
    expect(stub, auth.tokens().id_token, "AKIA2", clock.now + HOUR)
    with stub:
        assert credentials(auth, client).access_key_id == "AKIA2"


@pytest.mark.usefixtures("signed_in")
def test_the_exchange_carries_the_refreshed_id_token(
    auth: Auth, clock: Clock, endpoint: Endpoint
) -> None:
    clock.now += 2 * HOUR
    client, stub = identity()
    # The stub endpoint numbers its tokens: the first refresh issues n=1.
    refreshed = make_jwt({"cognito:groups": ["researchers"], "n": 1})
    expect(stub, refreshed, "AKIA3", clock.now + HOUR)
    with stub:
        assert credentials(auth, client).access_key_id == "AKIA3"
    assert endpoint.calls[-1]["grant_type"] == "refresh_token"


def test_no_sign_in_means_sign_in_required(auth: Auth) -> None:
    client, stub = identity()
    with stub, pytest.raises(SignInRequiredError):
        credentials(auth, client)


@pytest.mark.usefixtures("signed_in")
def test_a_refused_token_asks_for_sign_in(auth: Auth) -> None:
    client, stub = identity()
    stub.add_client_error("get_id", service_error_code="NotAuthorizedException")
    with stub, pytest.raises(SignInRequiredError) as caught:
        credentials(auth, client)
    assert caught.value.__cause__ is not None


@pytest.mark.usefixtures("signed_in")
def test_other_refusals_keep_the_original_as_the_cause(auth: Auth) -> None:
    client, stub = identity()
    stub.add_client_error("get_id", service_error_code="InternalErrorException")
    with stub, pytest.raises(CredentialsUnavailableError) as caught:
        credentials(auth, client)
    assert "InternalErrorException" not in str(caught.value)
    assert "InternalErrorException" in str(caught.value.__cause__)


@pytest.mark.usefixtures("signed_in")
def test_network_failure_is_platform_unreachable(auth: Auth) -> None:
    class Down:
        def get_id(self, **_kwargs: str | Mapping[str, str]) -> NoReturn:
            raise EndpointConnectionError(endpoint_url="https://x.invalid")

        def get_credentials_for_identity(
            self, **_kwargs: str | Mapping[str, str]
        ) -> NoReturn:
            raise AssertionError

    with pytest.raises(PlatformUnreachableError) as caught:
        credentials(auth, Down())
    assert isinstance(caught.value.__cause__, EndpointConnectionError)


def test_session_carries_the_credentials(
    make_auth: Callable[[], Auth], clock: Clock, signed_in: Tokens
) -> None:
    client, stub = identity()
    expect(stub, signed_in.id_token, "AKIA1", clock.now + HOUR)
    with stub:
        held = session(make_auth(), client).get_credentials()
    assert held is not None
    assert (held.access_key, held.token) == ("AKIA1", "token-AKIA1")


def test_public_sign_in_with_a_valid_cache_prints_nothing_and_calls_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    config: PlatformConfig,
) -> None:
    (tmp_path / "platform.json").write_text(json.dumps(asdict(config)))
    monkeypatch.setenv("ASTEROIDEA_CONFIG_DIR", str(tmp_path))
    later = time.time() + HOUR
    TokenCache(tmp_path / "tokens.json").write(
        {
            "tokens": {
                "id_token": make_jwt({"cognito:groups": ["researchers"]}),
                "access_token": "a",
                "refresh_token": "r",
                "expires_at": later,
            },
            "credentials": {
                "access_key_id": "AKIA1",
                "secret_key": "s",
                "session_token": "t",
                "expires_at": later,
            },
        }
    )
    asteroidea.sign_in()  # no stub is installed: any network call would fail
    shown = capsys.readouterr()
    assert shown.out + shown.err == ""
