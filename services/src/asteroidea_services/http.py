"""Shared HTTP transport: one request, one response; retry, record, replay layers.

The live transport speaks https only through ``http.client`` and follows no redirect.
``retrying`` wraps any transport with the retry policy. ``recording`` and
``replaying`` save and
serve request/response pairs for tests and fixture generation.
"""

import hashlib
import http.client
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from http import HTTPStatus
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from pathlib import Path

TIMEOUT_SECONDS = 30.0
ATTEMPTS = 3
BACKOFF_SECONDS = (1.0, 2.0, 4.0)
_RETRY_STATUSES = frozenset({429, *range(500, 600)})
_OK_STATUS_LIMIT = 300
_PROJECT_URL = "https://github.com/droyston/asteroidea-public"
_USER_AGENT = f"asteroidea-services/0.0.0 (+{_PROJECT_URL})"


@dataclass(frozen=True)
class Request:
    """One HTTP request."""

    method: str
    url: str
    headers: tuple[tuple[str, str], ...] = ()
    body: bytes | None = None


@dataclass(frozen=True)
class Response:
    """One HTTP response."""

    status: int
    body: bytes


type Transport = Callable[[Request], Response]


class HTTPError(Exception):
    """A request that failed for good: URL, cause class and attempt count."""

    def __init__(
        self, url: str, message: str, attempts: int, cause_type: str | None = None
    ) -> None:
        """Keep the fields the failure report needs."""
        super().__init__(f"{message} ({url}, {attempts} attempt(s))")
        self.url = url
        self.message = message
        self.attempts = attempts
        self.error_type = cause_type or "HTTPError"


class ReplayMissError(HTTPError):
    """The replay directory holds no response for a request."""

    def __init__(self, url: str, message: str) -> None:
        """Report a miss as a failed request that was never attempted."""
        super().__init__(url, message, 0, "ReplayMissError")


def status_message(status: int) -> str:
    """Return ``HTTP 500 Internal Server Error`` for a status code."""
    try:
        return f"HTTP {status} {HTTPStatus(status).phrase}"
    except ValueError:
        return f"HTTP {status}"


def live_transport(timeout: float = TIMEOUT_SECONDS) -> Transport:
    """Return the single-attempt https transport (no redirects, one connection each)."""

    def send(request: Request) -> Response:
        """Send one HTTPS request and return the response.

        Raises `ValueError` for a URL that is not HTTPS.
        """
        parts = urlsplit(request.url)
        if parts.scheme != "https" or parts.hostname is None:
            msg = f"https URL required, got {request.url!r}"
            raise ValueError(msg)
        target = parts.path or "/"
        if parts.query:
            target = f"{target}?{parts.query}"
        headers = {
            "User-Agent": _USER_AGENT,
            "Accept": "application/json",
            **dict(request.headers),
        }
        connection = http.client.HTTPSConnection(
            parts.hostname, parts.port, timeout=timeout
        )
        try:
            connection.request(
                request.method, target, body=request.body, headers=headers
            )
            reply = connection.getresponse()
            return Response(reply.status, reply.read())
        finally:
            connection.close()

    return send


def retrying(
    inner: Transport,
    *,
    sleep: Callable[[float], None] = time.sleep,
    backoff: tuple[float, ...] = BACKOFF_SECONDS,
) -> Transport:
    """Wrap ``inner``: retry connection errors, timeouts, 429 and 5xx.

    The attempt count is ``len(backoff)``. A 3xx or other 4xx fails at once. Failure
    raises ``HTTPError``.
    """
    attempts = len(backoff)

    def send(request: Request) -> Response:
        """Send the request, trying up to `attempts` times on a transport error."""
        message = ""
        cause: str | None = None
        for attempt in range(1, attempts + 1):
            try:
                response = inner(request)
            except (OSError, http.client.HTTPException) as exc:
                message, cause = f"{type(exc).__name__}: {exc}", type(exc).__name__
            else:
                if HTTPStatus.OK <= response.status < _OK_STATUS_LIMIT:
                    return response
                message, cause = status_message(response.status), None
                if response.status not in _RETRY_STATUSES:
                    raise HTTPError(request.url, message, attempt, cause)
            if attempt < attempts:
                sleep(backoff[attempt - 1])
        raise HTTPError(request.url, message, attempts, cause)

    return send


def _canonical_body(body: bytes | None) -> str:
    """Return the body as canonical JSON text, else decoded text."""
    if body is None:
        return ""
    try:
        return json.dumps(json.loads(body), sort_keys=True, separators=(",", ":"))
    except ValueError:
        return body.decode("utf-8", errors="replace")


def request_key(request: Request) -> str:
    """Return the stable file stem for a request: method, URL and canonical body."""
    text = f"{request.method} {request.url} {_canonical_body(request.body)}"
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _encode_body(body: bytes | None) -> dict[str, object]:
    """Return the body as a `json` or `text` entry for a recorded file."""
    if body is None:
        return {}
    try:
        return {"json": json.loads(body)}
    except ValueError:
        return {"text": body.decode("utf-8", errors="replace")}


def _decode_body(entry: dict[str, object]) -> bytes:
    """Return the body bytes of a recorded entry."""
    if "json" in entry:
        return json.dumps(entry["json"], separators=(",", ":")).encode()
    text = entry.get("text", "")
    return str(text).encode()


def _read(path: Path) -> dict[str, object]:
    """Return the recorded file as a dict.

    Raises `TypeError` when it holds anything but a JSON object.
    """
    loaded: object = json.loads(path.read_text())
    if not isinstance(loaded, dict):
        msg = f"{path}: expected a JSON object"
        raise TypeError(msg)
    return {str(k): v for k, v in loaded.items()}


def _responses(saved: dict[str, object]) -> list[object]:
    """Return the `responses` list of a recorded file.

    Raises `TypeError` when the list is missing.
    """
    responses = saved.get("responses")
    if not isinstance(responses, list):
        msg = "recorded file without a responses array"
        raise TypeError(msg)
    return list(responses)


def recording(inner: Transport, directory: Path) -> Transport:
    """Wrap ``inner``: save each request/response pair under ``directory``."""
    directory.mkdir(parents=True, exist_ok=True)

    def send(request: Request) -> Response:
        """Send the request and append the response to its recorded file."""
        response = inner(request)
        path = directory / f"{request_key(request)}.json"
        responses = _responses(_read(path)) if path.exists() else []
        responses.append({"status": response.status, **_encode_body(response.body)})
        saved = {
            "method": request.method,
            "url": request.url,
            "request": _encode_body(request.body),
            "responses": responses,
        }
        path.write_text(json.dumps(saved, indent=1) + "\n")
        return response

    return send


def replaying(directory: Path) -> Transport:
    """Serve recorded responses: n-th call gets the n-th response, then the last."""
    served: dict[str, int] = {}

    def send(request: Request) -> Response:
        """Return the next recorded response for the request.

        Raises `ReplayMissError` when nothing was recorded for it.
        """
        key = request_key(request)
        path = directory / f"{key}.json"
        if not path.exists():
            msg = f"no recorded response for {request.method} {request.url} (key {key})"
            raise ReplayMissError(request.url, msg)
        responses = _responses(_read(path))
        index = min(served.get(key, 0), len(responses) - 1)
        served[key] = served.get(key, 0) + 1
        entry = responses[index]
        if not isinstance(entry, dict) or not isinstance(entry.get("status"), int):
            msg = f"{path}: response {index} needs an integer status"
            raise TypeError(msg)
        fields = {str(k): v for k, v in entry.items()}
        return Response(int(str(fields["status"])), _decode_body(fields))

    return send
