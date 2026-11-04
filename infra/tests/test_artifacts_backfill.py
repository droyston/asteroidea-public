"""`make artifacts-backfill` over refusing fakes."""

import io
import json
from typing import TYPE_CHECKING, Any

from botocore.exceptions import ClientError

from asteroidea_infra import artifacts_backfill as bf

if TYPE_CHECKING:
    from collections.abc import Callable

    import pytest

BUCKET = "artifacts-bucket"
POOL = "us-east-1_pool"
SUB = "11111111-1111-1111-1111-111111111111"
AID = "22222222-2222-2222-2222-222222222222"
KEY = f"artifacts/{SUB}/{AID}/provenance.json"
PRECONDITION = "PreconditionFailed"
NO_KEY = "NoSuchKey"
NOT_FOUND = "404"
PUT_ARGS = {"Bucket", "Key", "Body", "ContentType", "IfMatch", "IfNoneMatch"}


def _err(code: str, op: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, op)


class FakeS3:
    """Refuses what S3 refuses: unknown arguments and failed If-Match, If-None-Match."""

    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, int]] = {}
        self.writes: list[str] = []
        self.fail_put: dict[str, str] = {}
        self.after_get: Callable[[], object] | None = None

    def seed(self, key: str, body: dict[str, Any] | bytes) -> None:
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.objects[key] = (raw, 1)

    def _known(self, kwargs: dict[str, object], allowed: set[str], op: str) -> None:
        extra = set(kwargs) - allowed
        if extra:
            msg = f"{op}: unknown parameter(s) {sorted(extra)}"
            raise TypeError(msg)
        assert kwargs["Bucket"] == BUCKET

    def list_objects_v2(self, **kwargs: object) -> dict[str, Any]:
        self._known(
            kwargs,
            {"Bucket", "Prefix", "MaxKeys", "ContinuationToken"},
            "ListObjectsV2",
        )
        assert isinstance(kwargs["MaxKeys"], int)
        assert 1 <= kwargs["MaxKeys"] <= 1000
        keys = sorted(k for k in self.objects if k.startswith(str(kwargs["Prefix"])))
        start = int(str(kwargs.get("ContinuationToken", "0")))
        size = 2  # small pages, to exercise continuation
        page = keys[start : start + size]
        out: dict[str, Any] = {"Contents": [{"Key": k} for k in page]}
        if start + size < len(keys):
            out["NextContinuationToken"] = str(start + size)
        return out

    def get_object(self, **kwargs: object) -> dict[str, Any]:
        self._known(kwargs, {"Bucket", "Key"}, "GetObject")
        key = str(kwargs["Key"])
        if key not in self.objects:
            raise _err(NO_KEY, "GetObject")
        raw, etag = self.objects[key]
        if self.after_get is not None:
            self.after_get()
        return {"Body": io.BytesIO(raw), "ETag": f'"{etag}"'}

    def head_object(self, **kwargs: object) -> dict[str, Any]:
        self._known(kwargs, {"Bucket", "Key"}, "HeadObject")
        if str(kwargs["Key"]) not in self.objects:
            raise _err(NOT_FOUND, "HeadObject")
        return {}

    def put_object(self, **kwargs: object) -> dict[str, Any]:
        self._known(kwargs, PUT_ARGS, "PutObject")
        key = str(kwargs["Key"])
        assert isinstance(kwargs["Body"], bytes)
        if key in self.fail_put:
            raise _err(self.fail_put[key], "PutObject")
        if kwargs.get("IfNoneMatch") == "*" and key in self.objects:
            raise _err(PRECONDITION, "PutObject")
        if "IfMatch" in kwargs and (
            key not in self.objects or f'"{self.objects[key][1]}"' != kwargs["IfMatch"]
        ):
            raise _err(PRECONDITION, "PutObject")
        etag = self.objects.get(key, (b"", 0))[1] + 1
        self.objects[key] = (kwargs["Body"], etag)
        self.writes.append(key)
        return {}


