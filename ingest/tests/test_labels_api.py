"""Labels routes over fake DynamoDB, Glue, S3, Athena.

The fake table evaluates the `ConditionExpression` the handler sends (existence and
equality clauses joined by AND), so a missing author condition lets a second
researcher through and the 403 tests fail.
"""

import gzip
import io
import json
import re
from dataclasses import replace
from typing import TYPE_CHECKING, Any, override

from botocore.exceptions import ClientError

from asteroidea_ingest import labels_api
from asteroidea_ingest.labels_api import LabelsApi, _from_item, _to_item

from . import ddb_rules
from .conftest import jwt_event

if TYPE_CHECKING:
    import pytest

ASSET = "asset-1"
VERSION = "v-abc123"
SNAPSHOTS = {"intervals": "111", "events": "222", "asset_headers": "333"}
CLAUSE = re.compile(
    r"^(?:attribute_(?P<fn>not_exists|exists)\((?P<fattr>[#\w]+)\)"
    r"|(?P<lhs>[#\w]+) = (?P<rhs>:\w+))$"
)

GET = "GET /files/{asset_id}/labels"
POST = "POST /files/{asset_id}/labels"
PATCH = "PATCH /files/{asset_id}/labels/{label_id}"
DELETE = "DELETE /files/{asset_id}/labels/{label_id}"


def _failed() -> ClientError:
    return ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException", "Message": "no"}},
        "WriteItem",
    )


class FakeLabels:
    """An in-memory labels table with real condition semantics and query paging."""

    def __init__(self, page_size: int = 1000) -> None:
        self.items: dict[tuple[str, str], dict[str, Any]] = {}
        self.page_size = page_size
        self.queries = 0

    def seed(self, count: int, author: str, asset: str = ASSET, tag: str = "") -> None:
        for i in range(count):
            self.items[(asset, f"{tag}{author}-{i}")] = {
                "asset_id": asset,
                "label_id": f"{tag}{author}-{i}",
                "author_sub": author,
                "name": "n",
                "note": "",
                "start_s": float(i),
                "stop_s": float(i) + 1,
                "version": 1,
            }

    def _key(self, key: dict[str, Any]) -> tuple[str, str]:
        plain = _from_item(key)
        return str(plain["asset_id"]), str(plain["label_id"])

    def _check(self, current: dict[str, Any] | None, kwargs: dict[str, Any]) -> None:
        ddb_rules.check(kwargs)
        names = kwargs.get("ExpressionAttributeNames", {})
        values = _from_item(kwargs.get("ExpressionAttributeValues", {}))
        for clause in kwargs["ConditionExpression"].split(" AND "):
            match = CLAUSE.match(clause)
            assert match, clause
            if match["fn"]:
                exists = current is not None
                ok = exists if match["fn"] == "exists" else not exists
            else:
                attr = names.get(match["lhs"], match["lhs"])
                ok = current is not None and current.get(attr) == values[match["rhs"]]
            if not ok:
                raise _failed()

    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        found = self.items.get(self._key(kwargs["Key"]))
        return {"Item": _to_item(found)} if found is not None else {}

    def put_item(self, **kwargs: Any) -> dict[str, Any]:
        item = _from_item(kwargs["Item"])
        key = (item["asset_id"], item["label_id"])
        self._check(self.items.get(key), kwargs)
        self.items[key] = item
        return {}

    def update_item(self, **kwargs: Any) -> dict[str, Any]:
        key = self._key(kwargs["Key"])
        current = self.items.get(key)
        self._check(current, kwargs)
        assert current is not None
        v = _from_item(kwargs["ExpressionAttributeValues"])
        current.update(
            name=v[":n"],
            note=v[":note"],
            start_s=v[":s"],
            stop_s=v[":e"],
            updated_at=v[":now"],
            version=v[":next"],
        )
        return {"Attributes": _to_item(current)}

    def delete_item(self, **kwargs: Any) -> dict[str, Any]:
        key = self._key(kwargs["Key"])
        self._check(self.items.get(key), kwargs)
        del self.items[key]
        return {}

    def query(self, **kwargs: Any) -> dict[str, Any]:
        self.queries += 1
        ddb_rules.check(kwargs)
        asset = _from_item(kwargs["ExpressionAttributeValues"])[":a"]
        rows = sorted(i for (a, i) in self.items if a == asset)
        after = kwargs.get("ExclusiveStartKey")
        if after is not None:
            rows = [r for r in rows if r > _from_item(after)["label_id"]]
        page = rows[: self.page_size]
        projection = kwargs.get("ProjectionExpression")
        out = []
        for label_id in page:
            item = self.items[(asset, label_id)]
            keep = {projection: item[projection]} if projection else item
            out.append(_to_item(keep))
        reply: dict[str, Any] = {"Items": out}
        if len(rows) > self.page_size:
            reply["LastEvaluatedKey"] = _to_item(
                {"asset_id": asset, "label_id": page[-1]}
            )
        return reply


