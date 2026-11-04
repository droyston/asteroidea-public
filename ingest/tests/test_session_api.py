"""Session API: the three routes and every refusal rule."""

import json
from typing import Any, NoReturn

import pytest
from botocore.stub import ANY

from asteroidea_ingest import owners, sessions
from asteroidea_ingest.sessions import SessionTable
from asteroidea_ingest.uploads_api import (
    SessionApi,
    dataset_meta_size,
    finalize_refusal,
    validate_create,
)

from . import ddb_rules
from .conftest import jwt_event

GOOD = {
    "dataset": "ds1",
    "path": "sub-01/anat/sub-01_T1w.nii",
    "size": 3_145_728,
    "crc64nvme": "AAAAAAAAAAA=",
}


def status(resp: dict[str, Any]) -> int:
    return int(resp["statusCode"])


def body(resp: dict[str, Any]) -> dict[str, Any]:
    return json.loads(resp["body"])


# -- 403 -----------------------------------------------------------------------------


def test_caller_outside_researchers_gets_403(harness: Any) -> None:
    resp = harness.api().handle(
        jwt_event("POST /sessions", body=GOOD, groups="[admins]"), now=100
    )
    assert status(resp) == 403
    harness.finish()  # nothing was read or written


def test_caller_with_no_groups_claim_gets_403(harness: Any) -> None:
    resp = harness.api().handle(
        jwt_event("GET /sessions/{id}", sid="s", groups=""), now=100
    )
    assert status(resp) == 403


# -- 400 -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        {**GOOD, "dataset": "Bad"},
        {**GOOD, "dataset": "-x"},
        {**GOOD, "dataset": "a" * 64},
        {**GOOD, "path": "../x.nii"},
        {**GOOD, "path": "a/../x.nii"},
        {**GOOD, "path": "/abs.nii"},
        {**GOOD, "path": "a//b.nii"},
        {**GOOD, "path": "a\\b.nii"},
        {**GOOD, "path": ""},
        {**GOOD, "size": 0},
        {**GOOD, "size": 5 * 1024**3 + 1},
        {**GOOD, "size": True},
        {**GOOD, "size": "10"},
        {**GOOD, "crc64nvme": "AAAA"},
        {**GOOD, "crc64nvme": "not base64!!"},
        {**GOOD, "crc64nvme": 5},
        ["not", "an", "object"],
    ],
)
def test_malformed_create_is_400(harness: Any, bad: object) -> None:
    resp = harness.api().handle(jwt_event("POST /sessions", body=bad), now=100)
    assert status(resp) == 400
    harness.finish()


def test_non_json_body_is_400(harness: Any) -> None:
    resp = harness.api().handle(jwt_event("POST /sessions", body="{nope"), now=100)
    assert status(resp) == 400


def test_limits_are_inclusive() -> None:
    assert validate_create({**GOOD, "size": 1}) is None
    assert validate_create({**GOOD, "size": 5 * 1024**3}) is None
    assert validate_create({**GOOD, "dataset": "a" * 63}) is None


# -- 409 -----------------------------------------------------------------------------


def test_create_refuses_an_existing_raw_key_with_409(harness: Any) -> None:
    harness.s3_stub.add_response(
        "head_object",
        {"ContentLength": 1},
        expected_params={
            "Bucket": "raw-bucket",
            "Key": "raw/ds1/sub-01/anat/sub-01_T1w.nii",
        },
    )
    resp = harness.api().handle(jwt_event("POST /sessions", body=GOOD), now=100)
    assert status(resp) == 409
    assert body(resp) == {"error": "raw_key_exists", "detail": "raw key already exists"}
    harness.finish()  # no put_item was stubbed


def test_finalize_of_an_incomplete_session_is_409_with_the_stored_reason(
    harness: Any,
) -> None:
    stored = {"reason": "checksum mismatch: client AAAA, landing BBBB"}
    harness.get_item(state=sessions.INCOMPLETE, finalize_token="t0", error=stored)
    harness.update_refused_open()
    harness.get_item(state=sessions.INCOMPLETE, finalize_token="t0", error=stored)
    resp = harness.api().handle(
        jwt_event("POST /sessions/{id}/finalize", sid="s"), now=100
    )
    assert status(resp) == 409
    assert body(resp) == {
        "error": "session_incomplete",
        "detail": "The upload session is INCOMPLETE: "
        "checksum mismatch: client AAAA, landing BBBB.",
    }
    harness.finish()


