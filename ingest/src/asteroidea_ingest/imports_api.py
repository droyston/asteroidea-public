"""Imports Lambda behind the HTTP API.

`POST /imports` starts a web import run for a curator, `GET /imports` lists the
caller's last runs and `GET /imports/{run_id}` shows one with the outcome of each
file. The run item lives in the import-runs table; ImportPlanFunction plans it and
invokes ImportFunction once per file. After that nothing writes the run, so this
module computes on read: a file's outcome comes from its sessions-table item
(`import_outcome.outcome`), a RUNNING run whose files all have an outcome reads
DONE, and a run still PLANNING or RUNNING past its lease
(`timing.IMPORT_RUN_LEASE_SECONDS`) reads FAILED.
"""

import json
import os
import re
import time
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

import boto3

from asteroidea_ingest import import_outcome, sessions, timing
from asteroidea_ingest.api_common import (
    ApiError,
    Invocation,
    Request,
    Response,
    Timing,
    json_response,
    serve,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from asteroidea_ingest.aws_types import ImportsStore, Invoker

__all__ = ["DISPATCH", "ImportsApi", "handler"]

DISPATCH: Final = {
    "POST /imports": "start",
    "GET /imports": "list_runs",
    "GET /imports/{run_id}": "show",
}
RUNS_CALLER_INDEX: Final = "by_caller"
MAX_GLOBS: Final = 20
MAX_GLOB_CHARS: Final = 256
LIST_LIMIT: Final = 20
BATCH_KEYS: Final = 100  # BatchGetItem's limit
BATCH_ROUNDS: Final = 5  # rounds to re-ask UnprocessedKeys before giving up
SOURCE_RE: Final = re.compile(
    r"^(?:dandi|openneuro):[A-Za-z0-9][A-Za-z0-9._-]{0,63}@[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"
)
FAILED: Final = "FAILED"
STALE_REASON: Final = "the planner did not finish"
SESSION_FIELDS: Final = (
    "session_id",
    "state",
    "extract_state",
    "error",
    "commit_error",
    "extract_error",
)
ID_MAX: Final = 128


def _sandbox(body: Mapping[str, object]) -> bool:
    """Return the optional `sandbox` flag, or raise a 400 when it is not a boolean."""
    flag = body.get("sandbox", False)
    if not isinstance(flag, bool):
        raise ApiError(400, "sandbox must be true or false")
    return flag


def _request(body: Mapping[str, object]) -> tuple[str, list[str]]:
    """Return (source, include) or raise a 400 naming the first problem."""
    source = body.get("source")
    if not isinstance(source, str) or not SOURCE_RE.match(source):
        msg = "source must be dandi:<id>@<version> or openneuro:<id>@<version>"
        raise ApiError(400, msg)
    include = body.get("include", [])
    if not isinstance(include, list):
        raise ApiError(400, "include must be a list of globs")
    if len(include) > MAX_GLOBS:
        raise ApiError(400, f"include holds at most {MAX_GLOBS} globs")
    globs = [g for g in include if isinstance(g, str)]
    if len(globs) != len(include) or any(
        not 1 <= len(g) <= MAX_GLOB_CHARS for g in globs
    ):
        msg = f"each glob must be 1 to {MAX_GLOB_CHARS} characters"
        raise ApiError(400, msg)
    return source, globs


def run_lease(run: Mapping[str, Any]) -> int:
    """Return the seconds a PLANNING or RUNNING run may sit after its last write."""
    files = len(run.get("files", ()))
    return (
        timing.IMPORT_RUN_LEASE_SECONDS
        + files * timing.IMPORT_RUN_FILE_ALLOWANCE_SECONDS
    )


def _stale(run: Mapping[str, Any], now: int) -> bool:
    if run.get("state") not in {import_outcome.PLANNING, import_outcome.RUNNING}:
        return False
    return now - int(run.get("updated_at", 0)) > run_lease(run)


def _summary(run: Mapping[str, Any], now: int) -> dict[str, Any]:
    """Return the run row of `GET /imports`: the stored state, stale ones FAILED."""
    shown: dict[str, Any] = {
        "run_id": run["run_id"],
        "source": run.get("source"),
        "include": run.get("include", []),
        "state": run.get("state"),
        "created_at": run.get("created_at"),
    }
    if "dataset" in run:
        shown["dataset"] = run["dataset"]
    if "refusal" in run:
        shown["refusal"] = run["refusal"]
    if _stale(run, now):
        shown["state"], shown["refusal"] = FAILED, STALE_REASON
    return shown


@dataclass(frozen=True, slots=True)
class ImportsApi:
    """The imports routes over injected DynamoDB and Lambda clients and a clock."""

    ddb: ImportsStore
    runs_table: str
    sessions_table: str
    lam: Invoker
    planner_function: str
    now: Callable[[], int] = lambda: int(time.time())
    new_id: Callable[[], str] = lambda: uuid.uuid4().hex

    def handle(
        self, event: dict[str, Any], *, invocation: Invocation | None = None
    ) -> Response:
        """Route one HTTP API (payload v2) event; curators only (`routes.ROUTES`)."""
        return serve(event, api=self, dispatch=DISPATCH, invocation=invocation)

    def start(self, request: Request, _timing: Timing) -> Response:
        """POST /imports: write the PLANNING run, then invoke the planner."""
        sub = request.sub
        body = request.json_body()
        source, include = _request(body)
        sandbox = _sandbox(body)
        run_id, now = self.new_id(), self.now()
        self.ddb.put_item(
            TableName=self.runs_table,
            Item=sessions.marshal(
                {
                    "run_id": run_id,
                    "source": source,
                    "include": include,
                    "caller_sub": sub,
                    "state": import_outcome.PLANNING,
                    "created_at": now,
                    "updated_at": now,
                }
            ),
            ConditionExpression="attribute_not_exists(run_id)",
        )
        planner_event: dict[str, Any] = {
            "run_id": run_id,
            "source": source,
            "include": include,
            "caller": sub,
        }
        if sandbox:  # absent when false: the event is unchanged for other runs
            planner_event["sandbox"] = True
        self.lam.invoke(
            FunctionName=self.planner_function,
            InvocationType="Event",  # the planner outlives the 30 s HTTP limit
            Payload=json.dumps(planner_event).encode(),
        )
        return json_response(202, {"run_id": run_id})

    def list_runs(self, request: Request, _timing: Timing) -> Response:
        """GET /imports: the caller's last runs, newest first."""
        sub = request.sub
        page = self.ddb.query(
            TableName=self.runs_table,
            IndexName=RUNS_CALLER_INDEX,
            KeyConditionExpression="caller_sub = :c",
            ExpressionAttributeValues=sessions.marshal({":c": sub}),
            ScanIndexForward=False,
            Limit=LIST_LIMIT,
        )
        now = self.now()
        runs = [_summary(sessions.unmarshal(i), now) for i in page.get("Items", [])]
        return json_response(200, {"runs": runs})

    def show(self, request: Request, _timing: Timing) -> Response:
        """GET /imports/{run_id}: one run with the outcome of each file."""
        run_id, sub = str(request.path_params.get("run_id") or ""), request.sub
        found = None
        if run_id and len(run_id) <= ID_MAX:
            found = self.ddb.get_item(
                TableName=self.runs_table,
                Key=sessions.marshal({"run_id": run_id}),
                ConsistentRead=True,
            ).get("Item")
        run = sessions.unmarshal(found) if found else None
        if run is None or run.get("caller_sub") != sub:
            raise ApiError(404, "no such run")  # another caller's run looks the same
        planned = list(run.get("files", ()))
        items = self._sessions([str(f["session_id"]) for f in planned])
        files = [
            {
                "relpath": f["relpath"],
                "outcome": import_outcome.outcome(items.get(str(f["session_id"]))),
            }
            for f in planned
        ]
        # A file imported earlier has no session this run owns; its PASS is the run's
        # record (the session item's TTL may have removed it).
        files += [
            {"relpath": r, "outcome": "PASS", "already_imported": True}
            for r in run.get("already_imported", ())
        ]
        shown = _summary(run, self.now())
        state = shown["state"]
        if state == import_outcome.RUNNING and all(
            f["outcome"] is not None for f in files
        ):
            state = import_outcome.DONE
        shown.pop("include", None)
        shown.pop("created_at", None)
        return json_response(200, {**shown, "state": state, "files": files})

    def _sessions(self, session_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
        """Return session_id -> item for the sessions that exist (100 keys a call)."""
        found: dict[str, dict[str, Any]] = {}
        names = {f"#{f}": f for f in SESSION_FIELDS}
        for start in range(0, len(session_ids), BATCH_KEYS):
            keys = [
                sessions.marshal({"session_id": s})
                for s in session_ids[start : start + BATCH_KEYS]
            ]
            for _ in range(BATCH_ROUNDS):
                reply = self.ddb.batch_get_item(
                    RequestItems={
                        self.sessions_table: {
                            "Keys": keys,
                            "ProjectionExpression": ", ".join(names),
                            "ExpressionAttributeNames": names,
                        }
                    }
                )
                for raw in reply.get("Responses", {}).get(self.sessions_table, ()):
                    item = sessions.unmarshal(raw)
                    found[str(item["session_id"])] = item
                keys = (
                    reply.get("UnprocessedKeys", {})
                    .get(self.sessions_table, {})
                    .get("Keys", [])
                )
                if not keys:
                    break
            else:
                msg = "DynamoDB left session keys unprocessed"
                raise ApiError(503, msg)
        return found


def handler(event: dict[str, Any], context: object) -> Response:
    """Lambda entry point."""
    return ImportsApi(
        ddb=boto3.client("dynamodb"),
        runs_table=os.environ["RUNS_TABLE"],
        sessions_table=os.environ["SESSIONS_TABLE"],
        lam=boto3.client("lambda"),
        planner_function=os.environ["IMPORT_PLAN_FUNCTION_NAME"],
    ).handle(event, invocation=Invocation.of(context))
