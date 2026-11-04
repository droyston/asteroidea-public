"""SDK run records and the create contract."""

import gzip
import io
import json
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pyarrow as pa
import pytest

import asteroidea
from asteroidea import _crc, _creds, _image, _nwb, _query, _run, _sql
from asteroidea._artifacts import Artifacts
from asteroidea._http import Reply
from asteroidea._query import FileRef, QueryResult
from asteroidea._run import SCHEMA, Run, active, snapshot_of
from asteroidea.errors import ArtifactRefusedError, NestedRunError, QueryError

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

    from asteroidea._auth import Auth

WAREHOUSE = "s3://asteroidea-warehouse-000000000000/asteroidea.db"
LOCATIONS = {
    "assets": f"{WAREHOUSE}/assets/metadata/00007-a.metadata.json",
    "asset_headers": f"{WAREHOUSE}/asset_headers/metadata/00003-b.gz.metadata.json",
    "units": f"{WAREHOUSE}/units/metadata/00001-c.metadata.json",
    "events": f"{WAREHOUSE}/events/metadata/00000-d.metadata.json",
}
# current-snapshot-id per metadata key; events has no snapshot yet (-1).
SNAPSHOTS = {"assets": 7001, "asset_headers": 3002, "units": 1003, "events": -1}
PUT_URL = "https://bucket.example.test/artifacts/u/a1/data/rates.csv?sig=x"
ARTIFACT = "3f0c8a8e-5b7e-4d0a-9c39-2f0d6c1b9a11"


def _metadata(name: str) -> bytes:
    # The Iceberg table metadata JSON, cut to the fields a reader needs.
    body = json.dumps(
        {
            "format-version": 2,
            "table-uuid": "6c1d0f9e-0000-4000-8000-000000000000",
            "location": f"{WAREHOUSE}/{name}",
            "current-snapshot-id": SNAPSHOTS[name],
            "snapshots": [],
        }
    ).encode()
    return (
        gzip.compress(body) if LOCATIONS[name].endswith(".gz.metadata.json") else body
    )


class FakeGlue:
    """Glue `GetTable`: the reply shape `_sql.metadata_locations` reads."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def get_table(self, **kwargs: str) -> dict[str, Any]:
        name = kwargs["Name"]
        assert kwargs["DatabaseName"] == "asteroidea"
        self.calls.append(name)
        return {
            "Table": {
                "Name": name,
                "Parameters": {
                    "table_type": "ICEBERG",
                    "metadata_location": LOCATIONS[name],
                },
            }
        }


class FakeS3:
    """S3 `GetObject`: `Body` is a stream with `read()`."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def get_object(self, **kwargs: str) -> dict[str, Any]:
        bucket, key = kwargs["Bucket"], kwargs["Key"]
        self.calls.append((bucket, key))
        name = next(n for n, loc in LOCATIONS.items() if loc == f"s3://{bucket}/{key}")
        return {"Body": io.BytesIO(_metadata(name)), "ContentLength": 1}


class FakeSession:
    def __init__(self) -> None:
        self.glue = FakeGlue()
        self.s3 = FakeS3()
        self.clients: list[str] = []

    def client(self, name: str) -> object:
        self.clients.append(name)
        return {"glue": self.glue, "s3": self.s3}[name]


class FakeEngine:
    def run(
        self,
        _query: str,
        _params: Sequence[str | None] | None,
        locations: Mapping[str, str],
        _credential: object,
    ) -> pa.Table:
        return pa.table({"table": list(locations)})


@pytest.fixture
def cloud(monkeypatch: pytest.MonkeyPatch) -> FakeSession:
    session = FakeSession()
    monkeypatch.setattr(_creds, "credentials", lambda _auth: object())
    monkeypatch.setattr(_creds, "session", lambda _auth: session)
    monkeypatch.setattr(_sql, "_engine", lambda _region: FakeEngine())
    return session


class Platform:
    """The artifacts routes: records each request's bearer and the create body."""

    def __init__(self, api: str, create: Reply | None = None) -> None:
        self.api = api
        self.create = create or Reply(
            201, json.dumps({"artifact_id": "a1", "url": PUT_URL}).encode()
        )
        self.bearer: dict[str, str | None] = {}
        self.body: dict[str, Any] = {}

    def __call__(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | Path | None,
    ) -> Reply:
        if url == PUT_URL:
            assert "Authorization" not in headers
            return Reply(200, b"")
        route = {
            ("POST", f"{self.api}/artifacts"): "create",
            ("POST", f"{self.api}/artifacts/a1/finalize"): "finalize",
        }[method, url]
        self.bearer[route] = headers.get("Authorization")
        if route == "create":
            assert isinstance(body, bytes)
            self.body = json.loads(body)
            return self.create
        return Reply(200, b'{"state": "COMMITTED"}')


