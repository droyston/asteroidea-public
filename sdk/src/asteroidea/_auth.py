"""Sign-in: PKCE authorization code by copy-paste, the token cache, and refresh.

The flow was proven live by `asteroidea_infra.auth_check`: print the
login address, the researcher signs in on any device and pastes the redirect address
(or the bare code), the SDK exchanges the code with the PKCE verifier.
"""

import base64
import hashlib
import http.client
import json
import os
import secrets
import sys
import time
import urllib.parse
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any, Final

from asteroidea._config import config_dir, load_config
from asteroidea._http import connect
from asteroidea.errors import (
    NotAResearcherError,
    PlatformUnreachableError,
    SignInFailedError,
    SignInRequiredError,
)

if TYPE_CHECKING:
    from pathlib import Path

    from asteroidea._config import PlatformConfig

GROUP: Final = "researchers"
SCOPES: Final = "openid email profile"
REFRESH_MARGIN: Final = 300.0  # refresh this many seconds before expiry
MAX_PASTE_TRIES: Final = 3
MAX_RESTARTS: Final = 3
TIMEOUT: Final = 30
CACHE_NAME: Final = "tokens.json"
CACHE_VERSION: Final = 1
FILE_MODE: Final = 0o600
DIR_MODE: Final = 0o700
_HTTP_OK: Final = 200
_HTTP_REFUSED: Final = (400, 401)

Clock = Callable[[], float]
Post = Callable[[str, Mapping[str, str]], dict[str, Any]]
Prompt = Callable[[str], str]
Echo = Callable[[str], None]


class GrantRefusedError(Exception):
    """The token endpoint refused a grant (an expired code, a revoked refresh token)."""

    def __init__(self, error: str) -> None:
        """Keep the endpoint's error code."""
        super().__init__(error)
        self.error = error


@dataclass(frozen=True)
class Tokens:
    """The three tokens and when the access and id tokens expire (epoch seconds)."""

    id_token: str = field(repr=False)
    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    expires_at: float


def _b64(raw: bytes) -> str:
    """Return `raw` as unpadded URL-safe base64 text."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def new_pkce() -> tuple[str, str]:
    """Return a PKCE (verifier, S256 challenge) pair."""
    verifier = _b64(secrets.token_bytes(48))
    return verifier, _b64(hashlib.sha256(verifier.encode()).digest())


def authorize_url(config: PlatformConfig, state: str, challenge: str) -> str:
    """Return the managed-login address for the PKCE S256 flow."""
    query = urllib.parse.urlencode(
        {
            "client_id": config.client_id,
            "response_type": "code",
            "scope": SCOPES,
            "redirect_uri": config.redirect_uri,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        },
        quote_via=urllib.parse.quote,
    )
    return f"{config.login_base_url.rstrip('/')}/oauth2/authorize?{query}"


def parse_paste(text: str) -> tuple[str | None, str | None]:
    """Return (code, state) from a pasted redirect address, query string or bare code.

    `code` is None when the paste holds no code. `state` is None for a bare code.
    """
    pasted = text.strip()
    if not pasted or any(ch.isspace() for ch in pasted):
        return None, None
    if not any(ch in pasted for ch in "=?/"):
        return pasted, None
    query = pasted.split("?", 1)[1] if "?" in pasted else pasted
    query = query.split("#", 1)[0]
    values = urllib.parse.parse_qs(query)
    code = values.get("code", [""])[0] or None
    state = values.get("state", [""])[0] or None
    return code, state


def claims(token: str) -> dict[str, Any]:
    """Return a token's payload. The endpoint issued it over TLS; no signature check."""
    payload = token.split(".")[1]
    decoded = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    return dict(decoded)


