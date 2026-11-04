"""Imports routes, offline.

DynamoDB and Lambda are real boto3 clients under botocore's Stubber, so every call is
checked against the service model and an unexpected call fails the test.
"""

import json
from typing import Any, Final

import boto3
import pytest
from botocore.config import Config
from botocore.stub import ANY, Stubber

from asteroidea_ingest import import_plan, sessions, timing
from asteroidea_ingest.imports_api import ImportsApi, run_lease

from . import ddb_rules

RUNS: Final = "import-runs"
SESSIONS: Final = "sessions"
PLANNER: Final = "asteroidea-import-plan"
CURATOR: Final = "curator-1"
NOW: Final = 1_790_000_000
SOURCE: Final = "dandi:000623@0.240227.2023"


def _client(service: str) -> Any:
    client = boto3.client(
        service,
        config=Config(signature_version="v4"),
        region_name="us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )
    return ddb_rules.install(client) if service == "dynamodb" else client


class Rig:
    """An `ImportsApi` over stubbed clients; `now` is fixed."""

    def __init__(self, now: int = NOW) -> None:
        self.ddb, self.lam = _client("dynamodb"), _client("lambda")
        self.ddb_stub, self.lam_stub = Stubber(self.ddb), Stubber(self.lam)
        self.ddb_stub.activate()
        self.lam_stub.activate()
        self.api = ImportsApi(
            ddb=self.ddb,
            runs_table=RUNS,
            sessions_table=SESSIONS,
            lam=self.lam,
            planner_function=PLANNER,
            now=lambda: now,
            new_id=lambda: "run-new",
        )

    def call(
        self,
        route: str,
        *,
        sub: str = CURATOR,
        groups: str = "[researchers curators]",
        body: object = None,
        run_id: str | None = None,
    ) -> tuple[int, dict[str, Any]]:
        event = {
            "routeKey": route,
            "pathParameters": {"run_id": run_id} if run_id is not None else None,
            "body": body if isinstance(body, str) or body is None else json.dumps(body),
            "requestContext": {
                "authorizer": {
                    "jwt": {"claims": {"sub": sub, "cognito:groups": groups}}
                }
            },
        }
        reply = self.api.handle(event)
        self.ddb_stub.assert_no_pending_responses()
        self.lam_stub.assert_no_pending_responses()
        return reply["statusCode"], json.loads(reply["body"])


def _run(**fields: Any) -> dict[str, Any]:
    run = {
        "run_id": "run-1",
        "source": SOURCE,
        "include": [],
        "caller_sub": CURATOR,
        "state": import_plan.RUNNING,
        "dataset": "dandi-000623",
        "created_at": NOW - 60,
        "updated_at": NOW - 50,
        "files": [
            {"relpath": "a.nwb", "session_id": "s-a"},
            {"relpath": "b.nwb", "session_id": "s-b"},
        ],
        "already_imported": [],
    }
    return {**run, **fields}


def _expect_run(rig: Rig, run: dict[str, Any] | None) -> None:
    rig.ddb_stub.add_response(
        "get_item",
        {"Item": sessions.marshal(run)} if run is not None else {},
        expected_params={
            "TableName": RUNS,
            "Key": {"run_id": {"S": "run-1"}},
            "ConsistentRead": True,
        },
    )


def _expect_sessions(rig: Rig, items: list[dict[str, Any]], keys: list[str]) -> None:
    rig.ddb_stub.add_response(
        "batch_get_item",
        {"Responses": {SESSIONS: [sessions.marshal(i) for i in items]}},
        expected_params={
            "RequestItems": {
                SESSIONS: {
                    "Keys": [{"session_id": {"S": k}} for k in keys],
                    "ProjectionExpression": ANY,
                    "ExpressionAttributeNames": ANY,
                }
            }
        },
    )


COMMITTED = {"state": sessions.COMMITTED, "extract_state": "EXTRACTED"}


class TestStart:
    def test_a_non_curator_gets_403_and_nothing_is_called(self) -> None:
        status, body = Rig().call(
            "POST /imports", groups="[researchers]", body={"source": SOURCE}
        )
        assert status == 403
        assert "curators" in body["detail"]

    @pytest.mark.parametrize("route", ["GET /imports", "GET /imports/{run_id}"])
    def test_every_route_is_curators_only(self, route: str) -> None:
        status, _ = Rig().call(route, groups="[researchers]", run_id="run-1")
        assert status == 403

    @pytest.mark.parametrize(
        "source", ["", "zenodo:1@2", "dandi:000623", "dandi:@1", "dandi:1@", 7, None]
    )
    def test_a_bad_source_is_400(self, source: object) -> None:
        status, body = Rig().call("POST /imports", body={"source": source})
        assert status == 400
        assert "source" in body["detail"]

    def test_21_globs_are_400_and_20_are_accepted(self) -> None:
        status, body = Rig().call(
            "POST /imports", body={"source": SOURCE, "include": ["*"] * 21}
        )
        assert status == 400
        assert "20" in body["detail"]
        rig = Rig()
        rig.ddb_stub.add_response("put_item", {})
        rig.lam_stub.add_response("invoke", {"StatusCode": 202})
        status, _ = rig.call(
            "POST /imports", body={"source": SOURCE, "include": ["*"] * 20}
        )
        assert status == 202

    @pytest.mark.parametrize("glob", ["x" * 257, "", 5])
    def test_a_bad_glob_is_400(self, glob: object) -> None:
        status, _ = Rig().call(
            "POST /imports", body={"source": SOURCE, "include": [glob]}
        )
        assert status == 400

    def test_a_non_object_body_is_400(self) -> None:
        assert Rig().call("POST /imports", body="[1]")[0] == 400
        assert Rig().call("POST /imports", body="{")[0] == 400

    def test_the_run_is_created_planning_then_the_planner_is_invoked_async(
        self,
    ) -> None:
        rig = Rig()
        rig.ddb_stub.add_response(
            "put_item",
            {},
            expected_params={
                "TableName": RUNS,
                "Item": sessions.marshal(
                    {
                        "run_id": "run-new",
                        "source": SOURCE,
                        "include": ["sub-1/*"],
                        "caller_sub": CURATOR,
                        "state": "PLANNING",
                        "created_at": NOW,
                        "updated_at": NOW,
                    }
                ),
                "ConditionExpression": "attribute_not_exists(run_id)",
            },
        )
        rig.lam_stub.add_response(
            "invoke",
            {"StatusCode": 202},
            expected_params={
                "FunctionName": PLANNER,
                "InvocationType": "Event",
                "Payload": ANY,
            },
        )
        seen: list[bytes] = []
        rig.lam.meta.events.register(
            "before-parameter-build.lambda.Invoke",
            lambda params, **_: seen.append(params["Payload"]),
        )
        status, body = rig.call(
            "POST /imports", body={"source": SOURCE, "include": ["sub-1/*"]}
        )
        assert (status, body) == (202, {"run_id": "run-new"})
        assert json.loads(seen[0]) == {
            "run_id": "run-new",
            "source": SOURCE,
            "include": ["sub-1/*"],
            "caller": CURATOR,
        }

    def test_sandbox_true_reaches_the_planner(self) -> None:
        """The planner event carries `sandbox` only when it is true."""
        rig = Rig()
        rig.ddb_stub.add_response("put_item", {})
        rig.lam_stub.add_response("invoke", {"StatusCode": 202})
        seen: list[bytes] = []
        rig.lam.meta.events.register(
            "before-parameter-build.lambda.Invoke",
            lambda params, **_: seen.append(params["Payload"]),
        )
        status, _ = rig.call("POST /imports", body={"source": SOURCE, "sandbox": True})
        assert status == 202
        assert json.loads(seen[0]) == {
            "run_id": "run-new",
            "source": SOURCE,
            "include": [],
            "caller": CURATOR,
            "sandbox": True,
        }

    def test_sandbox_false_sends_the_same_planner_event_as_no_flag(self) -> None:
        payloads: list[bytes] = []
        for body in ({"source": SOURCE}, {"source": SOURCE, "sandbox": False}):
            rig = Rig()
            rig.ddb_stub.add_response("put_item", {})
            rig.lam_stub.add_response("invoke", {"StatusCode": 202})
            rig.lam.meta.events.register(
                "before-parameter-build.lambda.Invoke",
                lambda params, **_: payloads.append(params["Payload"]),
            )
            assert rig.call("POST /imports", body=body)[0] == 202
        assert payloads[0] == payloads[1]
        assert "sandbox" not in json.loads(payloads[0])

    @pytest.mark.parametrize("flag", ["true", 1, None, [True]])
    def test_a_non_boolean_sandbox_is_400(self, flag: object) -> None:
        status, body = Rig().call(
            "POST /imports", body={"source": SOURCE, "sandbox": flag}
        )
        assert status == 400
        assert body["detail"] == "sandbox must be true or false"


class TestList:
    def test_the_callers_last_20_runs_newest_first_from_the_gsi(self) -> None:
        rig = Rig()
        rig.ddb_stub.add_response(
            "query",
            {"Items": [sessions.marshal(_run(run_id="r2")), sessions.marshal(_run())]},
            expected_params={
                "TableName": RUNS,
                "IndexName": "by_caller",
                "KeyConditionExpression": "caller_sub = :c",
                "ExpressionAttributeValues": {":c": {"S": CURATOR}},
                "ScanIndexForward": False,
                "Limit": 20,
            },
        )
        status, body = rig.call("GET /imports")
        assert status == 200
        assert [r["run_id"] for r in body["runs"]] == ["r2", "run-1"]
        assert "caller_sub" not in body["runs"][0]

    def test_a_stale_run_lists_as_failed(self) -> None:
        rig = Rig()
        stale = _run(state="PLANNING", files=[], updated_at=NOW - 5000)
        rig.ddb_stub.add_response("query", {"Items": [sessions.marshal(stale)]})
        _, body = rig.call("GET /imports")
        assert body["runs"][0]["state"] == "FAILED"
        assert body["runs"][0]["refusal"] == "the planner did not finish"


class TestShow:
    def test_another_callers_run_is_404_like_a_missing_one(self) -> None:
        rig = Rig()
        _expect_run(rig, _run(caller_sub="someone-else"))
        assert rig.call("GET /imports/{run_id}", run_id="run-1")[0] == 404
        rig = Rig()
        _expect_run(rig, None)
        assert rig.call("GET /imports/{run_id}", run_id="run-1")[0] == 404

    def test_each_files_outcome_comes_from_its_session(self) -> None:
        rig = Rig()
        _expect_run(rig, _run())
        _expect_sessions(
            rig,
            [
                {"session_id": "s-a", **COMMITTED},
                {
                    "session_id": "s-b",
                    "state": sessions.INCOMPLETE,
                    "error": "bad",
                    "commit_error": "worse",
                },
            ],
            ["s-a", "s-b"],
        )
        status, body = rig.call("GET /imports/{run_id}", run_id="run-1")
        assert status == 200
        assert body["files"] == [
            {"relpath": "a.nwb", "outcome": "PASS"},
            {"relpath": "b.nwb", "outcome": "FAIL INCOMPLETE bad worse"},
        ]
        assert body["state"] == "DONE"  # every file has an outcome: computed on read

    def test_a_file_still_moving_has_no_outcome_and_the_run_stays_running(
        self,
    ) -> None:
        rig = Rig()
        _expect_run(rig, _run())
        _expect_sessions(
            rig,
            [{"session_id": "s-a", **COMMITTED}],  # s-b has no session yet
            ["s-a", "s-b"],
        )
        _, body = rig.call("GET /imports/{run_id}", run_id="run-1")
        assert body["state"] == "RUNNING"
        assert [f["outcome"] for f in body["files"]] == ["PASS", None]

    def test_an_extraction_failure_is_a_fail(self) -> None:
        rig = Rig()
        _expect_run(rig, _run(files=[{"relpath": "a.nwb", "session_id": "s-a"}]))
        _expect_sessions(
            rig,
            [
                {
                    "session_id": "s-a",
                    "state": sessions.COMMITTED,
                    "extract_state": "FAILED",
                    "extract_error": "boom",
                }
            ],
            ["s-a"],
        )
        _, body = rig.call("GET /imports/{run_id}", run_id="run-1")
        assert body["files"][0]["outcome"] == "FAIL extraction boom"

    def test_already_imported_files_read_pass_and_a_refusal_shows(self) -> None:
        rig = Rig()
        _expect_run(rig, _run(state="DONE", files=[], already_imported=["x.nwb"]))
        _, body = rig.call("GET /imports/{run_id}", run_id="run-1")
        assert body["state"] == "DONE"
        assert body["files"] == [
            {"relpath": "x.nwb", "outcome": "PASS", "already_imported": True}
        ]
        rig = Rig()
        _expect_run(rig, _run(state="REFUSED", files=[], refusal="too many"))
        _, body = rig.call("GET /imports/{run_id}", run_id="run-1")
        assert (body["state"], body["refusal"], body["files"]) == (
            "REFUSED",
            "too many",
            [],
        )

    def test_sessions_are_read_100_keys_a_call_and_unprocessed_keys_are_retried(
        self,
    ) -> None:
        planned = [{"relpath": f"f{i}", "session_id": f"s{i}"} for i in range(101)]
        rig = Rig()
        _expect_run(rig, _run(files=planned))
        first = [f"s{i}" for i in range(100)]
        rig.ddb_stub.add_response(
            "batch_get_item",
            {
                "Responses": {SESSIONS: []},
                "UnprocessedKeys": {
                    SESSIONS: {"Keys": [{"session_id": {"S": "s99"}}]},
                },
            },
            expected_params={
                "RequestItems": {
                    SESSIONS: {
                        "Keys": [{"session_id": {"S": k}} for k in first],
                        "ProjectionExpression": ANY,
                        "ExpressionAttributeNames": ANY,
                    }
                }
            },
        )
        _expect_sessions(rig, [{"session_id": "s99", **COMMITTED}], ["s99"])
        _expect_sessions(rig, [{"session_id": "s100", **COMMITTED}], ["s100"])
        _, body = rig.call("GET /imports/{run_id}", run_id="run-1")
        outcomes = {f["relpath"]: f["outcome"] for f in body["files"]}
        assert outcomes["f99"] == "PASS"
        assert outcomes["f100"] == "PASS"
        assert outcomes["f0"] is None


class TestStale:
    """I2's gap: a run nothing finishes reads FAILED (computed on read, no writer)."""

    def test_the_lease_derives_from_the_planner_timeout(self) -> None:
        assert (
            timing.IMPORT_RUN_LEASE_SECONDS
            > timing.IMPORT_PLAN_ATTEMPTS * timing.IMPORT_PLAN_TIMEOUT_SECONDS
        )
        assert run_lease({"files": []}) == timing.IMPORT_RUN_LEASE_SECONDS

    @pytest.mark.parametrize("state", ["PLANNING", "RUNNING"])
    def test_a_run_past_its_lease_reads_failed(self, state: str) -> None:
        age = timing.IMPORT_RUN_LEASE_SECONDS + 1
        rig = Rig()
        _expect_run(rig, _run(state=state, files=[], updated_at=NOW - age))
        status, body = rig.call("GET /imports/{run_id}", run_id="run-1")
        assert status == 200
        assert body["state"] == "FAILED"
        assert body["refusal"] == "the planner did not finish"

    def test_a_run_inside_its_lease_is_unchanged(self) -> None:
        age = timing.IMPORT_RUN_LEASE_SECONDS
        rig = Rig()
        _expect_run(rig, _run(state="PLANNING", files=[], updated_at=NOW - age))
        _, body = rig.call("GET /imports/{run_id}", run_id="run-1")
        assert body["state"] == "PLANNING"

    def test_running_gets_one_commit_lease_per_file(self) -> None:
        assert (
            run_lease(_run())
            == timing.IMPORT_RUN_LEASE_SECONDS
            + 2 * timing.IMPORT_RUN_FILE_ALLOWANCE_SECONDS
        )

    def test_a_finished_run_is_never_stale(self) -> None:
        rig = Rig()
        _expect_run(rig, _run(state="REFUSED", files=[], updated_at=1, refusal="x"))
        _, body = rig.call("GET /imports/{run_id}", run_id="run-1")
        assert body["state"] == "REFUSED"
