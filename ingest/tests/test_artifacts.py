"""Artifact routes: refusals, the 404 for a non-author, the finalize check."""

import functools
import io
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import parse_qs, urlsplit

import botocore.session
import pytest
from botocore.exceptions import ClientError

from asteroidea_ingest import artifacts
from asteroidea_ingest.artifacts import ArtifactsApi, validate_create

from .conftest import CRC, OTHER_CRC, jwt_event

if TYPE_CHECKING:
    from asteroidea_ingest.aws_types import S3

BUCKET = "artifacts-bucket"
NOW = 5_000
SOURCE = {"asset_id": "a-1", "s3_version_id": "v-1"}
GOOD: dict[str, Any] = {
    "name": "psth.png",
    "size": 1234,
    "crc64nvme": CRC,
    "snapshot_id": 42,
    "sources": [SOURCE],
}
MISSING = ClientError({"Error": {"Code": "404", "Message": ""}}, "HeadObject")
S3_MAX_KEY_BYTES = 1024
EMAIL = "u1@example.org"


@functools.cache
def real_presigner() -> Any:
    """Return a real botocore S3 client with dummy keys: it presigns offline.

    Presigning validates `Params` against the S3 model, so the fake refuses a
    parameter botocore refuses (ParamValidationError).
    """
    return botocore.session.get_session().create_client(
        "s3",
        region_name="us-east-1",
        aws_access_key_id="test-key",
        aws_secret_access_key="test-secret",
        config=artifacts.S3_CLIENT_CONFIG,
    )


@dataclass
class FakeBucket:
    """An in-memory artifacts bucket: objects, their heads, and the calls made."""

    objects: dict[str, bytes] = field(default_factory=dict)
    heads: dict[str, dict[str, Any]] = field(default_factory=dict)
    deleted: list[str] = field(default_factory=list)
    listed: list[str] = field(default_factory=list)
    presigned: list[dict[str, Any]] = field(default_factory=list)
    signed: list[str] = field(default_factory=list)
    head_error: ClientError | None = None

    def put_object(self, **kw: Any) -> dict[str, Any]:
        # S3 refuses a key over 1024 bytes of UTF-8 (KeyTooLongError).
        if len(kw["Key"].encode()) > S3_MAX_KEY_BYTES:
            raise ClientError(
                {"Error": {"Code": "KeyTooLongError", "Message": ""}}, "PutObject"
            )
        if not isinstance(kw["Body"], bytes):
            msg = "this fake stores bytes bodies only"
            raise TypeError(msg)
        self.objects[kw["Key"]] = kw["Body"]
        return {}

    def get_object(self, **kw: Any) -> dict[str, Any]:
        if kw["Key"] not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey", "Message": ""}}, "Get")
        return {"Body": io.BytesIO(self.objects[kw["Key"]])}

    def head_object(self, **kw: Any) -> dict[str, Any]:
        if self.head_error is not None:
            raise self.head_error
        if kw["Key"] not in self.heads:
            raise MISSING
        return self.heads[kw["Key"]]

    def delete_object(self, **kw: Any) -> dict[str, Any]:
        self.deleted.append(kw["Key"])
        self.heads.pop(kw["Key"], None)
        self.objects.pop(kw["Key"], None)
        return {}

    def list_objects_v2(self, **kw: Any) -> dict[str, Any]:
        # S3 lists in key order, at most MaxKeys (default and cap 1000) per page.
        self.listed.append(kw["Prefix"])
        limit = min(kw.get("MaxKeys", 1000), 1000)
        start = int(kw.get("ContinuationToken", "0"))
        keys = [
            (i, k)
            for i, k in enumerate(sorted(self.objects))
            if k.startswith(kw["Prefix"])
        ]
        page = keys[start : start + limit]
        out: dict[str, Any] = {
            "Contents": [{"Key": k, "LastModified": i} for i, k in page]
        }
        if start + limit < len(keys):
            out["NextContinuationToken"] = str(start + limit)
        return out

    def generate_presigned_url(self, **kw: Any) -> str:
        # The real presigner refuses a parameter the S3 model does not have.
        self.signed.append(real_presigner().generate_presigned_url(**kw))
        self.presigned.append(kw)
        return f"https://signed.example/{kw['Params']['Key']}"


def api(s3: FakeBucket) -> ArtifactsApi:
    return ArtifactsApi(s3=cast("S3", s3), bucket=BUCKET)


def send(
    s3: FakeBucket, event: dict[str, Any], email: str | None
) -> tuple[int, dict[str, Any]]:
    if email is not None:  # an ID token carries `email`; an access token does not
        event["requestContext"]["authorizer"]["jwt"]["claims"]["email"] = email
    resp = api(s3).handle(event, now=NOW)
    return int(resp["statusCode"]), json.loads(resp["body"])


def call(
    s3: FakeBucket, route: str, *, sub: str = "u1", body: object = None, aid: Any = None
) -> tuple[int, dict[str, Any]]:
    return send(s3, jwt_event(route, sub=sub, body=body, sid=aid), EMAIL)


def create_as(
    s3: FakeBucket, body: object, email: str | None
) -> tuple[int, dict[str, Any]]:
    return send(s3, jwt_event("POST /artifacts", sub="u1", body=body), email)


def create(s3: FakeBucket, sub: str = "u1", **over: Any) -> str:
    status, out = call(s3, "POST /artifacts", sub=sub, body={**GOOD, **over})
    assert status == 201
    return str(out["artifact_id"])


def upload(s3: FakeBucket, sub: str, aid: str, **head: Any) -> str:
    key = f"artifacts/{sub}/{aid}/data/psth.png"
    s3.heads[key] = {"ContentLength": 1234, "ChecksumCRC64NVME": CRC, **head}
    return key


def test_create_writes_pending_record_and_presigns_put() -> None:
    s3 = FakeBucket()
    status, out = call(
        s3, "POST /artifacts", body={**GOOD, "note": "n", "sdk_version": "0.1"}
    )
    assert status == 201
    aid = out["artifact_id"]
    assert out["expires_at"] == NOW + 3600
    key = f"artifacts/u1/{aid}/data/psth.png"
    assert out["url"] == f"https://signed.example/{key}"
    assert s3.presigned == [
        {
            "ClientMethod": "put_object",
            "Params": {"Bucket": BUCKET, "Key": key, "ChecksumCRC64NVME": CRC},
            "ExpiresIn": 3600,
        }
    ]
    record = json.loads(s3.objects[f"artifacts/u1/{aid}/provenance.json"])
    # author_email from the ID token; data_key fixed here
    assert record == {
        "artifact_id": aid,
        "author_sub": "u1",
        "author_email": EMAIL,
        "data_key": key,
        "name": "psth.png",
        "size": 1234,
        "crc64nvme": CRC,
        "content_type": "application/octet-stream",
        "snapshot_id": 42,
        "sources": [SOURCE],
        "note": "n",
        "sdk_version": "0.1",
        "state": "PENDING",
        "created_at": NOW,
        "finalized_at": None,
    }


def test_content_type_is_kept_when_given() -> None:
    s3 = FakeBucket()
    aid = create(s3, content_type="image/png")
    record = json.loads(s3.objects[f"artifacts/u1/{aid}/provenance.json"])
    assert record["content_type"] == "image/png"


@pytest.mark.parametrize("route", ["POST /artifacts", "GET /artifacts"])
@pytest.mark.parametrize("groups", ["[admins]", ""])
def test_non_researcher_gets_403(route: str, groups: str) -> None:
    s3 = FakeBucket()
    resp = api(s3).handle(
        jwt_event(route, groups=groups, body=GOOD if "POST" in route else None),
        now=NOW,
    )
    assert resp["statusCode"] == 403
    assert not s3.objects


