"""`make reextract` (admin, live): re-extract assets.

`make reextract [DATASET=<d>] [ASSET=<id>] [FORMATS=nwb,tsv] [DRY=1]
[FORCE=1 ASSET=<id>]` lists the catalog `assets` rows (PyIceberg Glue), writes one
tracking item per asset in the sessions table (`session_id =
reextract:<run_id>:<asset_id>`, `reextract_state` QUEUED, `reextract_lease_until` now +
`timing.REEXTRACT_LEASE_SECONDS`, `expires_at` now + 7 days) and starts one execution of
the re-extract state machine for the run (`reextract_flow.py`). Its Map derives
each asset's rows to the derived store, a bounded number at a time, then puts one
rows-by-pointer message per asset on `asteroidea-extract.fifo`, whose one handler
commits the rows serially. This module never invokes a function and never
sends to the queue itself. A run of more than `MAX_ASSETS_PER_EXECUTION` assets starts
one execution per that many assets (Step Functions' 256 KiB input and 25,000-event
history limits). The CLI polls every 10 s and prints PASS or FAIL per asset (a FAIL
names the tables already replaced).

Refusals (a FAIL line, nothing started): an asset whose format has no extractor is a
SKIP line; an asset with another run's QUEUED tracking item whose lease is still live
names that run and the lease end. `FORCE=1 ASSET=<id>` first expires that lease. A DONE
or FAILED item holds no lease, so it never blocks a later run. An execution that cannot
start marks its assets FAILED with the error, so their FAIL lines name it.

Exit codes: 0 every asset PASS or SKIP; 1 a FAIL or a refusal; 2 usage; 4 no sandbox.
"""

import argparse
import json
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final, Protocol

from botocore.exceptions import BotoCoreError, ClientError
from pyiceberg.expressions import And, BooleanExpression, EqualTo

from asteroidea_infra import schema
from asteroidea_infra.admin import session_for, stack_outputs
from asteroidea_infra.catalog_init import glue_catalog
from asteroidea_infra.reclaim import scan_items, sessions_table_name
from asteroidea_infra.sandbox_guard import require_sandbox
from asteroidea_ingest import commit, sessions
from asteroidea_ingest.extract import bids

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from boto3 import Session
    from pyiceberg.catalog import Catalog

POLL_SECONDS: Final = 10
ASSET_TIMEOUT_SECONDS: Final = 30 * 60
EXIT_FAIL: Final = 1
EXIT_USAGE: Final = 2
STATE_MACHINE_OUTPUT: Final = "ReextractStateMachineArn"
# One execution's input is at most 256 KiB and its history at most 25,000 events. An
# asset item is about 14 history events (Map iteration start and end, the Lambda
# task's 5, the Choice's 2, the SQS task's 5), so 500 items are 7,000 events with
# room for retries. An item carries the relpath twice (relpath, s3_key): 580 B with a
# 190-character path (`test_long_path_items_split_by_bytes`), so `chunks` also splits
# by encoded size.
MAX_ASSETS_PER_EXECUTION: Final = 500
MAX_INPUT_BYTES: Final = 256 * 1024
ASSET_COLUMNS: Final = (
    "asset_id",
    "dataset",
    "s3_key",
    "s3_version_id",
    "size_bytes",
    "format",
    "extension",
)


class StepFunctions(Protocol):
    """The one Step Functions call the CLI makes."""

    def start_execution(self, **kwargs: str) -> dict[str, Any]:
        """Start one execution of a state machine."""
        ...


@dataclass(frozen=True, slots=True)
class Request:
    """What to re-extract: filters, and whether to start at all."""

    dataset: str = ""
    asset: str = ""
    formats: tuple[str, ...] = ()
    dry: bool = False
    force: bool = False


@dataclass(frozen=True, slots=True)
class Deps:
    """The two AWS clients the run uses (DynamoDB, Step Functions; never Lambda)."""

    table: sessions.SessionTable
    sfn: StepFunctions
    state_machine_arn: str
    run_id: str
    now: Callable[[], int] = lambda: int(time.time())
    sleep: Callable[[float], None] = time.sleep