class FakeAthena:
    """Answers the asset row only; records the texts."""

    def __init__(self) -> None:
        self.started: list[dict[str, Any]] = []
        self.asset_known = True
        self.state = "SUCCEEDED"

    def start_query_execution(self, **kwargs: Any) -> dict[str, Any]:
        self.started.append(kwargs)
        return {"QueryExecutionId": f"q{len(self.started)}"}

    def get_query_execution(self, **_kwargs: Any) -> dict[str, Any]:
        return {"QueryExecution": {"Status": {"State": self.state}}}

    def get_query_results(self, **_kwargs: Any) -> dict[str, Any]:
        cols = [("dataset", "varchar"), ("s3_version_id", "varchar")]
        rows = [["ds1", VERSION]] if self.asset_known else []
        return {
            "ResultSet": {
                "Rows": [
                    {"Data": [{"VarCharValue": n} for n, _ in cols]},
                    *({"Data": [{"VarCharValue": v} for v in r]} for r in rows),
                ],
                "ResultSetMetadata": {
                    "ColumnInfo": [{"Name": n, "Type": t} for n, t in cols]
                },
            }
        }

    def stop_query_execution(self, **_kwargs: Any) -> dict[str, Any]:
        return {}


WAREHOUSE = "wh-bkt"


def _metadata_key(table: str) -> str:
    return f"iceberg/{table}/metadata/00007-{table}.metadata.json"


class FakeGlue:
    """`GetTable` answering each table's `metadata_location`; records the calls."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.deny = False
        self.no_location: set[str] = set()

    def get_table(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self.deny:
            raise ClientError(
                {"Error": {"Code": "AccessDeniedException", "Message": "no"}},
                "GetTable",
            )
        table = kwargs["Name"]
        params = (
            {}
            if table in self.no_location
            else {"metadata_location": f"s3://{WAREHOUSE}/{_metadata_key(table)}"}
        )
        return {"Table": {"Name": table, "Parameters": params}}


class FakeS3:
    """Holds each table's Iceberg metadata JSON; records the keys read."""

    def __init__(self) -> None:
        self.reads: list[tuple[str, str]] = []
        self.objects: dict[str, bytes] = {}
        for table, snap in SNAPSHOTS.items():
            self.set_current(table, snap)

    def set_current(self, table: str, current: object, *, gz: bool = False) -> None:
        # Other snapshots in the log are newer than `current`: a rolled-back table.
        doc: dict[str, Any] = {
            "format-version": 2,
            "snapshots": [{"snapshot-id": 999}, {"snapshot-id": 1000}],
        }
        if current is not None:
            doc["current-snapshot-id"] = current
        raw = json.dumps(doc).encode()
        self.objects[_metadata_key(table)] = gzip.compress(raw) if gz else raw

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        self.reads.append((kwargs["Bucket"], kwargs["Key"]))
        assert kwargs["Bucket"] == WAREHOUSE
        return {"Body": io.BytesIO(self.objects[kwargs["Key"]])}