def test_finalize_of_an_incomplete_session_without_a_reason_names_the_state(
    harness: Any,
) -> None:
    harness.get_item(state=sessions.INCOMPLETE, finalize_token="t0")
    harness.update_refused_open()
    harness.get_item(state=sessions.INCOMPLETE, finalize_token="t0")
    resp = harness.api().handle(
        jwt_event("POST /sessions/{id}/finalize", sid="s"), now=100
    )
    assert body(resp) == {
        "error": "session_incomplete",
        "detail": "The upload session is INCOMPLETE and cannot be finalized.",
    }
    harness.finish()


@pytest.mark.parametrize(
    ("state", "stored", "code"),
    [
        (sessions.INCOMPLETE, {"reason": "r"}, "session_incomplete"),
        ("EXPIRED", None, "session_expired"),
        ("ODD_STATE", "not a mapping", "session_odd_state"),
    ],
)
def test_finalize_refusal_codes_follow_the_state(
    state: str, stored: object, code: str
) -> None:
    err = finalize_refusal(state, stored)
    assert (err.status, err.code) == (409, code)
    assert err.detail.startswith(f"The upload session is {state}")


# -- 404 / 410 -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "route", ["GET /sessions/{id}", "POST /sessions/{id}/finalize"]
)
def test_unknown_session_is_404(harness: Any, route: str) -> None:
    harness.ddb_stub.add_response("get_item", {})
    assert status(harness.api().handle(jwt_event(route, sid="s"), now=100)) == 404
    harness.finish()


@pytest.mark.parametrize(
    "route", ["GET /sessions/{id}", "POST /sessions/{id}/finalize"]
)
def test_someone_elses_session_is_404(harness: Any, route: str) -> None:
    harness.get_item(owner_sub="user-2")
    assert status(harness.api().handle(jwt_event(route, sid="s"), now=100)) == 404
    harness.finish()  # no claim was attempted, no send


@pytest.mark.parametrize(
    "route", ["GET /sessions/{id}", "POST /sessions/{id}/finalize"]
)
def test_expired_session_is_410(harness: Any, route: str) -> None:
    harness.get_item(expires_at=99, state=sessions.OPEN)
    resp = harness.api().handle(jwt_event(route, sid="s"), now=100)
    assert (status(resp), body(resp)) == (
        410,
        {"error": "session_expired", "detail": "session expired"},
    )
    harness.finish()


# -- 403 dataset not yours -------------------------------------------------------


def test_create_into_another_researchers_dataset_is_403_and_never_heads(
    harness: Any,
) -> None:
    harness.owners_ddb.rows.update(
        {"ds1": {**harness.owners_ddb.rows["ds1"], "owner_sub": "user-2"}}
    )
    resp = harness.api().handle(jwt_event("POST /sessions", body=GOOD), now=100)
    assert status(resp) == 403
    assert body(resp) == {
        "error": "dataset_not_yours",
        "detail": (
            "dataset ds1 belongs to another researcher; "
            "upload under another dataset name"
        ),
    }
    harness.finish()  # no head_object and no put_item were stubbed


def test_create_into_a_pre_ownership_dataset_is_403_and_records_the_admin(
    harness: Any,
) -> None:
    harness.owners_ddb.rows.clear()
    harness.s3_stub.add_response(
        "list_objects_v2",
        {"KeyCount": 1, "Contents": [{"Key": "raw/ds1/old.nwb"}]},
        expected_params={"Bucket": "raw-bucket", "Prefix": "raw/ds1/", "MaxKeys": 1},
    )
    resp = harness.api().handle(jwt_event("POST /sessions", body=GOOD), now=100)
    assert status(resp) == 403
    assert body(resp)["error"] == "dataset_not_yours"
    assert harness.owners_ddb.rows["ds1"]["owner_kind"] == "admin"
    harness.finish()


