"""Shared fixtures: a local SQLite Iceberg catalog and a stubbed-AWS harness."""

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import boto3
import pytest
from botocore.config import Config
from botocore.exceptions import ClientError
from botocore.stub import ANY, Stubber
from pyiceberg.catalog.sql import SqlCatalog

from asteroidea_infra import schema
from asteroidea_infra.catalog_init import iceberg_schema
from asteroidea_ingest import catalog_api, commit, owners, sessions
from asteroidea_ingest.catalog_writer import CatalogWriter
from asteroidea_ingest.commit import CommitDeps
from asteroidea_ingest.sessions import SessionTable
from asteroidea_ingest.uploads_api import SessionApi

from . import ddb_rules

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from pyiceberg.catalog import Catalog

TABLE = "sessions"
BUCKET = "raw-bucket"
CRC = "AAAAAAAAAAA="
OTHER_CRC = "AQEBAQEBAQE="
TOKEN = "tok-1"
SID = "11111111-1111-4111-8111-111111111111"
COMMITTED_AT = datetime(2026, 10, 25, 12, 0, 0, tzinfo=UTC).replace(tzinfo=None)
RAW_KEY = "raw/ds1/sub-01/anat/sub-01_T1w.nii"
COMMIT_QUEUE_URL = (
    "https://sqs.us-east-1.amazonaws.com/000000000000/asteroidea-commit.fifo"
)
EXTRACT_QUEUE_URL = (
    "https://sqs.us-east-1.amazonaws.com/000000000000/asteroidea-extract.fifo"
)


