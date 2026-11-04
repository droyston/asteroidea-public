"""Live tests: concurrency, reclaim and import idempotency.

Run with `make live T=tests/live/test_ingest_core.py`. Every test touches the sandbox.
Labelled seam of `test_reclaim_brings_a_stuck_session_to_committed`: one admin
DynamoDB write manufactures the stuck state; the crash itself is not reproduced.
The `make reclaim` and `make import` targets are run as the modules they invoke
(`python -m asteroidea_infra.reclaim` and `.curate`) through their `main()`, in this
process, because a subprocess call needs a ruff S603 suppression the repo forbids.
"""

import contextlib
import importlib
import io
import itertools
import os
import re
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, NamedTuple

import pytest

from asteroidea import _sample, _upload
from asteroidea._creds import session as researcher_session
from asteroidea_infra import reclaim
from asteroidea_infra.admin import stack_outputs
from asteroidea_ingest import sessions

if TYPE_CHECKING:
    import boto3

    from asteroidea._auth import Auth

REPO: Final = Path(__file__).resolve().parents[2]
CONCURRENT: Final = 10
DONE_WITHIN: Final = 600.0
RECLAIM_WITHIN: Final = 300.0
LOGS_WITHIN: Final = 240.0
CS62_SOURCE: Final = "dandi:000623@0.240227.2023"
CS62_INCLUDE: Final = "sub-CS62/*P62CSR2*"
CS62_DATASET: Final = "dandi-000623"
CS62_SESSION: Final = "8d420796-2980-54da-a85b-455099c2f625"
EXTRA_MANIFEST: Final = str(REPO / "curation/imports/000623-stimuli.json")
REPORT_LINE: Final = re.compile(r"^REPORT RequestId: (\S+)\s+Duration: ([\d.]+) ms")
INIT_LINE: Final = re.compile(r"Init Duration: ([\d.]+) ms")
START_LINE: Final = re.compile(r"^START RequestId: (\S+)")


def report(line: str) -> None:
    """Write a measured number to the run output and to `measurements.txt`."""
    sys.stdout.write(line + "\n")
    scratch = os.environ.get("ASTEROIDEA_LIVE_SCRATCH")
    if scratch is not None and scratch != "":
        with (Path(scratch) / "measurements.txt").open("a") as handle:
            handle.write(line + "\n")


class Cli(NamedTuple):
    """The outcome of one admin command: exit code and both streams."""

    code: int
    out: str
    err: str


def run_cli(module: str, argv: list[str], log_name: str) -> Cli:
    """Run `python -m <module> <argv>` in this process; keep its output in the log."""
    out, err = io.StringIO(), io.StringIO()
    saved = sys.argv
    sys.argv = [module, *argv]
    code = 0
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                importlib.import_module(module).main()
            except SystemExit as stop:
                code = stop.code if isinstance(stop.code, int) else 1
    finally:
        sys.argv = saved
    scratch = os.environ.get("ASTEROIDEA_LIVE_SCRATCH")
    if scratch is not None and scratch != "":
        (Path(scratch) / log_name).write_text(
            f"$ python -m {module} {' '.join(argv)}\n{out.getvalue()}\n"
            f"--- stderr\n{err.getvalue()}\nEXIT={code}\n"
        )
    return Cli(code, out.getvalue(), err.getvalue())


class Live(NamedTuple):
    """Clients and names the tests share."""

    admin: boto3.Session
    outputs: dict[str, str]
    table: sessions.SessionTable
    commit_fn: str
    extract_fn: str


@pytest.fixture(scope="module")
def live(admin_session: boto3.Session) -> Live:
    """Admin clients plus the deployed names (sandbox profile only)."""
    outputs = stack_outputs(admin_session)
    table = sessions.SessionTable(
        admin_session.client("dynamodb"), reclaim.sessions_table_name(admin_session)
    )
    names: list[str] = []
    pages = admin_session.client("lambda").get_paginator("list_functions")
    for page in pages.paginate():
        names.extend(f["FunctionName"] for f in page["Functions"])
    extract = [n for n in names if "ExtractFunction" in n]
    assert len(extract) == 1, extract
    return Live(
        admin_session, outputs, table, outputs["CommitFunctionName"], extract[0]
    )


def item_of(live_: Live, sid: str) -> dict[str, Any]:
    """Read one session item (strongly consistent)."""
    found = sessions.get(live_.table, sid)
    assert found is not None, sid
    return found


