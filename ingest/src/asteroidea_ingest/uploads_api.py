"""Session API Lambda behind the HTTP API: create, finalize, get, list.

The JWT authorizer (access token) runs before this code and answers 401. This module
adds the group check (403), the request checks (400), the ownership check (404), the
expiry check (410) and the state conflicts (409).
"""

import json
import os
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from asteroidea_ingest import owners, queues, sessions
from asteroidea_ingest.api_common import (
    ApiError,
    Invocation,
    Request,
    Response,
    Timing,
    claim_groups,
    error_response,
    json_response,
    parse_json,
    serve,
)
from asteroidea_ingest.aws_types import S3_NOT_FOUND
from asteroidea_ingest.sessions import SessionTable, Transition
from asteroidea_ingest.upload_rules import (
    DATASET_RE,
    MAX_SIZE,
    PUT_URL_SECONDS,
    crc_problem,
    path_problem,
    upload_id_problem,
)

if TYPE_CHECKING:
    from asteroidea_ingest.aws_types import S3, Sqs

GROUP: Final = "researchers"
# `dataset_meta`: these string keys only, at most 4 KiB as JSON. `tier` takes
# one value: a caller may mark its own dataset sandbox, never curated.
DATASET_META_KEYS: Final = frozenset({"title", "license", "description", "tier"})
SANDBOX_TIER: Final = "sandbox"
DATASET_META_MAX_BYTES: Final = 4096
UPLOAD: Final = "upload"
# Optional session fields GET /sessions/{id} returns when present (minor 4).
OUTCOME_FIELDS: Final = (
    "asset_id",
    "snapshot_id",
    "error",
    "commit_error",
    "extract_error",
)
DISPATCH: Final = {
    "POST /sessions": "create",
    "POST /sessions/{id}/finalize": "finalize",
    "GET /sessions/{id}": "get",
    "GET /uploads": "uploads",
}


# Kept only for `ingest/tests/test_catalog_api.py`, which imports and tests it; the
# handlers check the group in `api_common.serve`.
def group_refusal(claims: dict[str, Any], group: str = GROUP) -> Response | None:
    """Return the 403 for a caller outside `group` (default `GROUP`), else None."""
    if group in claim_groups(claims):
        return None
    return error_response(ApiError(403, f"caller is not in the {group} group"))


def finalize_refusal(state: str, stored: object) -> ApiError:
    """Return the 409 for a session in `state` that cannot be finalized.

    `stored` is the session's `error` attribute (`{"reason": <text>}` from the commit
    path); its reason is kept verbatim inside the sentence.
    """
    code = f"session_{state.lower()}"  # INCOMPLETE is "session_incomplete"
    reason = stored.get("reason") if isinstance(stored, dict) else None
    if isinstance(reason, str) and reason:
        detail = f"The upload session is {state}: {reason}."
    else:
        detail = f"The upload session is {state} and cannot be finalized."
    return ApiError(409, detail, code=code)


def dataset_meta_size(meta: dict[str, Any]) -> int:
    """Return the size of `meta` in UTF-8 bytes of compact JSON."""
    return len(json.dumps(meta, separators=(",", ":"), ensure_ascii=False).encode())


def _dataset_meta_problem(raw: object) -> str | None:
    if not isinstance(raw, dict):
        return "dataset_meta must be a JSON object"
    meta: dict[str, Any] = {str(k): v for k, v in raw.items()}
    extra = sorted(set(meta) - DATASET_META_KEYS)
    if extra:
        allowed = ", ".join(sorted(DATASET_META_KEYS))
        return f"dataset_meta keys must be among {allowed}; got {', '.join(extra)}"
    if not all(isinstance(v, str) for v in meta.values()):
        return "dataset_meta values must be strings"
    if "tier" in meta and meta["tier"] != SANDBOX_TIER:
        return f"dataset_meta.tier must be sandbox when given; got {meta['tier']!r}"
    size = dataset_meta_size(meta)
    if size > DATASET_META_MAX_BYTES:
        return (
            f"dataset_meta must be at most {DATASET_META_MAX_BYTES} bytes (4 KiB) "
            f"of JSON; got {size}"
        )
    return None


def validate_create(body: object) -> str | None:
    """Return the reason a create request is malformed, or `None` when it is valid."""
    if not isinstance(body, dict):
        return "body must be a JSON object"
    dataset, size = body.get("dataset"), body.get("size")
    if not isinstance(dataset, str) or not DATASET_RE.match(dataset):
        return "dataset must match ^[a-z0-9][a-z0-9_-]{0,62}$"
    problem = path_problem(body.get("path"))
    if problem is not None:
        return problem
    if isinstance(size, bool) or not isinstance(size, int) or not 1 <= size <= MAX_SIZE:
        return "size must be an integer from 1 B to 5 GiB"
    problem = crc_problem(body.get("crc64nvme"))
    if problem is None and "upload_id" in body:
        problem = upload_id_problem(body.get("upload_id"))
    if problem is None and "dataset_meta" in body:
        problem = _dataset_meta_problem(body.get("dataset_meta"))
    return problem