@pytest.mark.parametrize(
    "bad",
    [
        {**GOOD, "name": ""},
        {**GOOD, "name": "a/b.png"},
        {**GOOD, "name": "../x"},
        {**GOOD, "name": 3},
        {**GOOD, "size": 0},
        {**GOOD, "size": 5 * 1024**3 + 1},
        {**GOOD, "size": True},
        {**GOOD, "size": "9"},
        {**GOOD, "crc64nvme": "AAAA"},
        {**GOOD, "crc64nvme": None},
        {**GOOD, "snapshot_id": "42"},
        {**GOOD, "snapshot_id": True},
        {**GOOD, "snapshot_id": None},
        {**GOOD, "sources": []},
        {**GOOD, "sources": "x"},
        {**GOOD, "sources": [SOURCE] * 1001},
        {**GOOD, "sources": [{"asset_id": "a"}]},
        {**GOOD, "sources": [{**SOURCE, "extra": "x"}]},
        {**GOOD, "sources": [{"asset_id": "", "s3_version_id": "v"}]},
        {**GOOD, "sources": [{"asset_id": 1, "s3_version_id": "v"}]},
        {**GOOD, "sources": ["a"]},
        {**GOOD, "note": "x" * 1025},
        {**GOOD, "note": 5},
        {**GOOD, "content_type": ""},
        {**GOOD, "content_type": 5},
        {**GOOD, "sdk_version": 1},
        {**GOOD, "sdk_version": "v" * 65},
    ],
)
def test_malformed_create_gets_400_and_writes_nothing(bad: dict[str, Any]) -> None:
    s3 = FakeBucket()
    status, out = call(s3, "POST /artifacts", body=bad)
    assert status == 400
    assert out["error"] == "invalid_request"
    assert not s3.objects
    assert validate_create(bad) == out["detail"]


@pytest.mark.parametrize("raw", ["", "{nope", "[1]", "null", "3"])
def test_body_that_is_not_a_json_object_gets_400(raw: str) -> None:
    s3 = FakeBucket()
    status, _ = call(s3, "POST /artifacts", body=raw)
    assert status == 400
    assert not s3.objects


@pytest.mark.parametrize(
    "good",
    [
        {**GOOD, "sources": [SOURCE] * 1000},
        {**GOOD, "note": "x" * 1024},
        {**GOOD, "note": None},
        {**GOOD, "size": 5 * 1024**3},
        {**GOOD, "size": 1},
    ],
)
def test_limits_are_inclusive(good: dict[str, Any]) -> None:
    assert validate_create(good) is None


def test_note_limit_counts_bytes() -> None:
    assert validate_create({**GOOD, "note": "é" * 513}) is not None
    assert validate_create({**GOOD, "note": "é" * 512}) is None


def test_finalize_before_any_upload_is_409_pending_and_keeps_the_record() -> None:
    s3 = FakeBucket()
    aid = create(s3)
    status, out = call(s3, "POST /artifacts/{id}/finalize", aid=aid)
    assert (status, out) == (
        409,
        {"error": "upload_pending", "detail": "no object uploaded yet"},
    )
    assert not s3.deleted
    record = json.loads(s3.objects[f"artifacts/u1/{aid}/provenance.json"])
    assert record["state"] == "PENDING"
    # the upload and a retry then succeed
    upload(s3, "u1", aid)
    assert call(s3, "POST /artifacts/{id}/finalize", aid=aid)[0] == 200


def test_finalize_commits_and_stamps_the_record() -> None:
    s3 = FakeBucket()
    aid = create(s3)
    upload(s3, "u1", aid)
    status, out = call(s3, "POST /artifacts/{id}/finalize", aid=aid)
    assert (status, out) == (200, {"state": "COMMITTED", "artifact_id": aid})
    record = json.loads(s3.objects[f"artifacts/u1/{aid}/provenance.json"])
    assert record["state"] == "COMMITTED"
    assert record["finalized_at"] == NOW
    assert not s3.deleted
    # idempotent
    assert call(s3, "POST /artifacts/{id}/finalize", aid=aid)[0] == 200


@pytest.mark.parametrize(
    "head", [{"ChecksumCRC64NVME": OTHER_CRC}, {"ContentLength": 9}]
)
def test_crc_or_size_mismatch_deletes_the_object_and_answers_409_incomplete(
    head: dict[str, Any],
) -> None:
    s3 = FakeBucket()
    aid = create(s3)
    key = upload(s3, "u1", aid, **head)
    status, out = call(s3, "POST /artifacts/{id}/finalize", aid=aid)
    assert (status, out) == (
        409,
        {
            "error": "upload_incomplete",
            "detail": "The uploaded file did not match its declared size or "
            "checksum, so it was deleted. Upload it again.",
        },
    )
    assert s3.deleted == [key]
    record = json.loads(s3.objects[f"artifacts/u1/{aid}/provenance.json"])
    assert record["state"] == "INCOMPLETE"
    # a second finalize stays INCOMPLETE and deletes nothing more
    assert call(s3, "POST /artifacts/{id}/finalize", aid=aid) == (
        409,
        {
            "error": "upload_incomplete",
            "detail": "This result was marked incomplete. Create a new result and "
            "upload the file again.",
        },
    )
    assert s3.deleted == [key]


def test_head_error_other_than_not_found_is_a_logged_500(
    capsys: pytest.CaptureFixture[str],
) -> None:
    s3 = FakeBucket()
    aid = create(s3)

    s3.head_error = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": ""}}, "Head"
    )
    status, out = call(s3, "POST /artifacts/{id}/finalize", aid=aid)
    assert (status, out["error"]) == (500, "internal_error")
    assert '{"unhandled":"ClientError"' in capsys.readouterr().out


# GET /artifacts/{id} is open to every researcher (tested below); finalize
# stays the author's own prefix only.
@pytest.mark.parametrize("route", ["POST /artifacts/{id}/finalize"])
def test_non_author_gets_404_never_403(route: str) -> None:
    s3 = FakeBucket()
    aid = create(s3, sub="author")
    upload(s3, "author", aid)
    call(s3, "POST /artifacts/{id}/finalize", sub="author", aid=aid)
    status, out = call(s3, route, sub="intruder", aid=aid)
    assert status == 404
    assert out == {"error": "not_found", "detail": "no such artifact"}
    assert (
        call(s3, route, sub="intruder", aid="11111111-1111-4111-8111-111111111111")[1]
        == out
    )  # same answer as an absent id
    record = json.loads(s3.objects[f"artifacts/author/{aid}/provenance.json"])
    assert record["state"] == "COMMITTED"
    assert not s3.deleted


def test_non_author_record_copy_under_own_prefix_is_still_404() -> None:
    s3 = FakeBucket()
    aid = create(s3, sub="author")
    upload(s3, "author", aid)
    call(s3, "POST /artifacts/{id}/finalize", sub="author", aid=aid)
    # a COMMITTED record whose author_sub differs from the prefix owner is refused
    s3.objects[f"artifacts/u2/{aid}/provenance.json"] = s3.objects[
        f"artifacts/author/{aid}/provenance.json"
    ]
    # finalize reads only the caller's prefix (GET follows index/by-id)
    assert call(s3, "POST /artifacts/{id}/finalize", sub="u2", aid=aid)[0] == 404


@pytest.mark.parametrize("aid", ["nope", "../x", "", None])
def test_malformed_id_gets_404(aid: Any) -> None:
    status, _ = call(FakeBucket(), "POST /artifacts/{id}/finalize", aid=aid)
    assert status == 404


def test_get_before_commit_is_404_and_after_returns_record_and_url() -> None:
    s3 = FakeBucket()
    aid = create(s3)
    assert call(s3, "GET /artifacts/{id}", aid=aid)[0] == 404
    key = upload(s3, "u1", aid)
    call(s3, "POST /artifacts/{id}/finalize", aid=aid)
    status, out = call(s3, "GET /artifacts/{id}", aid=aid)
    assert status == 200
    assert out["record"]["artifact_id"] == aid
    assert out["record"]["state"] == "COMMITTED"
    assert out["url"] == f"https://signed.example/{key}"
    assert s3.presigned[-1]["ExpiresIn"] == artifacts.GET_URL_SECONDS == 900
    assert s3.presigned[-1]["ClientMethod"] == "get_object"


