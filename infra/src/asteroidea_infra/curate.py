"""Curator CLI: `make import`, `make crosswalk`, `make studies`, `make tiers`.

`make import SOURCE=<dandi:ID@VERSION | openneuro:ID@VERSION> [INCLUDE=<glob,...>]
[EXTRA=<manifest.json>]` lists the source, resolves the dataset's title and licence
before anything is invoked, writes `~/asteroidea-imports/<run-id>/manifest.json`,
invokes ImportFunction asynchronously per file (at most 4 in flight), polls each
session to COMMITTED and EXTRACTED (30 minutes per file at most) and prints one PASS
or FAIL line per file. The mode per file comes from its source bucket's region (an
anonymous HEAD of the bucket root, `x-amz-bucket-region`), never from the archive.

`make crosswalk FILE=<csv>` loads a reviewed CSV (dataset, subject, subject_key, status,
linked_by, linked_at, basis, note) into `subject_links`: a new (dataset, subject) is
added, a changed one replaced, an equal one kept.

`make studies FILE=curation/studies/studies.csv` makes `studies` and `study_datasets`
(the sibling `study_datasets.csv`) equal the CSVs; `make tiers FILE=<csv>` sets
`datasets.tier` by rewriting each named row in full. Each prints added / replaced /
kept counts and writes nothing when a check fails.

Exit codes: 0 every file PASS; 1 a FAIL, a listing or metadata failure (no title or
licence), or an OpenNeuro snapshot mismatch; 2 an unknown archive or a second version
of an imported file (one version per dataset); 4 no sandbox env.
"""

import argparse
import csv
import json
import re
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from asteroidea_infra import config, schema
from asteroidea_infra.admin import session_for, stack_outputs
from asteroidea_infra.catalog_init import glue_catalog
from asteroidea_infra.reclaim import sessions_table_name
from asteroidea_infra.sandbox_guard import require_sandbox
from asteroidea_ingest import import_plan, sessions
from asteroidea_ingest.catalog_writer import CatalogWriter, utc_now_naive
from asteroidea_ingest.import_outcome import outcome

# The planning core lives in `asteroidea_ingest.import_plan` (
# the web import path): ImportPlanFunction plans web runs with the same code.
from asteroidea_ingest.import_plan import (
    EXIT_FAIL,
    AnonymousWeb,
    CurateError,
    ImportPlan,
    Listed,
    PlanInputs,
    Source,
    Web,
    existing_from_catalog,
    plan_source,
    session_status,
)
from asteroidea_ingest.upload_rules import DATASET_RE

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from pyiceberg.catalog import Catalog

MAX_IN_FLIGHT: Final = 4
FILE_TIMEOUT_SECONDS: Final = 30 * 60
POLL_SECONDS: Final = 10
IMPORTS_DIR: Final = Path.home() / "asteroidea-imports"
CROSSWALK_COLUMNS: Final = (
    "dataset",
    "subject",
    "subject_key",
    "status",
    "linked_by",
    "linked_at",
    "basis",
    "note",
)
STUDY_COLUMNS: Final = ("study", "title", "authors", "lab", "citation", "doi", "note")
STUDY_DATASET_COLUMNS: Final = ("study", "dataset", "role")
TIER_COLUMNS: Final = ("dataset", "tier", "note")
LINK_STATUSES: Final = ("pending", "confirmed")
TIERS: Final = ("curated", "upload", "sandbox")
EMAIL_PLACEHOLDER: Final = "OWNER_SIGN_IN_EMAIL"
SLUG_RE: Final = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


def choose_modes(web: Web, files: Sequence[Listed]) -> dict[str, str]:
    """Return relpath -> mode for the platform region (`import_plan.choose_modes`)."""
    return import_plan.choose_modes(web, files, config.REGION)