def wait_for(
    live_: Live, sids: list[str], *, extracted: bool, within: float
) -> dict[str, float]:
    """Poll until each session is COMMITTED (and EXTRACTED); return seconds taken."""
    started = time.monotonic()
    done: dict[str, float] = {}
    while len(done) < len(sids):
        pending: dict[str, tuple[Any, Any]] = {}
        for sid in sids:
            if sid in done:
                continue
            item = item_of(live_, sid)
            assert item["state"] != sessions.INCOMPLETE, item
            assert item.get("extract_state") != "FAILED", item
            ready = item["state"] == sessions.COMMITTED and (
                not extracted or item.get("extract_state") == "EXTRACTED"
            )
            if ready:
                done[sid] = time.monotonic() - started
            else:
                pending[sid] = (item["state"], item.get("extract_state"))
        assert time.monotonic() - started < within or not pending, pending
        time.sleep(3)
    return done


def athena_count(client: Any, dataset: str) -> int:
    """Count the `assets` rows of one dataset through Athena."""
    qid = client.start_query_execution(
        QueryString="SELECT count(*) FROM assets WHERE dataset = ?",
        ExecutionParameters=[f"'{dataset}'"],
        QueryExecutionContext={"Database": "asteroidea"},
        WorkGroup="asteroidea",
    )["QueryExecutionId"]
    status: dict[str, Any] = {}
    for _ in range(90):
        status = client.get_query_execution(QueryExecutionId=qid)["QueryExecution"][
            "Status"
        ]
        if status["State"] in {"SUCCEEDED", "FAILED", "CANCELLED"}:
            break
        time.sleep(1)
    assert status["State"] == "SUCCEEDED", status
    rows = client.get_query_results(QueryExecutionId=qid)["ResultSet"]["Rows"]
    return int(rows[1]["Data"][0]["VarCharValue"])


def athena_counts(auth: Auth, datasets: list[str]) -> dict[str, int]:
    """Count `assets` rows per dataset through Athena, as the researcher."""
    client = researcher_session(auth).client("athena")
    return {d: athena_count(client, d) for d in datasets}


def dlq_depths(live_: Live) -> dict[str, int]:
    """Return visible plus in-flight messages of both DLQs."""
    sqs = live_.admin.client("sqs")
    out: dict[str, int] = {}
    for kind, key in reclaim.DLQ_OUTPUTS.items():
        attrs = sqs.get_queue_attributes(
            QueueUrl=live_.outputs[key],
            AttributeNames=[
                "ApproximateNumberOfMessages",
                "ApproximateNumberOfMessagesNotVisible",
            ],
        )["Attributes"]
        out[kind] = int(attrs["ApproximateNumberOfMessages"]) + int(
            attrs["ApproximateNumberOfMessagesNotVisible"]
        )
    return out


class Invocation(NamedTuple):
    """One Lambda invocation from its START and REPORT log lines (epoch ms)."""

    request_id: str
    start_ms: int
    end_ms: int
    duration_ms: float
    init_ms: float | None


def invocations(live_: Live, function: str, since_ms: int) -> list[Invocation]:
    """Pair START and REPORT lines of `function` since `since_ms`."""
    logs = live_.admin.client("logs")
    starts: dict[str, int] = {}
    ends: dict[str, tuple[int, float, float | None]] = {}
    pages = logs.get_paginator("filter_log_events").paginate(
        logGroupName=f"/aws/lambda/{function}",
        startTime=since_ms,
        filterPattern="?START ?REPORT",
    )
    for page in pages:
        for event in page["events"]:
            text = event["message"]
            if m := START_LINE.match(text):
                starts[m.group(1)] = int(event["timestamp"])
            elif m := REPORT_LINE.match(text):
                init = INIT_LINE.search(text)
                ends[m.group(1)] = (
                    int(event["timestamp"]),
                    float(m.group(2)),
                    float(init.group(1)) if init else None,
                )
    return sorted(
        (
            Invocation(rid, starts[rid], end, dur, init)
            for rid, (end, dur, init) in ends.items()
            if rid in starts
        ),
        key=lambda i: i.start_ms,
    )


def overlaps(found: list[Invocation]) -> list[tuple[Invocation, Invocation]]:
    """Return adjacent pairs whose intervals overlap (touching at one ms is fine)."""
    return [(a, b) for a, b in itertools.pairwise(found) if b.start_ms < a.end_ms]