@dataclass(slots=True)
class Tally:
    """Per-run counts for the summary."""

    passed: int = 0
    failed: int = 0
    skipped: int = 0
    queued: list[Mapping[str, Any]] = field(default_factory=list)


def connect(session: Session, run_id: str) -> Deps:
    """Build the clients: DynamoDB for the items, Step Functions for the start."""
    outputs = stack_outputs(session)
    return Deps(
        table=sessions.SessionTable(
            session.client("dynamodb"), sessions_table_name(session)
        ),
        sfn=session.client("stepfunctions"),
        state_machine_arn=outputs[STATE_MACHINE_OUTPUT],
        run_id=run_id,
    )


def _filter(request: Request) -> BooleanExpression | None:
    terms: list[BooleanExpression] = []
    if request.dataset:
        terms.append(EqualTo(term="dataset", value=request.dataset))
    if request.asset:
        terms.append(EqualTo(term="asset_id", value=request.asset))
    if not terms:
        return None
    expr = terms[0]
    for term in terms[1:]:
        expr = And(expr, term)
    return expr


def list_assets(catalog: Catalog, request: Request) -> list[dict[str, Any]]:
    """Return the catalog `assets` rows that match the filters, by asset id."""
    table = catalog.load_table(f"{schema.DATABASE}.{schema.TABLE}")
    expr = _filter(request)
    scan = (
        table.scan(selected_fields=ASSET_COLUMNS)
        if expr is None
        else table.scan(row_filter=expr, selected_fields=ASSET_COLUMNS)
    )
    rows: list[dict[str, Any]] = scan.to_arrow().to_pylist()
    return sorted(rows, key=lambda r: str(r["asset_id"]))


def asset_fields(row: Mapping[str, Any]) -> dict[str, str]:
    """Return the Map item fields of one `assets` row."""
    dataset, key = str(row["dataset"]), str(row["s3_key"])
    relpath = key.removeprefix(f"raw/{dataset}/")
    fmt = row.get("format") or commit.file_format(str(row["extension"]))
    return {
        "asset_id": str(row["asset_id"]),
        "dataset": dataset,
        "relpath": relpath,
        "s3_key": key,
        "s3_version_id": str(row["s3_version_id"]),
        "file_format": str(fmt),
        "size": str(int(row["size_bytes"])),
    }


def _live_leases(
    items: Sequence[Mapping[str, Any]], asset_id: str, run_id: str, now: int
) -> list[Mapping[str, Any]]:
    """Return other runs' QUEUED tracking items for the asset whose lease is live."""
    return [
        item
        for item in items
        if str(item["session_id"]).startswith(sessions.REEXTRACT_PREFIX)
        and item.get("asset_id") == asset_id
        and item.get("run_id") != run_id
        and item.get(sessions.REEXTRACT_STATE) == sessions.REEXTRACT_QUEUED
        and int(item.get(sessions.REEXTRACT_LEASE, 0)) > now
    ]


def _expire(deps: Deps, item: Mapping[str, Any], now: int) -> None:
    """Expire a live tracking lease (FORCE=1): the lease becomes `now - 1`."""
    deps.table.ddb.update_item(
        TableName=deps.table.name,
        Key=sessions.marshal({"session_id": item["session_id"]}),
        UpdateExpression="SET #lease = :new, #updated_at = :now",
        ConditionExpression="#state = :queued",
        ExpressionAttributeNames={
            "#lease": sessions.REEXTRACT_LEASE,
            "#updated_at": "updated_at",
            "#state": sessions.REEXTRACT_STATE,
        },
        ExpressionAttributeValues=sessions.marshal(
            {":new": now - 1, ":now": now, ":queued": sessions.REEXTRACT_QUEUED}
        ),
    )