def post_form(url: str, form: Mapping[str, str]) -> dict[str, Any]:
    """POST a form to the token endpoint; return its JSON.

    Raises `GrantRefusedError` for a refused grant, else `PlatformUnreachableError`.
    """
    conn, target = connect(
        url,
        timeout=TIMEOUT,
        refusal=(
            "The sign-in address in platform.json is not https; "
            "ask your administrator for a new platform.json."
        ),
    )
    try:
        conn.request(
            "POST",
            target,
            body=urllib.parse.urlencode(form),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        response = conn.getresponse()
        body = response.read()
    except (OSError, http.client.HTTPException) as err:
        raise PlatformUnreachableError from err
    finally:
        conn.close()
    try:
        data = json.loads(body)
    except ValueError:
        data = None
    if response.status == _HTTP_OK and isinstance(data, dict):
        return dict(data)
    if response.status in _HTTP_REFUSED and isinstance(data, dict):
        raise GrantRefusedError(str(data.get("error", "invalid_request")))
    raise PlatformUnreachableError


class TokenCache:
    """The cache file: mode 0600, in a directory of mode 0700."""

    def __init__(self, path: Path) -> None:
        """Remember where the cache lives."""
        self.path = path

    def read(self) -> dict[str, Any]:
        """Return the cache content; an absent or damaged file reads as empty."""
        try:
            data = json.loads(self.path.read_text())
        except OSError, ValueError:
            return {}
        if not isinstance(data, dict) or data.get("version") != CACHE_VERSION:
            return {}
        return dict(data)

    def write(self, data: Mapping[str, Any]) -> None:
        """Replace the cache atomically, never wider than 0600 in a 0700 directory."""
        directory = self.path.parent
        directory.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
        directory.chmod(DIR_MODE)
        scratch = self.path.with_name(self.path.name + ".new")
        fd = os.open(scratch, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, FILE_MODE)
        try:
            os.fchmod(fd, FILE_MODE)
            with os.fdopen(fd, "w") as handle:
                handle.write(json.dumps({**data, "version": CACHE_VERSION}))
        except BaseException:
            scratch.unlink(missing_ok=True)
            raise
        scratch.replace(self.path)

    def clear(self) -> None:
        """Delete the cache."""
        self.path.unlink(missing_ok=True)


def _stdout(message: str) -> None:
    """Write `message` and a newline to standard output."""
    sys.stdout.write(message + "\n")


class Auth:
    """Tokens for one platform: cached, refreshed, or obtained by copy-paste sign-in."""

    def __init__(
        self,
        config: PlatformConfig,
        cache: TokenCache,
        *,
        post: Post = post_form,
        clock: Clock = time.time,
    ) -> None:
        """Bind the settings and the cache; `post` and `clock` are seams for tests."""
        self.config = config
        self.cache = cache
        self.post = post
        self.clock = clock

    @property
    def token_url(self) -> str:
        """The token endpoint."""
        return f"{self.config.login_base_url.rstrip('/')}/oauth2/token"

    def tokens(self) -> Tokens:
        """Return fresh tokens, refreshing 5 minutes before expiry; never prompts.

        Raises `SignInRequiredError` when there is no cache or no refresh token.
        """
        data = self.cache.read()
        saved = data.get("tokens")
        try:
            tokens = Tokens(**saved) if isinstance(saved, dict) else None
        except TypeError:
            tokens = None
        if tokens is None:
            raise SignInRequiredError
        if tokens.expires_at - self.clock() > REFRESH_MARGIN:
            return tokens
        try:
            reply = self.post(
                self.token_url,
                {
                    "grant_type": "refresh_token",
                    "client_id": self.config.client_id,
                    "refresh_token": tokens.refresh_token,
                },
            )
        except GrantRefusedError as err:
            self.cache.clear()
            raise SignInRequiredError from err
        refreshed = self._tokens_from(reply, fallback_refresh=tokens.refresh_token)
        self.cache.write({**data, "tokens": asdict(refreshed)})
        return refreshed

    def sign_in(self, *, prompt: Prompt = input, echo: Echo = _stdout) -> Tokens:
        """Return tokens from the cache, or sign in by copy-paste when there are none.

        Raises `SignInFailedError` after too many failed attempts, and
        `NotAResearcherError` when the account lacks the researchers group.
        """
        try:
            return self.tokens()
        except SignInRequiredError:
            pass
        for _ in range(MAX_RESTARTS):
            tokens = self._login_once(prompt, echo)
            if tokens is not None:
                self.cache.write({"tokens": asdict(tokens)})
                return tokens
        msg = "Sign-in did not finish after several tries; run sign_in() again."
        raise SignInFailedError(msg)

    def _login_once(self, prompt: Prompt, echo: Echo) -> Tokens | None:
        """Run one attempt; return None when the attempt must restart."""
        verifier, challenge = new_pkce()
        state = _b64(secrets.token_bytes(16))
        echo(
            "Open this address in any browser, sign in, then copy the address "
            "the browser ends on (or just the code in it):"
        )
        echo(authorize_url(self.config, state, challenge))
        code = self._ask_code(state, prompt, echo)
        if code is None:
            return None
        try:
            reply = self.post(
                self.token_url,
                {
                    "grant_type": "authorization_code",
                    "client_id": self.config.client_id,
                    "code": code,
                    "redirect_uri": self.config.redirect_uri,
                    "code_verifier": verifier,
                },
            )
        except GrantRefusedError as err:
            if err.error == "invalid_grant":
                echo("That code has expired or was already used; starting over.")
                return None
            msg = "The platform refused the sign-in; run sign_in() again."
            raise SignInFailedError(msg) from err
        return self._tokens_from(reply, fallback_refresh=None)

    def _ask_code(self, state: str, prompt: Prompt, echo: Echo) -> str | None:
        """Read a paste, up to 3 tries; None when the state differs (restart)."""
        for _ in range(MAX_PASTE_TRIES):
            code, pasted_state = parse_paste(
                prompt("Paste the address (or the code) here: ")
            )
            if code is None:
                echo(
                    "That paste has no code in it. Copy the whole address, or the code."
                )
                continue
            if pasted_state is not None and pasted_state != state:
                echo("That address came from a different sign-in; starting over.")
                return None
            return code
        msg = (
            "No code was pasted after 3 tries; run sign_in() again and paste "
            "the whole address the browser ends on."
        )
        raise SignInFailedError(msg)

    def _tokens_from(
        self, reply: Mapping[str, Any], *, fallback_refresh: str | None
    ) -> Tokens:
        """Build Tokens from a token reply, and refuse accounts outside the group."""
        try:
            id_token = str(reply["id_token"])
            refresh = str(reply.get("refresh_token") or fallback_refresh or "")
            tokens = Tokens(
                id_token=id_token,
                access_token=str(reply["access_token"]),
                refresh_token=refresh,
                expires_at=self.clock() + float(reply["expires_in"]),
            )
        except (KeyError, TypeError, ValueError) as err:
            raise PlatformUnreachableError from err
        if not refresh:
            raise PlatformUnreachableError
        if GROUP not in claims(id_token).get("cognito:groups", []):
            self.cache.clear()
            raise NotAResearcherError
        return tokens


def default_cache_path() -> Path:
    """Return the token cache file: `tokens.json` in the settings directory."""
    return config_dir() / CACHE_NAME


def default_auth(config_path: Path | str | None = None) -> Auth:
    """Return an `Auth` for the platform settings and the default cache."""
    return Auth(load_config(config_path), TokenCache(default_cache_path()))