@pytest.fixture
def result_file(tmp_path: Path) -> Path:
    path = tmp_path / "rates.csv"
    path.write_bytes(b"unit,rate\n0,1.5\n")
    return path


def _ref(asset_id: str, version: str) -> FileRef:
    return FileRef(asset_id, "ds", "sub-01/x.nwb", "raw/ds/sub-01/x.nwb", version, 10)


# Contract of `validate_create` (version 2), from the
# plan's table: kind -> the rule for `version`.
KINDS = {
    "asset": str,
    "artifact": None,
    "table_snapshot": int,
    "defaults": str,
    "recipe": str,
}


def assert_v2_body(body: dict[str, Any]) -> None:
    assert body["schema"] == "asteroidea.provenance/2"
    assert isinstance(body["name"], str)
    assert "/" not in body["name"]
    assert isinstance(body["size"], int)
    assert body["size"] >= 1
    assert isinstance(body["crc64nvme"], str)
    assert isinstance(body["content_type"], str)
    assert body["content_type"]
    sources, inputs = body.get("sources", []), body.get("inputs", [])
    assert len(sources) + len(inputs) >= 1
    assert len(sources) <= 1000
    assert len(inputs) <= 1000
    for source in sources:
        assert set(source) == {"asset_id", "s3_version_id"}
        assert all(isinstance(v, str) and v for v in source.values())
    for item in inputs:
        assert {"kind", "id"} <= set(item) <= {"kind", "id", "version", "role"}
        rule = KINDS[item["kind"]]
        if rule is None:
            assert item.get("version") is None
        else:
            assert type(item["version"]) is rule
    if "snapshot_id" in body:
        assert type(body["snapshot_id"]) is int
    run = body["run"]
    assert {"run_id", "function", "parameters"} <= set(run)
    assert set(run) <= {
        "run_id",
        "function",
        "parameters",
        "defaults_version",
        "sdk_version",
        "analysis_sha256",
        "environment",
    }
    assert isinstance(run["parameters"], dict)
    assert len(json.dumps(run["parameters"]).encode()) <= 64 * 1024


def test_sql_in_a_run_records_each_table_snapshot(
    cloud: FakeSession, auth: Auth
) -> None:
    with Run("rates", auth=auth) as r:
        _sql.sql(auth, "SELECT * FROM files JOIN units USING (asset_id)")
    assert list(r.inputs.values()) == [
        {
            "kind": "table_snapshot",
            "id": "asteroidea.assets",
            "version": 7001,
            "role": "sql",
        },
        {
            "kind": "table_snapshot",
            "id": "asteroidea.asset_headers",
            "version": 3002,
            "role": "sql",
        },
        {
            "kind": "table_snapshot",
            "id": "asteroidea.units",
            "version": 1003,
            "role": "sql",
        },
    ]
    bucket = "asteroidea-warehouse-000000000000"
    assert cloud.s3.calls == [
        (bucket, "asteroidea.db/assets/metadata/00007-a.metadata.json"),
        (bucket, "asteroidea.db/asset_headers/metadata/00003-b.gz.metadata.json"),
        (bucket, "asteroidea.db/units/metadata/00001-c.metadata.json"),
    ]


def test_one_metadata_read_per_location_in_a_run(
    cloud: FakeSession, auth: Auth
) -> None:
    with Run("rates", auth=auth) as r:
        _sql.sql(auth, "SELECT * FROM units")
        _sql.sql(auth, "SELECT count(*) FROM units")
    assert len(cloud.s3.calls) == 1
    assert len(r.inputs) == 1


def test_a_table_with_no_snapshot_is_not_recorded(
    cloud: FakeSession, auth: Auth
) -> None:
    with Run("rates", auth=auth) as r:
        _sql.sql(auth, "SELECT * FROM events")
    assert cloud.s3.calls
    assert r.inputs == {}


def test_sql_outside_a_run_reads_no_metadata(cloud: FakeSession, auth: Auth) -> None:
    _sql.sql(auth, "SELECT * FROM units")
    assert cloud.clients == ["glue"]
    assert cloud.s3.calls == []


