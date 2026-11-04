"""Rows R3 to R5 against the sandbox: the first hour, end to end.

A signed-in test researcher uploads the synthetic sample, queries it, reads one slice
lazily and reads the raw object with DuckDB. Every step goes through the SDK's own
code; the sign-in is the `sandbox-test` stand-in of the `auth` fixture.
"""

import os
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

import duckdb
import pytest
from botocore.exceptions import ClientError

from asteroidea import _image, _query, _sample, _upload
from asteroidea._creds import credentials, session
from asteroidea._http import Reply, send
from asteroidea.errors import UploadIncompleteError

if TYPE_CHECKING:
    from collections.abc import Mapping

    import boto3

    from asteroidea._auth import Auth

VISIBLE_WITHIN_S = 60.0
QUERY_SECONDS = 10.0
SLICE_SECONDS = 5.0
SLICE_BYTES = 64 * 1024
SLICE_INDEX = 48
WRONG_CRC = "AAAAAAAAAAA="  # eight zero bytes
ACCEPTED = 202


class Recorder:
    """A transport that remembers when the finalize call was accepted."""

    def __init__(self) -> None:
        self.accepted_at: float | None = None

    def __call__(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | Path | None,
    ) -> Reply:
        reply = send(method, url, headers, body)
        if url.endswith("/finalize") and reply.status == ACCEPTED:
            self.accepted_at = time.monotonic()
        return reply


class First(NamedTuple):
    """The first upload and the seconds from its 202 until its row was visible."""

    dataset: str
    result: _upload.UploadResult
    seconds_to_visible: float
    athena_seconds: float


def athena_count(held: boto3.Session, dataset: str) -> int:
    """Count the `assets` rows of a dataset through Athena, as the researcher."""
    client = held.client("athena")
    qid = client.start_query_execution(
        QueryString="SELECT count(*) FROM assets WHERE dataset = ?",
        ExecutionParameters=[f"'{dataset}'"],
        QueryExecutionContext={"Database": "asteroidea"},
        WorkGroup="asteroidea",
    )["QueryExecutionId"]
    status: dict[str, Any] = {}
    for _ in range(60):
        status = client.get_query_execution(QueryExecutionId=qid)["QueryExecution"][
            "Status"
        ]
        if status["State"] in {"SUCCEEDED", "FAILED", "CANCELLED"}:
            break
        time.sleep(1)
    assert status["State"] == "SUCCEEDED", status
    rows = client.get_query_results(QueryExecutionId=qid)["ResultSet"]["Rows"]
    return int(rows[1]["Data"][0]["VarCharValue"])


@pytest.fixture(scope="module")
def first(auth: Auth) -> First:
    """R3: upload the sample, then time PyIceberg and Athena visibility."""
    dataset, path = _sample.write_sample()
    recorder = Recorder()
    result = _upload.Uploader(auth, transport=recorder).upload(
        path, dataset, sandbox=True
    )
    assert recorder.accepted_at is not None
    seen = _query.query(auth, dataset=dataset)
    visible = time.monotonic() - recorder.accepted_at
    held = session(auth)
    assert athena_count(held, dataset) == 1
    athena = time.monotonic() - recorder.accepted_at
    assert [r["asset_id"] for r in seen.rows] == [result.asset_id]
    return First(dataset, result, visible, athena)


def report(line: str) -> None:
    """Write a measured number to the run output and to `measurements.txt`."""
    sys.stdout.write(line + "\n")
    scratch = os.environ.get("ASTEROIDEA_LIVE_SCRATCH")
    if scratch is not None and scratch != "":
        with (Path(scratch) / "measurements.txt").open("a") as handle:
            handle.write(line + "\n")


def test_r3_upload_and_q4_visible_within_60_seconds(first: First) -> None:
    report(
        f"R3: 202 to visible in PyIceberg {first.seconds_to_visible:.1f} s, "
        f"in Athena {first.athena_seconds:.1f} s; asset {first.result.asset_id}"
    )
    assert first.athena_seconds <= VISIBLE_WITHIN_S


