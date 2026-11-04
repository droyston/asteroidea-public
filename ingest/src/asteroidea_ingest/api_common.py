"""The shared HTTP API handler kit: caller, request, refusal and one timing line.

Standard library only, so the zip Lambdas can import it. Each handler module exports
`DISPATCH`, a map from an HTTP API routeKey to a method name on its Api class; the
class's `.handle` calls `serve`, which finds the `routes.ROUTES` row (404), checks the
row's group (403), calls the method with a `Request` and a `Timing`, and turns an
`ApiError` into its response. Any other exception is logged as one `unhandled` JSON
line and answered with a 500 that names the request id.

Every refusal body is exactly `{"error": <code>, "detail": <sentence>}`: the
code is short and stable for a client to branch on, the sentence is for a person.

The caller comes from the JWT claims the HTTP API authorizer passed on. Nothing here
refuses a token for a missing `client_id` or a `token_use` other than `access`: the
SDK sends a Cognito ID token on `POST /artifacts`, and the authorizer has
already checked `aud` or `client_id` against its audience.
"""

import base64
import binascii
import json
import logging
import sys
import time
import traceback
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Final, override

from asteroidea_ingest import timing_log
from asteroidea_ingest.routes import ROUTES, Route

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Mapping

__all__ = [
    "CODES",
    "ApiError",
    "Invocation",
    "Request",
    "Response",
    "Timing",
    "claim_groups",
    "error_response",
    "internal_detail",
    "json_response",
    "parse_json",
    "serve",
    "timing_name",
]

# HTTP API payload v2 proxy response.
Response = dict[str, Any]

CODES: Final = {
    400: "invalid_request",
    403: "forbidden",
    404: "not_found",
    409: "conflict",
    410: "gone",
    413: "too_large",
    422: "unprocessable",
    500: "internal_error",
    502: "upstream_failed",
    503: "unavailable",
}
NO_ROUTE: Final = "no such route"
ROUTES_BY_KEY: Final = {r.key: r for r in ROUTES}
INTERNAL: Final = 500


class ApiError(Exception):
    """A refusal the API answers with `status`, a short `code` and a `detail` sentence.

    `code` defaults to CODES[status].
    """

    def __init__(self, status: int, detail: str, *, code: str | None = None) -> None:
        """Keep the status, the detail sentence and the code."""
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.code = code if code is not None else CODES.get(status, "error")


def json_response(status: int, body: Mapping[str, object]) -> Response:
    """Return the proxy response for a JSON `body`."""
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(body),
    }


def error_response(err: ApiError) -> Response:
    """Return the refusal body `{"error": code, "detail": detail}`."""
    return json_response(err.status, {"error": err.code, "detail": err.detail})


def internal_detail(request_id: str) -> str:
    """Return the 500 sentence that names `request_id`."""
    return f"The platform failed on this request. Report request id {request_id}."


class _JsonLines(logging.Handler):
    """Write a record's `line` mapping and its traceback as one JSON line on stdout.

    It reads `sys.stdout` at each write, so a test's captured stdout sees the line.
    """

    @override
    def emit(self, record: logging.LogRecord) -> None:
        """Write the record's JSON line and any traceback to stdout."""
        line: dict[str, object] = dict(getattr(record, "line", {}))
        exc_info = record.exc_info
        if isinstance(exc_info, tuple) and exc_info[1] is not None:
            line["traceback"] = "".join(traceback.format_exception(exc_info[1]))
        sys.stdout.write(json.dumps(line, separators=(",", ":")) + "\n")
        sys.stdout.flush()


# One JSON line per unhandled exception. It does not propagate, so the Lambda
# runtime's own log handler does not write it a second time.
logger = logging.getLogger("asteroidea_ingest.unhandled")
logger.addHandler(_JsonLines())
logger.propagate = False


def claim_groups(claims: Mapping[str, Any]) -> frozenset[str]:
    """Return the `cognito:groups` claim; HTTP API v2 renders arrays as "[a b]"."""
    raw = claims.get("cognito:groups", "")
    if isinstance(raw, list):
        return frozenset(str(g) for g in raw)
    return frozenset(str(raw).strip("[]").replace(",", " ").split())


def parse_json(raw_body: str | None) -> dict[str, Any]:
    """Return the decoded request object of an upload route, else raise its 400.

    The session and artifact routes' wording; `Request.json_body` is the imports and
    labels wording; the two are not yet one.
    """
    try:
        body = json.loads(raw_body or "")
    except json.JSONDecodeError:
        raise ApiError(400, "body must be JSON") from None
    if not isinstance(body, dict):
        raise ApiError(400, "body must be a JSON object")
    return {str(k): v for k, v in body.items()}


@dataclass(frozen=True)
class Request:
    """One HTTP API request after the route and group checks."""

    # The routes.ROUTES row for event["routeKey"].
    route: Route
    sub: str
    # The token's `email` claim; None when absent (an access token has none).
    email: str | None
    # cognito:groups; the "[a b]" rendering is handled.
    groups: frozenset[str]
    path_params: Mapping[str, str]
    query: Mapping[str, str]
    # requestContext.domainName (get-started); "" when absent.
    domain: str
    raw_body: str | None
    is_base64: bool
    # Seconds since the epoch at the start of the request (tests inject it).
    now: int
    # Lambda context.aws_request_id, else "local".
    request_id: str
    # Lambda context.get_remaining_time_in_millis(), for compute's deadline.
    remaining_ms: int | None

    def json_body(self) -> dict[str, Any]:
        """Return the decoded object (base64 aware); ApiError(400) otherwise."""
        raw = self.raw_body or ""
        if self.is_base64:
            try:
                raw = base64.b64decode(raw, validate=True).decode()
            except (binascii.Error, UnicodeDecodeError) as err:
                raise ApiError(400, "the body is not valid base64 text") from err
        try:
            body = json.loads(raw)
        except json.JSONDecodeError as err:
            raise ApiError(400, "the body must be a JSON object") from err
        if not isinstance(body, dict):
            raise ApiError(400, "the body must be a JSON object")
        return body

    def path(self, name: str) -> str:
        """Return a path parameter; ApiError(400) when absent or empty."""
        value = self.path_params.get(name, "")
        if not value:
            raise ApiError(400, f"{name} is required")
        return value