def test_an_unreadable_metadata_file_is_a_query_error() -> None:
    class Broken:
        def get_object(self, **_: str) -> dict[str, Any]:
            return {"Body": io.BytesIO(b"not json")}

    with pytest.raises(QueryError, match="units"):
        snapshot_of(Broken(), "units", LOCATIONS["units"])


@pytest.fixture
def opened(monkeypatch: pytest.MonkeyPatch, auth: Auth) -> list[str]:
    calls: list[str] = []

    def open_asset(_auth: Auth, asset_id: str) -> object:
        calls.append(asset_id)
        return SimpleNamespace(source=SimpleNamespace(version_id=f"ver-{asset_id}"))

    monkeypatch.setattr(asteroidea, "default_auth", lambda: auth)
    monkeypatch.setattr(_image, "open_asset", open_asset)
    monkeypatch.setattr(_nwb, "read_nwb", lambda remote: ("nwb", remote))
    return calls


def test_open_image_records_the_asset_version(
    opened: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_image, "image_over", lambda remote: ("image", remote))
    with Run("anatomy") as r:
        asteroidea.open_image("t1-1")
    assert opened == ["t1-1"]
    assert list(r.inputs.values()) == [
        {"kind": "asset", "id": "t1-1", "version": "ver-t1-1", "role": "open_image"},
    ]


def test_open_file_and_open_nwb_record_the_asset_version(opened: list[str]) -> None:
    with Run("rates") as r:
        asteroidea.open_file("a-1")
        asteroidea.open_nwb("a-2")
        asteroidea.open_file("a-1")
    assert opened == ["a-1", "a-2", "a-1"]
    assert list(r.inputs.values()) == [
        {"kind": "asset", "id": "a-1", "version": "ver-a-1", "role": "open_file"},
        {"kind": "asset", "id": "a-2", "version": "ver-a-2", "role": "open_nwb"},
    ]


def test_open_outside_a_run_records_nothing(opened: list[str]) -> None:
    assert active() is None
    asteroidea.open_file("a-1")
    assert opened == ["a-1"]


@pytest.fixture
def queried(monkeypatch: pytest.MonkeyPatch, auth: Auth) -> list[QueryResult]:
    replies = [
        QueryResult(
            rows=[], files=[_ref("a-1", "v1"), _ref("a-2", "v2")], snapshot_id=42
        ),
        QueryResult(rows=[], files=[_ref("a-1", "v1")], snapshot_id=42),
        QueryResult(rows=[], files=[_ref("a-3", "v3")], snapshot_id=43),
    ]
    monkeypatch.setattr(asteroidea, "default_auth", lambda: auth)
    monkeypatch.setattr(_query, "query", lambda *_, **__: replies.pop(0))
    return replies


def test_query_records_its_files_and_one_snapshot(queried: list[QueryResult]) -> None:
    with Run("rates") as r:
        asteroidea.query(dataset="ds")
        asteroidea.query(dataset="ds", subject="01")
    record = r.record()
    assert record["sources"] == [
        {"asset_id": "a-1", "s3_version_id": "v1"},
        {"asset_id": "a-2", "s3_version_id": "v2"},
    ]
    assert record["inputs"] == [
        {
            "kind": "table_snapshot",
            "id": "asteroidea.assets",
            "version": 42,
            "role": "query",
        }
    ]
    assert record["snapshot_id"] == 42
    assert len(queried) == 1


def test_two_query_snapshots_leave_out_the_top_level_one(
    queried: list[QueryResult],
) -> None:
    with Run("rates") as r:
        asteroidea.query(dataset="ds")
        asteroidea.query(dataset="ds")
        asteroidea.query(dataset="ds")
    assert len(queried) == 0
    record = r.record()
    assert "snapshot_id" not in record
    assert [i["version"] for i in record["inputs"]] == [42, 43]


def test_a_nested_run_is_refused_and_the_outer_run_stays_open() -> None:
    with Run("outer") as outer:
        with pytest.raises(NestedRunError), Run("inner"):
            pass
        assert active() is outer
    assert active() is None
    with Run("next") as again:
        assert active() is again


def test_run_parameters_must_be_json() -> None:
    with pytest.raises(ArtifactRefusedError, match="JSON"):
        Run("rates", {"window": object()})
    with pytest.raises(ArtifactRefusedError, match="64 KiB"):
        Run("rates", {"big": "x" * (64 * 1024)})


