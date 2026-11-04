"""`make notes` and `make notes-done ID=<id>` (admin, live): read the app-dev notes.

`make notes` prints every open note, newest first, as markdown: its id, time (UTC),
author, page address, view and text. `make notes-done ID=<id>` sets one note's
`status` to `done` and stamps `done_at`, with a condition that the note exists, so an
unknown id is refused and nothing is written. The table is DataStack's `NotesTable`,
found by its logical id; `make notes` scans it (the table is small; no index).
Exit codes: 1 an unknown id or a stack lookup fails; 4 no sandbox.
"""

import argparse
import sys
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final, NotRequired, Protocol, TypedDict, Unpack

from botocore.exceptions import ClientError

from asteroidea_infra.admin import session_for
from asteroidea_infra.sandbox_guard import require_sandbox
from asteroidea_ingest.sessions import marshal, unmarshal

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

OPEN: Final = "open"
DONE: Final = "done"
EXIT_UNKNOWN: Final = 1
LOGICAL_PREFIX: Final = "NotesTable"


class NotesScanArgs(TypedDict):
    """Keyword arguments of `DynamoDB.Scan` as used here."""

    TableName: str
    FilterExpression: str
    ExpressionAttributeNames: dict[str, str]
    ExpressionAttributeValues: dict[str, Any]
    ExclusiveStartKey: NotRequired[dict[str, Any]]


class NotesUpdateArgs(TypedDict):
    """Keyword arguments of `DynamoDB.UpdateItem` as used here."""

    TableName: str
    Key: dict[str, Any]
    UpdateExpression: str
    ConditionExpression: str
    ExpressionAttributeNames: dict[str, str]
    ExpressionAttributeValues: dict[str, Any]


class NotesDdb(Protocol):
    """The DynamoDB client methods `make notes` calls."""

    def scan(self, **kwargs: Unpack[NotesScanArgs]) -> dict[str, Any]:
        """Read one page of matching items."""
        ...

    def update_item(self, **kwargs: Unpack[NotesUpdateArgs]) -> dict[str, Any]:
        """Update one item."""
        ...


def open_notes(ddb: NotesDdb, table: str) -> list[dict[str, Any]]:
    """Return every open note, newest first (ties by id), following scan pages."""
    args: NotesScanArgs = {
        "TableName": table,
        "FilterExpression": "#status = :open",
        "ExpressionAttributeNames": {"#status": "status"},
        "ExpressionAttributeValues": marshal({":open": OPEN}),
    }
    items: list[dict[str, Any]] = []
    while True:
        page = ddb.scan(**args)
        items.extend(unmarshal(i) for i in page.get("Items", []))
        if "LastEvaluatedKey" not in page:
            break
        args["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    items.sort(key=lambda n: (-int(n.get("created_at", 0)), str(n["note_id"])))
    return items


def _author(note: dict[str, Any]) -> str:
    email = note.get("author_email")
    if isinstance(email, str) and email:
        return email
    return f"sub {note.get('author_sub', '?')} (the token carried no email)"


def _when(seconds: int) -> str:
    return datetime.fromtimestamp(seconds, UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def render(notes: Sequence[dict[str, Any]]) -> str:
    """Return the notes as markdown: a heading, then one section per note."""
    if not notes:
        return "# Open notes (0)\n\nNo open notes.\n"
    parts = [f"# Open notes ({len(notes)})\n"]
    for note in notes:
        text = str(note.get("text", ""))
        quoted = "\n".join(f"> {line}" if line else ">" for line in text.splitlines())
        parts.append(
            f"\n## {note['note_id']}\n\n"
            f"- time: {_when(int(note.get('created_at', 0)))}\n"
            f"- author: {_author(note)}\n"
            f"- url: {note.get('url', '')}\n"
            f"- view: `{note.get('view', '{}')}`\n\n"
            f"{quoted}\n"
        )
    return "".join(parts)


def mark_done(ddb: NotesDdb, table: str, note_id: str, now: int) -> bool:
    """Set one note `done`; return False (nothing written) when the id is unknown."""
    try:
        ddb.update_item(
            TableName=table,
            Key=marshal({"note_id": note_id}),
            UpdateExpression="SET #status = :done, #done_at = :now",
            ConditionExpression="attribute_exists(#note_id)",
            ExpressionAttributeNames={
                "#status": "status",
                "#done_at": "done_at",
                "#note_id": "note_id",
            },
            ExpressionAttributeValues=marshal({":done": DONE, ":now": now}),
        )
    except ClientError as err:
        if err.response.get("Error", {}).get("Code") == (
            "ConditionalCheckFailedException"
        ):
            return False
        raise
    return True


class DescribeArgs(TypedDict):
    """Keyword arguments of `CloudFormation.DescribeStackResources` as used here."""

    StackName: str


class StackResources(Protocol):
    """The one CloudFormation method the table lookup calls."""

    def describe_stack_resources(
        self, **kwargs: Unpack[DescribeArgs]
    ) -> Mapping[str, Any]:
        """Describe a stack's resources."""
        ...


def notes_table_name(cfn: StackResources) -> str:
    """Return the notes table's name from the DataStack resources."""
    for res in cfn.describe_stack_resources(StackName="DataStack")["StackResources"]:
        if (
            res["LogicalResourceId"].startswith(LOGICAL_PREFIX)
            and res["ResourceType"] == "AWS::DynamoDB::Table"
        ):
            return str(res["PhysicalResourceId"])
    msg = "notes table not found in DataStack (deploy this change first)"
    raise SystemExit(msg)


def run(ddb: NotesDdb, table: str, done: str, now: int) -> int:
    """List the open notes, or mark `done` handled; return the exit code."""
    if done:
        if not mark_done(ddb, table, done, now):
            sys.stderr.write(f"refused: no note {done}; nothing changed\n")
            return EXIT_UNKNOWN
        sys.stdout.write(f"note {done} marked done\n")
        return 0
    sys.stdout.write(render(open_notes(ddb, table)))
    return 0


def main() -> None:
    """Parse `--done <id>`, find the table and run."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--done", default="", help="the note id to mark done")
    args = parser.parse_args()
    profile, account = require_sandbox()
    session = session_for(profile, account)
    table = notes_table_name(session.client("cloudformation"))
    code = run(session.client("dynamodb"), table, args.done, int(time.time()))
    raise SystemExit(code)


if __name__ == "__main__":
    main()