def test_list_returns_only_the_callers_summaries_newest_first() -> None:
    s3 = FakeBucket()
    first = create(s3)
    second = create(s3)
    create(s3, sub="other")
    status, out = call(s3, "GET /artifacts")
    assert status == 200
    ids = [a["artifact_id"] for a in out["artifacts"]]
    assert sorted(ids) == sorted([first, second])
    assert set(out["artifacts"][0]) == {
        *artifacts.SUMMARY_FIELDS,
        *artifacts.SOURCE_FIELDS,
        artifacts.ROW_RUN_FIELD,
        artifacts.ROW_RUN_FUNCTION_FIELD,
    }
    assert "sources" not in out["artifacts"][0]


def test_list_is_capped_at_100() -> None:
    s3 = FakeBucket()
    for _ in range(105):
        create(s3)
    assert len(call(s3, "GET /artifacts")[1]["artifacts"]) == 100


def test_unknown_route_is_404() -> None:
    assert call(FakeBucket(), "DELETE /artifacts")[0] == 404


V2_GOOD: dict[str, Any] = {
    "schema": "asteroidea.provenance/2",
    "name": "psth.png",
    "size": 1234,
    "crc64nvme": CRC,
    "inputs": [
        {"kind": "asset", "id": "a-1", "version": "v-1", "role": "spikes"},
        {"kind": "table_snapshot", "id": "units", "version": 7},
    ],
    "run": {"run_id": "r-1", "function": "rates", "parameters": {"bin_s": 0.05}},
}


def test_v2_create_accepts_artifact_only_inputs() -> None:
    s3 = FakeBucket()
    parent = create(s3)
    upload(s3, "u1", parent)
    assert call(s3, "POST /artifacts/{id}/finalize", aid=parent)[0] == 200
    # snapshot_id is given so only the sources rule decides this case
    body = {
        **V2_GOOD,
        "snapshot_id": 42,
        "inputs": [{"kind": "artifact", "id": parent}],
    }
    status, out = call(s3, "POST /artifacts", body=body)
    assert (status, out.get("error")) == (201, None), out


def test_v2_create_accepts_absent_snapshot_id() -> None:
    assert "snapshot_id" not in V2_GOOD
    assert validate_create(V2_GOOD) is None


def test_v1_create_still_refuses_empty_sources() -> None:
    assert validate_create({**GOOD, "sources": []}) == (
        "sources must be a list of 1 to 1000 entries"
    )


def stored(s3: FakeBucket, key: str) -> dict[str, Any]:
    return cast("dict[str, Any]", json.loads(s3.objects[key]))


def committed(s3: FakeBucket, sub: str = "u1") -> str:
    aid = create(s3, sub=sub)
    upload(s3, sub, aid)
    assert call(s3, "POST /artifacts/{id}/finalize", sub=sub, aid=aid)[0] == 200
    return aid


def test_v1_create_with_access_token_records_null_email() -> None:
    # shipped SDK wheels send version 1 bodies with the access token (no `email`);
    # 5c back-fills the email
    s3 = FakeBucket()
    status, out = create_as(s3, GOOD, None)
    assert status == 201, out
    aid = out["artifact_id"]
    assert stored(s3, f"artifacts/u1/{aid}/provenance.json")["author_email"] is None
    assert stored(s3, f"index/by-id/{aid}")["state"] == "PENDING"


def test_v2_create_without_email_claim_is_401_id_token_required() -> None:
    s3 = FakeBucket()
    status, out = create_as(s3, V2_GOOD, None)
    assert (status, out) == (
        401,
        {"error": "id_token_required", "detail": artifacts.NO_EMAIL},
    )
    assert not s3.objects
    assert not s3.presigned


@pytest.mark.parametrize("email", ["", None])
def test_empty_email_claim_is_refused_like_none(email: str | None) -> None:
    s3 = FakeBucket()
    assert create_as(s3, V2_GOOD, email)[0] == 401
    assert not s3.objects


def test_v2_record_keeps_run_inputs_and_null_snapshot() -> None:
    s3 = FakeBucket()
    status, out = call(s3, "POST /artifacts", body={**V2_GOOD, "sources": [SOURCE]})
    assert status == 201
    aid = out["artifact_id"]
    record = stored(s3, f"artifacts/u1/{aid}/provenance.json")
    assert record["schema"] == "asteroidea.provenance/2"
    assert record["inputs"] == V2_GOOD["inputs"]
    assert record["run"] == V2_GOOD["run"]
    assert record["sources"] == [SOURCE]
    assert record["snapshot_id"] is None
    assert record["author_email"] == EMAIL
    assert record["data_key"] == f"artifacts/u1/{aid}/data/psth.png"
    assert s3.presigned[-1]["Params"]["Key"] == record["data_key"]


def test_v2_record_without_sources_stores_an_empty_list() -> None:
    s3 = FakeBucket()
    aid = call(s3, "POST /artifacts", body=V2_GOOD)[1]["artifact_id"]
    assert stored(s3, f"artifacts/u1/{aid}/provenance.json")["sources"] == []


def test_create_writes_by_id_and_by_input_index_pending() -> None:
    s3 = FakeBucket()
    body = {**V2_GOOD, "sources": [SOURCE, SOURCE]}
    aid = call(s3, "POST /artifacts", body=body)[1]["artifact_id"]
    assert stored(s3, f"index/by-id/{aid}") == {"author_sub": "u1", "state": "PENDING"}
    index = sorted(k for k in s3.objects if k.startswith("index/by-input/"))
    # one entry per distinct source asset and input id
    assert index == sorted(
        [
            f"index/by-input/a-1/{aid}",
            f"index/by-input/units/{aid}",
        ]
    )
    assert all(stored(s3, k) == {"state": "PENDING"} for k in index)
    assert not [k for k in s3.objects if k.startswith("index/by-time/")]


def test_v1_create_indexes_its_sources() -> None:
    s3 = FakeBucket()
    aid = create(s3)
    assert stored(s3, f"index/by-input/a-1/{aid}") == {"state": "PENDING"}
    assert stored(s3, f"index/by-id/{aid}")["state"] == "PENDING"


def test_by_input_key_quotes_a_slash_in_an_id() -> None:
    s3 = FakeBucket()
    body = {**V2_GOOD, "inputs": [{"kind": "recipe", "id": "rec/v1", "version": "1"}]}
    aid = call(s3, "POST /artifacts", body=body)[1]["artifact_id"]
    assert f"index/by-input/rec%2Fv1/{aid}" in s3.objects
    assert artifacts.by_input_prefix("rec/v1") == "index/by-input/rec%2Fv1/"


def test_longest_input_id_fits_the_s3_key_limit() -> None:
    s3 = FakeBucket()
    long_id = "/" * artifacts.MAX_INPUT_ID  # quotes to three bytes each
    body = {**V2_GOOD, "inputs": [{"kind": "recipe", "id": long_id, "version": "1"}]}
    assert call(s3, "POST /artifacts", body=body)[0] == 201