class FakeCognito:
    """Refuses what Cognito refuses: a missing pool id and a malformed filter."""

    def __init__(self, users: dict[str, str]) -> None:
        self.users = users
        self.calls = 0
        self.error: ClientError | None = None

    def list_users(self, **kwargs: object) -> dict[str, Any]:
        assert set(kwargs) <= {"UserPoolId", "Filter", "Limit"}
        assert kwargs["UserPoolId"] == POOL
        filt = str(kwargs["Filter"])
        assert filt.startswith('sub = "')
        assert filt.endswith('"')
        self.calls += 1
        if self.error is not None:
            raise self.error
        email = self.users.get(filt[len('sub = "') : -1])
        if email is None:
            return {"Users": []}
        return {"Users": [{"Attributes": [{"Name": "email", "Value": email}]}]}


def _v1(**over: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "artifact_id": AID,
        "author_sub": SUB,
        "name": "out.csv",
        "size": 3,
        "crc64nvme": "AAAA",
        "content_type": "text/csv",
        "snapshot_id": 7,
        "sources": [{"asset_id": "a/1"}],
        "note": None,
        "sdk_version": "0.1",
        "state": "COMMITTED",
        "created_at": 1000,
        "finalized_at": 2000,
    }
    return {**record, **over}


def _run(s3: FakeS3, cognito: FakeCognito, *, dry: bool = False) -> int:
    return bf.run(s3, cognito, bucket=BUCKET, pool=POOL, dry=dry)


def test_fills_email_data_key_and_all_three_indexes(
    capsys: pytest.CaptureFixture[str],
) -> None:
    s3, cognito = FakeS3(), FakeCognito({SUB: "a@b.org"})
    s3.seed(KEY, _v1())
    assert _run(s3, cognito) == 0
    record = json.loads(s3.objects[KEY][0])
    assert record["author_email"] == "a@b.org"
    assert record["data_key"] == f"artifacts/{SUB}/{AID}/data/out.csv"
    assert record["snapshot_id"] == 7
    assert json.loads(s3.objects[f"index/by-id/{AID}"][0]) == {
        "author_sub": SUB,
        "state": "COMMITTED",
    }
    assert json.loads(s3.objects[f"index/by-input/a%2F1/{AID}"][0]) == {
        "state": "COMMITTED"
    }
    by_time = [k for k in s3.objects if k.startswith("index/by-time/")]
    assert len(by_time) == 1
    assert by_time[0].endswith(f"-{AID}")
    assert json.loads(s3.objects[by_time[0]][0])["author_email"] == "a@b.org"
    assert f"PASS {KEY}: wrote author_email, data_key" in capsys.readouterr().out


def test_second_run_writes_nothing() -> None:
    s3, cognito = FakeS3(), FakeCognito({SUB: "a@b.org"})
    s3.seed(KEY, _v1())
    assert _run(s3, cognito) == 0
    first = dict(s3.objects)
    s3.writes.clear()
    assert _run(s3, cognito) == 0
    assert s3.writes == []
    assert s3.objects == first


def test_never_rewrites_a_filled_field_or_an_existing_index() -> None:
    s3, cognito = FakeS3(), FakeCognito({SUB: "new@b.org"})
    s3.seed(
        KEY, _v1(author_email="old@b.org", data_key="artifacts/custom/data/x", note="n")
    )
    s3.seed(f"index/by-id/{AID}", {"author_sub": SUB, "state": "WITHDRAWN"})
    assert _run(s3, cognito) == 0
    record = json.loads(s3.objects[KEY][0])
    assert record["author_email"] == "old@b.org"
    assert record["data_key"] == "artifacts/custom/data/x"
    assert KEY not in s3.writes
    assert json.loads(s3.objects[f"index/by-id/{AID}"][0])["state"] == "WITHDRAWN"
    assert cognito.calls == 0


def test_fills_only_the_email_a_late_v1_record_lacks() -> None:
    s3, cognito = FakeS3(), FakeCognito({SUB: "a@b.org"})
    s3.seed(KEY, _v1(data_key=f"artifacts/{SUB}/{AID}/data/out.csv"))
    assert _run(s3, cognito) == 0
    s3.seed(
        KEY, _v1(data_key=f"artifacts/{SUB}/{AID}/data/out.csv")
    )  # an older wheel wrote it again, without an email
    s3.writes.clear()
    assert _run(s3, cognito) == 0
    assert s3.writes == [KEY]


