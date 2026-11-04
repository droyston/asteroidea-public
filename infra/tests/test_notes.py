"""`make notes` and `make notes-done` over a fake that refuses like DynamoDB.

The fake runs the shared `ddb_rules.check` (unused placeholders), refuses a reserved
word written bare in an expression (`status` is one), pages its scan, applies the
`status` filter only after a page is read (as DynamoDB does), and evaluates
`attribute_exists(#note_id)` on update.
"""

import importlib.util
import re
from pathlib import Path
from typing import Any

import pytest
from botocore.exceptions import ClientError

from asteroidea_infra import notes
from asteroidea_ingest.sessions import marshal, unmarshal

_RULES = Path(__file__).resolve().parents[2] / "ingest" / "tests" / "ddb_rules.py"
_SPEC = importlib.util.spec_from_file_location("ddb_rules_shared", _RULES)
assert _SPEC is not None
assert _SPEC.loader is not None
ddb_rules = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(ddb_rules)

TABLE = "notes-t"
RESERVED = frozenset({"status"})
VALIDATION = "ValidationException"
CONDITION = "ConditionalCheckFailedException"
BARE = re.compile(r"(?<![#:\w])([A-Za-z_]\w*)(?=\s*=)")


def _refuse(code: str, message: str, op: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": message}}, op)


class FakeNotesTable:
    """In-memory notes: paged scan with a post-read filter, conditional update."""

    def __init__(self, page_size: int = 2) -> None:
        self.items: dict[str, dict[str, Any]] = {}
        self.page_size = page_size
        self.scans = 0
        # When set, update_item raises this error code (a throttle, say).
        self.fail_with: str | None = None

    def seed(self, note_id: str, created_at: int, **fields: Any) -> None:
        self.items[note_id] = {
            "note_id": note_id,
            "created_at": created_at,
            "author_sub": "sub-1",
            "status": "open",
            "text": f"text of {note_id}",
            "url": f"https://app.example/{note_id}",
            "view": '{"path":"/"}',
            **fields,
        }

    def _check(self, kwargs: dict[str, Any], op: str) -> None:
        ddb_rules.check(kwargs, op)
        assert kwargs["TableName"] == TABLE
        for key in ("FilterExpression", "UpdateExpression", "ConditionExpression"):
            for word in BARE.findall(str(kwargs.get(key, ""))):
                if word.lower() in RESERVED:
                    msg = f"Attribute name is a reserved keyword: {word}"
                    raise _refuse(VALIDATION, msg, op)

    def _resolve(self, expr: str, names: dict[str, str]) -> str:
        return re.sub(r"#\w+", lambda m: names[m.group(0)], expr)

    def scan(self, **kwargs: Any) -> dict[str, Any]:
        self._check(kwargs, "Scan")
        self.scans += 1
        names = kwargs["ExpressionAttributeNames"]
        values = unmarshal(kwargs["ExpressionAttributeValues"])
        attr, _, placeholder = self._resolve(kwargs["FilterExpression"], names).split()
        keys = sorted(self.items)
        start = 0
        if "ExclusiveStartKey" in kwargs:
            start = keys.index(unmarshal(kwargs["ExclusiveStartKey"])["note_id"]) + 1
        page = keys[start : start + self.page_size]
        found = [
            self.items[k] for k in page if self.items[k][attr] == values[placeholder]
        ]
        out: dict[str, Any] = {"Items": [marshal(i) for i in found]}
        if start + self.page_size < len(keys):
            out["LastEvaluatedKey"] = marshal({"note_id": page[-1]})
        return out

    def update_item(self, **kwargs: Any) -> dict[str, Any]:
        self._check(kwargs, "UpdateItem")
        if self.fail_with is not None:
            raise _refuse(self.fail_with, "refused by the test", "UpdateItem")
        names = kwargs["ExpressionAttributeNames"]
        values = unmarshal(kwargs["ExpressionAttributeValues"])
        key = unmarshal(kwargs["Key"])["note_id"]
        condition = self._resolve(kwargs["ConditionExpression"], names)
        assert condition == "attribute_exists(note_id)"
        if key not in self.items:
            raise _refuse(CONDITION, "The conditional request failed", "UpdateItem")
        sets = self._resolve(kwargs["UpdateExpression"], names).removeprefix("SET ")
        for clause in sets.split(", "):
            attr, placeholder = clause.split(" = ")
            self.items[key][attr] = values[placeholder]
        return {}


def _table() -> FakeNotesTable:
    table = FakeNotesTable()
    table.seed("n-old", 1_761_500_000, author_email="a@example.org")
    table.seed("n-new", 1_761_590_170, author_email_missing=True, author_sub="sub-9")
    table.seed("n-done", 1_761_600_000, status="done")
    table.seed(
        "n-mid",
        1_761_550_000,
        author_email="b@example.org",
        text="line one\n\nline three",
        view='{"path":"/studies/s1","query":{"tab":"lfp"}}',
    )
    return table


def test_open_notes_are_newest_first_across_scan_pages() -> None:
    table = _table()
    got = notes.open_notes(table, TABLE)
    assert [n["note_id"] for n in got] == ["n-new", "n-mid", "n-old"]
    assert table.scans == 2  # four items, two per page


def test_render_prints_id_time_author_url_view_and_text() -> None:
    out = notes.render(notes.open_notes(_table(), TABLE))
    assert out == (
        "# Open notes (3)\n"
        "\n## n-new\n\n"
        "- time: 2025-10-27 18:36:10 UTC\n"
        "- author: sub sub-9 (the token carried no email)\n"
        "- url: https://app.example/n-new\n"
        '- view: `{"path":"/"}`\n\n'
        "> text of n-new\n"
        "\n## n-mid\n\n"
        "- time: 2025-10-27 07:26:40 UTC\n"
        "- author: b@example.org\n"
        "- url: https://app.example/n-mid\n"
        '- view: `{"path":"/studies/s1","query":{"tab":"lfp"}}`\n\n'
        "> line one\n>\n> line three\n"
        "\n## n-old\n\n"
        "- time: 2025-10-26 17:33:20 UTC\n"
        "- author: a@example.org\n"
        "- url: https://app.example/n-old\n"
        '- view: `{"path":"/"}`\n\n'
        "> text of n-old\n"
    )


def test_no_open_notes_says_so() -> None:
    assert notes.render([]) == "# Open notes (0)\n\nNo open notes.\n"


def test_done_marks_one_note_and_it_leaves_the_list(
    capsys: pytest.CaptureFixture[str],
) -> None:
    table = _table()
    assert notes.run(table, TABLE, "n-mid", 1_761_700_000) == 0
    assert table.items["n-mid"]["status"] == "done"
    assert table.items["n-mid"]["done_at"] == 1_761_700_000
    assert capsys.readouterr().out == "note n-mid marked done\n"
    assert [n["note_id"] for n in notes.open_notes(table, TABLE)] == ["n-new", "n-old"]


def test_done_refuses_an_unknown_id_and_writes_nothing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    table = _table()
    before = {k: dict(v) for k, v in table.items.items()}
    assert notes.run(table, TABLE, "n-nope", 1_761_700_000) == notes.EXIT_UNKNOWN
    assert table.items == before
    assert "no note n-nope" in capsys.readouterr().err


def test_run_without_an_id_prints_the_markdown(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert notes.run(_table(), TABLE, "", 0) == 0
    assert capsys.readouterr().out.startswith("# Open notes (3)\n\n## n-new\n")


def test_another_update_error_is_raised() -> None:
    table = _table()
    table.fail_with = "ProvisionedThroughputExceededException"
    with pytest.raises(ClientError, match="ProvisionedThroughputExceeded"):
        notes.mark_done(table, TABLE, "n-old", 0)


def test_the_table_is_found_by_its_logical_id() -> None:
    class Cfn:
        def describe_stack_resources(self, **_: Any) -> dict[str, Any]:
            return {
                "StackResources": [
                    {
                        "LogicalResourceId": "SessionsTable1",
                        "ResourceType": "AWS::DynamoDB::Table",
                        "PhysicalResourceId": "sessions",
                    },
                    {
                        "LogicalResourceId": "NotesTableABC",
                        "ResourceType": "AWS::DynamoDB::Table",
                        "PhysicalResourceId": "DataStack-NotesTable-1",
                    },
                ]
            }

    assert notes.notes_table_name(Cfn()) == "DataStack-NotesTable-1"