def concurrent_max(live_: Live, function: str, since_ms: int) -> float | None:
    """Return the max `ConcurrentExecutions` datapoint since `since_ms`, or None."""
    cw = live_.admin.client("cloudwatch")
    stamp = "%Y-%m-%dT%H:%M:%SZ"
    stats = cw.get_metric_statistics(
        Namespace="AWS/Lambda",
        MetricName="ConcurrentExecutions",
        Dimensions=[{"Name": "FunctionName", "Value": function}],
        StartTime=time.strftime(stamp, time.gmtime(since_ms / 1000 - 60)),
        EndTime=time.strftime(stamp, time.gmtime(time.time() + 120)),
        Period=60,
        Statistics=["Maximum"],
    )["Datapoints"]
    peaks = [float(p["Maximum"]) for p in stats]
    return max(peaks) if peaks else None


def test_ten_concurrent_finalizes(auth: Auth, live: Live) -> None:
    """G-conc: 10 finalizes give 10 rows, 10 EXTRACTED, empty DLQs, no overlap."""
    window_ms = int(time.time() * 1000) - 5000
    samples = [_sample.write_sample("conc") for _ in range(CONCURRENT)]
    uploader = _upload.Uploader(auth)
    opened = [
        uploader.open_session(path, ds, batch=_upload.sandbox_batch())
        for ds, path in samples
    ]
    for held, (_, path) in zip(opened, samples, strict=True):
        uploader.send_file(held, path)
    gate = threading.Barrier(CONCURRENT)

    def fire(sid: str) -> int:
        gate.wait()
        return uploader.finalize(sid).status

    sids = [s.session_id for s in opened]
    with ThreadPoolExecutor(max_workers=CONCURRENT) as pool:
        statuses = list(pool.map(fire, sids))
    assert statuses == [202] * CONCURRENT, statuses

    took = wait_for(live, sids, extracted=True, within=DONE_WITHIN)
    ordered = sorted(took.values())
    report(
        f"G-conc: {CONCURRENT} COMMITTED+EXTRACTED; last {ordered[-1]:.0f} s after the "
        f"finalizes, median {ordered[CONCURRENT // 2]:.0f} s"
    )

    datasets = [ds for ds, _ in samples]
    counts = athena_counts(auth, datasets)
    asset_ids = {item_of(live, sid)["asset_id"] for sid in sids}
    report(f"G-conc: assets rows per dataset {counts}; {len(asset_ids)} asset ids")
    assert counts == dict.fromkeys(datasets, 1)
    assert len(asset_ids) == CONCURRENT

    depths = dlq_depths(live)
    report(f"G-conc: DLQ depths {depths}")
    assert depths == {"commit": 0, "extract": 0}

    found: dict[str, list[Invocation]] = {}
    deadline = time.monotonic() + LOGS_WITHIN
    while True:
        found = {
            kind: invocations(live, fn, window_ms)
            for kind, fn in (("commit", live.commit_fn), ("extract", live.extract_fn))
        }
        if all(len(v) >= CONCURRENT for v in found.values()):
            break
        assert time.monotonic() < deadline, {k: len(v) for k, v in found.items()}
        time.sleep(10)
    for kind, invs in found.items():
        clash = overlaps(invs)
        longest = max(i.duration_ms for i in invs)
        report(
            f"G-conc: {kind} writer {len(invs)} START-to-REPORT intervals, "
            f"{len(clash)} overlaps, longest {longest:.0f} ms"
        )
        assert not clash, clash
        colds = [(i.request_id, i.init_ms) for i in invs if i.init_ms is not None]
        report(f"G-conc: {kind} cold starts in the window (Init Duration ms): {colds}")
    for kind, fn in (("commit", live.commit_fn), ("extract", live.extract_fn)):
        peak = concurrent_max(live, fn, window_ms)
        report(f"G-conc: {kind} ConcurrentExecutions max {peak} (None: no datapoint)")
        if peak is not None:
            assert peak <= 1


