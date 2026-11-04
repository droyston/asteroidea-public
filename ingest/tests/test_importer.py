"""ImportFunction pins."""

import base64
import hashlib
import io
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import IO, TYPE_CHECKING, Any, override

import boto3
import pytest
from botocore.config import Config
from botocore.stub import ANY, Stubber

from asteroidea_infra import curate
from asteroidea_ingest import import_file, owners, sessions
from asteroidea_ingest.import_file import ImportDeps, ImportRefusedError
from asteroidea_ingest.sessions import SessionTable

if TYPE_CHECKING:
    from collections.abc import Iterator

NOW = 5000
PAYLOAD = b"x" * 1000
HEX = hashlib.sha256(PAYLOAD).hexdigest()
B64 = base64.b64encode(hashlib.sha256(PAYLOAD).digest()).decode()
OTHER_B64 = base64.b64encode(hashlib.sha256(b"other").digest()).decode()
QUEUE = "commit-url"


def _s3_event(**overrides: Any) -> dict[str, Any]:
    return {
        "mode": "s3",
        "dataset": "ds004798",
        "relpath": "sub-p62cs/anat/sub-p62cs_T1w.nii.gz",
        "size": 1000,
        "source": {
            "archive": "openneuro",
            "dataset": "ds004798",
            "version": "1.0.5",
            "path": "sub-p62cs/anat/sub-p62cs_T1w.nii.gz",
            "bucket": "openneuro.org",
            "key": "ds004798/sub-p62cs/anat/sub-p62cs_T1w.nii.gz",
            "region": "us-east-1",
        },
        "dataset_meta": {"title": "bmovie fMRI", "license": "CC0"},
        "caller": "curator",
        **overrides,
    }


def _https_event(*, digest: bool = True, **overrides: Any) -> dict[str, Any]:
    source = {
        "archive": "dandi",
        "dataset": "000623",
        "version": "0.240227.2023",
        "path": "sub-CS62/sub-CS62_ses-R2_ecephys+image.nwb",
        "url": "https://dandiarchive.s3.amazonaws.com/blobs/c68/ff6/c68ff62e",
        "region": "us-east-2",
    }
    if digest:
        source["digest"] = f"sha2-256:{HEX}"
    return {
        "mode": "https",
        "dataset": "dandi-000623",
        "relpath": "sub-CS62/sub-CS62_ses-R2_ecephys+image.nwb",
        "size": 1000,
        "source": source,
        "dataset_meta": {"title": "bmovie", "license": "CC-BY-4.0"},
        "caller": "curator",
        **overrides,
    }