def test_create_claims_a_new_dataset_then_proceeds_to_the_head(harness: Any) -> None:
    harness.owners_ddb.rows.clear()
    harness.s3_stub.add_response("list_objects_v2", {"KeyCount": 0})
    harness.s3_stub.add_client_error("head_object", service_error_code="404")
    harness.ddb_stub.add_response("put_item", {})
    resp = harness.api().handle(jwt_event("POST /sessions", body=GOOD), now=1000)
    assert status(resp) == 201
    assert harness.owners_ddb.rows["ds1"]["owner_sub"] == "user-1"
    harness.finish()


# -- 201 create ----------------------------------------------------------------------


def test_create_presigns_one_landing_put_and_stores_an_open_item(harness: Any) -> None:
    harness.s3_stub.add_client_error("head_object", service_error_code="404")
    harness.ddb_stub.add_response(
        "put_item",
        {},
        expected_params={
            "TableName": "sessions",
            "Item": ANY,
            "ConditionExpression": "attribute_not_exists(session_id)",
        },
    )
    resp = harness.api().handle(jwt_event("POST /sessions", body=GOOD), now=1000)
    assert status(resp) == 201
    out = body(resp)
    assert out["expires_at"] == 1000 + 48 * 3600
    assert f"/landing/{out['session_id']}/sub-01/anat/sub-01_T1w.nii" in out["url"]
    assert "X-Amz-Expires=3600" in out["url"]
    harness.finish()


def test_create_stores_the_session_fields(harness: Any) -> None:
    captured: list[dict[str, Any]] = []
    harness.s3_stub.add_client_error("head_object", service_error_code="404")
    harness.ddb_stub.add_response("put_item", {})

    def spy(params: dict[str, Any], **_kw: object) -> None:
        captured.append(sessions.unmarshal(params["Item"]))

    harness.ddb.meta.events.register(
        "before-parameter-build.dynamodb.PutItem", lambda params, **_: spy(params)
    )
    harness.api().handle(jwt_event("POST /sessions", body=GOOD), now=1000)
    (item,) = captured
    assert item["state"] == sessions.OPEN
    assert item["owner_sub"] == "user-1"
    assert item["raw_key"] == "raw/ds1/sub-01/anat/sub-01_T1w.nii"
    assert (
        item["landing_key"]
        == f"landing/{item['session_id']}/sub-01/anat/sub-01_T1w.nii"
    )
    assert item["expires_at"] == 1000 + 48 * 3600
    assert "finalize_token" not in item


# -- finalize ------------------------------------------------------------------------


def test_finalize_claims_once_then_queues_the_commit(harness: Any) -> None:
    """Finalize sends to `asteroidea-commit.fifo` instead of invoking."""
    harness.get_item(state=sessions.OPEN, finalize_token=None)
    updates: list[dict[str, Any]] = []
    sends: list[dict[str, Any]] = []
    harness.ddb.meta.events.register(
        "before-parameter-build.dynamodb.UpdateItem",
        lambda params, **_: updates.append(
            sessions.unmarshal(params["ExpressionAttributeValues"])
        ),
    )
    harness.sqs.meta.events.register(
        "before-parameter-build.sqs.SendMessage",
        lambda params, **_: sends.append(dict(params)),
    )
    harness.ddb_stub.add_response(
        "update_item",
        {},
        expected_params={
            "TableName": ANY,
            "Key": ANY,
            "UpdateExpression": ANY,
            "ExpressionAttributeNames": ANY,
            "ConditionExpression": "#state = :expect",
            "ExpressionAttributeValues": ANY,
        },
    )
    harness.sqs_stub.add_response("send_message", {"MessageId": "m"})
    resp = harness.api().handle(
        jwt_event("POST /sessions/{id}/finalize", sid="s"), now=100
    )
    assert status(resp) == 202
    harness.finish()
    token = updates[0][":finalize_token"]
    (send,) = sends
    assert send["QueueUrl"] == harness.api().commit_queue_url
    assert json.loads(send["MessageBody"]) == {
        "session_id": "s",
        "finalize_token": token,
    }
    assert send["MessageGroupId"] == "catalog"
    assert send["MessageDeduplicationId"] == f"s:{token}"