def write_manifest(
    directory: Path, run_id: str, plan: ImportPlan, planned: Sequence[dict[str, Any]]
) -> Path:
    """Write `<directory>/<run-id>/manifest.json` and return its path."""
    run_dir = directory / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / "manifest.json"
    body = {
        "run_id": run_id,
        "source": (
            f"{plan.source.archive}:{plan.source.dataset_id}@{plan.source.version}"
        ),
        "dataset": plan.source.dataset,
        "dataset_meta": plan.dataset_meta,
        "files": list(planned),
    }
    path.write_text(json.dumps(body, indent=1, sort_keys=True) + "\n")
    return path


# -- invoke and poll -----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Runner:
    """Invoke and poll, over injected calls and clock (tests use fakes)."""

    invoke: Callable[[dict[str, Any]], None]
    get_session: Callable[[str], dict[str, Any] | None]
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep


def run(planned: Sequence[dict[str, Any]], runner: Runner) -> dict[str, str]:
    """Invoke each event (at most 4 in flight) and poll it; return relpath -> line."""
    pending = list(planned)
    active: dict[str, tuple[dict[str, Any], float]] = {}
    results: dict[str, str] = {}
    while pending or active:
        while pending and len(active) < MAX_IN_FLIGHT:
            event = pending.pop(0)
            runner.invoke({k: v for k, v in event.items() if k != "session_id"})
            active[event["session_id"]] = (event, runner.clock())
        runner.sleep(POLL_SECONDS)
        for sid, (event, started) in list(active.items()):
            item = runner.get_session(sid)
            line = outcome(item)
            if line is None and runner.clock() - started > FILE_TIMEOUT_SECONDS:
                line = f"FAIL timed out after 30 min: {session_status(item)}"
            if line is not None:
                results[event["relpath"]] = line
                verdict, _, why = line.partition(" ")
                sys.stdout.write(f"{verdict} {event['relpath']} {why}".rstrip() + "\n")
                del active[sid]
    return results


@dataclass(frozen=True, slots=True)
class ImportRequest:
    """What `make import` asked for."""

    source: Source
    include: tuple[str, ...]
    extra: Mapping[str, Any] | None
    caller: str


@dataclass(frozen=True, slots=True)
class CurateDeps:
    """The CLI's outside world: the web, the catalog's versions, the runner."""

    web: Web
    existing: Callable[[str], Mapping[str, str | None]]
    runner: Runner
    imports_dir: Path = IMPORTS_DIR


def import_source(request: ImportRequest, deps: CurateDeps) -> int:
    """List, resolve metadata, refuse a second version, write the manifest, run."""
    planned = plan_source(
        deps.web,
        request.source,
        request.include,
        PlanInputs(
            caller=request.caller,
            region=config.REGION,
            existing=deps.existing,
            extra=request.extra,
        ),
    )
    plan, todo, done = planned.plan, planned.todo, planned.done
    run_id = f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:6]}"
    manifest = write_manifest(deps.imports_dir, run_id, plan, planned.events)
    sys.stdout.write(f"manifest: {manifest}\n")
    for relpath in done:
        sys.stdout.write(f"PASS {relpath} already imported\n")
    results = run(todo, deps.runner)
    failed = sum(1 for line in results.values() if line.startswith("FAIL"))
    sys.stdout.write(
        f"summary: {len(results) - failed + len(done)} PASS "
        f"({len(done)} already imported), {failed} FAIL\n"
    )
    return EXIT_FAIL if failed else 0


# -- loaders: crosswalk, studies, tiers -----------------------------------------


@dataclass(frozen=True, slots=True)
class Counts:
    """What a loader did to one table: rows added, replaced, kept and removed."""

    added: int
    replaced: int
    kept: int
    removed: int = 0


def _read(text: str, columns: tuple[str, ...], what: str) -> list[dict[str, str]]:
    """Parse a CSV whose header is exactly `columns`; cells are stripped."""
    reader = csv.DictReader(text.splitlines())
    if tuple(reader.fieldnames or ()) != columns:
        raise CurateError(EXIT_FAIL, f"{what} columns must be {','.join(columns)}")
    return [{k: (v or "").strip() for k, v in rec.items()} for rec in reader]