def test_dry_run_lists_and_writes_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    s3, cognito = FakeS3(), FakeCognito({SUB: "a@b.org"})
    s3.seed(KEY, _v1())
    before = dict(s3.objects)
    assert _run(s3, cognito, dry=True) == 0
    assert s3.objects == before
    assert s3.writes == []
    out = capsys.readouterr().out
    assert f"PASS {KEY}: would write author_email, data_key" in out
    assert "dry run, nothing written" in out


def test_pending_record_gets_by_id_and_by_input_but_no_by_time() -> None:
    s3, cognito = FakeS3(), FakeCognito({SUB: "a@b.org"})
    s3.seed(KEY, _v1(state="PENDING", finalized_at=None))
    assert _run(s3, cognito) == 0
    assert f"index/by-id/{AID}" in s3.objects
    assert not [k for k in s3.objects if k.startswith("index/by-time/")]


def test_version_2_inputs_each_get_a_by_input_entry() -> None:
    s3, cognito = FakeS3(), FakeCognito({SUB: "a@b.org"})
    s3.seed(
        KEY,
        _v1(
            schema="asteroidea.provenance/2",
            sources=[],
            inputs=[{"kind": "artifact", "id": "x-1"}, {"kind": "recipe", "id": "r"}],
        ),
    )
    assert _run(s3, cognito) == 0
    assert f"index/by-input/x-1/{AID}" in s3.objects
    assert f"index/by-input/r/{AID}" in s3.objects


def test_unknown_author_fails_but_writes_the_rest_and_a_rerun_fills_by_time(
    capsys: pytest.CaptureFixture[str],
) -> None:
    s3, cognito = FakeS3(), FakeCognito({})
    s3.seed(KEY, _v1())
    assert _run(s3, cognito) == 1
    out = capsys.readouterr().out
    assert f"FAIL {KEY}: LookupError: no Cognito user with sub {SUB}" in out
    assert f"index/by-id/{AID}" in s3.objects
    assert not [k for k in s3.objects if k.startswith("index/by-time/")]
    cognito.users[SUB] = "a@b.org"
    assert _run(s3, cognito) == 0
    assert [k for k in s3.objects if k.startswith("index/by-time/")]


def test_cognito_error_text_is_surfaced(capsys: pytest.CaptureFixture[str]) -> None:
    s3, cognito = FakeS3(), FakeCognito({SUB: "a@b.org"})
    cognito.error = _err("AccessDeniedException", "ListUsers")
    s3.seed(KEY, _v1())
    assert _run(s3, cognito) == 1
    assert "AccessDeniedException" in capsys.readouterr().out


def test_s3_error_text_is_surfaced_and_other_records_continue(
    capsys: pytest.CaptureFixture[str],
) -> None:
    other = "33333333-3333-3333-3333-333333333333"
    s3, cognito = FakeS3(), FakeCognito({SUB: "a@b.org"})
    s3.seed(KEY, _v1())
    s3.seed(f"artifacts/{SUB}/{other}/provenance.json", _v1(artifact_id=other))
    s3.fail_put[KEY] = "SlowDown"
    assert _run(s3, cognito) == 1
    out = capsys.readouterr().out
    assert f"FAIL {KEY}: ClientError" in out
    assert "SlowDown" in out
    assert f"PASS artifacts/{SUB}/{other}/provenance.json" in out


def test_a_record_changed_since_it_was_read_is_refused_not_overwritten() -> None:
    s3, cognito = FakeS3(), FakeCognito({SUB: "a@b.org"})
    s3.seed(KEY, _v1())
    s3.after_get = lambda: s3.objects.__setitem__(KEY, (s3.objects[KEY][0], 9))
    assert _run(s3, cognito) == 1
    assert s3.writes == []


def test_only_record_files_are_visited(capsys: pytest.CaptureFixture[str]) -> None:
    s3, cognito = FakeS3(), FakeCognito({SUB: "a@b.org"})
    s3.seed(KEY, _v1())
    s3.seed(f"artifacts/{SUB}/{AID}/data/provenance.json", b"not a record")
    s3.seed(f"artifacts/{SUB}/{AID}/data/out.csv", b"1,2,3")
    assert _run(s3, cognito) == 0
    assert "1 record(s), 0 FAIL" in capsys.readouterr().out
