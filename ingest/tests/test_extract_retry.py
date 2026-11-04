"""Which extraction errors are retried in the invocation, and how long."""

from typing import TYPE_CHECKING, Final

import pytest
import requests
from botocore.exceptions import (
    ClientError,
    ConnectionClosedError,
    ReadTimeoutError,
)

from asteroidea_ingest import timing
from asteroidea_ingest.extract import retry

if TYPE_CHECKING:
    from collections.abc import Callable

DEADLINE: Final = 1000.0


class Clock:
    """A clock that only moves when `sleep` or `spend` says so."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "m"}}, "GetObject")


def _fails(times: int, error: Exception) -> tuple[list[int], Callable[[], None]]:
    calls: list[int] = []

    def work() -> None:
        calls.append(1)
        if len(calls) <= times:
            raise error

    return calls, work


def test_backoff_plus_the_measured_extraction_fits_under_the_function_timeout() -> None:
    assert len(retry.BACKOFF_SECONDS) == retry.RETRIES
    total = sum(retry.BACKOFF_SECONDS) + retry.MEASURED_EXTRACT_SECONDS
    assert total < timing.EXTRACT_TIMEOUT_SECONDS
    assert retry.BUDGET_SECONDS < timing.EXTRACT_TIMEOUT_SECONDS


def test_every_transient_class_and_code_has_a_reason() -> None:
    assert all(reason.strip() for reason in retry.TRANSIENT.values())
    assert all(reason.strip() for reason in retry.TRANSIENT_CODES.values())


@pytest.mark.parametrize("failures", [1, 2])
def test_a_transient_error_is_retried_with_backoff(failures: int) -> None:
    clock = Clock()
    calls, work = _fails(failures, ConnectionClosedError(endpoint_url="https://s3"))
    used = retry.run(work, sleep=clock.sleep, clock=clock)
    assert used == failures
    assert len(calls) == failures + 1
    assert clock.slept == list(retry.BACKOFF_SECONDS[:failures])


def test_a_transient_error_after_the_last_retry_propagates() -> None:
    clock = Clock()
    calls, work = _fails(9, ReadTimeoutError(endpoint_url="https://s3"))
    with pytest.raises(ReadTimeoutError):
        retry.run(work, sleep=clock.sleep, clock=clock)
    assert len(calls) == retry.RETRIES + 1
    assert clock.slept == list(retry.BACKOFF_SECONDS)


@pytest.mark.parametrize(
    "error",
    [ValueError("bad"), _client_error("AccessDenied"), _client_error("NoSuchKey")],
)
def test_a_deterministic_error_raises_at_once(error: Exception) -> None:
    clock = Clock()
    calls, work = _fails(9, error)
    with pytest.raises(type(error)):
        retry.run(work, sleep=clock.sleep, clock=clock)
    assert len(calls) == 1
    assert clock.slept == []


def test_a_retry_that_would_pass_the_budget_is_refused() -> None:
    clock = Clock()

    def slow() -> None:
        clock.now += 80.0
        raise ConnectionClosedError(endpoint_url="https://s3")

    with pytest.raises(ConnectionClosedError):
        retry.run(slow, sleep=clock.sleep, clock=clock, budget=retry.BUDGET_SECONDS)
    assert clock.slept == []


@pytest.mark.parametrize("code", ["SlowDown", "ThrottlingException", "503"])
def test_throttling_codes_are_transient(code: str) -> None:
    assert retry.is_transient(_client_error(code))


@pytest.mark.parametrize(
    ("status", "expected"), [(503, True), (429, True), (404, False), (403, False)]
)
def test_http_statuses_of_a_range_read(status: int, *, expected: bool) -> None:
    response = requests.Response()
    response.status_code = status
    assert (
        retry.is_transient(requests.exceptions.HTTPError(response=response)) is expected
    )


def test_a_404_is_not_transient() -> None:
    # Production: extraction starts after the commit copied the object, and S3 reads
    # are strongly consistent, so a missing object is a fault, not a delay.
    assert not retry.is_transient(_client_error("NoSuchKey"))
    assert not retry.is_transient(_client_error("404"))