class Rig:
    """A table, an Athena and an API over them, with a steady clock and ids."""

    def __init__(self, page_size: int = 1000) -> None:
        self.table = FakeLabels(page_size)
        self.athena = FakeAthena()
        self.glue = FakeGlue()
        self.s3 = FakeS3()
        self.counter = 0
        self.clock_now = 1000
        self.api = LabelsApi(
            ddb=self.table,
            table="labels",
            athena=self.athena,
            glue=self.glue,
            s3=self.s3,
            workgroup="asteroidea",
            database="asteroidea",
            clock=lambda: 0.0,
            sleep=lambda _s: None,
            now=self._now,
            new_id=self._new_id,
        )

    def _now(self) -> int:
        self.clock_now += 1
        return self.clock_now

    def _new_id(self) -> str:
        self.counter += 1
        return f"L{self.counter}"

    def call(
        self,
        route: str,
        sub: str,
        body: object = None,
        label_id: str | None = None,
        groups: str = "[researchers]",
    ) -> tuple[int, dict[str, Any]]:
        event = jwt_event(route, sub=sub, groups=groups, body=body)
        event["pathParameters"] = {
            "asset_id": ASSET,
            **({"label_id": label_id} if label_id is not None else {}),
        }
        resp = self.api.handle(event)
        return resp["statusCode"], json.loads(resp["body"]) if resp["body"] else {}


GOOD = {"name": "stimulus on", "note": "scene cut", "start_s": 1.5, "stop_s": 2.25}


def _post(rig: Rig, sub: str = "alice", **over: object) -> dict[str, Any]:
    status, body = rig.call(POST, sub, {**GOOD, **over})
    assert status == 201, body
    return body


def test_post_stores_the_label_with_server_stamped_provenance() -> None:
    rig = Rig()
    label = _post(rig, series="lfp/probe0")
    assert label == {
        "asset_id": ASSET,
        "label_id": "L1",
        "dataset": "ds1",
        "name": "stimulus on",
        "note": "scene cut",
        "start_s": 1.5,
        "stop_s": 2.25,
        "series": "lfp/probe0",
        "created_at": 1001,
        "updated_at": 1001,
        "version": 1,
        "s3_version_id": VERSION,
        "snapshots": SNAPSHOTS,
        "mine": True,
    }
    stored = rig.table.items[(ASSET, "L1")]
    assert stored["author_sub"] == "alice"  # from the JWT, never from the body
    assert [q["QueryString"] for q in rig.athena.started] == [labels_api.ASSET_SQL]
    assert sorted(c["Name"] for c in rig.glue.calls) == sorted(SNAPSHOTS)
    assert {c["DatabaseName"] for c in rig.glue.calls} == {"asteroidea"}
    assert sorted(k for _, k in rig.s3.reads) == sorted(
        _metadata_key(t) for t in SNAPSHOTS
    )
    asset_query = next(
        q for q in rig.athena.started if "FROM assets" in q["QueryString"]
    )
    assert asset_query["ExecutionParameters"] == [f"'{ASSET}'"]


def test_the_author_in_the_body_is_ignored() -> None:
    rig = Rig()
    _post(rig, author_sub="mallory", s3_version_id="forged", snapshots={"x": "1"})
    stored = rig.table.items[(ASSET, "L1")]
    assert stored["author_sub"] == "alice"
    assert stored["s3_version_id"] == VERSION
    assert stored["snapshots"] == SNAPSHOTS


def test_a_second_researcher_lists_the_first_researchers_label_as_not_mine() -> None:
    rig = Rig()
    _post(rig, "alice")
    status, body = rig.call(GET, "bob")
    assert status == 200
    (label,) = body["labels"]
    assert label["name"] == "stimulus on"
    assert label["mine"] is False
    assert "author_sub" not in label
    status, body = rig.call(GET, "alice")
    assert body["labels"][0]["mine"] is True


def test_get_lists_in_time_order_and_follows_query_pages() -> None:
    rig = Rig(page_size=2)
    for start in (5.0, 1.0, 3.0, 2.0, 4.0):
        _post(rig, "alice", start_s=start, stop_s=start + 0.5)
    _, body = rig.call(GET, "bob")
    assert [x["start_s"] for x in body["labels"]] == [1.0, 2.0, 3.0, 4.0, 5.0]


def test_get_with_no_labels_is_an_empty_list() -> None:
    assert Rig().call(GET, "bob") == (200, {"labels": []})


def test_a_second_researchers_patch_is_403_and_changes_nothing() -> None:
    rig = Rig()
    _post(rig, "alice")
    status, body = rig.call(PATCH, "bob", {**GOOD, "name": "x", "version": 1}, "L1")
    assert status == 403, body
    stored = rig.table.items[(ASSET, "L1")]
    assert stored["name"] == "stimulus on"
    assert stored["version"] == 1


