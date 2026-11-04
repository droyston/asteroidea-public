"""Live test: any researcher reads a saved result; only its author changes it.

Run with `make live T=tests/live/test_artifacts.py` after the deploy. Every test
touches the sandbox. Sign-in is the headless `sandbox-test` client (conftest `auth` for
researcher A, `researcher_b` for researcher B).

- a: A saves a file with `sources` from a real `query()` and its `snapshot_id`; the
  result is COMMITTED, listed first, and downloads as identical bytes.
- b: B (created by the module, deleted at the end) does not see it in the default
  `mine` listing, finds it with `scope="all"`, downloads the same bytes, and gets
  `NotTheAuthorError` when withdrawing it.
- c: A's own identity-pool credentials (ResearcherRole) cannot read the object: the
  explicit DENY holds.
- d: a finalize after a PUT of different bytes than declared answers 409 INCOMPLETE and
  the result is gone.

Left behind: artifacts have no delete route, so A's artifact objects (the saved file and
its provenance) stay in the artifacts bucket, and the dataset the sample landed in stays
in the catalog. Researcher B is deleted at the end of the module. Every wait is bounded.
"""

import base64
import json
import time
from typing import TYPE_CHECKING, Any, Final, NamedTuple

import pytest
from botocore.exceptions import ClientError

from asteroidea import _crc, _creds, _query, _sample, _upload
from asteroidea._artifacts import Artifacts
from asteroidea._client import PlatformClient, json_of
from asteroidea._http import send
from asteroidea.errors import NotTheAuthorError
from asteroidea_infra.admin import stack_outputs

if TYPE_CHECKING:
    from pathlib import Path

    import boto3

    from asteroidea._auth import Auth

LIST_WITHIN: Final = 30.0
HTTP_OK: Final = 200
HTTP_CREATED: Final = 201
HTTP_BAD_REQUEST: Final = 400
HTTP_FORBIDDEN: Final = 403
HTTP_NOT_FOUND: Final = 404
HTTP_CONFLICT: Final = 409
DENIED: Final = "AccessDenied"


class Saved(NamedTuple):
    """Researcher A's saved result and the bytes it holds."""

    artifact_id: str
    name: str
    data: bytes
    sub: str


def _sub(auth: Auth) -> str:
    """Return the `sub` claim of the signed-in access token."""
    payload = auth.tokens().access_token.split(".")[1]
    padded = payload + "=" * (-len(payload) % 4)
    return str(json.loads(base64.urlsafe_b64decode(padded))["sub"])


def _api(auth: Auth) -> PlatformClient:
    return PlatformClient(auth.config.api_url, auth.tokens().access_token, send)


@pytest.fixture(scope="module")
def bucket(admin_session: boto3.Session) -> str:
    """Return the artifacts bucket's name (a DataStack output)."""
    return stack_outputs(admin_session, "DataStack")["ArtifactsBucketName"]


@pytest.fixture(scope="module")
def saved(auth: Auth, tmp_path_factory: pytest.TempPathFactory) -> Saved:
    """Step a: A queries one real file and saves a result computed from it."""
    dataset, sample = _sample.write_sample("artifact")
    _upload.Uploader(auth).upload(sample, dataset, sandbox=True)
    found = _query.query(auth, dataset=dataset)
    assert found.snapshot_id is not None
    assert len(found.files) == 1
    data = f"result-{time.time_ns()}".encode()
    path: Path = tmp_path_factory.mktemp("artifact") / "result.txt"
    path.write_bytes(data)
    result = Artifacts(auth).save(
        path, sources=found.files, snapshot_id=found.snapshot_id, note="live test"
    )
    return Saved(result.artifact_id, path.name, data, _sub(auth))