def _track(deps: Deps, fields: dict[str, str], now: int) -> dict[str, str]:
    """Write the QUEUED tracking item; return the Map item (tracking id and fields)."""
    item = sessions.reextract_item(deps.run_id, fields["asset_id"], now)
    deps.table.ddb.put_item(
        TableName=deps.table.name,
        Item=sessions.marshal(item),
        ConditionExpression="attribute_not_exists(session_id)",
    )
    return {"session_id": str(item["session_id"]), "run_id": deps.run_id, **fields}


def execution_input(run_id: str, items: Sequence[Mapping[str, str]]) -> str:
    """Return one execution's input; refuse one over Step Functions' 256 KiB."""
    body = json.dumps({"run_id": run_id, "assets": list(items)}, sort_keys=True)
    size = len(body.encode())
    if size > MAX_INPUT_BYTES:
        msg = f"execution input is {size} bytes, over {MAX_INPUT_BYTES}"
        raise ValueError(msg)
    return body


def chunks(run_id: str, items: Sequence[dict[str, str]]) -> list[list[dict[str, str]]]:
    """Split a run's items into execution inputs under both Step Functions limits.

    At most `MAX_ASSETS_PER_EXECUTION` items and `MAX_INPUT_BYTES` encoded each. The
    size is exact for `json.dumps`'s default separators: the empty input, plus each
    item's encoding, plus 2 bytes (", ") between items. An item too large alone
    gets a chunk of its own, which `execution_input` then refuses.
    """
    empty = len(execution_input(run_id, []).encode())
    out: list[list[dict[str, str]]] = []
    size = empty
    for item in items:
        added = len(json.dumps(item, sort_keys=True).encode())
        added += 2 if out and out[-1] else 0
        full = out and len(out[-1]) >= MAX_ASSETS_PER_EXECUTION
        if not out or full or size + added > MAX_INPUT_BYTES:
            out.append([])
            size = empty
            added = len(json.dumps(item, sort_keys=True).encode())
        out[-1].append(item)
        size += added
    return out


def _fail_unstarted(
    deps: Deps, items: Sequence[Mapping[str, str]], exc: Exception
) -> None:
    """Mark each item of an execution that did not start FAILED, with the error."""
    for item in items:
        sessions.set_reextract(
            deps.table,
            item["session_id"],
            {
                sessions.REEXTRACT_STATE: sessions.REEXTRACT_FAILED,
                "reextract_error": {
                    "class": type(exc).__name__,
                    "message": f"the execution did not start: {exc}",
                    "tables_replaced": [],
                },
                "tables_replaced": [],
                "updated_at": deps.now(),
            },
        )


def _start(deps: Deps, items: Sequence[dict[str, str]]) -> None:
    """Start the run's executions: one, unless `chunks` must split the run."""
    for index, chunk in enumerate(chunks(deps.run_id, items)):
        name = f"{deps.run_id}-{index}"
        try:
            deps.sfn.start_execution(
                stateMachineArn=deps.state_machine_arn,
                name=name,
                input=execution_input(deps.run_id, chunk),
            )
        except (ClientError, BotoCoreError, ValueError) as exc:
            sys.stderr.write(f"execution {name} did not start: {exc}\n")
            _fail_unstarted(deps, chunk, exc)


def _plan(
    rows: Sequence[Mapping[str, Any]], request: Request, deps: Deps, tally: Tally
) -> list[dict[str, str]]:
    """Print a SKIP or refusal line per asset that is not sent; return those sent."""
    now = deps.now()
    items = scan_items(deps.table)
    chosen: list[dict[str, str]] = []
    for row in rows:
        fields = asset_fields(row)
        asset_id, relpath = fields["asset_id"], fields["relpath"]
        if request.formats and fields["file_format"] not in request.formats:
            continue
        if bids.extractor_for(relpath) is None:
            sys.stdout.write(
                f"SKIP {asset_id} {relpath}: no extractor for the format\n"
            )
            tally.skipped += 1
            continue
        live = _live_leases(items, asset_id, deps.run_id, now)
        if live and request.force and request.asset and not request.dry:
            for item in live:
                _expire(deps, item, now)
                sys.stdout.write(f"FORCE {item['session_id']}: lease expired\n")
            live = []
        if live:
            item = live[0]
            sys.stdout.write(
                f"FAIL {asset_id} {relpath}: refused, run {item['run_id']} holds a "
                f"live lease until {item[sessions.REEXTRACT_LEASE]}; "
                "FORCE=1 ASSET=<id> expires it\n"
            )
            tally.failed += 1
            continue
        chosen.append(fields)
    return chosen