def test_a_second_researchers_delete_is_403_and_keeps_the_label() -> None:
    rig = Rig()
    _post(rig, "alice")
    status, _ = rig.call(DELETE, "bob", label_id="L1")
    assert status == 403
    assert (ASSET, "L1") in rig.table.items


def test_the_author_patches_and_the_version_moves() -> None:
    rig = Rig()
    _post(rig, "alice")
    edit = {"name": "renamed", "note": "", "start_s": 0.5, "stop_s": 9, "version": 1}
    status, body = rig.call(PATCH, "alice", edit, "L1")
    assert status == 200, body
    assert (body["name"], body["start_s"], body["stop_s"]) == ("renamed", 0.5, 9.0)
    assert (body["version"], body["updated_at"], body["mine"]) == (2, 1002, True)
    assert body["s3_version_id"] == VERSION  # provenance is not rewritten
    assert body["snapshots"] == SNAPSHOTS


def test_a_stale_version_is_409() -> None:
    rig = Rig()
    _post(rig, "alice")
    edit = {**GOOD, "version": 1}
    assert rig.call(PATCH, "alice", edit, "L1")[0] == 200
    status, body = rig.call(PATCH, "alice", edit, "L1")
    assert (status, body["error"]) == (409, "conflict")
    assert "changed" in body["detail"]


def test_a_non_author_with_a_stale_version_is_403_not_409() -> None:
    rig = Rig()
    _post(rig, "alice")
    status, _ = rig.call(PATCH, "bob", {**GOOD, "version": 7}, "L1")
    assert status == 403


def test_the_author_deletes_with_204() -> None:
    rig = Rig()
    _post(rig, "alice")
    status, body = rig.call(DELETE, "alice", label_id="L1")
    assert (status, body) == (204, {})
    assert rig.table.items == {}


def test_patch_and_delete_of_an_absent_label_are_404() -> None:
    rig = Rig()
    assert rig.call(PATCH, "alice", {**GOOD, "version": 1}, "nope")[0] == 404
    assert rig.call(DELETE, "alice", label_id="nope")[0] == 404


def test_the_2001st_label_on_an_asset_is_409() -> None:
    rig = Rig(page_size=700)
    for author in ("a", "b", "c", "d"):
        rig.table.seed(500, author)
    assert len(rig.table.items) == 2000
    status, body = rig.call(POST, "e", GOOD)
    assert status == 409
    assert "2000" in body["detail"]
    assert len(rig.table.items) == 2000


def test_the_501st_label_of_one_author_is_409_but_another_author_may_post() -> None:
    rig = Rig()
    rig.table.seed(500, "alice")
    status, body = rig.call(POST, "alice", GOOD)
    assert status == 409
    assert "500" in body["detail"]
    assert rig.call(POST, "bob", GOOD)[0] == 201


def test_the_per_author_cap_is_per_asset() -> None:
    rig = Rig()
    rig.table.seed(500, "alice", asset="other-asset")
    assert rig.call(POST, "alice", GOOD)[0] == 201


def test_bounds_are_400() -> None:
    rig = Rig()
    bad: list[dict[str, object]] = [
        {**GOOD, "name": ""},
        {**GOOD, "name": "   "},
        {**GOOD, "name": "x" * 65},
        {**GOOD, "name": 7},
        {**GOOD, "note": "x" * 1025},
        {**GOOD, "note": "é" * 600},  # 1,200 bytes
        {**GOOD, "note": None},
        {**GOOD, "start_s": -0.1},
        {**GOOD, "start_s": "1"},
        {**GOOD, "start_s": True},
        {**GOOD, "stop_s": float("inf")},
        {**GOOD, "stop_s": 1.0e7 + 1},
        {**GOOD, "start_s": 3, "stop_s": 2},
        {**GOOD, "series": ""},
        {**GOOD, "series": "s" * 257},
        {"name": "n", "note": "", "start_s": 1},
    ]
    for body in bad:
        status, reply = rig.call(POST, "alice", body)
        assert status == 400, (body, reply)
    assert rig.table.items == {}
    assert rig.athena.started == []  # bounds are checked before any Athena read


def test_the_edge_values_are_accepted() -> None:
    rig = Rig()
    edge = {"name": "n" * 64, "note": "x" * 1024, "start_s": 0, "stop_s": 0}
    assert rig.call(POST, "alice", edge)[0] == 201