def test_reclaim_brings_a_stuck_session_to_committed(auth: Auth, live: Live) -> None:
    """Live reclaim proof: one admin write manufactures the stuck session."""
    ds, path = _sample.write_sample("reclaim")
    uploader = _upload.Uploader(auth)
    opened = uploader.open_session(path, ds, batch=_upload.sandbox_batch())
    uploader.send_file(opened, path)
    sid = opened.session_id
    open_item = item_of(live, sid)
    assert open_item["state"] == sessions.OPEN
    head = live.admin.client("s3").head_object(
        Bucket=auth.config.raw_bucket, Key=open_item["landing_key"]
    )
    now = int(time.time())
    # THE SEAM (labelled): the one admin write. State ACCEPTED, a matching token, the
    # landing version, and a lease still in the future (so FORCE=1 has work to do); no
    # raw object, no catalog row. The crash that leaves this state is not reproduced.
    live.table.ddb.update_item(
        TableName=live.table.name,
        Key=sessions.marshal({"session_id": sid}),
        UpdateExpression=(
            "SET #s = :accepted, finalize_token = :t, landing_version_id = :v, "
            "lease_until = :lease, updated_at = :now"
        ),
        ConditionExpression="#s = :open",
        ExpressionAttributeNames={"#s": "state"},
        ExpressionAttributeValues=sessions.marshal(
            {
                ":accepted": sessions.ACCEPTED,
                ":open": sessions.OPEN,
                ":t": uuid.uuid4().hex,
                ":v": head["VersionId"],
                ":lease": now + 3600,
                ":now": now,
            }
        ),
    )
    plain = run_cli(
        "asteroidea_infra.reclaim", ["--session", sid], "reclaim-without-force.txt"
    )
    report(f"reclaim without FORCE: exit {plain.code} ({plain.err.strip()[:120]})")
    assert plain.code == 1, plain
    assert item_of(live, sid)["state"] == sessions.ACCEPTED
    forced = run_cli(
        "asteroidea_infra.reclaim", ["--session", sid, "--force"], "reclaim-force.txt"
    )
    report(f"reclaim FORCE=1: exit {forced.code}; {forced.out.strip()[-300:]}")
    assert forced.code == 0, forced
    assert "FORCE" in forced.out
    took = wait_for(live, [sid], extracted=False, within=RECLAIM_WITHIN)
    final = item_of(live, sid)
    counts = athena_counts(auth, [ds])
    report(
        f"reclaim: {sid} COMMITTED {took[sid]:.0f} s after FORCE; extract_state "
        f"{final.get('extract_state')}; rows {counts}"
    )
    assert counts == {ds: 1}


def import_summary(done: Cli) -> str:
    """Return the `summary:` line of an import run (or its whole output)."""
    lines = [x for x in done.out.splitlines() if x.startswith("summary:")]
    return lines[-1] if lines else done.out + done.err


def dataset_session_count(live_: Live, dataset: str) -> int:
    """Count session items of `dataset` (the table is small; admin scan)."""
    items = reclaim.scan_items(live_.table)
    return sum(1 for i in items if i.get("dataset") == dataset)


def import_args(*, extra: bool) -> list[str]:
    """Return the `curate import` arguments for the CS62 R2 file (and the ancillary)."""
    args = ["import", "--source", CS62_SOURCE, "--include", CS62_INCLUDE]
    return [*args, "--extra", EXTRA_MANIFEST] if extra else args


def test_reimport_of_cs62_is_a_no_op(auth: Auth, live: Live) -> None:
    """Idempotent import: CS62 R2 reports "already imported"; no session is created."""
    before = dataset_session_count(live, CS62_DATASET)
    rows_before = athena_counts(auth, [CS62_DATASET])
    done = run_cli(
        "asteroidea_infra.curate", import_args(extra=False), "import-cs62.txt"
    )
    report(f"import CS62 again: exit {done.code}; {import_summary(done)}")
    assert done.code == 0, done
    assert "already imported" in done.out
    assert "(1 already imported), 0 FAIL" in done.out
    assert dataset_session_count(live, CS62_DATASET) == before
    assert athena_counts(auth, [CS62_DATASET]) == rows_before
    item = item_of(live, CS62_SESSION)
    assert item["state"] == sessions.COMMITTED
    assert item["extract_state"] == "EXTRACTED"


def test_https_ancillary_import_and_rerun_no_op(auth: Auth, live: Live) -> None:
    """One small HTTPS ancillary file imports once; the rerun creates nothing."""
    before = dataset_session_count(live, CS62_DATASET)
    first = run_cli(
        "asteroidea_infra.curate", import_args(extra=True), "import-ancillary-1.txt"
    )
    mid = dataset_session_count(live, CS62_DATASET)
    report(
        f"import ancillary: exit {first.code}; {import_summary(first)}; "
        f"sessions {before} -> {mid}"
    )
    assert first.code == 0, first
    assert "0 FAIL" in first.out
    assert mid - before in {0, 1}
    rows_mid = athena_counts(auth, [CS62_DATASET])
    second = run_cli(
        "asteroidea_infra.curate", import_args(extra=True), "import-ancillary-2.txt"
    )
    after = dataset_session_count(live, CS62_DATASET)
    rows_after = athena_counts(auth, [CS62_DATASET])
    report(
        f"import ancillary rerun: exit {second.code}; {import_summary(second)}; "
        f"sessions {mid} -> {after}; rows {rows_mid} -> {rows_after}"
    )
    assert second.code == 0, second
    assert "(2 already imported), 0 FAIL" in second.out
    assert after == mid
    assert rows_after == rows_mid
