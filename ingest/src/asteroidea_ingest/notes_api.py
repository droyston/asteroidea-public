"""`POST /notes`: an app-dev feedback note from the web app's top bar.

PlatformFunction serves it (`get_started_api.handler` hands `/notes` here). The body is
`{"text": <1-4000 characters>, "url": <1-2048 characters>, "view": <object>}`; `view`
is the page's route and query state, at most 4096 bytes as compact JSON. Any other key
is refused. The server stamps `note_id`, `created_at` (seconds since the epoch),
`author_sub`, `status` `open`, and `author_email` from the token's `email` claim. An
access token has no `email` claim: the note then stores `author_email_missing` true
and the `sub` only. `view` is stored as its compact JSON text, so DynamoDB never sees
a float or a nested empty value.

There is no read route: `make notes` reads the table with the admin profile.
"""

import json
import os
import time
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Protocol, Unpack

import boto3

from asteroidea_ingest.api_common import (
    ApiError,
    Invocation,
    Request,
    Response,
    Timing,
    json_response,
    serve,
)
from asteroidea_ingest.sessions import marshal

if TYPE_CHECKING:
    from collections.abc import Callable

    from asteroidea_ingest.aws_types import PutItemArgs

DISPATCH: Final = {"POST /notes": "create"}
TEXT_MAX: Final = 4000
URL_MAX: Final = 2048
VIEW_MAX_BYTES: Final = 4096
FIELDS: Final = frozenset({"text", "url", "view"})
OPEN: Final = "open"


class NoteStore(Protocol):
    """The one DynamoDB method the notes route calls."""

    def put_item(self, **kwargs: Unpack[PutItemArgs]) -> dict[str, Any]:
        """Write one item."""
        ...


def _now() -> int:
    return int(time.time())


def _new_id() -> str:
    return uuid.uuid4().hex


def validate(body: dict[str, Any]) -> tuple[str, str, str]:
    """Return (text, url, view as compact JSON); ApiError(400) with a sentence."""
    extra = sorted(set(body) - FIELDS)
    if extra:
        raise ApiError(400, f"Unknown field {extra[0]}; send text, url and view only.")
    text = body.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ApiError(400, "text is required: write the note before sending it.")
    if len(text) > TEXT_MAX:
        raise ApiError(400, f"text is {len(text)} characters; the limit is {TEXT_MAX}.")
    url = body.get("url")
    if not isinstance(url, str) or not url:
        raise ApiError(400, "url is required: send the page address.")
    if len(url) > URL_MAX:
        raise ApiError(400, f"url is {len(url)} characters; the limit is {URL_MAX}.")
    view = body.get("view")
    if not isinstance(view, dict):
        raise ApiError(400, "view must be an object of the page's route and query.")
    view_json = json.dumps(view, separators=(",", ":"), sort_keys=True)
    size = len(view_json.encode())
    if size > VIEW_MAX_BYTES:
        raise ApiError(
            400, f"view is {size} bytes as JSON; the limit is {VIEW_MAX_BYTES}."
        )
    return text, url, view_json


@dataclass(frozen=True)
class NotesApi:
    """`POST /notes` over an injected DynamoDB client and the notes table name."""

    ddb: NoteStore
    table: str
    now: Callable[[], int] = _now
    new_id: Callable[[], str] = _new_id

    def handle(
        self, event: dict[str, Any], *, invocation: Invocation | None = None
    ) -> Response:
        """Route one HTTP API (payload v2) event."""
        return serve(event, api=self, dispatch=DISPATCH, invocation=invocation)

    def create(self, request: Request, _timing: Timing) -> Response:
        """POST /notes: validate, stamp the author and time, put; 201 {note_id}."""
        text, url, view_json = validate(request.json_body())
        note_id = self.new_id()
        item: dict[str, Any] = {
            "note_id": note_id,
            "created_at": self.now(),
            "author_sub": request.sub,
            "status": OPEN,
            "text": text,
            "url": url,
            "view": view_json,
        }
        email = request.email or None
        if email is not None:
            item["author_email"] = email
        else:
            item["author_email_missing"] = True
        self.ddb.put_item(
            TableName=self.table,
            Item=marshal(item),
            ConditionExpression="attribute_not_exists(note_id)",
        )
        return json_response(201, {"note_id": note_id})


def handler_api() -> NotesApi:
    """Return the API over the Lambda's DynamoDB client and `NOTES_TABLE`."""
    return NotesApi(ddb=boto3.client("dynamodb"), table=os.environ["NOTES_TABLE"])