def test_r4_q3_snapshot_read_ignores_later_rows(auth: Auth, first: First) -> None:
    started = time.monotonic()
    q1 = _query.query(auth, datatype="anat", subject="01")
    q1_seconds = time.monotonic() - started
    assert first.dataset in {r["dataset"] for r in q1.rows}
    assert q1.snapshot_id is not None

    ds2, path2 = _sample.write_sample()
    _upload.Uploader(auth).upload(path2, ds2, sandbox=True)

    started = time.monotonic()
    q2 = _query.query(auth, datatype="anat", subject="01")
    q2_seconds = time.monotonic() - started
    q3 = _query.query(auth, snapshot_id=q1.snapshot_id, datatype="anat", subject="01")
    report(
        f"R4: q1 {q1_seconds:.1f} s, q2 {q2_seconds:.1f} s; rows q1={len(q1.rows)} "
        f"q2={len(q2.rows)} q3={len(q3.rows)}; snapshots {q1.snapshot_id} -> "
        f"{q2.snapshot_id}"
    )
    assert max(q1_seconds, q2_seconds) <= QUERY_SECONDS
    assert q3.rows == q1.rows
    assert ds2 in {r["dataset"] for r in q2.rows}
    assert ds2 not in {r["dataset"] for r in q3.rows}
    assert q3.snapshot_id == q1.snapshot_id != q2.snapshot_id


def test_r5_one_slice_reads_at_most_64_kib(auth: Auth, first: First) -> None:
    started = time.monotonic()
    image: Any = _image.open_image(auth, first.result.asset_id)
    got = image.dataobj[:, :, SLICE_INDEX]
    seconds = time.monotonic() - started
    remote = image.extra["remote_file"]
    report(
        f"R5: slice in {seconds:.2f} s, {remote.bytes_fetched} bytes in "
        f"{remote.requests} requests of {remote.source.size}"
    )
    numpy = _sample.optional("numpy")
    voxels: Any = _sample.sample_array()
    assert numpy.array_equal(got, voxels[:, :, SLICE_INDEX])
    assert seconds <= SLICE_SECONDS
    assert remote.bytes_fetched <= SLICE_BYTES


def test_duckdb_reads_the_raw_object(auth: Auth, first: First) -> None:
    ref = _query.find_asset(
        _query.open_catalog(auth).load_table(_query.TABLE), first.result.asset_id
    )
    held = credentials(auth)
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute(
        "CREATE SECRET (TYPE S3, KEY_ID ?, SECRET ?, SESSION_TOKEN ?, REGION ?)",
        [held.access_key_id, held.secret_key, held.session_token, auth.config.region],
    )
    size = con.execute(
        "SELECT sum(octet_length(content)) FROM read_blob(?)",
        [f"s3://{auth.config.raw_bucket}/{ref.s3_key}"],
    ).fetchone()
    report(f"DuckDB object read: {size} bytes of {ref.s3_key}")
    assert size == (ref.size_bytes,)


def test_q2_wrong_checksum_leaves_nothing_readable(auth: Auth) -> None:
    dataset, path = _sample.write_sample("q2")
    uploader = _upload.Uploader(auth)
    session_ = uploader.open_session(
        path, dataset, checksum=WRONG_CRC, batch=_upload.sandbox_batch()
    )
    try:
        uploader.send_file(session_, path)
        put_result = "accepted by the store"
    except UploadIncompleteError:
        put_result = "refused by the store"
    accepted = uploader.finalize(session_.session_id)
    assert accepted.status == ACCEPTED
    state: dict[str, Any] = {}
    for _ in range(60):
        state = uploader.state(session_.session_id)
        if state["state"] != "FINALIZING":
            break
        time.sleep(1)
    held = session(auth)
    key = f"raw/{dataset}/{session_.relpath}"
    with pytest.raises(ClientError) as missing:
        held.client("s3").head_object(Bucket=auth.config.raw_bucket, Key=key)
    rows = _query.query(auth, dataset=dataset).rows
    report(
        f"PUT {put_result}; session {state['state']} ({state.get('error')}); "
        f"HeadObject raw -> {missing.value.response['Error']['Code']}; rows {len(rows)}"
    )
    assert state["state"] == "INCOMPLETE"
    assert missing.value.response["Error"]["Code"] in {"404", "NoSuchKey", "NotFound"}
    assert rows == []
    assert athena_count(held, dataset) == 0
