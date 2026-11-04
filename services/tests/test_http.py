import http.client
from typing import TYPE_CHECKING

import pytest

from asteroidea_services import http as transport
from asteroidea_services.http import (
    HTTPError,
    ReplayMissError,
    Request,
    Response,
    live_transport,
    recording,
    replaying,
    request_key,
    retrying,
)

if TYPE_CHECKING:
    from pathlib import Path

GET = Request("GET", "https://example.test/a")


class Script:
    """A transport that returns or raises the next scripted outcome."""

    def __init__(self, *outcomes: Response | Exception) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0

    def __call__(self, _request: Request) -> Response:
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def test_retry_5xx_then_ok_sleeps_1_then_2() -> None:
    sleeps: list[float] = []
    script = Script(Response(503, b""), Response(500, b""), Response(200, b"ok"))
    assert retrying(script, sleep=sleeps.append)(GET).body == b"ok"
    assert script.calls == 3
    assert sleeps == [1.0, 2.0]


def test_retry_gives_up_after_three_attempts() -> None:
    sleeps: list[float] = []
    script = Script(*[Response(500, b"")] * 3)
    with pytest.raises(HTTPError) as caught:
        retrying(script, sleep=sleeps.append)(GET)
    assert caught.value.attempts == 3
    assert script.calls == 3
    assert sleeps == [1.0, 2.0]
    assert caught.value.message == "HTTP 500 Internal Server Error"
    assert caught.value.url == GET.url


def test_retry_429_is_retried() -> None:
    script = Script(Response(429, b""), Response(200, b"ok"))
    assert retrying(script, sleep=lambda _s: None)(GET).status == 200


def test_connection_error_and_timeout_are_retried_then_named() -> None:
    script = Script(
        TimeoutError("slow"), ConnectionResetError("reset"), TimeoutError("slow")
    )
    with pytest.raises(HTTPError) as caught:
        retrying(script, sleep=lambda _s: None)(GET)
    assert caught.value.attempts == 3
    assert caught.value.error_type == "TimeoutError"
    assert "slow" in caught.value.message


def test_http_client_exception_is_retried() -> None:
    script = Script(http.client.RemoteDisconnected("gone"), Response(200, b""))
    assert retrying(script, sleep=lambda _s: None)(GET).status == 200


def test_404_fails_at_once() -> None:
    script = Script(Response(404, b""))
    with pytest.raises(HTTPError) as caught:
        retrying(script, sleep=lambda _s: None)(GET)
    assert caught.value.attempts == 1
    assert script.calls == 1


@pytest.mark.parametrize("status", [301, 302, 307])
def test_3xx_is_an_error_not_followed(status: int) -> None:
    script = Script(Response(status, b""))
    with pytest.raises(HTTPError) as caught:
        retrying(script, sleep=lambda _s: None)(GET)
    assert caught.value.attempts == 1
    assert f"HTTP {status}" in caught.value.message


def test_live_transport_refuses_plain_http() -> None:
    with pytest.raises(ValueError, match="https URL required"):
        live_transport()(Request("GET", "http://example.test/"))


def test_live_transport_sends_path_query_and_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    class Reply:
        status = 200

        def read(self) -> bytes:
            return b"body"

    class Connection:
        def __init__(self, host: str, _port: int | None, *, timeout: float) -> None:
            seen["host"], seen["timeout"] = host, timeout

        def request(
            self,
            method: str,
            target: str,
            *,
            body: bytes | None,
            headers: dict[str, str],
        ) -> None:
            seen["call"] = (method, target, body, headers["Content-Type"])

        def getresponse(self) -> Reply:
            return Reply()

        def close(self) -> None:
            seen["closed"] = True

    monkeypatch.setattr(http.client, "HTTPSConnection", Connection)
    request = Request(
        "POST",
        "https://example.test/x/y?a=1",
        (("Content-Type", "application/json"),),
        b"{}",
    )
    assert live_transport(timeout=30.0)(request) == Response(200, b"body")
    assert seen == {
        "host": "example.test",
        "timeout": 30.0,
        "call": ("POST", "/x/y?a=1", b"{}", "application/json"),
        "closed": True,
    }


def test_defaults_match_the_plan() -> None:
    assert transport.ATTEMPTS == 3
    assert transport.BACKOFF_SECONDS == (1.0, 2.0, 4.0)
    assert transport.TIMEOUT_SECONDS == 30.0


def test_record_then_replay_serves_in_order_then_repeats_last(tmp_path: Path) -> None:
    script = Script(
        Response(503, b"no"), Response(200, b'{"a":1}'), Response(200, b'{"a":1}')
    )
    record = recording(script, tmp_path)
    for _ in range(3):
        record(GET)
    replay = replaying(tmp_path)
    assert [replay(GET).status for _ in range(4)] == [503, 200, 200, 200]
    assert replay(GET).body == b'{"a":1}'


def test_replay_miss_names_the_request(tmp_path: Path) -> None:
    with pytest.raises(
        ReplayMissError, match=r"no recorded response for GET https://example.test/a"
    ):
        replaying(tmp_path)(GET)


def test_request_key_ignores_json_key_order_and_spacing() -> None:
    a = Request("POST", "https://example.test/g", (), b'{"a": 1, "b": 2}')
    b = Request("POST", "https://example.test/g", (), b'{"b":2,"a":1}')
    assert request_key(a) == request_key(b)
    assert request_key(a) != request_key(GET)