def test_finalize_writes_by_time_and_rewrites_states_committed() -> None:
    s3 = FakeBucket()
    aid = call(s3, "POST /artifacts", body={**V2_GOOD, "sources": [SOURCE]})[1][
        "artifact_id"
    ]
    upload(s3, "u1", aid)
    assert call(s3, "POST /artifacts/{id}/finalize", aid=aid)[0] == 200
    assert stored(s3, f"index/by-id/{aid}") == {
        "author_sub": "u1",
        "state": "COMMITTED",
    }
    for input_id in ("a-1", "units"):
        assert stored(s3, f"index/by-input/{input_id}/{aid}") == {"state": "COMMITTED"}
    time_keys = [k for k in s3.objects if k.startswith("index/by-time/")]
    assert time_keys == [artifacts.by_time_key(NOW, aid)]
    summary = stored(s3, time_keys[0])
    assert set(summary) == {
        *artifacts.SUMMARY_FIELDS,
        *artifacts.SOURCE_FIELDS,
        artifacts.ROW_RUN_FIELD,
        artifacts.ROW_RUN_FUNCTION_FIELD,
        "author_email",
    }
    assert summary["author_email"] == EMAIL
    assert summary["state"] == "COMMITTED"
    assert summary["finalized_at"] == NOW


def test_by_time_keys_sort_newest_first() -> None:
    older = artifacts.by_time_key(1_000, "b")
    newer = artifacts.by_time_key(2_000, "a")
    latest = artifacts.by_time_key(9_999_999_999, "c")  # year 2286
    assert sorted([older, newer, latest]) == [latest, newer, older]
    assert len(older) == len(latest)


def test_finalize_mismatch_rewrites_states_incomplete_and_no_by_time() -> None:
    s3 = FakeBucket()
    aid = create(s3)
    upload(s3, "u1", aid, ContentLength=9)
    assert call(s3, "POST /artifacts/{id}/finalize", aid=aid)[0] == 409
    assert stored(s3, f"index/by-id/{aid}")["state"] == "INCOMPLETE"
    assert stored(s3, f"index/by-input/a-1/{aid}") == {"state": "INCOMPLETE"}
    assert not [k for k in s3.objects if k.startswith("index/by-time/")]


def test_finalize_and_get_use_data_key_not_name() -> None:
    s3 = FakeBucket()
    aid = create(s3)
    record_key = f"artifacts/u1/{aid}/provenance.json"
    record = stored(s3, record_key)
    # a later display-name edit (5b PATCH) must not move the object
    s3.objects[record_key] = json.dumps({**record, "name": "renamed.png"}).encode()
    upload(s3, "u1", aid)
    assert call(s3, "POST /artifacts/{id}/finalize", aid=aid)[0] == 200
    out = call(s3, "GET /artifacts/{id}", aid=aid)[1]
    assert out["url"] == f"https://signed.example/{record['data_key']}"


def test_v1_record_without_data_key_falls_back_to_name() -> None:
    record = {"artifact_id": "x", "author_sub": "s", "name": "n.png"}
    assert artifacts.data_key(record) == "artifacts/s/x/data/n.png"


@pytest.mark.parametrize("state", ["PENDING", "INCOMPLETE", None])
def test_artifact_input_that_is_not_committed_is_400_naming_it(
    state: str | None,
) -> None:
    s3 = FakeBucket()
    parent = create(s3)
    if state is None:
        del s3.objects[f"index/by-id/{parent}"]
    elif state == "INCOMPLETE":
        upload(s3, "u1", parent, ContentLength=9)
        call(s3, "POST /artifacts/{id}/finalize", aid=parent)
    before = dict(s3.objects)
    body = {**V2_GOOD, "inputs": [{"kind": "artifact", "id": parent}]}
    status, out = call(s3, "POST /artifacts", body=body)
    assert (status, out) == (
        400,
        {
            "error": "invalid_request",
            "detail": f"artifact input {parent} is not a committed result",
        },
    )
    assert s3.objects == before


def test_artifact_input_of_another_author_is_accepted() -> None:
    s3 = FakeBucket()
    parent = committed(s3, sub="someone-else")
    body = {**V2_GOOD, "inputs": [{"kind": "artifact", "id": parent}]}
    aid = call(s3, "POST /artifacts", body=body)[1]["artifact_id"]
    assert stored(s3, f"index/by-input/{parent}/{aid}") == {"state": "PENDING"}


def test_v1_body_with_a_stray_inputs_field_is_not_read() -> None:
    s3 = FakeBucket()
    aid = create(s3, inputs="garbage")
    record = stored(s3, f"artifacts/u1/{aid}/provenance.json")
    assert "inputs" not in record
    assert "schema" not in record


ASSET = {"kind": "asset", "id": "a-1", "version": "v-1"}
RUN = V2_GOOD["run"]


@pytest.mark.parametrize(
    "bad",
    [
        {**V2_GOOD, "schema": "asteroidea.provenance/3"},
        {**V2_GOOD, "schema": None},
        {**V2_GOOD, "inputs": []},
        {**V2_GOOD, "inputs": [], "sources": []},
        {**V2_GOOD, "inputs": "x"},
        {**V2_GOOD, "inputs": [ASSET] * 1001},
        {**V2_GOOD, "sources": [SOURCE] * 1001},
        {**V2_GOOD, "sources": "x"},
        {**V2_GOOD, "sources": [{"asset_id": "a"}]},
        {**V2_GOOD, "inputs": ["a"]},
        {**V2_GOOD, "inputs": [{"kind": "asset", "version": "v"}]},
        {**V2_GOOD, "inputs": [{**ASSET, "extra": 1}]},
        {**V2_GOOD, "inputs": [{**ASSET, "kind": "session"}]},
        {**V2_GOOD, "inputs": [{**ASSET, "kind": 3}]},
        {**V2_GOOD, "inputs": [{**ASSET, "id": ""}]},
        {**V2_GOOD, "inputs": [{**ASSET, "id": "x" * 257}]},
        {**V2_GOOD, "inputs": [{**ASSET, "id": 5}]},
        {**V2_GOOD, "inputs": [{"kind": "asset", "id": "a-1"}]},
        {**V2_GOOD, "inputs": [{**ASSET, "version": ""}]},
        {**V2_GOOD, "inputs": [{**ASSET, "role": 1}]},
        {**V2_GOOD, "inputs": [{**ASSET, "role": ""}]},
        {**V2_GOOD, "inputs": [{"kind": "table_snapshot", "id": "u", "version": "7"}]},
        {**V2_GOOD, "inputs": [{"kind": "table_snapshot", "id": "u", "version": True}]},
        {**V2_GOOD, "inputs": [{"kind": "table_snapshot", "id": "u"}]},
        {**V2_GOOD, "inputs": [{"kind": "artifact", "id": "not-a-uuid"}]},
        {
            **V2_GOOD,
            "inputs": [
                {
                    "kind": "artifact",
                    "id": "11111111-1111-4111-8111-111111111111",
                    "version": "1",
                }
            ],
        },
        {**V2_GOOD, "inputs": [{"kind": "defaults", "id": "000623"}]},
        {**V2_GOOD, "snapshot_id": "42"},
        {**V2_GOOD, "snapshot_id": None},
        {**V2_GOOD, "snapshot_id": True},
        {k: v for k, v in V2_GOOD.items() if k != "run"},
        {**V2_GOOD, "run": "rates"},
        {**V2_GOOD, "run": {**RUN, "extra": 1}},
        {**V2_GOOD, "run": {"run_id": "r", "function": "f"}},
        {**V2_GOOD, "run": {**RUN, "run_id": ""}},
        {**V2_GOOD, "run": {**RUN, "function": 3}},
        {**V2_GOOD, "run": {**RUN, "parameters": [1]}},
        {**V2_GOOD, "run": {**RUN, "parameters": {"p": "x" * (64 * 1024 - 7)}}},
        {**V2_GOOD, "run": {**RUN, "sdk_version": 1}},
        {**V2_GOOD, "run": {**RUN, "analysis_sha256": ""}},
        {**V2_GOOD, "run": {**RUN, "environment": [1]}},
        {**V2_GOOD, "note": "x" * 1025},
        {**V2_GOOD, "name": "a/b.png"},
        {**V2_GOOD, "size": 0},
        {**V2_GOOD, "crc64nvme": None},
    ],
)
def test_malformed_v2_create_gets_400_and_writes_nothing(bad: dict[str, Any]) -> None:
    s3 = FakeBucket()
    status, out = call(s3, "POST /artifacts", body=bad)
    assert status == 400
    assert out["error"] == "invalid_request"
    assert not s3.objects
    assert validate_create(bad) == out["detail"]


