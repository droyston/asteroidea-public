"""Platform API client: one authenticated call that maps error statuses to SDK errors.

`PlatformClient` is the only place that turns the API's 401, 403, 409 and 500
answers into the SDK's exception classes. Callers read the other statuses
themselves. `json_of` and `refusal` read a reply body.
"""

import json
from typing import TYPE_CHECKING, Any, Final

from asteroidea.errors import (
    DatasetNotYoursError,
    HasDependentsError,
    IdTokenRequiredError,
    NotAResearcherError,
    NotTheAuthorError,
    PlatformUnreachableError,
    SignInRequiredError,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from asteroidea._http import Reply, Transport

_UNAUTHORIZED: Final = 401
_FORBIDDEN: Final = 403
_CONFLICT: Final = 409
_INTERNAL: Final = 500
_NOT_YOURS: Final = "dataset_not_yours"
_NOT_AUTHOR: Final = "not_author"
_NO_EMAIL: Final = "id_token_required"
_HAS_DEPENDENTS: Final = "has_dependents"


def json_of(reply: Reply) -> dict[str, Any]:
    """Return the reply body as a JSON object, or `{}` when it is not one."""
    try:
        data = json.loads(reply.body)
    except ValueError:
        return {}
    return dict(data) if isinstance(data, dict) else {}


def _text(fields: Mapping[str, Any], key: str) -> str | None:
    """Return the non-empty string at `key`, else `None`."""
    value = fields.get(key)
    return value if isinstance(value, str) and value else None


def refusal(reply: Reply) -> str:
    """Return the refusal sentence: `detail`, else an older API's `error` text."""
    fields = json_of(reply)
    return _text(fields, "detail") or _text(fields, "error") or "no reason given"


def _not_yours(fields: Mapping[str, Any]) -> bool:
    """Whether a 403 says the dataset is another researcher's.

    The API sends `error: "dataset_not_yours"`; an API from before the uniform
    body sent the same code under `code`, accepted for one release.
    """
    return _NOT_YOURS in {fields.get("error"), fields.get("code")}


class PlatformClient:
    """The three session routes, with status codes turned into SDK errors."""

    def __init__(self, base: str, token: str, transport: Transport) -> None:
        self.base = base.rstrip("/")
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        self.transport = transport

    def call(self, method: str, route: str, body: object | None = None) -> Reply:
        """Send one request and return the reply.

        Raises `SignInRequiredError` or `IdTokenRequiredError` on a 401,
        `NotTheAuthorError`, `DatasetNotYoursError` or `NotAResearcherError`
        on a 403, `HasDependentsError` on a 409 with that code, and
        `PlatformUnreachableError` on a 500 that carries an error code.
        Any other status is returned for the caller to read.
        """
        data = None if body is None else json.dumps(body).encode()
        reply = self.transport(method, self.base + route, self.headers, data)
        if reply.status == _UNAUTHORIZED:
            if json_of(reply).get("error") == _NO_EMAIL:
                raise IdTokenRequiredError
            raise SignInRequiredError
        if reply.status == _FORBIDDEN:
            fields = json_of(reply)
            if fields.get("error") == _NOT_AUTHOR:
                raise NotTheAuthorError(_text(fields, "detail"))
            if _not_yours(fields):
                text = _text(fields, "detail")
                if text is None and fields.get("code") == _NOT_YOURS:
                    text = _text(fields, "error")
                raise DatasetNotYoursError(text)
            raise NotAResearcherError
        if reply.status == _CONFLICT and json_of(reply).get("error") == _HAS_DEPENDENTS:
            raise HasDependentsError(_text(json_of(reply), "detail"))
        if reply.status == _INTERNAL:
            fields = json_of(reply)
            detail = _text(fields, "detail")
            if detail is not None and _text(fields, "error") is not None:
                raise PlatformUnreachableError(detail)
        return reply