@dataclass
class Timing:
    """Phases the handler records; `phase(name)` adds "<name>_ms"."""

    asset_id: str | None = None
    cache: str | None = None
    phases: dict[str, int] = field(default_factory=dict)

    @contextmanager
    def phase(self, name: str) -> Generator[None]:
        """Time the block in whole milliseconds as `<name>_ms`."""
        started = time.perf_counter()
        try:
            yield
        finally:
            self.phases[f"{name}_ms"] = _ms_since(started)


def _ms_since(started: float) -> int:
    return round((time.perf_counter() - started) * 1000)


def timing_name(route: Route) -> str:
    """Return the timing line's name: the row's `timing`, else its routeKey."""
    return route.timing or route.key


@dataclass(frozen=True)
class Invocation:
    """What the Lambda runtime and the caller's clock give one request."""

    # Lambda context.aws_request_id, else "local".
    request_id: str = "local"
    # Lambda context.get_remaining_time_in_millis(), else None.
    remaining_ms: int | None = None
    # Seconds since the epoch; None reads the clock when the request starts.
    now: int | None = None

    def at(self, now: int | None) -> Invocation:
        """Return this invocation with the clock fixed at `now` (None keeps it)."""
        return self if now is None else replace(self, now=now)

    @classmethod
    def of(cls, context: object) -> Invocation:
        """Return the request id and remaining time of a Lambda `context`."""
        remaining = getattr(context, "get_remaining_time_in_millis", None)
        return cls(
            request_id=str(getattr(context, "aws_request_id", "local")),
            remaining_ms=int(remaining()) if callable(remaining) else None,
        )


def _request(event: Mapping[str, Any], route: Route, invocation: Invocation) -> Request:
    context = event["requestContext"]
    claims = context["authorizer"]["jwt"]["claims"]
    email = claims.get("email")
    now = invocation.now
    return Request(
        route=route,
        sub=str(claims["sub"]),
        email=str(email) if email is not None else None,
        groups=claim_groups(claims),
        path_params=dict(event.get("pathParameters") or {}),
        query=dict(event.get("queryStringParameters") or {}),
        domain=str(context.get("domainName", "")),
        raw_body=event.get("body"),
        is_base64=bool(event.get("isBase64Encoded")),
        now=int(time.time()) if now is None else now,
        request_id=invocation.request_id,
        remaining_ms=invocation.remaining_ms,
    )


def _dispatch(
    event: Mapping[str, Any],
    api: object,
    dispatch: Mapping[str, str],
    invocation: Invocation,
    timing: Timing,
) -> Response:
    """Answer one event: 404, then 403, then the method; an ApiError is its body."""
    key = str(event["routeKey"])
    route = ROUTES_BY_KEY.get(key)
    if route is None or key not in dispatch:
        return error_response(ApiError(404, NO_ROUTE))
    claims = event["requestContext"]["authorizer"]["jwt"]["claims"]
    if route.group not in claim_groups(claims):
        return error_response(
            ApiError(403, f"caller is not in the {route.group} group")
        )
    method: Callable[[Request, Timing], Response] = getattr(api, dispatch[key])
    try:
        response = method(_request(event, route, invocation), timing)
    except ApiError as err:
        response = error_response(err)
    return response


def serve(
    event: Mapping[str, Any],
    *,
    api: object,
    dispatch: Mapping[str, str],
    invocation: Invocation | None = None,
) -> Response:
    """Answer one event and write one timing line.

    Find the route (404 "no such route"), check its group (403), call the method
    `dispatch[route.key]` names on `api` with (Request, Timing), and turn an
    ApiError into error_response. The method returns the Response itself. Any
    other exception is logged as one `unhandled` line and answered with the 500
    `internal_error` body that names the request id; its timing line records 500.
    The Lambda `Errors` metric no longer counts these: the `unhandled` lines do.
    """
    invocation = invocation or Invocation()
    timing_log.begin_request()
    started = time.perf_counter()
    key = str(event["routeKey"])
    route = ROUTES_BY_KEY.get(key)
    timing = Timing()
    status = INTERNAL
    try:
        try:
            response = _dispatch(event, api, dispatch, invocation, timing)
        except Exception as err:
            line = {
                "unhandled": type(err).__name__,
                "request_id": invocation.request_id,
                "route": key,
            }
            logger.exception("unhandled", extra={"line": line})
            response = error_response(
                ApiError(INTERNAL, internal_detail(invocation.request_id))
            )
        status = int(response["statusCode"])
    finally:
        timing_log.emit(
            timing_name(route) if route is not None else key,
            {**timing.phases, "total_ms": _ms_since(started)},
            timing.asset_id,
            cache=timing.cache,
            answer={"status": status, "request_id": invocation.request_id},
        )
    return response