@pytest.mark.usefixtures("cloud", "queried")
def test_save_sends_a_version_2_body_the_server_accepts(
    auth: Auth, result_file: Path
) -> None:
    platform = Platform(auth.config.api_url)
    with Run(
        "rates", {"bin_s": 0.05, "event": "stim_on"}, auth=auth, transport=platform
    ) as r:
        asteroidea.query(dataset="ds")
        _sql.sql(auth, "SELECT * FROM units")
        r.add_input("artifact", ARTIFACT, None, "opened")
        result = r.save_artifact(result_file, note="first pass")
    body = platform.body
    assert_v2_body(body)
    assert body["schema"] == SCHEMA
    assert body["name"] == "rates.csv"
    assert body["crc64nvme"] == _crc.crc64nvme(result_file)
    assert body["content_type"] == "text/csv"
    assert body["note"] == "first pass"
    assert body["snapshot_id"] == 42
    assert body["run"]["run_id"] == r.run_id
    assert body["run"]["function"] == "rates"
    assert body["run"]["parameters"] == {"bin_s": 0.05, "event": "stim_on"}
    assert body["run"]["sdk_version"] == body["sdk_version"]
    assert len(body["run"]["analysis_sha256"]) == 64
    assert [(i["kind"], i["id"]) for i in body["inputs"]] == [
        ("table_snapshot", "asteroidea.assets"),
        ("table_snapshot", "asteroidea.units"),
        ("artifact", ARTIFACT),
    ]
    assert (result.artifact_id, result.snapshot_id) == ("a1", 42)


@pytest.mark.usefixtures("queried")
def test_the_id_token_is_the_bearer_on_the_create_only(
    auth: Auth, result_file: Path
) -> None:
    platform = Platform(auth.config.api_url)
    with Run("rates", auth=auth, transport=platform) as r:
        asteroidea.query(dataset="ds")
    r.save_artifact(result_file)
    tokens = auth.tokens()
    assert tokens.id_token != tokens.access_token
    assert platform.bearer == {
        "create": f"Bearer {tokens.id_token}",
        "finalize": f"Bearer {tokens.access_token}",
    }


@pytest.mark.usefixtures("queried")
def test_a_version_1_save_in_a_run_stays_version_1(
    auth: Auth, result_file: Path
) -> None:
    platform = Platform(auth.config.api_url)
    with Run("rates", auth=auth, transport=platform):
        found = asteroidea.query(dataset="ds")
        Artifacts(auth, platform).save(result_file, sources=found.files, snapshot_id=42)
    assert "schema" not in platform.body
    assert "run" not in platform.body
    assert platform.bearer["create"] == f"Bearer {auth.tokens().access_token}"


def test_settings_inputs_and_defaults_version_reach_the_body(
    auth: Auth, result_file: Path
) -> None:
    # `defaults` and `recipe` inputs take text versions, and
    # `run.defaults_version` is optional text (server `RUN_TEXT_OPTIONAL`).
    platform = Platform(auth.config.api_url)
    with Run("screen", auth=auth, transport=platform) as r:
        _run.record_settings("dandi-000623", "defaults-1", "recipe-1", "rates")
        _run.record_settings("dandi-000623", "defaults-1", "recipe-1", "tuning")
    r.save_artifact(result_file)
    body = platform.body
    assert_v2_body(body)
    assert body["inputs"] == [
        {
            "kind": "defaults",
            "id": "analysis_defaults/dandi-000623",
            "version": "defaults-1",
            "role": "rates",
        },
        {
            "kind": "recipe",
            "id": "recipes/dandi-000623",
            "version": "recipe-1",
            "role": "rates",
        },
    ]
    assert body["run"]["defaults_version"] == "defaults-1"


def test_two_defaults_versions_leave_out_run_defaults_version() -> None:
    with Run("screen") as r:
        _run.record_settings("ds-a", "defaults-1", "recipe-1", "rates")
        _run.record_settings("ds-b", "defaults-2", "recipe-1", "rates")
    assert "defaults_version" not in r.record()["run"]
    assert len(r.inputs) == 4


def test_record_settings_outside_a_run_does_nothing() -> None:
    _run.record_settings("ds-a", "defaults-1", "recipe-1", "rates")
    assert active() is None


def test_a_run_that_read_nothing_is_refused_before_any_request(
    auth: Auth, result_file: Path
) -> None:
    platform = Platform(auth.config.api_url)
    with Run("rates", auth=auth, transport=platform) as r:
        pass
    with pytest.raises(ArtifactRefusedError, match="read nothing"):
        r.save_artifact(result_file)
    assert platform.bearer == {}
