"""Labels Lambda behind the HTTP API.

Researchers' labels live in the DynamoDB `LabelsTable`, outside the catalog. Every
researcher reads every label; only the author updates or deletes one, enforced by a
`ConditionExpression`, not by a read-then-write. The author is the JWT `sub`. A new
label carries server-stamped provenance: the `s3_version_id` of the catalog asset row
and, per table the timeline draws against, the current Iceberg snapshot id read
through Athena `"<table>$snapshots"`.

Caps are checked before the put and are soft under concurrency: 2,000 labels an asset,
500 an author on an asset.
"""

import gzip
import json
import math
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Final

import boto3
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import ClientError

from asteroidea_ingest.api_common import (
    ApiError,
    Invocation,
    Request,
    Response,
    Timing,
    json_response,
    serve,
)
from asteroidea_ingest.athena_query import BUDGET_SECONDS, AthenaRunner
from asteroidea_ingest.sessions import CONDITIONAL_FAILED

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from asteroidea_ingest.aws_types import (
        Athena,
        Glue,
        ItemsQueryArgs,
        LabelStore,
        ObjectReader,
    )

__all__ = ["DISPATCH", "LabelsApi", "handler"]

DISPATCH: Final = {
    "GET /files/{asset_id}/labels": "list_labels",
    "POST /files/{asset_id}/labels": "create",
    "PATCH /files/{asset_id}/labels/{label_id}": "update",
    "DELETE /files/{asset_id}/labels/{label_id}": "delete",
}
MAX_PER_ASSET: Final = 2000
MAX_PER_AUTHOR: Final = 500
NAME_MAX: Final = 64
NOTE_MAX_BYTES: Final = 1024
SERIES_MAX: Final = 256
MAX_TIME_S: Final = 1.0e7  # about 115 days: far beyond any recording
ID_MAX: Final = 128
SNAPSHOT_TABLES: Final = ("intervals", "events", "asset_headers")
GZIP_MAGIC: Final = b"\x1f\x8b"
NO_SNAPSHOT: Final = -1
ASSET_SQL: Final = "SELECT dataset, s3_version_id FROM assets WHERE asset_id = ?"
_SER = TypeSerializer()
_DESER = TypeDeserializer()
_INTEGER_FIELDS: Final = frozenset({"created_at", "updated_at", "version"})


def _plain(value: object) -> object:
    """Make a value DynamoDB can hold: floats go through Decimal."""
    if isinstance(value, float):
        return Decimal(repr(value))
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    return value


def _to_item(values: Mapping[str, object]) -> dict[str, Any]:
    """Convert plain values to DynamoDB attribute values."""
    return {k: _SER.serialize(_plain(v)) for k, v in values.items()}


def _from_item(item: Mapping[str, Any]) -> dict[str, Any]:
    """Convert attribute values to plain values: counters `int`, numbers `float`."""
    out: dict[str, Any] = {}
    for key, attr in item.items():
        value = _DESER.deserialize(attr)
        if isinstance(value, Decimal):
            value = int(value) if key in _INTEGER_FIELDS else float(value)
        out[key] = value
    return out


def _label(item: Mapping[str, Any], sub: str) -> dict[str, Any]:
    """Return the label the API shows: no author sub, plus the `mine` flag."""
    plain = _from_item(item)
    shown = {k: v for k, v in plain.items() if k != "author_sub"}
    shown["mine"] = plain.get("author_sub") == sub
    return shown


def _number(body: Mapping[str, Any], key: str) -> float:
    value = body.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ApiError(400, f"{key} must be a number")
    number = float(value)
    if not math.isfinite(number) or not 0 <= number <= MAX_TIME_S:
        raise ApiError(400, f"{key} must be from 0 to {MAX_TIME_S:g} seconds")
    return number


def _text(body: Mapping[str, Any], key: str, limit: int) -> str:
    value = body.get(key)
    if not isinstance(value, str):
        raise ApiError(400, f"{key} must be a string")
    if key == "note":
        if len(value.encode()) > limit:
            raise ApiError(400, f"note must be at most {limit} bytes")
    elif not value.strip() or len(value) > limit:
        raise ApiError(400, f"{key} must be 1 to {limit} characters")
    return value