def _settle(deps: Deps, tracking: str, fields: Mapping[str, str]) -> bool:
    """Poll one item to DONE or FAILED (30 minutes at most); print its line."""
    asset_id, relpath = fields["asset_id"], fields["relpath"]
    deadline = deps.now() + ASSET_TIMEOUT_SECONDS
    while True:
        item = sessions.get(deps.table, tracking) or {}
        state = item.get(sessions.REEXTRACT_STATE)
        replaced = ", ".join(item.get("tables_replaced", [])) or "none"
        if state == sessions.REEXTRACT_DONE:
            sys.stdout.write(f"PASS {asset_id} {relpath}: replaced {replaced}\n")
            return True
        if state == sessions.REEXTRACT_FAILED:
            error = item.get("reextract_error", {})
            sys.stdout.write(
                f"FAIL {asset_id} {relpath}: {error.get('class')}: "
                f"{error.get('message')}; tables replaced before the failure: "
                f"{replaced}\n"
            )
            return False
        if deps.now() >= deadline:
            sys.stdout.write(
                f"FAIL {asset_id} {relpath}: not finished after "
                f"{ASSET_TIMEOUT_SECONDS} s (state {state})\n"
            )
            return False
        deps.sleep(POLL_SECONDS)


def run(rows: Sequence[Mapping[str, Any]], request: Request, deps: Deps) -> int:
    """Re-extract each asset in `rows`; return the exit code (0 or 1)."""
    tally = Tally()
    chosen = _plan(rows, request, deps, tally)
    if request.dry:
        for fields in chosen:
            sys.stdout.write(
                f"would send {fields['asset_id']} {fields['relpath']} "
                f"({fields['file_format']})\n"
            )
        sys.stdout.write(
            f"summary: {len(chosen)} would be sent, {tally.skipped} SKIP, "
            f"{tally.failed} refused (dry run)\n"
        )
        return EXIT_FAIL if tally.failed else 0
    items = [_track(deps, fields, deps.now()) for fields in chosen]
    if items:
        _start(deps, items)
    for item in items:
        if _settle(deps, item["session_id"], item):
            tally.passed += 1
        else:
            tally.failed += 1
    sys.stdout.write(
        f"summary: {tally.passed} PASS, {tally.failed} FAIL, {tally.skipped} SKIP "
        f"(run {deps.run_id})\n"
    )
    return EXIT_FAIL if tally.failed else 0


def execute(session: Session, catalog: Catalog, request: Request, run_id: str) -> int:
    """List the assets, connect (DynamoDB and Step Functions only) and run."""
    rows = list_assets(catalog, request)
    return run(rows, request, connect(session, run_id))


def new_run_id() -> str:
    """Return a sortable run id: UTC time and a short random suffix.

    It names the run's executions (`<run_id>-<n>`), so it holds only characters a
    Step Functions execution name allows.
    """
    return f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:6]}"


def main() -> None:
    """Parse the flags, require the sandbox env, and run."""
    parser = argparse.ArgumentParser(prog="reextract")
    parser.add_argument("--dataset", default="")
    parser.add_argument("--asset", default="")
    parser.add_argument("--formats", default="")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    if args.force and not args.asset:
        sys.stderr.write("--force needs --asset <id>\n")
        raise SystemExit(EXIT_USAGE)
    profile, account = require_sandbox()
    request = Request(
        dataset=args.dataset,
        asset=args.asset,
        formats=tuple(f for f in args.formats.split(",") if f),
        dry=args.dry_run,
        force=args.force,
    )
    session = session_for(profile, account)
    raise SystemExit(execute(session, glue_catalog(profile), request, new_run_id()))


if __name__ == "__main__":
    main()