def _sync(
    catalog: Catalog,
    table_name: str,
    rows: Sequence[Mapping[str, Any]],
    *,
    prune: bool,
) -> Counts:
    """Make the table's rows match `rows` by key; `curated_by`/`created_at` don't count.

    A new key is appended, a key whose other columns differ is replaced
    (`CatalogWriter.replace`), an equal one is kept. With `prune` a stored key
    absent from `rows` is deleted.
    """
    spec = schema.SPECS[table_name]
    writer = CatalogWriter(catalog)
    table = catalog.load_table(f"{schema.DATABASE}.{table_name}")
    stored = {
        tuple(rec[k] for k in spec.key): rec
        for rec in table.scan().to_arrow().to_pylist()
    }
    skip = {"curated_by", "created_at"}
    added = replaced = kept = 0
    for row in rows:
        key = {k: row[k] for k in spec.key}
        old = stored.pop(tuple(row[k] for k in spec.key), None)
        if old is None:
            writer.append_if_absent(table_name, key, [row])
            added += 1
        elif {k: v for k, v in row.items() if k not in skip} == {
            k: old.get(k) for k in row if k not in skip
        }:
            kept += 1
        else:
            writer.replace(table_name, key, [row])
            replaced += 1
    if prune:
        for old in stored.values():
            writer.replace(table_name, {k: old[k] for k in spec.key}, [])
    return Counts(added, replaced, kept, len(stored) if prune else 0)


def _utc_naive(text: str, n: int) -> datetime:
    """Parse an ISO-8601 timestamp with an offset into naive UTC."""
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        moment = None
    if moment is None or moment.tzinfo is None:
        msg = f"line {n}: linked_at must be ISO-8601 with an offset, got {text!r}"
        raise CurateError(EXIT_FAIL, msg)
    return moment.astimezone(UTC).replace(tzinfo=None)


def crosswalk_rows(text: str, *, curated_by: str) -> list[dict[str, Any]]:
    """Parse and check a reviewed subject-links CSV; return `subject_links` rows."""
    now = utc_now_naive()
    rows: list[dict[str, Any]] = []
    for n, rec in enumerate(_read(text, CROSSWALK_COLUMNS, "crosswalk"), start=2):
        if not DATASET_RE.match(rec["dataset"]) or not rec["subject"].startswith(
            "sub-"
        ):
            raise CurateError(EXIT_FAIL, f"line {n}: bad dataset or subject")
        if not rec["subject_key"]:
            raise CurateError(EXIT_FAIL, f"line {n}: empty subject_key")
        if rec["status"] not in LINK_STATUSES:
            msg = f"line {n}: status must be one of {', '.join(LINK_STATUSES)}"
            raise CurateError(EXIT_FAIL, msg)
        if rec["linked_by"] == EMAIL_PLACEHOLDER:
            msg = (
                f"line {n}: linked_by is still {EMAIL_PLACEHOLDER}; "
                "put your sign-in email there before loading"
            )
            raise CurateError(EXIT_FAIL, msg)
        if not rec["linked_by"]:
            raise CurateError(EXIT_FAIL, f"line {n}: empty linked_by")
        rows.append(
            {
                "dataset": rec["dataset"],
                "subject": rec["subject"],
                "subject_key": rec["subject_key"],
                "note": rec["note"] or None,
                "curated_by": curated_by,
                "created_at": now,
                "status": rec["status"],
                "linked_by": rec["linked_by"],
                "linked_at": _utc_naive(rec["linked_at"], n),
                "basis": rec["basis"] or None,
            }
        )
    return rows