@pytest.mark.parametrize(
    "good",
    [
        {**V2_GOOD, "inputs": [ASSET] * 1000},
        {**V2_GOOD, "inputs": [ASSET] * 1000, "sources": [SOURCE] * 1000},
        {**V2_GOOD, "inputs": [], "sources": [SOURCE]},
        {**V2_GOOD, "run": {**RUN, "parameters": {"p": "x" * (64 * 1024 - 8)}}},
        {**V2_GOOD, "run": {**RUN, "parameters": {}}},
        {**V2_GOOD, "inputs": [{**ASSET, "id": "x" * 256, "role": None}]},
        {**V2_GOOD, "inputs": [{"kind": "defaults", "id": "000623", "version": "1"}]},
        {**V2_GOOD, "snapshot_id": 0},
        {
            **V2_GOOD,
            "run": {
                **RUN,
                "defaults_version": "1",
                "sdk_version": "0.4.0",
                "analysis_sha256": "ab" * 32,
                "environment": {"python": "3.14"},
            },
        },
        {**V2_GOOD, "run": {**RUN, "sdk_version": None, "environment": None}},
    ],
)
def test_v2_limits_are_inclusive(good: dict[str, Any]) -> None:
    assert validate_create(good) is None


def test_parameters_limit_counts_utf8_bytes() -> None:
    two_byte = {**RUN, "parameters": {"p": "é" * ((64 * 1024 - 8) // 2)}}
    assert validate_create({**V2_GOOD, "run": two_byte}) is None
    over = {**RUN, "parameters": {"p": "é" * ((64 * 1024 - 8) // 2 + 1)}}
    assert validate_create({**V2_GOOD, "run": over}) is not None


def test_s3_client_config_sizes_the_pool_for_the_all_listing() -> None:
    # botocore's stubs do not declare Config's attributes; they are instance fields
    config = vars(artifacts.S3_CLIENT_CONFIG)
    assert config["max_pool_connections"] == artifacts.S3_POOL_CONNECTIONS == 32
    assert config["signature_version"] == "s3v4"


# --- scope, input lookup, any-author GET, PATCH, withdraw, DELETE ---

UNKNOWN_ID = "11111111-1111-4111-8111-111111111111"


def request(
    s3: FakeBucket, route: str, *, sub: str = "u1", now: int = NOW, **kw: Any
) -> tuple[int, dict[str, Any]]:
    """Send `route` as `sub` (ID token, email `<sub>@x.org`); kw: aid, body, query."""
    event = jwt_event(route, sub=sub, body=kw.get("body"), sid=kw.get("aid"))
    event["queryStringParameters"] = kw.get("query")
    event["requestContext"]["authorizer"]["jwt"]["claims"]["email"] = f"{sub}@x.org"
    resp = api(s3).handle(event, now=now)
    return int(resp["statusCode"]), json.loads(resp["body"])


def finalized(s3: FakeBucket, sub: str = "u1", *, now: int = NOW) -> str:
    """Create, upload and finalize one result of `sub` at `now`."""
    status, out = request(s3, "POST /artifacts", sub=sub, body=GOOD)
    assert status == 201
    aid = str(out["artifact_id"])
    upload(s3, sub, aid)
    route = "POST /artifacts/{id}/finalize"
    assert request(s3, route, sub=sub, aid=aid, now=now)[0] == 200
    return aid


def v2_using(s3: FakeBucket, sub: str, parent: str, *, finalize: bool = True) -> str:
    """Create a version 2 result of `sub` whose only input is result `parent`."""
    body = {**V2_GOOD, "inputs": [{"kind": "artifact", "id": parent}]}
    status, out = request(s3, "POST /artifacts", sub=sub, body=body)
    assert status == 201, out
    aid = str(out["artifact_id"])
    if finalize:
        upload(s3, sub, aid)
        request(s3, "POST /artifacts/{id}/finalize", sub=sub, aid=aid)
    return aid


def record_of(s3: FakeBucket, sub: str, aid: str) -> dict[str, Any]:
    return json.loads(s3.objects[f"artifacts/{sub}/{aid}/provenance.json"])


def index_of(s3: FakeBucket, key: str) -> dict[str, Any]:
    return json.loads(s3.objects[key])


def by_time(s3: FakeBucket, aid: str) -> str:
    (key,) = [k for k in s3.objects if k.startswith("index/by-time/") and aid in k]
    return key


def test_no_scope_lists_only_the_callers_records() -> None:
    """Seam from 5a: shipped wheels send no scope and never see another author."""
    s3 = FakeBucket()
    mine = finalized(s3, "u1")
    other = v2_using(s3, "u2", mine)  # a version 2 record, snapshot_id null
    assert record_of(s3, "u2", other)["snapshot_id"] is None
    status, out = request(s3, "GET /artifacts")
    assert status == 200
    assert [a["artifact_id"] for a in out["artifacts"]] == [mine]
    assert set(out["artifacts"][0]) == {
        *artifacts.SUMMARY_FIELDS,
        *artifacts.SOURCE_FIELDS,
        artifacts.ROW_RUN_FIELD,
        artifacts.ROW_RUN_FUNCTION_FIELD,
    }
    assert request(s3, "GET /artifacts", query={"scope": "mine"})[1] == out


def test_scope_all_lists_every_authors_finalized_records_newest_first() -> None:
    s3 = FakeBucket()
    old = finalized(s3, "u1", now=1_000)
    new = finalized(s3, "u2", now=2_000)
    create(s3, sub="u3")  # PENDING: never in by-time
    status, out = request(s3, "GET /artifacts", query={"scope": "all"})
    assert status == 200
    assert [a["artifact_id"] for a in out["artifacts"]] == [new, old]
    assert out["artifacts"][0]["author_email"] == "u2@x.org"
    assert set(out["artifacts"][0]) == {
        *artifacts.SUMMARY_FIELDS,
        *artifacts.SOURCE_FIELDS,
        artifacts.ROW_RUN_FIELD,
        artifacts.ROW_RUN_FUNCTION_FIELD,
        "author_email",
    }


def test_scope_all_is_the_newest_100() -> None:
    s3 = FakeBucket()
    ids = [finalized(s3, "u1", now=1_000 + i) for i in range(105)]
    out = request(s3, "GET /artifacts", query={"scope": "all"})[1]["artifacts"]
    assert [a["artifact_id"] for a in out] == ids[::-1][:100]


@pytest.mark.parametrize("scope", ["", "ALL", "everyone", "mine,all"])
def test_bad_scope_is_400_invalid_scope(scope: str) -> None:
    status, out = request(FakeBucket(), "GET /artifacts", query={"scope": scope})
    assert (status, out) == (
        400,
        {"error": "invalid_scope", "detail": "scope must be mine or all"},
    )


@pytest.mark.parametrize("bad", ["", "x" * 257])
def test_bad_input_is_400_invalid_input(bad: str) -> None:
    status, out = request(FakeBucket(), "GET /artifacts", query={"input": bad})
    assert status == 400
    assert out["error"] == "invalid_input"


def test_input_lookup_finds_results_that_used_an_asset_or_result() -> None:
    s3 = FakeBucket()
    parent = finalized(s3, "u1")  # source asset a-1
    child_mine = v2_using(s3, "u1", parent)
    child_other = v2_using(s3, "u2", parent)
    v2_using(s3, "u3", parent, finalize=False)  # another author's PENDING
    request(s3, "POST /artifacts/{id}/withdraw", sub="u2", aid=child_other)

    def ids(sub: str, query: dict[str, str]) -> set[str]:
        status, out = request(s3, "GET /artifacts", sub=sub, query=query)
        assert status == 200
        return {a["artifact_id"] for a in out["artifacts"]}

    assert ids("u1", {"input": parent}) == {child_mine}  # no scope: mine
    assert ids("u1", {"input": parent, "scope": "all"}) == {child_mine, child_other}
    assert ids("u9", {"input": "a-1", "scope": "all"}) == {parent}
    assert ids("u9", {"input": "a-1"}) == set()
    assert ids("u1", {"input": "nothing-used-this", "scope": "all"}) == set()


def test_get_reads_any_authors_committed_or_withdrawn_record() -> None:
    """Lineage and bytes are readable by every researcher."""
    s3 = FakeBucket()
    aid = finalized(s3, "author")
    status, out = request(s3, "GET /artifacts/{id}", sub="reader", aid=aid)
    assert status == 200
    assert out["record"]["author_sub"] == "author"
    assert out["url"].endswith(f"artifacts/author/{aid}/data/psth.png")
    request(s3, "POST /artifacts/{id}/withdraw", sub="author", aid=aid)
    status, out = request(s3, "GET /artifacts/{id}", sub="reader", aid=aid)
    assert (status, out["record"]["state"]) == (200, "WITHDRAWN")


def test_get_of_another_authors_unfinished_record_is_404() -> None:
    s3 = FakeBucket()
    aid = create(s3, sub="author")
    status, out = request(s3, "GET /artifacts/{id}", sub="reader", aid=aid)
    assert (status, out["error"]) == (404, "not_found")


def test_version_1_record_without_by_id_is_found_by_its_author_only() -> None:
    """Before the 5c back-fill a version 1 record has no index/by-id entry."""
    s3 = FakeBucket()
    aid = finalized(s3, "u1")
    del s3.objects[f"index/by-id/{aid}"]
    assert request(s3, "GET /artifacts/{id}", sub="u1", aid=aid)[0] == 200
    assert request(s3, "GET /artifacts/{id}", sub="u2", aid=aid)[0] == 404


def test_patch_edits_note_and_name_and_rewrites_by_time() -> None:
    s3 = FakeBucket()
    aid = finalized(s3, "u1")
    body = {"note": "better", "name": "rates.png"}
    status, out = request(s3, "PATCH /artifacts/{id}", aid=aid, body=body)
    assert status == 200
    assert out["artifact"]["name"] == "rates.png"
    record = record_of(s3, "u1", aid)
    assert (record["note"], record["name"]) == ("better", "rates.png")
    assert record["data_key"] == f"artifacts/u1/{aid}/data/psth.png"  # fixed
    assert index_of(s3, by_time(s3, aid))["name"] == "rates.png"
    got = request(s3, "GET /artifacts/{id}", aid=aid)[1]
    assert got["url"].endswith("/data/psth.png")  # downloads use data_key


def test_patch_of_a_pending_record_writes_no_by_time() -> None:
    s3 = FakeBucket()
    aid = create(s3)
    assert request(s3, "PATCH /artifacts/{id}", aid=aid, body={"note": None})[0] == 200
    assert not [k for k in s3.objects if k.startswith("index/by-time/")]


@pytest.mark.parametrize(
    "bad",
    [
        {},
        {"size": 3},
        {"note": "n", "state": "COMMITTED"},
        {"name": ""},
        {"name": "a/b.png"},
        {"note": 3},
        {"note": "x" * 1025},
    ],
)
def test_malformed_patch_is_400_and_writes_nothing(bad: dict[str, Any]) -> None:
    s3 = FakeBucket()
    aid = finalized(s3, "u1")
    before = dict(s3.objects)
    status, out = request(s3, "PATCH /artifacts/{id}", aid=aid, body=bad)
    assert (status, out["error"]) == (400, "invalid_request")
    assert s3.objects == before


AUTHOR_ROUTES = [
    ("PATCH /artifacts/{id}", {"note": "mine now"}),
    ("POST /artifacts/{id}/withdraw", None),
    ("DELETE /artifacts/{id}", None),
]


@pytest.mark.parametrize(("route", "body"), AUTHOR_ROUTES)
def test_non_author_is_403_not_author_and_changes_nothing(
    route: str, body: object
) -> None:
    s3 = FakeBucket()
    aid = finalized(s3, "author")
    before = dict(s3.objects)
    status, out = request(s3, route, sub="intruder", aid=aid, body=body)
    assert (status, out) == (
        403,
        {"error": "not_author", "detail": artifacts.NOT_AUTHOR},
    )
    assert s3.objects == before
    assert not s3.deleted


@pytest.mark.parametrize(("route", "body"), AUTHOR_ROUTES)
def test_non_author_of_an_unfinished_or_absent_record_is_404(
    route: str, body: object
) -> None:
    s3 = FakeBucket()
    aid = create(s3, sub="author")
    absent = {"error": "not_found", "detail": "no such artifact"}
    for target in (aid, UNKNOWN_ID, "nope"):
        status, out = request(s3, route, sub="intruder", aid=target, body=body)
        assert (status, out) == (404, absent)


def test_withdraw_sets_withdrawn_everywhere_and_keeps_the_bytes() -> None:
    s3 = FakeBucket()
    aid = finalized(s3, "u1")
    status, out = request(s3, "POST /artifacts/{id}/withdraw", aid=aid)
    assert (status, out) == (200, {"state": "WITHDRAWN", "artifact_id": aid})
    assert record_of(s3, "u1", aid)["state"] == "WITHDRAWN"
    assert index_of(s3, f"index/by-id/{aid}")["state"] == "WITHDRAWN"
    assert index_of(s3, f"index/by-input/a-1/{aid}") == {"state": "WITHDRAWN"}
    assert index_of(s3, by_time(s3, aid))["state"] == "WITHDRAWN"
    assert not s3.deleted
    assert request(s3, "POST /artifacts/{id}/withdraw", aid=aid)[0] == 200  # again


def test_withdraw_of_an_unfinished_record_is_409() -> None:
    s3 = FakeBucket()
    pending = create(s3)
    status, out = request(s3, "POST /artifacts/{id}/withdraw", aid=pending)
    assert (status, out["error"]) == (409, "upload_pending")
    bad = create(s3)
    upload(s3, "u1", bad, ContentLength=1)
    request(s3, "POST /artifacts/{id}/finalize", aid=bad)
    status, out = request(s3, "POST /artifacts/{id}/withdraw", aid=bad)
    assert (status, out["error"]) == (409, "upload_incomplete")
    assert record_of(s3, "u1", pending)["state"] == "PENDING"


def test_withdrawn_record_is_not_an_artifact_input() -> None:
    s3 = FakeBucket()
    parent = finalized(s3, "u1")
    request(s3, "POST /artifacts/{id}/withdraw", aid=parent)
    body = {**V2_GOOD, "inputs": [{"kind": "artifact", "id": parent}]}
    assert request(s3, "POST /artifacts", body=body)[0] == 400


def test_delete_removes_the_record_its_bytes_and_its_index() -> None:
    s3 = FakeBucket()
    aid = finalized(s3, "u1")
    s3.objects[f"artifacts/u1/{aid}/data/psth.png"] = b"png"
    status, out = request(s3, "DELETE /artifacts/{id}", aid=aid)
    assert (status, out) == (200, {"state": "DELETED", "artifact_id": aid})
    assert not [k for k in s3.objects if aid in k]
    assert s3.deleted[-1] == f"artifacts/u1/{aid}/provenance.json"  # record last
    assert request(s3, "GET /artifacts/{id}", aid=aid)[0] == 404


def test_delete_of_a_pending_record_has_no_by_time_to_delete() -> None:
    s3 = FakeBucket()
    aid = create(s3)
    assert request(s3, "DELETE /artifacts/{id}", aid=aid)[0] == 200
    assert not [k for k in s3.objects if aid in k]
    assert not [k for k in s3.deleted if k.startswith("index/by-time/")]


def test_delete_with_dependents_is_409_naming_them() -> None:
    """Refuse deleting a result others used; withdraw instead."""
    s3 = FakeBucket()
    parent = finalized(s3, "u1")
    users = sorted(
        [
            v2_using(s3, "u2", parent),  # COMMITTED
            v2_using(s3, "u3", parent, finalize=False),  # PENDING
        ]
    )
    before = dict(s3.objects)
    status, out = request(s3, "DELETE /artifacts/{id}", aid=parent)
    assert (status, out) == (
        409,
        {
            "error": "has_dependents",
            "detail": f"Other results used this one: {users[0]}, {users[1]}. "
            "Withdraw it instead.",
        },
    )
    assert s3.objects == before
    assert not s3.deleted


def test_withdrawn_dependent_still_blocks_and_incomplete_does_not() -> None:
    s3 = FakeBucket()
    parent = finalized(s3, "u1")
    child = v2_using(s3, "u2", parent)
    request(s3, "POST /artifacts/{id}/withdraw", sub="u2", aid=child)
    assert request(s3, "DELETE /artifacts/{id}", aid=parent)[0] == 409
    s3.objects[f"index/by-input/{parent}/{child}"] = b'{"state": "INCOMPLETE"}'
    assert request(s3, "DELETE /artifacts/{id}", aid=parent)[0] == 200


def test_delete_reads_every_dependent_page() -> None:
    s3 = FakeBucket()
    parent = finalized(s3, "u1")
    for i in range(1001):
        s3.objects[f"index/by-input/{parent}/dep-{i:04d}"] = b'{"state": "COMMITTED"}'
    status, out = request(s3, "DELETE /artifacts/{id}", aid=parent)
    assert status == 409
    assert "dep-0019 and 981 more. Withdraw it instead." in out["detail"]


# --- listing rows carry the asset ids each result used ---


def test_every_listing_names_the_asset_ids_a_result_used() -> None:
    s3 = FakeBucket()
    parent = finalized(s3, "u1")  # version 1: source asset a-1
    body = {
        **V2_GOOD,
        "sources": [SOURCE, {"asset_id": "a-2", "s3_version_id": "v2"}],
        "inputs": [
            {"kind": "artifact", "id": parent},
            {"kind": "asset", "id": "a-3", "version": "v3", "role": "events"},
            {"kind": "asset", "id": "a-1", "version": "v1"},
            {"kind": "recipe", "id": "rates", "version": "1"},
        ],
    }
    status, out = request(s3, "POST /artifacts", sub="u2", body=body)
    assert status == 201, out
    child = str(out["artifact_id"])
    upload(s3, "u2", child)
    request(s3, "POST /artifacts/{id}/finalize", sub="u2", aid=child)
    want = {"source_asset_ids": ["a-1", "a-2", "a-3"], "source_asset_count": 3}

    def row(sub: str, query: dict[str, str] | None) -> dict[str, Any]:
        rows = request(s3, "GET /artifacts", sub=sub, query=query)[1]["artifacts"]
        (found,) = [r for r in rows if r["artifact_id"] == child]
        return found

    for sub, query in (
        ("u2", None),
        ("u1", {"scope": "all"}),
        ("u1", {"scope": "all", "input": parent}),
    ):
        got = row(sub, query)
        assert {k: got[k] for k in artifacts.SOURCE_FIELDS} == want, query
    mine = request(s3, "GET /artifacts", sub="u1")[1]["artifacts"]
    assert mine[0]["source_asset_ids"] == ["a-1"]


def test_listing_caps_the_ids_and_keeps_the_count() -> None:
    s3 = FakeBucket()
    n = artifacts.LIST_SOURCE_IDS + 5
    sources = [{"asset_id": f"a-{i}", "s3_version_id": "v"} for i in range(n)]
    status, out = request(s3, "POST /artifacts", body={**GOOD, "sources": sources})
    assert status == 201, out
    (row,) = request(s3, "GET /artifacts")[1]["artifacts"]
    assert row["source_asset_ids"] == [f"a-{i}" for i in range(100)]
    assert row["source_asset_count"] == n


def test_all_listing_fills_sources_of_a_by_time_body_written_before_them() -> None:
    s3 = FakeBucket()
    aid = finalized(s3, "u1")
    gone = finalized(s3, "u2", now=NOW + 1)
    for key in (by_time(s3, aid), by_time(s3, gone)):
        body = index_of(s3, key)
        for name in artifacts.SOURCE_FIELDS:
            del body[name]
        s3.objects[key] = json.dumps(body).encode()
    del s3.objects[f"index/by-id/{gone}"]
    rows = request(s3, "GET /artifacts", query={"scope": "all"})[1]["artifacts"]
    by_id = {r["artifact_id"]: r for r in rows}
    assert by_id[aid]["source_asset_ids"] == ["a-1"]
    assert by_id[aid]["source_asset_count"] == 1
    assert by_id[gone]["source_asset_ids"] == []


# --- typed inline GET, run ids on rows, the by-run index (showcase-results 1.2) ---


def v2_finalized(
    s3: FakeBucket, sub: str = "u1", *, run_id: str = "r-1", **over: Any
) -> str:
    """Create, upload and finalize one version 2 result of run `run_id`."""
    run = {**V2_GOOD["run"], "run_id": run_id}
    body = {**V2_GOOD, "run": run, **over}
    status, out = request(s3, "POST /artifacts", sub=sub, body=body)
    assert status == 201, out
    aid = str(out["artifact_id"])
    upload(s3, sub, aid)
    assert request(s3, "POST /artifacts/{id}/finalize", sub=sub, aid=aid)[0] == 200
    return aid


def signed_query(s3: FakeBucket) -> dict[str, list[str]]:
    return parse_qs(urlsplit(s3.signed[-1]).query)


def test_get_presigns_the_record_type_inline() -> None:
    s3 = FakeBucket()
    aid = v2_finalized(s3, content_type="image/png")
    status, _ = request(s3, "GET /artifacts/{id}", sub="reader", aid=aid)
    assert status == 200
    assert s3.presigned[-1]["Params"] == {
        "Bucket": BUCKET,
        "Key": f"artifacts/u1/{aid}/data/psth.png",
        "ResponseContentType": "image/png",
        "ResponseContentDisposition": (
            "inline; filename=\"psth.png\"; filename*=UTF-8''psth.png"
        ),
    }
    query = signed_query(s3)
    assert query["response-content-type"] == ["image/png"]
    assert query["response-content-disposition"][0].startswith("inline;")


def test_get_with_download_presigns_an_attachment() -> None:
    s3 = FakeBucket()
    aid = v2_finalized(s3, content_type="text/html")
    request(s3, "GET /artifacts/{id}", aid=aid, query={"download": "1"})
    params = s3.presigned[-1]["Params"]
    assert params["ResponseContentType"] == "text/html"
    assert params["ResponseContentDisposition"].startswith('attachment; filename="')
    disposition = signed_query(s3)["response-content-disposition"][0]
    assert disposition.startswith("attachment;")
    request(s3, "GET /artifacts/{id}", aid=aid, query={"download": "yes"})
    disposition = s3.presigned[-1]["Params"]["ResponseContentDisposition"]
    assert disposition.startswith("inline;")


@pytest.mark.parametrize(
    ("given", "served"),
    [
        (None, "application/octet-stream"),
        ("image/jpeg", "image/jpeg"),
        ("text/csv; charset=utf-8", "text/csv; charset=utf-8"),
        ("text/html\r\nX-Evil: 1", "application/octet-stream"),
        ("not a type", "application/octet-stream"),
    ],
)
def test_the_served_type_is_the_record_type_when_plain(
    given: str | None, served: str
) -> None:
    assert artifacts.served_type({"content_type": given}) == served


def test_disposition_keeps_the_exact_name_in_filename_star() -> None:
    got = artifacts.disposition("inline", 'tête "q".png')
    assert got == (
        "inline; filename=\"t_te _q_.png\"; filename*=UTF-8''t%C3%AAte%20%22q%22.png"
    )


def test_rows_carry_the_run_id_and_null_for_version_1() -> None:
    s3 = FakeBucket()
    v1 = finalized(s3, "u1")
    v2 = v2_finalized(s3, "u1", run_id="run-7", sources=[SOURCE])
    for query in (None, {"scope": "all"}, {"scope": "all", "input": "a-1"}):
        rows = request(s3, "GET /artifacts", query=query)[1]["artifacts"]
        run_ids = {r["artifact_id"]: r["run_id"] for r in rows}
        assert run_ids == {v1: None, v2: "run-7"}, query


def test_rows_carry_the_run_function_and_null_for_version_1() -> None:
    s3 = FakeBucket()
    v1 = finalized(s3, "u1")
    v2 = v2_finalized(s3, "u1", run_id="run-7", sources=[SOURCE])
    for query in (None, {"scope": "all"}, {"scope": "all", "input": "a-1"}):
        rows = request(s3, "GET /artifacts", query=query)[1]["artifacts"]
        functions = {r["artifact_id"]: r["run_function"] for r in rows}
        assert functions == {v1: None, v2: "rates"}, query
    rows = request(s3, "GET /artifacts", query={"scope": "all", "run": "run-7"})[1]
    assert [r["run_function"] for r in rows["artifacts"]] == ["rates"]


def test_finalize_writes_by_run_and_withdraw_rewrites_it() -> None:
    s3 = FakeBucket()
    status, out = request(s3, "POST /artifacts", body=V2_GOOD)
    assert status == 201
    aid = str(out["artifact_id"])
    assert not [k for k in s3.objects if k.startswith("index/by-run/")]  # PENDING
    upload(s3, "u1", aid)
    request(s3, "POST /artifacts/{id}/finalize", aid=aid)
    assert index_of(s3, f"index/by-run/r-1/{aid}") == {"state": "COMMITTED"}
    request(s3, "POST /artifacts/{id}/withdraw", aid=aid)
    assert index_of(s3, f"index/by-run/r-1/{aid}") == {"state": "WITHDRAWN"}


def test_a_version_1_record_writes_no_by_run() -> None:
    s3 = FakeBucket()
    finalized(s3, "u1")
    assert not [k for k in s3.objects if k.startswith("index/by-run/")]


def test_by_run_key_quotes_a_slash_and_the_longest_run_id_fits() -> None:
    s3 = FakeBucket()
    aid = v2_finalized(s3, run_id="a/b")
    assert f"index/by-run/a%2Fb/{aid}" in s3.objects
    longest = "/" * artifacts.MAX_TEXT  # quotes to three bytes each
    v2_finalized(s3, run_id=longest)
    assert artifacts.by_run_prefix(longest).count("%2F") == artifacts.MAX_TEXT


def test_delete_removes_the_by_run_entry() -> None:
    s3 = FakeBucket()
    aid = v2_finalized(s3)
    assert request(s3, "DELETE /artifacts/{id}", aid=aid)[0] == 200
    assert f"index/by-run/r-1/{aid}" in s3.deleted
    assert not [k for k in s3.objects if aid in k]


def test_run_lookup_lists_only_that_runs_finished_results() -> None:
    s3 = FakeBucket()
    mine = v2_finalized(s3, "u1", run_id="run-a")
    other = v2_finalized(s3, "u2", run_id="run-a")
    v2_finalized(s3, "u1", run_id="run-b")
    request(s3, "POST /artifacts", sub="u1", body=V2_GOOD)  # PENDING, run r-1
    request(s3, "POST /artifacts/{id}/withdraw", sub="u2", aid=other)

    def ids(sub: str, query: dict[str, str]) -> set[str]:
        status, out = request(s3, "GET /artifacts", sub=sub, query=query)
        assert status == 200, out
        return {a["artifact_id"] for a in out["artifacts"]}

    assert ids("u9", {"scope": "all", "run": "run-a"}) == {mine, other}
    assert ids("u1", {"run": "run-a"}) == {mine}  # no scope: mine
    assert ids("u1", {"scope": "all", "run": "r-1"}) == set()  # PENDING
    assert ids("u1", {"scope": "all", "run": "no-such-run"}) == set()
    rows = request(s3, "GET /artifacts", query={"scope": "all", "run": "run-a"})[1]
    assert {r["run_id"] for r in rows["artifacts"]} == {"run-a"}
    assert {r["author_email"] for r in rows["artifacts"]} == {"u1@x.org", "u2@x.org"}


def test_run_rows_carry_a_typed_inline_link_each() -> None:
    """One `?run=` request gives every row its own link: no per-file GET."""
    s3 = FakeBucket()
    png = v2_finalized(s3, run_id="run-a", content_type="image/png")
    csv = v2_finalized(s3, "u2", run_id="run-a", content_type="text/csv")
    s3.presigned.clear()
    s3.signed.clear()
    status, out = request(s3, "GET /artifacts", query={"scope": "all", "run": "run-a"})
    assert status == 200
    urls = {r["artifact_id"]: r["url"] for r in out["artifacts"]}
    assert urls == {
        png: f"https://signed.example/artifacts/u1/{png}/data/psth.png",
        csv: f"https://signed.example/artifacts/u2/{csv}/data/psth.png",
    }
    by_key = {p["Params"]["Key"]: p for p in s3.presigned}
    assert {p["ClientMethod"] for p in s3.presigned} == {"get_object"}
    assert {p["ExpiresIn"] for p in s3.presigned} == {artifacts.GET_URL_SECONDS}
    png_params = by_key[f"artifacts/u1/{png}/data/psth.png"]["Params"]
    csv_params = by_key[f"artifacts/u2/{csv}/data/psth.png"]["Params"]
    assert png_params["ResponseContentType"] == "image/png"
    assert csv_params["ResponseContentType"] == "text/csv"
    for params in (png_params, csv_params):
        assert params["ResponseContentDisposition"].startswith("inline;")
    assert {r["content_type"] for r in out["artifacts"]} == {"image/png", "text/csv"}
    # Only run rows carry a link.
    rows = request(s3, "GET /artifacts", query={"scope": "all", "input": "a-1"})[1]
    assert all("url" not in r for r in rows["artifacts"])


@pytest.mark.parametrize(
    "query",
    [
        {"run": ""},
        {"run": "x" * 257},
        {"scope": "all", "run": "r-1", "input": "a-1"},
    ],
)
def test_bad_run_is_400_invalid_run(query: dict[str, str]) -> None:
    status, out = request(FakeBucket(), "GET /artifacts", query=query)
    assert (status, out["error"]) == (400, "invalid_run")


def test_all_listing_fills_the_run_id_of_a_by_time_body_written_before_it() -> None:
    s3 = FakeBucket()
    aid = v2_finalized(s3, run_id="run-old")
    key = by_time(s3, aid)
    body = index_of(s3, key)
    del body["run_id"]
    s3.objects[key] = json.dumps(body).encode()
    (row,) = request(s3, "GET /artifacts", query={"scope": "all"})[1]["artifacts"]
    assert row["run_id"] == "run-old"


def test_all_listing_fills_the_run_function_of_a_body_with_only_the_run_id() -> None:
    """A `by-time` body written after `run_id` but before `run_function` is filled."""
    s3 = FakeBucket()
    aid = v2_finalized(s3, run_id="run-old")
    key = by_time(s3, aid)
    body = index_of(s3, key)
    del body["run_function"]
    s3.objects[key] = json.dumps(body).encode()
    (row,) = request(s3, "GET /artifacts", query={"scope": "all"})[1]["artifacts"]
    assert (row["run_id"], row["run_function"]) == ("run-old", "rates")