@pytest.mark.parametrize("state", [sessions.FINALIZING, sessions.ACCEPTED])
def test_refused_claim_while_in_flight_is_202_and_never_sends(
    harness: Any, state: str
) -> None:
    harness.get_item(state=sessions.OPEN)
    harness.update_refused_open()
    harness.get_item(state=state, finalize_token="t0")
    resp = harness.api().handle(
        jwt_event("POST /sessions/{id}/finalize", sid="s"), now=100
    )
    assert status(resp) == 202
    harness.finish()  # a send would raise: none is stubbed


def test_refused_claim_on_a_committed_session_is_200_with_the_ids(
    harness: Any,
) -> None:
    harness.get_item(state=sessions.OPEN)
    harness.update_refused_open()
    harness.get_item(state=sessions.COMMITTED, asset_id="a-1", snapshot_id="42")
    resp = harness.api().handle(
        jwt_event("POST /sessions/{id}/finalize", sid="s"), now=100
    )
    assert status(resp) == 200
    assert body(resp) == {"state": "COMMITTED", "asset_id": "a-1", "snapshot_id": "42"}
    harness.finish()


# -- get -----------------------------------------------------------------------------


def test_get_returns_state_and_outcome_fields(harness: Any) -> None:
    harness.get_item(state=sessions.COMMITTED, asset_id="a-1", snapshot_id="42")
    resp = harness.api().handle(jwt_event("GET /sessions/{id}", sid="s"), now=100)
    assert status(resp) == 200
    assert body(resp) == {"state": "COMMITTED", "asset_id": "a-1", "snapshot_id": "42"}


def test_get_of_an_open_session_has_only_the_state(harness: Any) -> None:
    harness.get_item(state=sessions.OPEN)
    resp = harness.api().handle(jwt_event("GET /sessions/{id}", sid="s"), now=100)
    assert body(resp) == {"state": "OPEN"}


def test_unknown_route_is_404(harness: Any) -> None:
    assert status(harness.api().handle(jwt_event("DELETE /sessions"), now=1)) == 404


# -- list (GET /uploads) ----------------------------------------------------------

CONTRACT_KEYS = {"session_id", "dataset", "relpath", "state", "created_at", "asset_id"}
STATES = {"OPEN", "FINALIZING", "ACCEPTED", "COMMITTED", "INCOMPLETE"}