def _client(service: str, config: Config | None = None) -> Any:
    return boto3.client(
        service,
        config=config or Config(signature_version="s3v4"),
        region_name="us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )


class Rig:
    """Stubbed clients, a fake HTTPS source and the import deps over them."""

    def __init__(self, harness: Any, body: bytes = PAYLOAD) -> None:
        self.h = harness
        # An import writes to an unclaimed dataset: an empty owners table (fixture).
        self.owners_ddb = type(harness.owners_ddb)()
        self.stream_s3 = _client("s3", import_file.STREAM_CLIENT_CONFIG)
        self.stream_stub = Stubber(self.stream_s3)
        self.stream_stub.activate()
        self.body = body
        self.fetched: list[str] = []
        self.sends: list[dict[str, Any]] = []
        self.updates: list[dict[str, Any]] = []
        harness.sqs.meta.events.register(
            "before-parameter-build.sqs.SendMessage",
            lambda params, **_: self.sends.append(dict(params)),
        )
        harness.ddb.meta.events.register(
            "before-parameter-build.dynamodb.UpdateItem",
            lambda params, **_: self.updates.append(
                sessions.unmarshal(params["ExpressionAttributeValues"])
            ),
        )

    def fetch(self, url: str) -> IO[bytes]:
        self.fetched.append(url)
        return io.BytesIO(self.body)

    def deps(self) -> ImportDeps:
        return ImportDeps(
            table=SessionTable(self.h.ddb, self.h.TABLE),
            s3=self.h.s3,
            stream_s3=self.stream_s3,
            sqs=self.h.sqs,
            bucket="raw-bucket",
            commit_queue_url=QUEUE,
            dataset_owners=owners.Owners(self.owners_ddb, "owners"),
            fetch=self.fetch,
        )

    def run(self, event: dict[str, Any]) -> dict[str, str]:
        return import_file.run_import(event, self.deps(), now=NOW)

    def new_session(self, event: dict[str, Any]) -> str:
        """Stub: no item yet, no raw key, the conditional create succeeds."""
        ev = import_file.ImportEvent.parse(event)
        self.h.ddb_stub.add_response("get_item", {})
        self.h.s3_stub.add_client_error(
            "head_object",
            service_error_code="404",
            expected_params={
                "Bucket": "raw-bucket",
                "Key": f"raw/{ev.dataset}/{ev.relpath}",
            },
        )
        self.h.ddb_stub.add_response(
            "put_item",
            {},
            expected_params={
                "TableName": self.h.TABLE,
                "Item": ANY,
                "ConditionExpression": "attribute_not_exists(session_id)",
            },
        )
        return import_file.session_id(ev.dataset, ev.relpath, ev.source["version"])

    def finalized(self) -> None:
        self.h.ddb_stub.add_response("update_item", {})  # OPEN -> FINALIZING
        self.h.sqs_stub.add_response("send_message", {"MessageId": "m"})

    def finish(self) -> None:
        self.h.finish()
        self.stream_stub.assert_no_pending_responses()


@pytest.fixture
def rig(harness: Any) -> Rig:
    return Rig(harness)


# -- mode s3 ---------------------------------------------------------------------


def test_s3_mode_copies_with_sha256_and_finalizes(rig: Rig) -> None:
    event = _s3_event()
    sid = rig.new_session(event)
    rig.h.s3_stub.add_response(
        "copy_object",
        {"CopyObjectResult": {"ChecksumSHA256": B64}},
        expected_params={
            "Bucket": "raw-bucket",
            "Key": f"landing/{sid}/sub-p62cs/anat/sub-p62cs_T1w.nii.gz",
            "CopySource": {
                "Bucket": "openneuro.org",
                "Key": "ds004798/sub-p62cs/anat/sub-p62cs_T1w.nii.gz",
            },
            "ChecksumAlgorithm": "SHA256",
            "MetadataDirective": "COPY",
            "TaggingDirective": "REPLACE",
        },
    )
    rig.finalized()
    assert rig.run(event) == {"outcome": "FINALIZING", "session_id": sid}
    rig.finish()
    (finalizing,) = rig.updates
    assert finalizing[":state"] == sessions.FINALIZING
    assert finalizing[":expected_checksum"] == {"algorithm": "SHA256", "value": B64}
    assert finalizing[":lease_until"] == NOW + 150
    (send,) = rig.sends
    assert send["MessageDeduplicationId"] == f"{sid}:{finalizing[':finalize_token']}"
    assert rig.fetched == []  # no HTTP in mode s3


def test_s3_mode_digest_mismatch_is_incomplete(rig: Rig) -> None:
    event = _s3_event()
    event["source"]["digest"] = f"sha2-256:{HEX}"
    rig.new_session(event)
    rig.h.s3_stub.add_response(
        "copy_object", {"CopyObjectResult": {"ChecksumSHA256": OTHER_B64}}
    )
    rig.h.ddb_stub.add_response("update_item", {})  # OPEN -> INCOMPLETE
    out = rig.run(event)
    assert out["outcome"] == sessions.INCOMPLETE
    assert out["reason"] == "source digest mismatch"
    rig.finish()  # no send stubbed
    assert rig.updates[0][":error"] == {"reason": "source digest mismatch"}


# -- dataset ownership -------------------------------------------------------------


def test_import_claims_an_unowned_dataset_as_admin(rig: Rig) -> None:
    event = _s3_event()
    rig.new_session(event)
    rig.h.s3_stub.add_response(
        "copy_object", {"CopyObjectResult": {"ChecksumSHA256": B64}}
    )
    rig.finalized()
    assert rig.run(event)["outcome"] == "FINALIZING"
    (put,) = rig.owners_ddb.puts
    assert put["condition"] == "attribute_not_exists(dataset) OR owner_kind = :admin"
    assert rig.owners_ddb.rows["ds004798"]["owner_kind"] == "admin"
    assert rig.owners_ddb.rows["ds004798"]["owner_sub"] == "import:curator"
    rig.finish()


def test_import_into_a_researcher_dataset_is_refused_and_reads_as_a_fail(
    rig: Rig,
) -> None:
    event = _s3_event()
    rig.owners_ddb.rows["ds004798"] = {
        "dataset": "ds004798",
        "owner_sub": "user-1",
        "owner_kind": "researcher",
        "claimed_at": 1,
    }
    rig.h.ddb_stub.add_response("get_item", {})  # no session yet
    rig.h.ddb_stub.add_response("put_item", {})  # the INCOMPLETE session
    written: list[dict[str, Any]] = []
    rig.h.ddb.meta.events.register(
        "before-parameter-build.dynamodb.PutItem",
        lambda params, **_: written.append(sessions.unmarshal(params["Item"])),
    )
    out = rig.run(event)
    assert out["outcome"] == import_file.REFUSED
    assert out["reason"] == "dataset owned by a researcher"
    # No HEAD and no copy: nothing reached S3. The session `make import` polls
    # reads as a FAIL naming the reason.
    rig.finish()
    (item,) = written
    assert item["state"] == sessions.INCOMPLETE
    line = curate.outcome(item)
    assert line is not None
    assert line.startswith("FAIL")
    assert "dataset owned by a researcher" in line
    assert rig.owners_ddb.rows["ds004798"]["owner_kind"] == "researcher"


# -- mode https ------------------------------------------------------------------


def test_https_mode_streams_one_put_with_the_publisher_digest(rig: Rig) -> None:
    event = _https_event()
    sid = rig.new_session(event)
    rig.stream_stub.add_response(
        "put_object",
        {"ChecksumSHA256": B64},
        expected_params={
            "Bucket": "raw-bucket",
            "Key": f"landing/{sid}/sub-CS62/sub-CS62_ses-R2_ecephys+image.nwb",
            "Body": ANY,
            "ContentLength": 1000,
            "ChecksumSHA256": B64,
        },
    )
    rig.finalized()
    assert rig.run(event)["outcome"] == "FINALIZING"
    rig.finish()
    assert rig.fetched == [event["source"]["url"]]
    assert rig.updates[0][":expected_checksum"]["value"] == B64


def test_https_bad_digest_is_incomplete_source_digest_mismatch(rig: Rig) -> None:
    event = _https_event()
    rig.new_session(event)
    rig.stream_stub.add_client_error(
        "put_object",
        service_error_code="BadDigest",
        service_message="The SHA256 you specified did not match the checksum.",
    )
    rig.h.ddb_stub.add_response("update_item", {})
    out = rig.run(event)
    assert (out["outcome"], out["reason"]) == ("INCOMPLETE", "source digest mismatch")
    rig.finish()


def test_https_without_a_digest_spools_and_puts_the_computed_sha256(
    harness: Any,
) -> None:
    rig = Rig(harness, body=PAYLOAD)
    event = _https_event(digest=False, ancillary=True)
    rig.new_session(event)
    rig.stream_stub.add_response(
        "put_object",
        {},
        expected_params={
            "Bucket": "raw-bucket",
            "Key": ANY,
            "Body": ANY,
            "ContentLength": 1000,
            "ChecksumSHA256": B64,
        },
    )
    rig.finalized()
    assert rig.run(event)["outcome"] == "FINALIZING"
    rig.finish()


def test_https_source_longer_than_listed_is_incomplete(harness: Any) -> None:
    rig = Rig(harness, body=PAYLOAD + b"y")
    event = _https_event(digest=False, ancillary=True)
    rig.new_session(event)
    rig.h.ddb_stub.add_response("update_item", {})
    out = rig.run(event)
    assert out["outcome"] == "INCOMPLETE"
    assert "differs from the listed 1000 B" in out["reason"]
    rig.finish()  # no put stubbed


def test_https_size_caps(rig: Rig) -> None:
    over_ancillary = _https_event(
        ancillary=True, size=import_file.ANCILLARY_MAX_SIZE + 1
    )
    assert rig.run(over_ancillary)["outcome"] == import_file.REFUSED
    at_ancillary = import_file.ImportEvent.parse(
        _https_event(ancillary=True, size=import_file.ANCILLARY_MAX_SIZE)
    )
    assert at_ancillary.size == import_file.ANCILLARY_MAX_SIZE
    over_listed = _https_event(size=5 * 1024**3 + 1)
    assert rig.run(over_listed)["outcome"] == import_file.REFUSED
    rig.finish()  # a refusal reads and writes nothing


def test_https_without_a_digest_over_100_mib_is_incomplete(rig: Rig) -> None:
    event = _https_event(digest=False, size=import_file.ANCILLARY_MAX_SIZE + 1)
    rig.new_session(event)
    rig.h.ddb_stub.add_response("update_item", {})
    out = rig.run(event)
    assert out["outcome"] == "INCOMPLETE"
    assert "no publisher digest" in out["reason"]
    rig.finish()


def test_a_refused_redirect_makes_the_import_incomplete(harness: Any) -> None:
    rig = Rig(harness)

    def refuse(url: str) -> IO[bytes]:
        msg = f"refused redirect from {url} to https://elsewhere"
        raise ImportRefusedError(msg)

    event = _https_event()
    rig.new_session(event)
    rig.h.ddb_stub.add_response("update_item", {})
    out = import_file.run_import(event, _with_fetch(rig.deps(), refuse), now=NOW)
    assert out["outcome"] == "INCOMPLETE"
    assert out["reason"].startswith("refused redirect")
    rig.finish()


def _with_fetch(deps: ImportDeps, fetch: Any) -> ImportDeps:
    return ImportDeps(
        table=deps.table,
        s3=deps.s3,
        stream_s3=deps.stream_s3,
        sqs=deps.sqs,
        bucket=deps.bucket,
        commit_queue_url=deps.commit_queue_url,
        dataset_owners=deps.dataset_owners,
        fetch=fetch,
    )


# -- redirects over a real local HTTP server -------------------------------------


class _Redirecting(BaseHTTPRequestHandler):
    target = ""

    def do_GET(self) -> None:
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", self.target)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    @override
    def log_message(self, format: str, *args: object) -> None:  # quiet test server
        return


@pytest.fixture
def server() -> Iterator[int]:
    httpd = HTTPServer(("127.0.0.1", 0), _Redirecting)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd.server_address[1]
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _open(url: str) -> bytes:
    opener = urllib.request.build_opener(import_file.SameHostRedirect)
    with opener.open(url, timeout=5) as resp:
        return bytes(resp.read())


def test_a_same_host_redirect_is_followed(server: int) -> None:
    _Redirecting.target = f"http://127.0.0.1:{server}/final"
    assert _open(f"http://127.0.0.1:{server}/redirect") == b"ok"


def test_a_redirect_to_another_host_is_refused(server: int) -> None:
    _Redirecting.target = f"http://localhost:{server}/final"  # same port, other host
    with pytest.raises(ImportRefusedError, match=r"refused redirect from 127\.0\.0\.1"):
        _open(f"http://127.0.0.1:{server}/redirect")


def test_fetch_refuses_a_non_tls_url() -> None:
    with pytest.raises(ImportRefusedError, match="non-TLS"):
        import_file.fetch_https("http://example.invalid/x")


# -- refusals and idempotency ----------------------------------------------------


def test_an_existing_raw_key_is_refused_and_no_session_is_created(rig: Rig) -> None:
    event = _s3_event()
    rig.h.ddb_stub.add_response("get_item", {})
    rig.h.s3_stub.add_response("head_object", {"ContentLength": 1})
    assert rig.run(event) == {"outcome": "REFUSED", "reason": "raw key already exists"}
    rig.finish()  # no put_item, copy or send was stubbed


def test_invoked_twice_one_session(rig: Rig) -> None:
    """One session item, one FINALIZING transition, one distinct message."""
    event = _s3_event()
    sid = rig.new_session(event)
    rig.h.s3_stub.add_response(
        "copy_object", {"CopyObjectResult": {"ChecksumSHA256": B64}}
    )
    rig.finalized()
    assert rig.run(event)["outcome"] == "FINALIZING"
    token = rig.updates[0][":finalize_token"]
    # The second delivery (Lambda's async retry or a CLI rerun) finds FINALIZING.
    rig.h.get_item(
        session_id=sid,
        state=sessions.FINALIZING,
        finalize_token=token,
    )
    rig.h.sqs_stub.add_response("send_message", {"MessageId": "m2"})
    assert rig.run(event) == {"outcome": "RESENT", "session_id": sid}
    rig.finish()  # exactly one put_item and one copy were stubbed
    assert len(rig.updates) == 1
    assert {s["MessageDeduplicationId"] for s in rig.sends} == {f"{sid}:{token}"}
    assert {s["MessageBody"] for s in rig.sends} == {
        f'{{"finalize_token": "{token}", "session_id": "{sid}"}}'
    }


def test_a_delivery_after_the_commit_does_nothing(rig: Rig) -> None:
    rig.h.get_item(state=sessions.COMMITTED)
    out = rig.run(_s3_event())
    assert (out["outcome"], out["state"]) == (import_file.NOTHING_TO_DO, "COMMITTED")
    rig.finish()


def test_an_open_item_is_moved_again_and_finalized(rig: Rig) -> None:
    event = _s3_event()
    ev = import_file.ImportEvent.parse(event)
    sid = import_file.session_id(ev.dataset, ev.relpath, "1.0.5")
    rig.h.get_item(session_id=sid, state=sessions.OPEN, finalize_token=None)
    rig.h.s3_stub.add_response(
        "copy_object", {"CopyObjectResult": {"ChecksumSHA256": B64}}
    )
    rig.finalized()
    assert rig.run(event)["outcome"] == "FINALIZING"
    rig.finish()  # no raw HEAD and no put_item on a resume


def test_the_session_id_is_deterministic_per_version() -> None:
    a = import_file.session_id("ds", "sub-1/a.nwb", "1")
    assert a == import_file.session_id("ds", "sub-1/a.nwb", "1")
    assert a != import_file.session_id("ds", "sub-1/a.nwb", "2")


def test_the_session_item_carries_import_provenance(rig: Rig) -> None:
    items: list[dict[str, Any]] = []
    rig.h.ddb.meta.events.register(
        "before-parameter-build.dynamodb.PutItem",
        lambda params, **_: items.append(sessions.unmarshal(params["Item"])),
    )
    event = _s3_event()
    rig.new_session(event)
    rig.h.s3_stub.add_response(
        "copy_object", {"CopyObjectResult": {"ChecksumSHA256": B64}}
    )
    rig.finalized()
    rig.run(event)
    (item,) = items
    assert item["owner_sub"] == "import:curator"  # never in a researcher's uploads
    assert item["ingest_method"] == "import"
    assert item["source"]["bucket"] == "openneuro.org"
    assert item["dataset_meta"]["license"] == "CC0"
    assert item["raw_key"] == "raw/ds004798/sub-p62cs/anat/sub-p62cs_T1w.nii.gz"


@pytest.mark.parametrize(
    "bad",
    [
        _s3_event(mode="ftp"),
        _s3_event(dataset="Bad"),
        _s3_event(relpath="../x"),
        _s3_event(source={"version": "1"}),  # s3 mode without bucket and key
        _https_event(source={"version": "1", "url": "http://x"}),
        _s3_event(source={"bucket": "b", "key": "k"}),  # no version
    ],
)
def test_malformed_events_are_refused(rig: Rig, bad: dict[str, Any]) -> None:
    assert rig.run(bad)["outcome"] == import_file.REFUSED
    rig.finish()


# -- client constraints ------------------------------------------------------


def test_stream_client_signs_no_payload_and_never_retries() -> None:
    """Pinned on the resolved client: `max_attempts: 1` would resolve to 2 attempts."""
    client = _client("s3", import_file.STREAM_CLIENT_CONFIG)
    assert client.meta.config.s3 == {"payload_signing_enabled": False}
    assert client.meta.config.retries["total_max_attempts"] == 1


def test_forward_only_body_tells_and_seeks_only_to_the_current_position() -> None:
    body = import_file.ForwardOnlyBody(io.BytesIO(b"abcdef"), limit=6)
    assert body.read(4) == b"abcd"
    assert body.tell() == 4
    assert body.seek(4) == 4
    assert body.seek(0, io.SEEK_CUR) == 4
    with pytest.raises(io.UnsupportedOperation):
        body.seek(0)
    with pytest.raises(io.UnsupportedOperation):
        body.seek(0, io.SEEK_END)


def test_forward_only_body_refuses_a_source_longer_than_listed() -> None:
    body = import_file.ForwardOnlyBody(io.BytesIO(b"abcdefg"), limit=6)
    body.read(6)
    with pytest.raises(ValueError, match="longer than its listed size 6 B"):
        body.read(1)


def test_the_session_item_carries_the_sandbox_tier(rig: Rig) -> None:
    """`commit.dataset_row` reads `tier` from this item's `dataset_meta`."""
    items: list[dict[str, Any]] = []
    rig.h.ddb.meta.events.register(
        "before-parameter-build.dynamodb.PutItem",
        lambda params, **_: items.append(sessions.unmarshal(params["Item"])),
    )
    event = _s3_event(
        dataset_meta={"title": "bmovie fMRI", "license": "CC0", "tier": "sandbox"}
    )
    rig.new_session(event)
    rig.h.s3_stub.add_response(
        "copy_object", {"CopyObjectResult": {"ChecksumSHA256": B64}}
    )
    rig.finalized()
    rig.run(event)
    (item,) = items
    assert item["dataset_meta"]["tier"] == "sandbox"