@dataclass(frozen=True, slots=True)
class SessionApi:
    """The four routes over injected AWS clients."""

    table: SessionTable
    s3: S3
    sqs: Sqs
    bucket: str
    commit_queue_url: str
    dataset_owners: owners.Owners

    def handle(
        self,
        event: dict[str, Any],
        *,
        now: int | None = None,
        invocation: Invocation | None = None,
    ) -> Response:
        """Route one HTTP API (payload v2) event."""
        return serve(
            event,
            api=self,
            dispatch=DISPATCH,
            invocation=(invocation or Invocation()).at(now),
        )

    def _own(self, sub: str, sid: str, now: int) -> dict[str, Any]:
        item = sessions.get(self.table, sid)
        if item is None or item.get("owner_sub") != sub:
            raise ApiError(404, "no such session")
        if item["expires_at"] < now:
            raise ApiError(410, "session expired", code="session_expired")
        return item

    def create(self, request: Request, _timing: Timing) -> Response:
        """POST /sessions: validate, check ownership, refuse a raw key, presign."""
        sub, now = request.sub, request.now
        body = parse_json(request.raw_body)
        problem = validate_create(body)
        if problem is not None:
            raise ApiError(400, problem)
        if not owners.claim_for_researcher(
            self.dataset_owners,
            owners.RawProbe(self.s3, self.bucket),
            body["dataset"],
            sub,
            now,
        ):
            raise ApiError(
                403,
                (
                    f"dataset {body['dataset']} belongs to another researcher; "
                    "upload under another dataset name"
                ),
                code="dataset_not_yours",
            )
        raw_key = f"raw/{body['dataset']}/{body['path']}"
        try:
            self.s3.head_object(Bucket=self.bucket, Key=raw_key)
        except ClientError as err:
            if err.response["Error"]["Code"] not in S3_NOT_FOUND:
                raise
        else:
            raise ApiError(409, "raw key already exists", code="raw_key_exists")
        sid = str(uuid.uuid4())
        landing_key = f"landing/{sid}/{body['path']}"
        expires_at = now + sessions.OPEN_TTL_SECONDS
        self.table.ddb.put_item(
            TableName=self.table.name,
            Item=sessions.marshal(
                {
                    "session_id": sid,
                    "state": sessions.OPEN,
                    "owner_sub": sub,
                    "dataset": body["dataset"],
                    "relpath": body["path"],
                    "size": body["size"],
                    "client_crc64nvme": body["crc64nvme"],
                    "landing_key": landing_key,
                    "raw_key": raw_key,
                    "created_at": now,
                    "updated_at": now,
                    "expires_at": expires_at,
                    "ingest_method": UPLOAD,
                }
                | {k: body[k] for k in ("upload_id", "dataset_meta") if k in body}
            ),
            ConditionExpression="attribute_not_exists(session_id)",
        )
        url = self.s3.generate_presigned_url(
            ClientMethod="put_object",
            Params={
                "Bucket": self.bucket,
                "Key": landing_key,
                "ChecksumCRC64NVME": body["crc64nvme"],
            },
            ExpiresIn=PUT_URL_SECONDS,
        )
        return json_response(
            201, {"session_id": sid, "expires_at": expires_at, "url": url}
        )

    def finalize(self, request: Request, _timing: Timing) -> Response:
        """POST /sessions/{id}/finalize: claim OPEN to FINALIZING once, then queue."""
        sub, sid, now = request.sub, request.path_params.get("id", ""), request.now
        self._own(sub, sid, now)
        finalize_token = uuid.uuid4().hex
        if sessions.transition(
            self.table,
            sid,
            Transition(
                expect=sessions.OPEN,
                to=sessions.FINALIZING,
                now=now,
                set_values={
                    "finalize_token": finalize_token,
                    "lease_until": now + sessions.COMMIT_LEASE_SECONDS,
                },
            ),
        ):
            queues.send_commit(self.sqs, self.commit_queue_url, sid, finalize_token)
            return json_response(202, {"state": sessions.FINALIZING})
        # The claim lost: report the current state and never send.
        current = self._own(sub, sid, now)
        state = current["state"]
        if state in {sessions.FINALIZING, sessions.ACCEPTED}:
            return json_response(202, {"state": state})
        if state == sessions.COMMITTED:
            return json_response(
                200,
                {
                    "state": state,
                    "asset_id": current.get("asset_id"),
                    "snapshot_id": current.get("snapshot_id"),
                },
            )
        raise finalize_refusal(state, current.get("error"))

    def get(self, request: Request, _timing: Timing) -> Response:
        """GET /sessions/{id}: the state plus the outcome fields that exist."""
        sid = request.path_params.get("id", "")
        item = self._own(request.sub, sid, request.now)
        out = {"state": item["state"]}
        out.update({k: item[k] for k in OUTCOME_FIELDS if k in item})
        return json_response(200, out)

    def uploads(self, request: Request, _timing: Timing) -> Response:
        """GET /uploads: the caller's sessions, newest first, at most 50."""
        found = sessions.by_owner(self.table, request.sub)
        rows = [_upload_row(item) for item in found]
        return json_response(200, {"uploads": rows})


def _upload_row(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "session_id": item["session_id"],
        "dataset": item["dataset"],
        "relpath": item["relpath"],
        "state": item["state"],
        "created_at": item["created_at"],
        "asset_id": item.get("asset_id"),
    }


def handler(event: dict[str, Any], context: object) -> Response:
    """Lambda entry point."""
    api = SessionApi(
        table=SessionTable(boto3.client("dynamodb"), os.environ["SESSIONS_TABLE"]),
        s3=boto3.client("s3", config=Config(signature_version="s3v4")),
        sqs=boto3.client("sqs"),
        bucket=os.environ["RAW_BUCKET"],
        commit_queue_url=os.environ["COMMIT_QUEUE_URL"],
        dataset_owners=owners.from_env(),
    )
    return api.handle(event, invocation=Invocation.of(context))