class FakeOwnerIndex:
    """A DynamoDB stand-in that answers `query` on `by_owner` like the real index.

    Like a real GSI it is sparse: an item lacking `owner_sub` or `created_at` is not in
    it. It filters by the `owner_sub` key value, sorts by `created_at`, honours
    `ScanIndexForward` and `Limit`, and refuses any other table, index or key condition.
    Every other call fails the test.
    """

    def __init__(self, items: list[dict[str, Any]]) -> None:
        self.items = items
        self.calls: list[dict[str, Any]] = []

    def query(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        ddb_rules.check(kwargs)
        assert kwargs["TableName"] == "sessions"
        assert kwargs["IndexName"] == "by_owner"
        assert kwargs["KeyConditionExpression"] == "owner_sub = :owner_sub"
        owner = kwargs["ExpressionAttributeValues"][":owner_sub"]["S"]
        indexed = [i for i in self.items if {"owner_sub", "created_at"} <= i.keys()]
        mine = [i for i in indexed if i["owner_sub"]["S"] == owner]
        mine.sort(
            key=lambda i: int(i["created_at"]["N"]),
            reverse=not kwargs["ScanIndexForward"],
        )
        return {"Items": mine[: kwargs["Limit"]]}

    @staticmethod
    def _refuse(call: str) -> NoReturn:
        msg = f"GET /uploads must only query, not {call}"
        raise AssertionError(msg)

    def get_item(self, **_kwargs: Any) -> dict[str, Any]:
        self._refuse("get_item")

    def put_item(self, **_kwargs: Any) -> dict[str, Any]:
        self._refuse("put_item")

    def scan(self, **_kwargs: Any) -> dict[str, Any]:
        self._refuse("scan")

    def update_item(self, **_kwargs: Any) -> dict[str, Any]:
        self._refuse("update_item")


def session_item(**overrides: Any) -> dict[str, Any]:
    """Return a marshalled session item with the section 5.1 listing fields."""
    item = {
        "session_id": "s",
        "state": sessions.COMMITTED,
        "owner_sub": "user-1",
        "dataset": "ds1",
        "relpath": "sub-01/anat/sub-01_T1w.nii",
        "size": 100,
        "created_at": 1,
        "updated_at": 1,
        "expires_at": 10**6,
    }
    return sessions.marshal({**item, **overrides})


def seeded(rows: list[tuple[str, str, int]], **extra: Any) -> FakeOwnerIndex:
    """Session items from (session_id, owner_sub, created_at) triples."""
    return FakeOwnerIndex(
        [
            session_item(session_id=sid, owner_sub=owner, created_at=created, **extra)
            for sid, owner, created in rows
        ]
    )


def api_over(fake: FakeOwnerIndex, harness: Any) -> SessionApi:
    return SessionApi(
        table=SessionTable(fake, "sessions"),
        s3=harness.s3,
        sqs=harness.sqs,
        bucket="raw-bucket",
        commit_queue_url="commit-url",
        dataset_owners=owners.Owners(harness.owners_ddb, "owners"),
    )


def test_uploads_response_matches_the_contract(harness: Any) -> None:
    fake = seeded(
        [("s-done", "user-1", 20), ("s-open", "user-1", 10)],
    )
    fake.items[0]["state"] = {"S": sessions.COMMITTED}
    fake.items[0]["asset_id"] = {"S": "a-1"}
    fake.items[1]["state"] = {"S": sessions.OPEN}
    resp = api_over(fake, harness).handle(jwt_event("GET /uploads"), now=100)
    assert status(resp) == 200
    assert resp["headers"]["content-type"] == "application/json"
    rows = body(resp)["uploads"]
    assert set(body(resp)) == {"uploads"}
    assert all(set(r) == CONTRACT_KEYS for r in rows)
    assert rows[0] == {
        "session_id": "s-done",
        "dataset": "ds1",
        "relpath": "sub-01/anat/sub-01_T1w.nii",
        "state": "COMMITTED",
        "created_at": 20,
        "asset_id": "a-1",
    }
    assert rows[1]["asset_id"] is None  # no asset yet: JSON null, key present
    assert isinstance(rows[0]["created_at"], int)
    assert {r["state"] for r in rows} <= STATES


def test_uploads_query_parameters_are_valid_for_dynamodb(harness: Any) -> None:
    """The real boto3 client checks the Query parameters against the service model."""
    harness.ddb_stub.add_response(
        "query",
        {"Items": [session_item(session_id="s1", created_at=5)]},
        expected_params={
            "TableName": "sessions",
            "IndexName": "by_owner",
            "KeyConditionExpression": "owner_sub = :owner_sub",
            "ExpressionAttributeValues": {":owner_sub": {"S": "user-1"}},
            "ScanIndexForward": False,
            "Limit": 50,
        },
    )
    resp = harness.api().handle(jwt_event("GET /uploads"), now=100)
    assert [r["session_id"] for r in body(resp)["uploads"]] == ["s1"]
    harness.finish()


def test_uploads_returns_only_the_callers_sessions(harness: Any) -> None:
    fake = seeded(
        [
            ("mine-1", "user-1", 10),
            ("theirs-1", "user-2", 11),
            ("mine-2", "user-1", 12),
            ("theirs-2", "user-2", 13),
        ]
    )
    resp = api_over(fake, harness).handle(
        jwt_event("GET /uploads", sub="user-1"), now=100
    )
    assert [r["session_id"] for r in body(resp)["uploads"]] == ["mine-2", "mine-1"]
    other = api_over(fake, harness).handle(
        jwt_event("GET /uploads", sub="user-2"), now=100
    )
    assert [r["session_id"] for r in body(other)["uploads"]] == ["theirs-2", "theirs-1"]
    nobody = api_over(fake, harness).handle(
        jwt_event("GET /uploads", sub="user-3"), now=100
    )
    assert body(nobody) == {"uploads": []}


def test_a_reextract_tracking_item_is_never_listed(harness: Any) -> None:
    # Tracking items have no owner_sub or created_at, so the sparse by_owner
    # index does not hold them and GET /uploads never lists them.
    tracking = sessions.marshal(sessions.reextract_item("run1", "asset1", 100))
    assert not {"owner_sub", "created_at"} & tracking.keys()
    fake = seeded([("mine", "user-1", 10)])
    fake.items.append(tracking)
    resp = api_over(fake, harness).handle(
        jwt_event("GET /uploads", sub="user-1"), now=100
    )
    assert [r["session_id"] for r in body(resp)["uploads"]] == ["mine"]


def test_uploads_are_newest_first(harness: Any) -> None:
    fake = seeded([("a", "user-1", 5), ("c", "user-1", 30), ("b", "user-1", 17)])
    resp = api_over(fake, harness).handle(jwt_event("GET /uploads"), now=100)
    assert [r["session_id"] for r in body(resp)["uploads"]] == ["c", "b", "a"]


def test_uploads_are_limited_to_50_newest(harness: Any) -> None:
    fake = seeded([(f"s{n:03d}", "user-1", n) for n in range(60)])
    resp = api_over(fake, harness).handle(jwt_event("GET /uploads"), now=100)
    rows = body(resp)["uploads"]
    assert len(rows) == 50
    assert rows[0]["created_at"] == 59
    assert rows[-1]["created_at"] == 10


def test_uploads_for_a_caller_outside_researchers_is_403_and_reads_nothing(
    harness: Any,
) -> None:
    fake = seeded([("mine", "user-1", 1)])
    resp = api_over(fake, harness).handle(
        jwt_event("GET /uploads", groups="[admins]"), now=100
    )
    assert status(resp) == 403
    assert fake.calls == []
    nogroups = api_over(fake, harness).handle(
        jwt_event("GET /uploads", groups=""), now=100
    )
    assert status(nogroups) == 403
    assert fake.calls == []


# -- upload_id, dataset_meta, lease_until, errors ---------------

UPLOAD_ID = "6f1c2b8e-3d4a-4c5b-9e6f-7a8b9c0d1e2f"
META = {"title": "t", "license": "CC-BY-4.0", "description": "d"}


@pytest.mark.parametrize(
    "bad",
    [
        {**GOOD, "upload_id": "not-a-uuid"},
        {**GOOD, "upload_id": UPLOAD_ID.upper()},
        {**GOOD, "upload_id": "6f1c2b8e-3d4a-1c5b-9e6f-7a8b9c0d1e2f"},  # uuid1
        {**GOOD, "upload_id": 5},
        {**GOOD, "dataset_meta": "title"},
        {**GOOD, "dataset_meta": {"title": "t", "doi": "x"}},
        {**GOOD, "dataset_meta": {"title": 5}},
        {**GOOD, "dataset_meta": {"description": "x" * 4096}},
    ],
)
def test_malformed_upload_id_or_dataset_meta_is_400(harness: Any, bad: object) -> None:
    resp = harness.api().handle(jwt_event("POST /sessions", body=bad), now=100)
    assert status(resp) == 400
    harness.finish()  # nothing was read or written


def test_dataset_meta_over_4_kib_is_refused_naming_the_limit() -> None:
    meta = {"description": "x" * 4096}
    problem = validate_create({**GOOD, "dataset_meta": meta})
    assert problem is not None
    assert "4096 bytes (4 KiB)" in problem


def test_dataset_meta_at_exactly_4_kib_is_accepted() -> None:
    # {"description":"..."} is 18 bytes of JSON around the value.
    meta = {"description": "x" * (4096 - 18)}
    assert dataset_meta_size(meta) == 4096
    assert validate_create({**GOOD, "dataset_meta": meta}) is None
    over = {"description": "x" * (4096 - 17)}
    assert validate_create({**GOOD, "dataset_meta": over}) is not None


def test_dataset_meta_limit_counts_utf8_bytes() -> None:
    meta = {"title": "é" * 2040}  # 4,080 B of UTF-8 plus 12 B of JSON
    assert dataset_meta_size(meta) == 4092
    assert validate_create({**GOOD, "dataset_meta": meta}) is None
    assert validate_create({**GOOD, "dataset_meta": {"title": "é" * 2043}}) is not None


def _capture_put(harness: Any) -> list[dict[str, Any]]:
    captured: list[dict[str, Any]] = []
    harness.s3_stub.add_client_error("head_object", service_error_code="404")
    harness.ddb_stub.add_response("put_item", {})
    harness.ddb.meta.events.register(
        "before-parameter-build.dynamodb.PutItem",
        lambda params, **_: captured.append(sessions.unmarshal(params["Item"])),
    )
    return captured


def test_create_stores_upload_id_and_dataset_meta(harness: Any) -> None:
    captured = _capture_put(harness)
    payload = {**GOOD, "upload_id": UPLOAD_ID, "dataset_meta": META}
    resp = harness.api().handle(jwt_event("POST /sessions", body=payload), now=1000)
    assert status(resp) == 201
    (item,) = captured
    assert item["upload_id"] == UPLOAD_ID
    assert item["dataset_meta"] == META
    assert item["ingest_method"] == "upload"
    harness.finish()


def test_create_without_the_optional_fields_stores_neither(harness: Any) -> None:
    captured = _capture_put(harness)
    harness.api().handle(jwt_event("POST /sessions", body=GOOD), now=1000)
    (item,) = captured
    assert "upload_id" not in item
    assert "dataset_meta" not in item


def test_finalize_writes_lease_until(harness: Any) -> None:
    harness.get_item(state=sessions.OPEN, finalize_token=None)
    captured: list[dict[str, Any]] = []
    harness.ddb.meta.events.register(
        "before-parameter-build.dynamodb.UpdateItem",
        lambda params, **_: captured.append(
            sessions.unmarshal(params["ExpressionAttributeValues"])
        ),
    )
    harness.ddb_stub.add_response("update_item", {})
    harness.sqs_stub.add_response("send_message", {"MessageId": "m"})
    resp = harness.api().handle(
        jwt_event("POST /sessions/{id}/finalize", sid="s"), now=100
    )
    assert status(resp) == 202
    (values,) = captured
    assert values[":state"] == sessions.FINALIZING
    assert values[":lease_until"] == 100 + 150
    harness.finish()


def test_get_returns_commit_and_extract_errors_when_present(harness: Any) -> None:
    commit_error = {"class": "CommitFailedException", "message": "m1"}
    extract_error = {"class": "KeyError", "message": "'units'"}
    harness.get_item(
        state=sessions.INCOMPLETE,
        commit_error=commit_error,
        extract_error=extract_error,
    )
    resp = harness.api().handle(jwt_event("GET /sessions/{id}", sid="s"), now=100)
    assert body(resp) == {
        "state": "INCOMPLETE",
        "commit_error": commit_error,
        "extract_error": extract_error,
    }


def test_uploads_rows_keep_exactly_six_keys_with_the_new_fields(harness: Any) -> None:
    item = sessions.marshal(
        {
            "session_id": "s1",
            "dataset": "ds1",
            "relpath": "a.nii",
            "state": sessions.COMMITTED,
            "created_at": 1,
            "owner_sub": "user-1",
            "upload_id": UPLOAD_ID,
            "dataset_meta": META,
            "commit_error": {"class": "X", "message": "y"},
        }
    )
    harness.ddb_stub.add_response("query", {"Items": [item]})
    resp = harness.api().handle(jwt_event("GET /uploads"), now=100)
    (row,) = body(resp)["uploads"]
    assert len(row) == 6


# -- dataset_meta.tier, sandbox only -------------------


def test_dataset_meta_tier_sandbox_is_accepted() -> None:
    meta = {**META, "tier": "sandbox"}
    assert validate_create({**GOOD, "dataset_meta": meta}) is None


@pytest.mark.parametrize("tier", ["curated", "upload", "Sandbox", ""])
def test_dataset_meta_tier_other_than_sandbox_is_refused(tier: str) -> None:
    problem = validate_create({**GOOD, "dataset_meta": {"tier": tier}})
    assert problem == f"dataset_meta.tier must be sandbox when given; got {tier!r}"


def test_a_tier_other_than_sandbox_is_400_and_nothing_is_written(
    harness: Any,
) -> None:
    body = {**GOOD, "dataset_meta": {"tier": "curated"}}
    resp = harness.api().handle(jwt_event("POST /sessions", body=body), now=100)
    assert status(resp) == 400
    harness.finish()