def test_a_saved_result_is_committed_listed_first_and_downloads_identically(
    auth: Auth, saved: Saved, tmp_path: Path
) -> None:
    deadline = time.monotonic() + LIST_WITHIN
    listing = Artifacts(auth).listing()
    while not listing and time.monotonic() < deadline:
        time.sleep(1)
        listing = Artifacts(auth).listing()
    assert listing[0].artifact_id == saved.artifact_id
    assert listing[0].state == "COMMITTED"
    assert listing[0].name == saved.name
    target = Artifacts(auth).download(saved.artifact_id, tmp_path)
    assert target.read_bytes() == saved.data


def test_another_researcher_reads_it_but_cannot_change_it(
    researcher_b: Auth, saved: Saved, tmp_path: Path
) -> None:
    theirs = Artifacts(researcher_b)
    assert saved.artifact_id not in [a.artifact_id for a in theirs.listing()]
    shared = theirs.listing(scope="all")
    assert saved.artifact_id in [a.artifact_id for a in shared]
    target = theirs.download(saved.artifact_id, tmp_path)
    assert target.read_bytes() == saved.data
    with pytest.raises(NotTheAuthorError):
        theirs.withdraw(saved.artifact_id)


def test_the_researcher_role_cannot_read_the_object_directly(
    auth: Auth, saved: Saved, bucket: str
) -> None:
    """The ResearcherRole path (identity-pool credentials) meets the explicit DENY."""
    s3 = _creds.session(auth).client("s3")
    prefix = f"artifacts/{saved.sub}/{saved.artifact_id}"
    for key in (f"{prefix}/data/{saved.name}", f"{prefix}/provenance.json"):
        with pytest.raises(ClientError) as raised:
            s3.get_object(Bucket=bucket, Key=key)
        assert raised.value.response["Error"]["Code"] == DENIED
    with pytest.raises(ClientError) as listed:
        s3.list_objects_v2(Bucket=bucket, Prefix=prefix)
    assert listed.value.response["Error"]["Code"] == DENIED


def test_a_put_of_other_bytes_is_refused_by_s3_and_the_result_never_commits(
    auth: Auth, tmp_path: Path
) -> None:
    declared = tmp_path / "declared.txt"
    declared.write_bytes(b"declared-bytes-0001")
    other = tmp_path / "other.txt"
    other.write_bytes(b"different-bytes-002")
    assert declared.stat().st_size == other.stat().st_size
    dataset, sample = _sample.write_sample("artifact-bad")
    _upload.Uploader(auth).upload(sample, dataset, sandbox=True)
    found = _query.query(auth, dataset=dataset)
    assert found.snapshot_id is not None
    api = _api(auth)
    body: dict[str, Any] = {
        "name": declared.name,
        "size": declared.stat().st_size,
        "crc64nvme": _crc.crc64nvme(declared),
        "snapshot_id": found.snapshot_id,
        "sources": [
            {"asset_id": f.asset_id, "s3_version_id": f.s3_version_id}
            for f in found.files
        ],
    }
    created = api.call("POST", "/artifacts", body)
    assert created.status == HTTP_CREATED
    made = json_of(created)
    artifact_id = str(made["artifact_id"])
    # The URL is signed with the declared CRC64NVME, so S3 refuses the other bytes
    # and no object lands (seen live: 400 or 403). Finalize then answers
    # upload_pending, as for a finalize before any upload. The size-or-CRC-mismatch
    # upload_incomplete path
    # is covered by ingest/tests/test_artifacts.py
    # ::test_crc_or_size_mismatch_deletes_the_object_and_answers_409_incomplete.
    refused = send("PUT", str(made["url"]), {_crc.HEADER: _crc.crc64nvme(other)}, other)
    assert refused.status in {HTTP_BAD_REQUEST, HTTP_FORBIDDEN}, refused.status
    done = api.call("POST", f"/artifacts/{artifact_id}/finalize")
    assert done.status == HTTP_CONFLICT
    assert json_of(done).get("error") == "upload_pending"
    gone = api.call("GET", f"/artifacts/{artifact_id}")
    assert gone.status == HTTP_NOT_FOUND
    assert artifact_id not in [
        a.artifact_id for a in Artifacts(auth).listing() if a.state == "COMMITTED"
    ]