def test_a_note_is_optional_and_stored_empty() -> None:
    rig = Rig()
    body = {k: v for k, v in GOOD.items() if k != "note"}
    assert rig.call(POST, "alice", body)[1]["note"] == ""


def test_a_body_that_is_not_a_json_object_is_400() -> None:
    rig = Rig()
    for raw in ("", "not json", "[1]", "3"):
        status, _ = rig.call(POST, "alice", raw)
        assert status == 400, raw


def test_patch_needs_a_positive_integer_version() -> None:
    rig = Rig()
    _post(rig, "alice")
    for version in (None, 0, "1", True, 1.5):
        body = {**GOOD, "version": version}
        assert rig.call(PATCH, "alice", body, "L1")[0] == 400, version


def test_post_to_an_unknown_asset_is_404_and_stores_nothing() -> None:
    rig = Rig()
    rig.athena.asset_known = False
    assert rig.call(POST, "alice", GOOD)[0] == 404
    assert rig.table.items == {}


def test_an_asset_read_past_the_budget_is_503_slow_and_stores_nothing() -> None:
    rig = Rig()
    rig.athena.state = "RUNNING"
    ticks = iter(range(10_000))
    rig.api = replace(rig.api, clock=lambda: float(next(ticks)))
    status, body = rig.call(POST, "alice", GOOD)
    assert (status, body) == (
        503,
        {"error": "slow", "detail": "the catalog is slow to answer; try again"},
    )
    assert rig.table.items == {}


def test_a_refused_snapshot_read_stores_null_snapshots() -> None:
    """A5 fallback: the label is kept and the live test decides if that is allowed."""
    rig = Rig()
    rig.glue.deny = True
    label = _post(rig)
    assert label["snapshots"] is None
    assert label["s3_version_id"] == VERSION


def test_a_table_with_no_snapshot_is_null_for_that_table_only() -> None:
    rig = Rig()
    rig.s3.set_current("events", None)
    label = _post(rig)
    assert label["snapshots"] == {**SNAPSHOTS, "events": None}
    rig.s3.set_current("events", -1)
    assert _post(rig)["snapshots"] == {**SNAPSHOTS, "events": None}


def test_a_table_with_no_metadata_location_is_null_for_that_table_only() -> None:
    rig = Rig()
    rig.glue.no_location = {"intervals"}
    assert _post(rig)["snapshots"] == {**SNAPSHOTS, "intervals": None}


def test_the_current_snapshot_wins_over_a_newer_one_after_a_rollback() -> None:
    """`current-snapshot-id` is what a reader sees; the log holds newer snapshots."""
    rig = Rig()
    assert _post(rig)["snapshots"] == SNAPSHOTS  # 999 and 1000 sit in the log


def test_a_gzipped_metadata_file_is_read() -> None:
    rig = Rig()
    rig.s3.set_current("events", 4242, gz=True)
    assert _post(rig)["snapshots"] == {**SNAPSHOTS, "events": "4242"}


def test_a_caller_outside_the_researchers_group_is_403() -> None:
    rig = Rig()
    for route in (GET, POST, PATCH, DELETE):
        assert rig.call(route, "alice", GOOD, "L1", groups="[]")[0] == 403
    assert rig.table.items == {}


def test_an_unknown_route_is_404() -> None:
    assert Rig().call("PUT /files/{asset_id}/labels", "alice")[0] == 404


class Faulty(FakeLabels):
    """A table whose update fails with a fault that is not a failed condition."""

    def __init__(self, err: ClientError) -> None:
        super().__init__()
        self.err = err

    @override
    def update_item(self, **_kwargs: Any) -> dict[str, Any]:
        raise self.err


def test_a_dynamodb_fault_is_not_turned_into_a_refusal(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rig = Rig()
    _post(rig, "alice")
    err = ClientError({"Error": {"Code": "ProvisionedThroughputExceeded"}}, "Update")
    rig.api = replace(rig.api, ddb=Faulty(err))
    capsys.readouterr()
    status, body = rig.call(PATCH, "alice", {**GOOD, "version": 1}, "L1")
    assert (status, body["error"]) == (500, "internal_error")
    assert "ProvisionedThroughputExceeded" in capsys.readouterr().out


def test_the_snapshot_tables_are_the_three_label_tables() -> None:
    assert set(labels_api.SNAPSHOT_TABLES) == set(SNAPSHOTS)