def _client(service: str) -> Any:
    client = boto3.client(
        service,
        config=Config(signature_version="s3v4"),
        region_name="us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )
    return ddb_rules.install(client) if service == "dynamodb" else client


class FakeOwners:
    """An in-memory owners table for the conditions `owners.py` writes (fixture)."""

    def __init__(self, rows: dict[str, dict[str, Any]] | None = None) -> None:
        self.rows = rows or {}
        self.puts: list[dict[str, Any]] = []
        self.gets = 0
        # Called with the dataset before a put: a test inserts a racing row here.
        self.before_put: Any = None

    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        self.gets += 1
        key = sessions.unmarshal(kwargs["Key"])["dataset"]
        row = self.rows.get(key)
        return {"Item": sessions.marshal(row)} if row is not None else {}

    def put_item(self, **kwargs: Any) -> dict[str, Any]:
        ddb_rules.check(kwargs)
        item = sessions.unmarshal(kwargs["Item"])
        values = sessions.unmarshal(kwargs.get("ExpressionAttributeValues", {}))
        self.puts.append({"item": item, "condition": kwargs["ConditionExpression"]})
        if self.before_put is not None:
            self.before_put(item["dataset"])
        current = self.rows.get(item["dataset"])
        condition = kwargs["ConditionExpression"]
        ok = current is None or (
            condition == "attribute_not_exists(dataset) OR owner_sub = :sub"
            and current["owner_sub"] == values[":sub"]
        )
        ok = ok or (
            condition == "attribute_not_exists(dataset) OR owner_kind = :admin"
            and current["owner_kind"] == values[":admin"]
        )
        if not ok:
            raise ClientError(
                {"Error": {"Code": "ConditionalCheckFailedException", "Message": ""}},
                "PutItem",
            )
        self.rows[item["dataset"]] = item
        return {}


def owned_by(sub: str, dataset: str = "ds1") -> dict[str, dict[str, Any]]:
    return {
        dataset: {
            "dataset": dataset,
            "owner_sub": sub,
            "owner_kind": owners.RESEARCHER,
            "claimed_at": 1,
        }
    }


def session_item(**overrides: Any) -> dict[str, Any]:
    item = {
        "session_id": SID,
        "state": sessions.FINALIZING,
        "owner_sub": "user-1",
        "dataset": "ds1",
        "relpath": "sub-01/anat/sub-01_T1w.nii",
        "size": 100,
        "client_crc64nvme": CRC,
        "landing_key": f"landing/{SID}/sub-01/anat/sub-01_T1w.nii",
        "raw_key": RAW_KEY,
        "finalize_token": TOKEN,
        "created_at": 1,
        "updated_at": 1,
        "expires_at": 10**6,
    }
    return sessions.marshal({**item, **overrides})


@dataclass
class Harness:
    """Stubbed DynamoDB and S3 clients plus a local catalog."""

    OTHER_CRC = OTHER_CRC
    CRC = CRC
    TABLE = TABLE

    catalog: Catalog
    ddb: Any = field(default_factory=lambda: _client("dynamodb"))
    s3: Any = field(default_factory=lambda: _client("s3"))
    sqs: Any = field(default_factory=lambda: _client("sqs"))
    # `ds1` belongs to `user-1`, so the create check passes without S3 or ddb stubs.
    owners_ddb: FakeOwners = field(
        default_factory=lambda: FakeOwners(owned_by("user-1"))
    )

    def __post_init__(self) -> None:
        self.ddb_stub = Stubber(self.ddb)
        self.s3_stub = Stubber(self.s3)
        self.sqs_stub = Stubber(self.sqs)
        self.sqs_stub.activate()
        self.ddb_stub.activate()
        self.s3_stub.activate()

    def get_item(self, **overrides: Any) -> None:
        self.ddb_stub.add_response("get_item", {"Item": session_item(**overrides)})

    @staticmethod
    def _token_condition() -> dict[str, Any]:
        return {
            "TableName": ANY,
            "Key": ANY,
            "UpdateExpression": ANY,
            "ExpressionAttributeNames": ANY,
            "ExpressionAttributeValues": ANY,
            "ConditionExpression": "#state = :expect AND #finalize_token = :token",
        }

    def update_ok(self) -> None:
        self.ddb_stub.add_response(
            "update_item", {}, expected_params=self._token_condition()
        )

    def update_refused(self) -> None:
        self.ddb_stub.add_client_error(
            "update_item",
            service_error_code="ConditionalCheckFailedException",
            expected_params=self._token_condition(),
        )

    def update_refused_open(self) -> None:
        self.ddb_stub.add_client_error(
            "update_item",
            service_error_code="ConditionalCheckFailedException",
            expected_params={
                "TableName": ANY,
                "Key": ANY,
                "UpdateExpression": ANY,
                "ExpressionAttributeNames": ANY,
                "ExpressionAttributeValues": ANY,
                "ConditionExpression": "#state = :expect",
            },
        )

    def head(self, *, crc: str = CRC, size: int = 100) -> None:
        self.s3_stub.add_response(
            "head_object",
            {"ContentLength": size, "ChecksumCRC64NVME": crc, "VersionId": "lv1"},
        )

    def head_missing(self) -> None:
        self.s3_stub.add_client_error("head_object", service_error_code="404")

    def copy(self, *, crc: str = CRC, version: str = "rv1", key: str = RAW_KEY) -> None:
        """Expect the copy; a verified one (CRC matched) is then locked."""
        self.s3_stub.add_response(
            "copy_object",
            {"VersionId": version, "CopyObjectResult": {"ChecksumCRC64NVME": crc}},
        )
        if crc != OTHER_CRC:  # OTHER_CRC is the mismatch: rolled back, never locked
            self.lock(version=version, key=key)

    def lock(self, *, version: str = "rv1", days: int = 1, key: str = RAW_KEY) -> None:
        """Expect the governance retention call on the verified raw version."""
        self.s3_stub.add_response(
            "put_object_retention",
            {},
            expected_params={
                "Bucket": BUCKET,
                "Key": key,
                "VersionId": version,
                "Retention": {
                    "Mode": "GOVERNANCE",
                    "RetainUntilDate": COMMITTED_AT.replace(tzinfo=UTC)
                    + timedelta(days=days),
                },
            },
        )

    def extract_sent(self, sid: str = SID) -> None:
        """Expect the commit's one extract send for `sid`."""
        self.sqs_stub.add_response(
            "send_message",
            {"MessageId": "m"},
            expected_params={
                "QueueUrl": EXTRACT_QUEUE_URL,
                "MessageBody": f'{{"session_id": "{sid}"}}',
                "MessageGroupId": "catalog",
                "MessageDeduplicationId": f"{sid}:x",
            },
        )

    def run(self, token: str = TOKEN, sid: str = SID) -> str:
        return commit.commit(
            {"session_id": sid, "finalize_token": token},
            CommitDeps(
                table=SessionTable(self.ddb, TABLE),
                s3=self.s3,
                writer=CatalogWriter(self.catalog),
                bucket=BUCKET,
                sqs=self.sqs,
                extract_queue_url=EXTRACT_QUEUE_URL,
            ),
            now=1000,
        )

    def rows(self, table_name: str = schema.TABLE) -> list[dict[str, Any]]:
        table = self.catalog.load_table(f"asteroidea.{table_name}")
        return table.scan().to_arrow().to_pylist()

    def finish(self) -> None:
        self.ddb_stub.assert_no_pending_responses()
        self.s3_stub.assert_no_pending_responses()
        self.sqs_stub.assert_no_pending_responses()

    def api(self) -> SessionApi:
        return SessionApi(
            table=SessionTable(self.ddb, TABLE),
            s3=self.s3,
            sqs=self.sqs,
            bucket=BUCKET,
            commit_queue_url=COMMIT_QUEUE_URL,
            dataset_owners=owners.Owners(self.owners_ddb, "owners"),
        )


@pytest.fixture
def catalog(tmp_path: Path) -> Iterator[Catalog]:
    cat = SqlCatalog(
        "test",
        uri=f"sqlite:///{tmp_path}/catalog.db",
        warehouse=f"file://{tmp_path}/warehouse",
    )
    cat.create_namespace("asteroidea")
    for spec in (schema.ASSETS, schema.DATASETS_SPEC):
        cat.create_table(f"asteroidea.{spec.name}", iceberg_schema(spec))
    yield cat
    cat.engine.dispose()


@pytest.fixture(autouse=True)
def _retention_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAW_RETENTION_DAYS", "1")
    monkeypatch.setattr(commit, "utc_now_naive", lambda: COMMITTED_AT)


@pytest.fixture(autouse=True)
def _fresh_download_cache() -> None:
    """Each test starts a fresh execution environment for the presign cache."""
    catalog_api.clear_download_cache()


@pytest.fixture
def harness(catalog: Catalog) -> Harness:
    return Harness(catalog)


def jwt_event(
    route: str,
    *,
    sub: str = "user-1",
    groups: str = "[researchers]",
    body: object = None,
    sid: str | None = None,
    **context: str,
) -> dict[str, Any]:
    """Build an HTTP API v2 JWT event; `context` adds requestContext fields."""
    return {
        "routeKey": route,
        "pathParameters": {"id": sid} if sid is not None else None,
        "body": body if isinstance(body, str) or body is None else json.dumps(body),
        "requestContext": {
            **context,
            "authorizer": {"jwt": {"claims": {"sub": sub, "cognito:groups": groups}}},
        },
    }
