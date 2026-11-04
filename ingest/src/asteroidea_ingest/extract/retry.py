"""In-handler retry of transient extraction errors.

A transient error is retried inside the same invocation, `RETRIES` times with the
`BACKOFF_SECONDS` delays, so the FIFO group never stalls and no operator step is
needed. A deterministic error (a malformed file, a missing field, a denied request)
raises at once and the handler records FAILED. Extraction is idempotent per table, so
a re-run skips the tables an earlier attempt finished.

The budget rule: the total backoff plus one extraction must fit under the function
timeout (`MEASURED_EXTRACT_SECONDS` is CS62 R2's cold first invoke, 47.1 s, measured
alongside the commit Lambda's cold start; a test asserts the sum). At run time a
retry is also refused when the time used so far plus the next delay plus the last
attempt's duration would pass `BUDGET_SECONDS`, so a slow file fails cleanly
instead of timing out.
"""

import time
from typing import TYPE_CHECKING, Final

import requests
from botocore import exceptions as botocore_exceptions
from botocore.exceptions import ClientError

from asteroidea_ingest import timing

if TYPE_CHECKING:
    from collections.abc import Callable

RETRIES: Final = 2
BACKOFF_SECONDS: Final = (2.0, 5.0)
MEASURED_EXTRACT_SECONDS: Final = 47.2
BUDGET_SECONDS: Final = float(timing.EXTRACT_TIMEOUT_SECONDS - 30)

# Every transient exception class, once, with its reason.
TRANSIENT: Final[dict[type[BaseException], str]] = {
    botocore_exceptions.EndpointConnectionError: "S3 or Glue endpoint unreachable",
    botocore_exceptions.ConnectionClosedError: "connection closed mid-response",
    botocore_exceptions.ConnectTimeoutError: "connect timed out",
    botocore_exceptions.ReadTimeoutError: "read timed out",
    botocore_exceptions.ResponseStreamingError: "response body cut off",
    requests.exceptions.ConnectionError: "remfile range read lost its connection",
    requests.exceptions.Timeout: "remfile range read timed out",
    requests.exceptions.ChunkedEncodingError: "range read body cut off",
}
# Throttling and server-side `ClientError` codes (S3, DynamoDB, Glue).
TRANSIENT_CODES: Final = {
    "SlowDown": "S3 throttling",
    "RequestTimeout": "S3 request timed out",
    "InternalError": "S3 internal error",
    "ServiceUnavailable": "service unavailable",
    "503": "service unavailable",
    "500": "internal error",
    "Throttling": "throttled",
    "ThrottlingException": "throttled",
    "RequestLimitExceeded": "throttled",
    "ProvisionedThroughputExceededException": "DynamoDB throttled",
    "TooManyRequestsException": "throttled",
}
# HTTP statuses of a presigned-URL range read that are the server's, not the file's.
TRANSIENT_HTTP: Final = frozenset({429, 500, 502, 503, 504})


def is_transient(exc: BaseException) -> bool:
    """Return whether `exc` is on the named transient list."""
    if isinstance(exc, ClientError):
        return str(exc.response.get("Error", {}).get("Code")) in TRANSIENT_CODES
    if isinstance(exc, requests.exceptions.HTTPError):
        status = exc.response.status_code if exc.response is not None else None
        return status in TRANSIENT_HTTP
    return isinstance(exc, tuple(TRANSIENT))


def run(
    work: Callable[[], None],
    *,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    budget: float = BUDGET_SECONDS,
) -> int:
    """Run `work`, retrying transient errors; return how many retries it took.

    A deterministic error, a transient one after `RETRIES` retries, and a transient one
    whose retry would pass `budget` all propagate unchanged.
    """
    started = clock()
    for retry in range(RETRIES + 1):
        attempt_started = clock()
        try:
            work()
        except Exception as exc:
            if retry == RETRIES or not is_transient(exc):
                raise
            delay = BACKOFF_SECONDS[retry]
            last = clock() - attempt_started
            if clock() - started + delay + last > budget:
                raise
            sleep(delay)
        else:
            return retry
    return RETRIES