def crosswalk(catalog: Catalog, rows: Sequence[dict[str, Any]]) -> Counts:
    """Append new links, replace changed ones, keep equal ones (never delete)."""
    return _sync(catalog, "subject_links", rows, prune=False)


def studies_rows(
    studies_text: str, links_text: str, *, curated_by: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Parse and check the two studies CSVs; return (studies, study_datasets) rows."""
    now = utc_now_naive()
    studies: list[dict[str, Any]] = []
    for n, rec in enumerate(_read(studies_text, STUDY_COLUMNS, "studies"), start=2):
        if not SLUG_RE.match(rec["study"]):
            msg = f"line {n}: study slug {rec['study']!r} must be lowercase-hyphenated"
            raise CurateError(EXIT_FAIL, msg)
        if not rec["title"]:
            raise CurateError(EXIT_FAIL, f"line {n}: empty title")
        if any(row["study"] == rec["study"] for row in studies):
            raise CurateError(EXIT_FAIL, f"line {n}: study {rec['study']} twice")
        studies.append(
            {
                **{k: rec[k] or None for k in STUDY_COLUMNS},
                "study": rec["study"],
                "title": rec["title"],
                "curated_by": curated_by,
                "created_at": now,
            }
        )
    known = {row["study"] for row in studies}
    links: list[dict[str, Any]] = []
    owner: dict[str, str] = {}
    for n, rec in enumerate(
        _read(links_text, STUDY_DATASET_COLUMNS, "study_datasets"), start=2
    ):
        if rec["study"] not in known:
            msg = f"line {n}: study {rec['study']!r} is not in studies.csv"
            raise CurateError(EXIT_FAIL, msg)
        if not DATASET_RE.match(rec["dataset"]):
            raise CurateError(EXIT_FAIL, f"line {n}: bad dataset {rec['dataset']!r}")
        if rec["dataset"] in owner:
            msg = (
                f"line {n}: dataset {rec['dataset']} is in studies "
                f"{owner[rec['dataset']]} and {rec['study']}; at most one"
            )
            raise CurateError(EXIT_FAIL, msg)
        owner[rec["dataset"]] = rec["study"]
        links.append(
            {
                "study": rec["study"],
                "dataset": rec["dataset"],
                "role": rec["role"] or None,
                "curated_by": curated_by,
                "created_at": now,
            }
        )
    return studies, links


def load_studies(
    catalog: Catalog,
    rows: tuple[Sequence[dict[str, Any]], Sequence[dict[str, Any]]],
) -> tuple[Counts, Counts]:
    """Make `studies` and `study_datasets` equal the CSVs; return both counts."""
    studies, links = rows
    return (
        _sync(catalog, "studies", studies, prune=True),
        _sync(catalog, "study_datasets", links, prune=True),
    )


def tiers_rows(text: str) -> list[dict[str, str]]:
    """Parse and check `tiers.csv`; return {dataset, tier} rows (`note` is CSV-only)."""
    rows: list[dict[str, str]] = []
    for n, rec in enumerate(_read(text, TIER_COLUMNS, "tiers"), start=2):
        if not DATASET_RE.match(rec["dataset"]):
            raise CurateError(EXIT_FAIL, f"line {n}: bad dataset {rec['dataset']!r}")
        if rec["tier"] not in TIERS:
            msg = f"line {n}: tier must be one of {', '.join(TIERS)}"
            raise CurateError(EXIT_FAIL, msg)
        if any(row["dataset"] == rec["dataset"] for row in rows):
            raise CurateError(EXIT_FAIL, f"line {n}: dataset {rec['dataset']} twice")
        rows.append({"dataset": rec["dataset"], "tier": rec["tier"]})
    return rows


def tiers(catalog: Catalog, rows: Sequence[Mapping[str, str]]) -> Counts:
    """Set `tier` on each named dataset by replacing its full row.

    `CatalogWriter.replace` deletes the key's rows and appends what it is given,
    so the row written is the stored row with only `tier` changed. A dataset
    absent from the catalog refuses the whole load before any write.
    """
    table = catalog.load_table(f"{schema.DATABASE}.{schema.DATASETS}")
    stored = {rec["dataset"]: rec for rec in table.scan().to_arrow().to_pylist()}
    absent = [row["dataset"] for row in rows if row["dataset"] not in stored]
    if absent:
        msg = f"datasets not in the catalog: {', '.join(absent)}"
        raise CurateError(EXIT_FAIL, msg)
    writer = CatalogWriter(catalog)
    replaced = kept = 0
    for row in rows:
        full = stored[row["dataset"]]
        if full.get("tier") == row["tier"]:
            kept += 1
            continue
        key = {"dataset": row["dataset"]}
        writer.replace(schema.DATASETS, key, [{**full, "tier": row["tier"]}])
        replaced += 1
    return Counts(0, replaced, kept)


def _line(table: str, counts: Counts) -> str:
    removed = f", {counts.removed} removed" if counts.removed else ""
    return (
        f"{table}: {counts.added} added, {counts.replaced} replaced, "
        f"{counts.kept} kept{removed}\n"
    )


# -- live wiring ---------------------------------------------------------------


def main() -> None:
    """`import` and `crosswalk` subcommands (admin, sandbox profile)."""
    parser = argparse.ArgumentParser(prog="curate")
    sub = parser.add_subparsers(dest="command", required=True)
    imp = sub.add_parser("import")
    imp.add_argument("--source", required=True)
    imp.add_argument("--include", default="")
    imp.add_argument("--extra", default="")
    walk = sub.add_parser("crosswalk")
    walk.add_argument("--file", required=True)
    for name in ("studies", "tiers"):
        sub.add_parser(name).add_argument("--file", required=True)
    args = parser.parse_args()
    profile, account = require_sandbox()
    session = session_for(profile, account)
    caller = str(session.client("sts").get_caller_identity()["Arn"]).rsplit("/", 1)[-1]
    catalog = glue_catalog(profile)
    try:
        if args.command == "crosswalk":
            rows = crosswalk_rows(Path(args.file).read_text(), curated_by=caller)
            sys.stdout.write(_line("subject_links", crosswalk(catalog, rows)))
            return
        if args.command == "studies":
            path = Path(args.file)
            found = studies_rows(
                path.read_text(),
                (path.parent / "study_datasets.csv").read_text(),
                curated_by=caller,
            )
            first, second = load_studies(catalog, found)
            sys.stdout.write(_line("studies", first) + _line("study_datasets", second))
            return
        if args.command == "tiers":
            rows = tiers_rows(Path(args.file).read_text())
            sys.stdout.write(_line("datasets", tiers(catalog, rows)))
            return
        outputs = stack_outputs(session)
        lam = session.client("lambda")
        table = sessions.SessionTable(
            session.client("dynamodb"), sessions_table_name(session)
        )

        def invoke(event: dict[str, Any]) -> None:
            """Invoke ImportFunction asynchronously with `event`; return nothing."""
            lam.invoke(
                FunctionName=outputs["ImportFunctionName"],
                InvocationType="Event",  # never a synchronous, retried call
                Payload=json.dumps(event).encode(),
            )

        extra = json.loads(Path(args.extra).read_text()) if args.extra else None
        code = import_source(
            ImportRequest(
                source=Source.parse(args.source),
                include=tuple(g for g in args.include.split(",") if g),
                extra=extra,
                caller=caller,
            ),
            CurateDeps(
                web=AnonymousWeb(config.REGION),
                existing=existing_from_catalog(catalog),
                runner=Runner(
                    invoke=invoke, get_session=lambda sid: sessions.get(table, sid)
                ),
            ),
        )
    except CurateError as err:
        sys.stderr.write(f"{err}\n")
        raise SystemExit(err.code) from err
    raise SystemExit(code)


if __name__ == "__main__":
    main()