def _span(body: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the four fields a POST and a PATCH share."""
    start, stop = _number(body, "start_s"), _number(body, "stop_s")
    if stop < start:
        raise ApiError(400, "stop_s must not be before start_s")
    return {
        "name": _text(body, "name", NAME_MAX),
        "note": _text(body, "note", NOTE_MAX_BYTES) if "note" in body else "",
        "start_s": start,
        "stop_s": stop,
    }


def _path_id(path: Mapping[str, Any], name: str) -> str:
    value = str(path.get(name) or "")
    if not value or len(value) > ID_MAX:
        raise ApiError(400, f"{name} must be 1 to {ID_MAX} characters")
    return value


def _no_content() -> Response:
    return {"statusCode": 204, "headers": {}, "body": ""}


def _now() -> int:
    return int(time.time())


def _new_id() -> str:
    return uuid.uuid4().hex


@dataclass(frozen=True, slots=True)
class LabelsApi:
    """The labels routes over injected DynamoDB, Athena, Glue and S3 clients."""

    ddb: LabelStore
    table: str
    athena: Athena
    glue: Glue
    s3: ObjectReader
    workgroup: str
    database: str
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    now: Callable[[], int] = _now
    new_id: Callable[[], str] = _new_id

    def handle(
        self, event: dict[str, Any], *, invocation: Invocation | None = None
    ) -> Response:
        """Route one HTTP API (payload v2) event."""
        return serve(event, api=self, dispatch=DISPATCH, invocation=invocation)

    def list_labels(self, request: Request, _timing: Timing) -> Response:
        """GET /files/{asset_id}/labels: every label of the asset."""
        asset_id = _path_id(request.path_params, "asset_id")
        labels = [_label(i, request.sub) for i in self._items(asset_id)]
        labels.sort(key=lambda x: (x["start_s"], x["label_id"]))
        return json_response(200, {"labels": labels})

    def create(self, request: Request, timing: Timing) -> Response:
        """POST /files/{asset_id}/labels: stamp provenance, check caps, put."""
        asset_id = _path_id(request.path_params, "asset_id")
        timing.asset_id = asset_id
        body = request.json_body()
        fields = _span(body)
        if "series" in body:
            fields["series"] = _text(body, "series", SERIES_MAX)
        return self._store(asset_id, fields, request.sub, timing.phases)

    def update(self, request: Request, _timing: Timing) -> Response:
        """PATCH /files/{asset_id}/labels/{label_id}: the author only."""
        asset_id = _path_id(request.path_params, "asset_id")
        label_id = _path_id(request.path_params, "label_id")
        return self._update(asset_id, label_id, request.json_body(), request.sub)

    def delete(self, request: Request, _timing: Timing) -> Response:
        """DELETE /files/{asset_id}/labels/{label_id}: the author only."""
        asset_id = _path_id(request.path_params, "asset_id")
        label_id = _path_id(request.path_params, "label_id")
        return self._delete(asset_id, label_id, request.sub)

    def _items(
        self, asset_id: str, *, projection: str | None = None
    ) -> list[dict[str, Any]]:
        """Return every item of the asset, following the 1 MB query pages."""
        items: list[dict[str, Any]] = []
        start: dict[str, Any] | None = None
        while True:
            args: ItemsQueryArgs = {
                "TableName": self.table,
                "KeyConditionExpression": "asset_id = :a",
                "ExpressionAttributeValues": _to_item({":a": asset_id}),
                "ConsistentRead": True,
            }
            if projection is not None:
                args["ProjectionExpression"] = projection
            if start is not None:
                args["ExclusiveStartKey"] = start
            page = self.ddb.query(**args)
            items.extend(page.get("Items", []))
            start = page.get("LastEvaluatedKey")
            if start is None:
                return items

    def _runner(self) -> AthenaRunner:
        return AthenaRunner(
            athena=self.athena,
            workgroup=self.workgroup,
            database=self.database,
            clock=self.clock,
            sleep=self.sleep,
        )

    def _asset_row(self, asset_id: str) -> tuple[list[dict[str, object]], int]:
        """Run the asset query; return its rows and the wall in milliseconds."""
        began = self.clock()
        deadline = began + BUDGET_SECONDS
        found = self._runner().run(ASSET_SQL, [asset_id], deadline)
        return found, int((self.clock() - began) * 1000)

    def _snapshot_id(self, table: str) -> tuple[str | None, int, int]:
        """Return the table's current snapshot id, its Glue ms and its S3 ms.

        Glue `GetTable` names the Iceberg metadata file in `Parameters`; that JSON's
        `current-snapshot-id` is the snapshot a reader of the table sees now.
        """
        began = self.clock()
        got = self.glue.get_table(DatabaseName=self.database, Name=table)
        glue_ms = int((self.clock() - began) * 1000)
        location = str(got["Table"].get("Parameters", {}).get("metadata_location", ""))
        bucket, _, key = location.removeprefix("s3://").partition("/")
        if not (bucket and key):
            return None, glue_ms, 0
        reading = self.clock()
        data = self.s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        s3_ms = int((self.clock() - reading) * 1000)
        if data[:2] == GZIP_MAGIC:
            data = gzip.decompress(data)
        current = json.loads(data).get("current-snapshot-id")
        # Absent or -1 is an Iceberg table with no snapshot yet.
        return (
            (None if current in {None, NO_SNAPSHOT} else str(current)),
            glue_ms,
            s3_ms,
        )

    def _provenance(self, asset_id: str, phases: dict[str, int]) -> dict[str, Any]:
        """Return the asset's dataset, `s3_version_id` and per-table snapshot ids.

        The asset query (Athena) and the three snapshot reads (Glue, S3) run
        concurrently; the snapshot phases are the slowest table's.
        """
        with ThreadPoolExecutor(max_workers=1 + len(SNAPSHOT_TABLES)) as pool:
            asset = pool.submit(self._asset_row, asset_id)
            reads = [pool.submit(self._snapshot_id, t) for t in SNAPSHOT_TABLES]
            found, phases["athena_ms"] = asset.result()
            if not found:
                raise ApiError(404, "no such asset")
            snapshots: dict[str, str | None] | None
            try:
                got = [r.result() for r in reads]
            except ClientError:
                # the metadata read was refused. The label is still stored, with
                # null snapshots; the live test is what decides if that is acceptable.
                snapshots = None
            else:
                snapshots = {t: g[0] for t, g in zip(SNAPSHOT_TABLES, got, strict=True)}
                phases["glue_ms"] = max(g[1] for g in got)
                phases["s3_ms"] = max(g[2] for g in got)
        return {
            "dataset": str(found[0]["dataset"]),
            "s3_version_id": str(found[0]["s3_version_id"]),
            "snapshots": snapshots,
        }

    def _store(
        self, asset_id: str, fields: dict[str, Any], sub: str, phases: dict[str, int]
    ) -> Response:
        """Read the provenance and the caps, then put the label; time each phase."""
        began = self.clock()
        provenance = self._provenance(asset_id, phases)
        phases["provenance_ms"] = int((self.clock() - began) * 1000)
        reading = self.clock()
        existing = self._items(asset_id, projection="author_sub")
        if len(existing) >= MAX_PER_ASSET:
            raise ApiError(409, f"the asset already has {MAX_PER_ASSET} labels")
        mine = sum(_from_item(i)["author_sub"] == sub for i in existing)
        if mine >= MAX_PER_AUTHOR:
            raise ApiError(
                409, f"you already have {MAX_PER_AUTHOR} labels on this asset"
            )
        now = self.now()
        item = _to_item(
            {
                **fields,
                **provenance,
                "asset_id": asset_id,
                "label_id": self.new_id(),
                "author_sub": sub,
                "created_at": now,
                "updated_at": now,
                "version": 1,
            }
        )
        self.ddb.put_item(
            TableName=self.table,
            Item=item,
            ConditionExpression="attribute_not_exists(label_id)",
        )
        phases["ddb_ms"] = int((self.clock() - reading) * 1000)
        return json_response(201, _label(item, sub))

    def _update(
        self, asset_id: str, label_id: str, body: dict[str, Any], sub: str
    ) -> Response:
        fields = _span(body)
        version = body.get("version")
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise ApiError(400, "version must be a positive integer")
        values = _to_item(
            {
                ":n": fields["name"],
                ":note": fields["note"],
                ":s": fields["start_s"],
                ":e": fields["stop_s"],
                ":now": self.now(),
                ":sub": sub,
                ":v": version,
                ":next": version + 1,
            }
        )
        try:
            out = self.ddb.update_item(
                TableName=self.table,
                Key=_to_item({"asset_id": asset_id, "label_id": label_id}),
                UpdateExpression=(
                    "SET #n = :n, #note = :note, start_s = :s, stop_s = :e, "
                    "updated_at = :now, #v = :next"
                ),
                ConditionExpression=(
                    "attribute_exists(label_id) AND #a = :sub AND #v = :v"
                ),
                ExpressionAttributeNames={
                    "#n": "name",
                    "#note": "note",
                    "#v": "version",
                    "#a": "author_sub",
                },
                ExpressionAttributeValues=values,
                ReturnValues="ALL_NEW",
            )
        except ClientError as err:
            if err.response["Error"]["Code"] != CONDITIONAL_FAILED:
                raise
            raise self._why_refused(asset_id, label_id, sub) from err
        return json_response(200, _label(out["Attributes"], sub))

    def _delete(self, asset_id: str, label_id: str, sub: str) -> Response:
        try:
            self.ddb.delete_item(
                TableName=self.table,
                Key=_to_item({"asset_id": asset_id, "label_id": label_id}),
                ConditionExpression="attribute_exists(label_id) AND #a = :sub",
                ExpressionAttributeNames={"#a": "author_sub"},
                ExpressionAttributeValues=_to_item({":sub": sub}),
            )
        except ClientError as err:
            if err.response["Error"]["Code"] != CONDITIONAL_FAILED:
                raise
            raise self._why_refused(asset_id, label_id, sub) from err
        return _no_content()

    def _why_refused(self, asset_id: str, label_id: str, sub: str) -> ApiError:
        """Name the failed condition: 404 absent, 403 not the author, else 409 stale."""
        found = self.ddb.get_item(
            TableName=self.table,
            Key=_to_item({"asset_id": asset_id, "label_id": label_id}),
            ConsistentRead=True,
        ).get("Item")
        if found is None:
            return ApiError(404, "no such label")
        if _from_item(found)["author_sub"] != sub:
            return ApiError(403, "only the author can change or delete a label")
        return ApiError(409, "the label changed; reload it and try again")


def handler(event: dict[str, Any], context: object) -> Response:
    """Lambda entry point."""
    return LabelsApi(
        ddb=boto3.client("dynamodb"),
        table=os.environ["LABELS_TABLE"],
        athena=boto3.client("athena"),
        glue=boto3.client("glue"),
        s3=boto3.client("s3"),
        workgroup=os.environ["ATHENA_WORKGROUP"],
        database=os.environ["GLUE_DATABASE"],
    ).handle(event, invocation=Invocation.of(context))
