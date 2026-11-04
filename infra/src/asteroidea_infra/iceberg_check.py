"""Round-trip check on the one catalog (live only): append, snapshot read-back, DDL.

Run with `make live-iceberg-check`. Refuses without the sandbox env (exit 4). It is
an admin tool: it creates the `s1_probe` Iceberg table and leaves it behind.
ResearcherRole cannot read `s1_probe`; its Glue grant names the catalog tables only.
Drop the table by hand after a run (`aws glue delete-table`); nothing here drops it.
"""

import argparse
import sys
import time
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import boto3
import pyarrow as pa

from asteroidea_infra import athena, schema
from asteroidea_infra.catalog_init import (
    create_table,
    glue_catalog,
    iceberg_schema,
    warehouse_bucket_name,
)
from asteroidea_infra.sandbox_guard import require_sandbox

if TYPE_CHECKING:
    from boto3 import Session

PROBE = schema.PROBE_TABLE
ROWS = 3
POLL_SECONDS = 2
POLL_LIMIT = 60


def _query(session: Session, sql: str) -> list[list[str]]:
    athena_client = session.client("athena")
    qid = athena_client.start_query_execution(
        QueryString=sql,
        QueryExecutionContext={"Database": schema.DATABASE},
        WorkGroup=athena.WORKGROUP,
    )["QueryExecutionId"]
    for _ in range(POLL_LIMIT):
        state = athena_client.get_query_execution(QueryExecutionId=qid)[
            "QueryExecution"
        ]["Status"]
        if state["State"] in {"SUCCEEDED", "FAILED", "CANCELLED"}:
            break
        time.sleep(POLL_SECONDS)
    if state["State"] != "SUCCEEDED":
        msg = f"athena query {state['State']}: {sql}: {state.get('StateChangeReason')}"
        raise SystemExit(msg)
    rows = athena_client.get_query_results(QueryExecutionId=qid)["ResultSet"]["Rows"]
    return [[c.get("VarCharValue", "") for c in r["Data"]] for r in rows[1:]]


def _rows() -> pa.Table:
    now = datetime.now(UTC).replace(tzinfo=None)
    data: list[dict[str, Any]] = [
        {
            "asset_id": str(uuid.uuid4()),
            "session_id": str(uuid.uuid4()),
            "dataset": "s1-probe",
            "s3_key": f"raw/s1-probe/{i}.nii",
            "s3_version_id": "v",
            "size_bytes": i,
            "checksum_crc64nvme": "x",
            "subject": None,
            "bids_session": None,
            "datatype": None,
            "suffix": None,
            "extension": ".nii",
            "committed_at": now,
        }
        for i in range(ROWS)
    ]
    return pa.Table.from_pylist(data, schema=iceberg_schema().as_arrow())


def main() -> None:
    """Run the check: append 3 rows, read at the snapshot, ADD COLUMNS."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", default=None)
    args = parser.parse_args()
    profile, account = require_sandbox()
    profile = args.profile or profile
    session = boto3.Session(profile_name=profile, region_name="us-east-1")
    got = session.client("sts").get_caller_identity()["Account"]
    if got != account:
        msg = f"profile {profile} is account {got}, expected {account}"
        raise SystemExit(msg)
    catalog = glue_catalog(profile)
    table = create_table(catalog, warehouse_bucket_name(session), PROBE)
    table.append(_rows())
    snap = table.refresh().current_snapshot()
    if snap is None:
        msg = "append produced no snapshot"
        raise SystemExit(msg)
    sql = " ".join(
        ["SELECT count(*) FROM s1_probe FOR VERSION AS OF", str(snap.snapshot_id)],
    )
    count = _query(session, sql)
    sys.stdout.write(f"1 append ok; 2 count at snapshot {snap.snapshot_id}: {count}\n")
    _query(session, f"ALTER TABLE {PROBE} ADD COLUMNS (probe_note string)")
    sys.stdout.write("3 ADD COLUMNS ok; 4 idle cost: see pricing (Glue free tier)\n")


if __name__ == "__main__":
    main()
