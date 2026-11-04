"""`POST /notes` over a fake notes table that refuses what DynamoDB refuses.

The fake runs `ddb_rules.check`, refuses a missing or empty key and a type it cannot
store, and evaluates `attribute_not_exists(note_id)`, so a second put of one id fails.
"""

import json
from typing import Any

import pytest
from botocore.exceptions import ClientError

from asteroidea_ingest import get_started_api, notes_api
from asteroidea_ingest.notes_api import NotesApi
from asteroidea_ingest.sessions import unmarshal

from . import ddb_rules
from .conftest import jwt_event

ROUTE = "POST /notes"
TABLE = "notes"
TYPES = frozenset({"S", "N", "BOOL"})
VALIDATION = "ValidationException"
CONDITION = "ConditionalCheckFailedException"


def _refuse(code: str, message: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": message}}, "PutItem")


class FakeNotes:
    """An in-memory notes table: key checks, typed values, the not-exists condition."""

    def __init__(self) -> None:
        self.items: dict[str, dict[str, Any]] = {}

    def put_item(self, **kwargs: Any) -> dict[str, Any]:
        ddb_rules.check(kwargs)
        assert kwargs["TableName"] == TABLE
        item = kwargs["Item"]
        key = item.get("note_id", {}).get("S", "")
        if not key:
            msg = "One or more parameter values were invalid: missing the key note_id"
            raise _refuse(VALIDATION, msg)
        for name, value in item.items():
            if set(value) - TYPES:
                msg = f"unsupported type for {name}"
                raise _refuse(VALIDATION, msg)
        if kwargs.get("ConditionExpression") == "attribute_not_exists(note_id)":
            if key in self.items:
                raise _refuse(CONDITION, "exists")
        elif kwargs.get("ConditionExpression") is not None:
            msg = f"the fake does not know {kwargs['ConditionExpression']}"
            raise AssertionError(msg)
        self.items[key] = unmarshal(item)
        return {}


def _api(table: FakeNotes, ids: list[str] | None = None) -> NotesApi:
    queue = list(ids or ["n1", "n2", "n3"])
    return NotesApi(ddb=table, table=TABLE, now=lambda: 1_700_000_000, new_id=queue.pop)


def _event(body: object, *, email: str | None = None) -> dict[str, Any]:
    event = jwt_event(ROUTE, sub="sub-9", body=body)
    if email is not None:
        claims = event["requestContext"]["authorizer"]["jwt"]["claims"]
        claims["email"] = email
    return event


GOOD = {
    "text": "the spectrogram axis is unlabelled",
    "url": "https://app.example/studies/s1?tab=lfp",
    "view": {"path": "/studies/s1", "query": {"tab": "lfp", "zoom": 2.5}},
}


def test_a_note_from_an_id_token_stores_the_email_and_answers_201() -> None:
    table = FakeNotes()
    response = _api(table).handle(_event(GOOD, email="r@example.org"))
    assert response["statusCode"] == 201
    assert json.loads(response["body"]) == {"note_id": "n3"}
    assert table.items["n3"] == {
        "note_id": "n3",
        "created_at": 1_700_000_000,
        "author_sub": "sub-9",
        "author_email": "r@example.org",
        "status": "open",
        "text": GOOD["text"],
        "url": GOOD["url"],
        "view": '{"path":"/studies/s1","query":{"tab":"lfp","zoom":2.5}}',
    }


def test_a_note_from_an_access_token_stores_the_sub_and_the_flag() -> None:
    table = FakeNotes()
    response = _api(table).handle(_event(GOOD))
    assert response["statusCode"] == 201
    item = table.items["n3"]
    assert item["author_sub"] == "sub-9"
    assert item["author_email_missing"] is True
    assert "author_email" not in item


def test_the_id_is_never_overwritten() -> None:
    table = FakeNotes()
    api = _api(table, ["same", "same"])
    assert api.handle(_event(GOOD))["statusCode"] == 201
    response = api.handle(_event({**GOOD, "text": "second"}))
    assert response["statusCode"] == 500
    assert table.items["same"]["text"] == GOOD["text"]


@pytest.mark.parametrize(
    ("body", "sentence"),
    [
        ({**GOOD, "text": ""}, "text is required: write the note before sending it."),
        (
            {**GOOD, "text": "   "},
            "text is required: write the note before sending it.",
        ),
        ({**GOOD, "text": 3}, "text is required: write the note before sending it."),
        ({**GOOD, "text": "x" * 4001}, "text is 4001 characters; the limit is 4000."),
        ({**GOOD, "url": ""}, "url is required: send the page address."),
        ({**GOOD, "url": "u" * 2049}, "url is 2049 characters; the limit is 2048."),
        (
            {**GOOD, "view": "path"},
            "view must be an object of the page's route and query.",
        ),
        (
            {**GOOD, "view": {"q": "v" * 4090}},
            "view is 4098 bytes as JSON; the limit is 4096.",
        ),
        (
            {**GOOD, "author": "me"},
            "Unknown field author; send text, url and view only.",
        ),
        ({k: v for k, v in GOOD.items() if k != "view"}, "view must be an object"),
    ],
)
def test_bad_input_is_400_with_a_sentence(body: object, sentence: str) -> None:
    table = FakeNotes()
    response = _api(table).handle(_event(body))
    assert response["statusCode"] == 400
    refusal = json.loads(response["body"])
    assert refusal["error"] == "invalid_request"
    assert refusal["detail"].startswith(sentence)
    assert table.items == {}


def test_text_of_exactly_4000_characters_and_view_of_4096_bytes_are_kept() -> None:
    table = FakeNotes()
    view = {"q": "v" * 4088}
    assert len(json.dumps(view, separators=(",", ":"))) == 4096
    body = {**GOOD, "text": "x" * 4000, "view": view}
    assert _api(table).handle(_event(body))["statusCode"] == 201


def test_a_body_that_is_not_json_is_400() -> None:
    response = _api(FakeNotes()).handle(_event("{not json"))
    assert response["statusCode"] == 400


def test_a_curator_outside_researchers_is_403() -> None:
    event = jwt_event(ROUTE, groups="[curators]", body=GOOD)
    assert _api(FakeNotes()).handle(event)["statusCode"] == 403


def test_the_platform_handler_sends_notes_to_the_notes_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    table = FakeNotes()
    monkeypatch.setattr(get_started_api, "notes_api", lambda: _api(table))
    response = get_started_api.handler(_event(GOOD), object())
    assert response["statusCode"] == 201
    assert list(table.items) == ["n3"]


def test_the_lambda_reads_the_table_name_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NOTES_TABLE", "notes-xyz")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    assert notes_api.handler_api().table == "notes-xyz"
