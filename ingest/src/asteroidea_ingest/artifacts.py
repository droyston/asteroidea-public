"""Artifact write path: `POST /artifacts` and friends.

Keys in the artifacts bucket, `<sub>` always from the JWT:

    artifacts/<sub>/<artifact_id>/provenance.json   the record (PlatformRole writes it)
    artifacts/<sub>/<artifact_id>/data/<name>       the file (presigned PUT); `data_key`
    index/by-id/<artifact_id>                       {author_sub, state}
    index/by-input/<quoted input id>/<artifact_id>  {state}, one per source and input
    index/by-time/<inverted ms>-<artifact_id>       the summary, written at finalize
    index/by-run/<quoted run id>/<artifact_id>      {state}, a version 2 record's run,
                                                    written at finalize

Every researcher reads any author's COMMITTED or WITHDRAWN record and its bytes
: `GET /artifacts/{id}` finds the author through `index/by-id/`. Only the
author edits, withdraws or deletes one; another researcher gets 403 `not_author`
on a readable record and 404 on an unfinished one, as for an absent id. Finalize
stays the author's own prefix only (404 for anyone else). `GET /artifacts` with no
`scope` is the caller's own records, as before: shipped SDK wheels send no scope.
Validation checks the shape only, except that an `artifact` input must
name a COMMITTED record; a record never expires. A body with `schema`
`asteroidea.provenance/2` is a version 2 record; a body without `schema` keeps the
version 1 rules exactly.
"""

import json
import re
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import quote

from botocore.config import Config
from botocore.exceptions import ClientError

from asteroidea_ingest.api_common import (
    ApiError,
    Invocation,
    Request,
    Response,
    Timing,
    json_response,
    parse_json,
    serve,
)
from asteroidea_ingest.aws_types import S3_NOT_FOUND
from asteroidea_ingest.upload_rules import (
    MAX_SIZE,
    PUT_URL_SECONDS,
    crc_problem,
    path_problem,
    upload_id_problem,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from asteroidea_ingest.aws_types import S3

PENDING: Final = "PENDING"
COMMITTED: Final = "COMMITTED"
INCOMPLETE: Final = "INCOMPLETE"
WITHDRAWN: Final = "WITHDRAWN"
# The states every researcher can read.
READABLE: Final = frozenset({COMMITTED, WITHDRAWN})
# A by-input entry in one of these states blocks deleting the input.
DEPENDENT_STATES: Final = frozenset({PENDING, COMMITTED, WITHDRAWN})
GET_URL_SECONDS: Final = 900
LIST_LIMIT: Final = 100
MAX_SOURCES: Final = 1000
MAX_INPUTS: Final = 1000
MAX_NOTE_BYTES: Final = 1024
MAX_SDK_VERSION: Final = 64
MAX_PARAMETERS_BYTES: Final = 64 * 1024
MAX_INPUT_ID: Final = 256
MAX_TEXT: Final = 256
SCHEMA_V2: Final = "asteroidea.provenance/2"
# The "All" listing fetches up to 100 index bodies in parallel;
# `get_started_api.handler` builds its client with this.
S3_POOL_CONNECTIONS: Final = 32
S3_CLIENT_CONFIG: Final = Config(
    signature_version="s3v4", max_pool_connections=S3_POOL_CONNECTIONS
)
DEFAULT_CONTENT_TYPE: Final = "application/octet-stream"
NOT_FOUND: Final = "no such artifact"
RECORD_FILE: Final = "provenance.json"
INDEX_BY_ID: Final = "index/by-id/"
INDEX_BY_INPUT: Final = "index/by-input/"
INDEX_BY_TIME: Final = "index/by-time/"
INDEX_BY_RUN: Final = "index/by-run/"
# A content type the presigned GET passes on: `type/subtype` and optional
# parameters of plain characters. Anything else is served as the default type.
CONTENT_TYPE_RE: Final = re.compile(
    r"[A-Za-z0-9][\w!#$&^.+-]*/[A-Za-z0-9][\w!#$&^.+-]*(; ?[\w.+-]+=[\w.+-]+)*"
)
# `GET /artifacts/{id}?download=1` signs an attachment disposition; any other
# GET signs an inline one, so a browser shows an image or page in place.
DOWNLOAD_FLAG: Final = "1"
# 13 digits hold every millisecond time before the year 2286.
TIME_DIGITS: Final = 13
# The version 2 create refusal for a token without `email` (an access token): 401
# with this code, never a 403, so the SDK does not read it as "not a researcher".
# Version 1 creates take any token.
NO_EMAIL_CODE: Final = "id_token_required"
NO_EMAIL: Final = (
    "The token has no email claim. Send the ID token, not the access token, as the "
    "bearer to save a result."
)
# The finalize 409s: a short code and a sentence each.
UPLOAD_INCOMPLETE: Final = "upload_incomplete"
UPLOAD_PENDING: Final = "upload_pending"
MARKED_INCOMPLETE: Final = (
    "This result was marked incomplete. Create a new result and upload the file again."
)
NO_OBJECT: Final = "no object uploaded yet"
MISMATCH: Final = (
    "The uploaded file did not match its declared size or checksum, so it was "
    "deleted. Upload it again."
)
# The 5b refusals. The 403 code is not `forbidden` (the group check's),
# so a client can tell "not the author" from "not a researcher".
NOT_FOUND_CODE: Final = "not_found"
NOT_AUTHOR_CODE: Final = "not_author"
NOT_AUTHOR: Final = "Only the author of this result can change, withdraw or delete it."
HAS_DEPENDENTS_CODE: Final = "has_dependents"
BAD_SCOPE_CODE: Final = "invalid_scope"
BAD_SCOPE: Final = "scope must be mine or all"
BAD_INPUT_CODE: Final = "invalid_input"
BAD_RUN_CODE: Final = "invalid_run"
BAD_PATCH_CODE: Final = "invalid_request"
NOT_FINALIZED: Final = "This result is not finalized yet. Finalize it first."
SCOPES: Final = frozenset({"mine", "all"})
PATCH_FIELDS: Final = ("note", "name")
# How many dependents a 409 names before "and N more".
NAMED_DEPENDENTS: Final = 20
DISPATCH: Final = {
    "POST /artifacts": "create",
    "POST /artifacts/{id}/finalize": "finalize",
    "GET /artifacts": "list_records",
    "GET /artifacts/{id}": "get",
    "PATCH /artifacts/{id}": "patch",
    "POST /artifacts/{id}/withdraw": "withdraw",
    "DELETE /artifacts/{id}": "delete",
}
SUMMARY_FIELDS: Final = (
    "artifact_id",
    "name",
    "size",
    "content_type",
    "snapshot_id",
    "state",
    "created_at",
    "finalized_at",
)
# Every listing row also carries the asset ids the result used (`source_asset_ids`,
# the first `LIST_SOURCE_IDS`) and how many there are (`source_asset_count`), so the
# web can group results by the data they used. The cap keeps 100 rows of a record
# with 2000 inputs under the Lambda body limit.
SOURCE_FIELDS: Final = ("source_asset_ids", "source_asset_count")
# Every listing row also carries its run's id and the run's function name: both
# null for a version 1 record.
ROW_RUN_FIELD: Final = "run_id"
ROW_RUN_FUNCTION_FIELD: Final = "run_function"
LIST_SOURCE_IDS: Final = 100
# kind -> the rule for `version`: "text" a non-empty string, "int" an integer
# (a table snapshot id), "none" absent or null (a result id is immutable).
INPUT_KINDS: Final = {
    "asset": "text",
    "artifact": "none",
    "table_snapshot": "int",
    "defaults": "text",
    "recipe": "text",
}
INPUT_FIELDS: Final = frozenset({"kind", "id", "version", "role"})
RUN_REQUIRED: Final = frozenset({"run_id", "function", "parameters"})
RUN_TEXT_OPTIONAL: Final = ("defaults_version", "sdk_version", "analysis_sha256")
RUN_FIELDS: Final = RUN_REQUIRED | {*RUN_TEXT_OPTIONAL, "environment"}


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_text(value: object, limit: int = MAX_TEXT) -> bool:
    return isinstance(value, str) and 1 <= len(value) <= limit


def _source_problem(source: object) -> str | None:
    message = (
        "each source must be an object of non-empty strings asset_id and s3_version_id"
    )
    if not isinstance(source, dict):
        return message
    fields = {str(k): v for k, v in source.items()}
    if set(fields) != {"asset_id", "s3_version_id"}:
        return message
    if not all(isinstance(v, str) and v for v in fields.values()):
        return message
    return None


def _sources_problem(sources: object) -> str | None:
    if not isinstance(sources, list) or not 1 <= len(sources) <= MAX_SOURCES:
        return f"sources must be a list of 1 to {MAX_SOURCES} entries"
    problems = (_source_problem(s) for s in sources)
    return next((p for p in problems if p is not None), None)


def _version_problem(rule: str, fields: dict[str, Any]) -> str | None:
    version = fields.get("version")
    kind = fields["kind"]
    if rule == "text" and not _is_text(version):
        return f"a {kind} input needs version, a string of 1 to {MAX_TEXT} characters"
    if rule == "int" and not _is_int(version):
        return f"a {kind} input needs version, an integer snapshot id"
    if rule == "none" and version is not None:
        return f"a {kind} input takes no version"
    return None


def _input_problem(item: object) -> str | None:
    """Return why one `inputs[]` entry is malformed, or None."""
    if not isinstance(item, dict):
        return "each input must be an object"
    fields: dict[str, Any] = {str(k): v for k, v in item.items()}
    if not set(fields) <= INPUT_FIELDS or not {"kind", "id"} <= set(fields):
        return "each input must have kind and id, and only kind, id, version, role"
    rule = INPUT_KINDS.get(fields["kind"]) if isinstance(fields["kind"], str) else None
    if rule is None:
        return "input kind must be one of " + ", ".join(INPUT_KINDS)
    return _input_id_problem(fields) or _version_problem(rule, fields)


def _input_id_problem(fields: dict[str, Any]) -> str | None:
    if not _is_text(fields["id"], MAX_INPUT_ID):
        return f"input id must be a string of 1 to {MAX_INPUT_ID} characters"
    if fields["kind"] == "artifact" and upload_id_problem(fields["id"]) is not None:
        return "an artifact input id must be a canonical uuid4 string"
    role = fields.get("role")
    if role is not None and not _is_text(role):
        return f"input role must be a string of 1 to {MAX_TEXT} characters, or null"
    return None


def _json_bytes(value: object) -> int:
    return len(json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode())


def _run_problem(run: object) -> str | None:
    """Return why the version 2 `run` object is malformed, or None."""
    if not isinstance(run, dict):
        return "run must be an object"
    fields: dict[str, Any] = {str(k): v for k, v in run.items()}
    if not RUN_REQUIRED <= set(fields) <= RUN_FIELDS:
        return "run must have run_id, function and parameters, and only " + ", ".join(
            sorted(RUN_FIELDS)
        )
    if not _is_text(fields["run_id"]) or not _is_text(fields["function"]):
        return f"run_id and function must be strings of 1 to {MAX_TEXT} characters"
    parameters = fields["parameters"]
    if not isinstance(parameters, dict) or _json_bytes(parameters) > (
        MAX_PARAMETERS_BYTES
    ):
        return "run.parameters must be a JSON object of at most 64 KiB"
    return _run_optional_problem(fields)


def _run_optional_problem(fields: dict[str, Any]) -> str | None:
    for name in RUN_TEXT_OPTIONAL:
        if fields.get(name) is not None and not _is_text(fields[name]):
            return f"run.{name} must be a string of 1 to {MAX_TEXT} characters, or null"
    environment = fields.get("environment")
    if environment is not None and not isinstance(environment, dict):
        return "run.environment must be an object, or null"
    return None


def _v2_links_problem(fields: dict[str, Any]) -> str | None:
    sources, inputs = fields.get("sources", []), fields.get("inputs", [])
    if not isinstance(sources, list) or len(sources) > MAX_SOURCES:
        return f"sources must be a list of at most {MAX_SOURCES} entries"
    if not isinstance(inputs, list) or len(inputs) > MAX_INPUTS:
        return f"inputs must be a list of at most {MAX_INPUTS} entries"
    if not sources and not inputs:
        return "a version 2 record needs at least one source or input"
    problems = [*(_source_problem(s) for s in sources), *map(_input_problem, inputs)]
    return next((p for p in problems if p is not None), None)


def _optional_text_problem(body: dict[str, Any]) -> str | None:
    note, content_type = body.get("note"), body.get("content_type")
    sdk_version = body.get("sdk_version")
    if note is not None and (
        not isinstance(note, str) or len(note.encode()) > MAX_NOTE_BYTES
    ):
        return "note must be a string of at most 1 KiB, or null"
    if content_type is not None and (
        not isinstance(content_type, str) or not content_type
    ):
        return "content_type must be a non-empty string"
    if sdk_version is not None and (
        not isinstance(sdk_version, str) or len(sdk_version) > MAX_SDK_VERSION
    ):
        return f"sdk_version must be a string of at most {MAX_SDK_VERSION} characters"
    return None


def _file_problem(fields: dict[str, Any]) -> str | None:
    name, size = fields.get("name"), fields.get("size")
    problem = path_problem(name)
    if problem is None and "/" in str(name):
        problem = "name must be a single path segment"
    if problem is not None:
        return problem
    if not isinstance(size, int) or isinstance(size, bool) or not 1 <= size <= MAX_SIZE:
        return "size must be an integer from 1 B to 5 GiB"
    return crc_problem(fields.get("crc64nvme"))


def _v1_problem(fields: dict[str, Any]) -> str | None:
    if not _is_int(fields.get("snapshot_id")):
        return "snapshot_id must be an integer"
    return _sources_problem(fields.get("sources")) or _optional_text_problem(fields)


def _v2_problem(fields: dict[str, Any]) -> str | None:
    if "snapshot_id" in fields and not _is_int(fields["snapshot_id"]):
        return "snapshot_id must be an integer, or absent"
    return (
        _v2_links_problem(fields)
        or _run_problem(fields.get("run"))
        or _optional_text_problem(fields)
    )


def validate_create(body: object) -> str | None:
    """Return the reason a create request is malformed, or `None` when it is valid.

    No `schema`: the version 1 rules, unchanged. `schema` `asteroidea.provenance/2`:
    `snapshot_id` optional, `sources` and `inputs` 0 to 1000 each with at least one
    entry between them, and a `run`. Whether an `artifact` input is COMMITTED is
    checked by `ArtifactsApi.create`, which can read the index.
    """
    if not isinstance(body, dict):
        return "body must be a JSON object"
    fields: dict[str, Any] = {str(k): v for k, v in body.items()}
    if "schema" in fields and fields["schema"] != SCHEMA_V2:
        return f"schema must be {SCHEMA_V2}, or absent for a version 1 record"
    if (problem := _file_problem(fields)) is not None:
        return problem
    return _v2_problem(fields) if "schema" in fields else _v1_problem(fields)


def _prefix(sub: str, artifact_id: str) -> str:
    return f"artifacts/{sub}/{artifact_id}"


def source_asset_ids(record: dict[str, Any]) -> list[str]:
    """Return the distinct asset ids a record used: sources, then `asset` inputs."""
    ids = [s["asset_id"] for s in record.get("sources") or []]
    ids += [i["id"] for i in record.get("inputs") or [] if i.get("kind") == "asset"]
    return list(dict.fromkeys(ids))


def _sources(record: dict[str, Any]) -> dict[str, Any]:
    ids = source_asset_ids(record)
    return {"source_asset_ids": ids[:LIST_SOURCE_IDS], "source_asset_count": len(ids)}


def run_id_of(record: dict[str, Any]) -> str | None:
    """Return the record's `run.run_id`, or None for a version 1 record."""
    run = record.get("run")
    run_id = run.get("run_id") if isinstance(run, dict) else None
    return run_id if isinstance(run_id, str) else None


def run_function_of(record: dict[str, Any]) -> str | None:
    """Return the record's `run.function`, or None for a version 1 record."""
    run = record.get("run")
    function = run.get("function") if isinstance(run, dict) else None
    return function if isinstance(function, str) else None


def _run_fields(record: dict[str, Any]) -> dict[str, Any]:
    return {
        ROW_RUN_FIELD: run_id_of(record),
        ROW_RUN_FUNCTION_FIELD: run_function_of(record),
    }


def _summary(record: dict[str, Any]) -> dict[str, Any]:
    return {
        **{k: record.get(k) for k in SUMMARY_FIELDS},
        **_sources(record),
        **_run_fields(record),
    }


def data_key(record: dict[str, Any]) -> str:
    """Return the record's object key; a version 1 record without one gets today's."""
    key = record.get("data_key")
    if isinstance(key, str):
        return key
    return (
        f"{_prefix(record['author_sub'], record['artifact_id'])}/data/{record['name']}"
    )


def input_ids(record: dict[str, Any]) -> list[str]:
    """Return the distinct ids a record was made from: source assets, then inputs."""
    ids = [s["asset_id"] for s in record.get("sources") or []]
    ids += [i["id"] for i in record.get("inputs") or []]
    return list(dict.fromkeys(ids))


def by_input_prefix(input_id: str) -> str:
    """Return the `index/by-input/` prefix for one input; the id is URL-quoted.

    Quoting keeps a `/` in an id from splitting the key. The by-input lookup uses this.
    """
    return f"{INDEX_BY_INPUT}{quote(input_id, safe='')}/"


def by_run_prefix(run_id: str) -> str:
    """Return the `index/by-run/` prefix for one run; the id is URL-quoted."""
    return f"{INDEX_BY_RUN}{quote(run_id, safe='')}/"


def disposition(kind: str, name: str) -> str:
    """Return a Content-Disposition of `kind` (inline or attachment) for `name`.

    The quoted `filename` is the name with every character outside printable
    ASCII, and every quote or backslash, replaced by `_`; `filename*` carries the
    exact name in UTF-8 (RFC 6266).
    """
    plain = "".join(
        c if " " <= c <= "~" and c not in {'"', "\\"} else "_" for c in name
    )
    return f"{kind}; filename=\"{plain}\"; filename*=UTF-8''{quote(name, safe='')}"


def served_type(record: dict[str, Any]) -> str:
    """Return the content type the presigned GET asks S3 to serve.

    The record's `content_type` when it is a plain media type, else the default.
    """
    kind = record.get("content_type")
    if isinstance(kind, str) and CONTENT_TYPE_RE.fullmatch(kind):
        return kind
    return DEFAULT_CONTENT_TYPE


def by_time_key(finalized_at: int, artifact_id: str) -> str:
    """Return the `index/by-time/` key: newest first in key order."""
    inverted = 10**TIME_DIGITS - 1 - finalized_at * 1000
    return f"{INDEX_BY_TIME}{inverted:0{TIME_DIGITS}d}-{artifact_id}"


def _patch_problem(body: dict[str, Any]) -> str | None:
    """Return why a PATCH body is malformed, or None."""
    if not body or not set(body) <= set(PATCH_FIELDS):
        return "send note, name or both, and no other field"
    if "name" in body:
        name = body["name"]
        problem = path_problem(name)
        if problem is None and "/" in str(name):
            problem = "name must be a single path segment"
        if problem is not None:
            return problem
    note = body.get("note")
    if note is not None and (
        not isinstance(note, str) or len(note.encode()) > MAX_NOTE_BYTES
    ):
        return "note must be a string of at most 1 KiB, or null"
    return None


def _is_current(body: dict[str, Any]) -> bool:
    """Whether a `by-time` body has every field a listing row carries."""
    return (
        "source_asset_ids" in body
        and ROW_RUN_FIELD in body
        and ROW_RUN_FUNCTION_FIELD in body
    )


def _shared_summary(record: dict[str, Any]) -> dict[str, Any]:
    return {**_summary(record), "author_email": record.get("author_email")}


def _dependents_detail(ids: list[str]) -> str:
    named = ", ".join(ids[:NAMED_DEPENDENTS])
    more = len(ids) - NAMED_DEPENDENTS
    tail = f" and {more} more" if more > 0 else ""
    return f"Other results used this one: {named}{tail}. Withdraw it instead."


def _new_record(
    body: dict[str, Any], *, sub: str, email: str | None, now: int
) -> dict[str, Any]:
    artifact_id = str(uuid.uuid4())
    record: dict[str, Any] = {
        "artifact_id": artifact_id,
        "author_sub": sub,
        "author_email": email,
        "name": body["name"],
        "data_key": f"{_prefix(sub, artifact_id)}/data/{body['name']}",
        "size": body["size"],
        "crc64nvme": body["crc64nvme"],
        "content_type": body.get("content_type") or DEFAULT_CONTENT_TYPE,
        "snapshot_id": body.get("snapshot_id"),
        "sources": body.get("sources", []),
        "note": body.get("note"),
        "sdk_version": body.get("sdk_version"),
        "state": PENDING,
        "created_at": now,
        "finalized_at": None,
    }
    if "schema" in body:
        record = {"schema": SCHEMA_V2, **record}
        record["inputs"] = body.get("inputs", [])
        record["run"] = body["run"]
    return record


@dataclass(frozen=True, slots=True)
class ArtifactsApi:
    """The seven artifact routes over an S3 client and the artifacts bucket."""

    s3: S3
    bucket: str

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

    def create(self, request: Request, _timing: Timing) -> Response:
        """POST /artifacts: write the PENDING record and index, presign the PUT.

        A body with `schema` from a token without `email` is refused 401
        `id_token_required` before validation. A version 1 body creates with any
        token; without `email` its `author_email` is null until the 5c back-fill
        (shipped SDK wheels send the access token). An `artifact` input that is not
        COMMITTED is a 400 naming it.
        """
        sub, now = request.sub, request.now
        email = request.email or None
        body = parse_json(request.raw_body)
        if "schema" in body and email is None:
            raise ApiError(401, NO_EMAIL, code=NO_EMAIL_CODE)
        if (problem := validate_create(body)) is not None:
            raise ApiError(400, problem)
        # A version 1 body's stray `inputs` is not validated, so never read it.
        self._check_artifact_inputs(body.get("inputs", []) if "schema" in body else [])
        record = _new_record(body, sub=sub, email=email, now=now)
        self._write(sub, record)
        self._write_state_index(record)
        url = self.s3.generate_presigned_url(
            ClientMethod="put_object",
            Params={
                "Bucket": self.bucket,
                "Key": record["data_key"],
                "ChecksumCRC64NVME": record["crc64nvme"],
            },
            ExpiresIn=PUT_URL_SECONDS,
        )
        return json_response(
            201,
            {
                "artifact_id": record["artifact_id"],
                "url": url,
                "expires_at": now + PUT_URL_SECONDS,
            },
        )

    def finalize(self, request: Request, _timing: Timing) -> Response:
        """POST /artifacts/{id}/finalize: check the object against the record.

        COMMITTED writes `index/by-time/` and rewrites the `by-id` and `by-input`
        state; INCOMPLETE rewrites those two.
        """
        sub, now = request.sub, request.now
        artifact_id = request.path_params.get("id", "")
        record = self._own(sub, artifact_id)
        if record["state"] == COMMITTED:
            return json_response(200, {"state": COMMITTED, "artifact_id": artifact_id})
        if record["state"] == INCOMPLETE:
            raise ApiError(409, MARKED_INCOMPLETE, code=UPLOAD_INCOMPLETE)
        key = data_key(record)
        try:
            head = self.s3.head_object(
                Bucket=self.bucket, Key=key, ChecksumMode="ENABLED"
            )
        except ClientError as err:
            if err.response["Error"]["Code"] not in S3_NOT_FOUND:
                raise
            # Nothing uploaded yet: stay PENDING so the caller can upload and retry.
            raise ApiError(409, NO_OBJECT, code=UPLOAD_PENDING) from None
        if (
            head.get("ContentLength") != record["size"]
            or head.get("ChecksumCRC64NVME") != record["crc64nvme"]
        ):
            self.s3.delete_object(Bucket=self.bucket, Key=key)
            incomplete = {**record, "state": INCOMPLETE}
            self._write(sub, incomplete)
            self._write_state_index(incomplete)
            raise ApiError(409, MISMATCH, code=UPLOAD_INCOMPLETE)
        committed = {**record, "state": COMMITTED, "finalized_at": now}
        self._write(sub, committed)
        self._write_by_time(committed)
        self._write_state_index(committed)
        return json_response(200, {"state": COMMITTED, "artifact_id": artifact_id})

    def list_records(self, request: Request, _timing: Timing) -> Response:
        """GET /artifacts: `scope` mine (the default) or all, optional `input` or `run`.

        No `scope` is `mine`: the caller's records of every state, newest first, at
        most 100, as before 5b. `all` is the newest 100 finalized records of every
        author from `index/by-time/`. `input` is the records that used that asset
        or result (`index/by-input/`): the caller's when `mine`, every author's
        COMMITTED or WITHDRAWN ones when `all`, newest first, at most 100. `run` is
        the finalized records of one run (`index/by-run/`), with the same scope
        rule. A request with both `input` and `run` is a 400. `all`, `input` and
        `run` rows add `author_email`; `run` rows also add `url`, a presigned inline
        GET of the file (900 s), so a page draws a run's thumbnails in one request.
        """
        scope = request.query.get("scope", "mine")
        if scope not in SCOPES:
            raise ApiError(400, BAD_SCOPE, code=BAD_SCOPE_CODE)
        owner = None if scope == "all" else request.sub
        if "run" in request.query:
            run_id = request.query["run"]
            if "input" in request.query or not _is_text(run_id):
                raise ApiError(
                    400,
                    f"run must be an id of 1 to {MAX_TEXT} characters, without input",
                    code=BAD_RUN_CODE,
                )
            rows = [
                {
                    **_shared_summary(r),
                    "url": self._presigned_get(r, download=False),
                }
                for r in self._indexed(by_run_prefix(run_id), owner)
            ]
        elif "input" in request.query:
            input_id = request.query["input"]
            if not _is_text(input_id, MAX_INPUT_ID):
                raise ApiError(
                    400,
                    f"input must be an id of 1 to {MAX_INPUT_ID} characters",
                    code=BAD_INPUT_CODE,
                )
            rows = [
                _shared_summary(r)
                for r in self._indexed(by_input_prefix(input_id), owner)
            ]
        elif scope == "all":
            rows = self._newest_of_all()
        else:
            rows = [_summary(r) for r in self._mine(request.sub)]
        return json_response(200, {"artifacts": rows})

    def _mine(self, sub: str) -> list[dict[str, Any]]:
        keys = [
            (item["LastModified"], item["Key"])
            for item in self._list(f"artifacts/{sub}/")
            if item["Key"].endswith(f"/{RECORD_FILE}")
        ]
        newest = [key for _, key in sorted(keys, reverse=True)[:LIST_LIMIT]]
        records = [r for r in self._read_many(newest) if r is not None]
        records.sort(key=lambda r: r["created_at"], reverse=True)
        return records

    def _newest_of_all(self) -> list[dict[str, Any]]:
        """Return the newest 100 `index/by-time/` bodies, fetched in parallel."""
        page = self.s3.list_objects_v2(
            Bucket=self.bucket, Prefix=INDEX_BY_TIME, MaxKeys=LIST_LIMIT
        )
        keys = sorted(item["Key"] for item in page.get("Contents", []))
        bodies = [b for b in self._read_many(keys[:LIST_LIMIT]) if b is not None]
        return self._with_sources(bodies)

    def _with_sources(self, bodies: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Fill the source and run fields of `by-time` bodies written before them.

        Such a body is rewritten with them at its next patch or withdraw; until then
        its record is read through `index/by-id/`. A record no longer readable keeps
        an empty list, a null run id and a null run function.
        """
        old = [b for b in bodies if not _is_current(b)]
        if not old:
            return bodies
        entries = self._read_many([f"{INDEX_BY_ID}{b['artifact_id']}" for b in old])
        keys = [
            f"{_prefix(e['author_sub'], b['artifact_id'])}/{RECORD_FILE}"
            for b, e in zip(old, entries, strict=True)
            if e is not None
        ]
        records = {r["artifact_id"]: r for r in self._read_many(keys) if r is not None}
        empty = {
            "source_asset_ids": [],
            "source_asset_count": 0,
            ROW_RUN_FIELD: None,
            ROW_RUN_FUNCTION_FIELD: None,
        }
        return [
            b
            if _is_current(b)
            else {
                **b,
                **(
                    {
                        **_sources(records[b["artifact_id"]]),
                        **_run_fields(records[b["artifact_id"]]),
                    }
                    if b["artifact_id"] in records
                    else empty
                ),
            }
            for b in bodies
        ]

    def _indexed(self, prefix: str, sub: str | None) -> list[dict[str, Any]]:
        """Return the records indexed under `prefix`: `sub`'s, or every readable one.

        `prefix` is one input's `index/by-input/` or one run's `index/by-run/`.
        """
        ids = [item["Key"].removeprefix(prefix) for item in self._list(prefix)]
        entries = self._read_many([f"{INDEX_BY_ID}{i}" for i in ids])
        wanted = [
            f"{_prefix(entry['author_sub'], artifact_id)}/{RECORD_FILE}"
            for artifact_id, entry in zip(ids, entries, strict=True)
            if entry is not None
            and (
                entry["author_sub"] == sub
                if sub is not None
                else entry["state"] in READABLE
            )
        ]
        records = [
            r
            for r in self._read_many(wanted)
            if r is not None and (sub is not None or r["state"] in READABLE)
        ]
        records.sort(key=lambda r: r["created_at"], reverse=True)
        return records[:LIST_LIMIT]

    def _presigned_get(self, record: dict[str, Any], *, download: bool) -> str:
        """Presign a GET of the record's file (900 s), typed, inline or attachment.

        Presigning signs locally; it makes no S3 call.
        """
        return self.s3.generate_presigned_url(
            ClientMethod="get_object",
            Params={
                "Bucket": self.bucket,
                "Key": data_key(record),
                "ResponseContentType": served_type(record),
                "ResponseContentDisposition": disposition(
                    "attachment" if download else "inline", record["name"]
                ),
            },
            ExpiresIn=GET_URL_SECONDS,
        )

    def get(self, request: Request, _timing: Timing) -> Response:
        """GET /artifacts/{id}: any author's COMMITTED or WITHDRAWN record, and a GET.

        The presigned GET is of `data_key` (900 s). It asks S3 to serve the record's
        content type, inline, so a browser shows an image or page in place;
        `?download=1` asks for an attachment, so a navigation to it saves the file.
        Unknown or unfinished is 404.
        """
        record = self._locate(request.sub, request.path_params.get("id", ""))
        if record["state"] not in READABLE:
            raise ApiError(404, NOT_FOUND, code=NOT_FOUND_CODE)
        download = request.query.get("download") == DOWNLOAD_FLAG
        url = self._presigned_get(record, download=download)
        return json_response(200, {"record": record, "url": url})

    def patch(self, request: Request, _timing: Timing) -> Response:
        """PATCH /artifacts/{id}: the author edits `note` and the display `name`.

        `data_key` never changes. A finalized record's `by-time` body is rewritten.
        """
        record = self._authored(request)
        body = parse_json(request.raw_body)
        if (problem := _patch_problem(body)) is not None:
            raise ApiError(400, problem, code=BAD_PATCH_CODE)
        patched = {**record, **{k: body[k] for k in PATCH_FIELDS if k in body}}
        self._write(patched["author_sub"], patched)
        self._write_by_time(patched)
        return json_response(200, {"artifact": _shared_summary(patched)})

    def withdraw(self, request: Request, _timing: Timing) -> Response:
        """POST /artifacts/{id}/withdraw: the author sets WITHDRAWN; bytes stay.

        Withdrawing a WITHDRAWN record answers 200 again. A PENDING record is 409
        `upload_pending`, an INCOMPLETE one 409 `upload_incomplete`.
        """
        record = self._authored(request)
        artifact_id = record["artifact_id"]
        if record["state"] == PENDING:
            raise ApiError(409, NOT_FINALIZED, code=UPLOAD_PENDING)
        if record["state"] == INCOMPLETE:
            raise ApiError(409, MARKED_INCOMPLETE, code=UPLOAD_INCOMPLETE)
        if record["state"] == COMMITTED:
            withdrawn = {**record, "state": WITHDRAWN}
            self._write(withdrawn["author_sub"], withdrawn)
            self._write_state_index(withdrawn)
            self._write_by_time(withdrawn)
        return json_response(200, {"state": WITHDRAWN, "artifact_id": artifact_id})

    def delete(self, request: Request, _timing: Timing) -> Response:
        """DELETE /artifacts/{id}: the author removes a result nothing else used.

        Refused 409 `has_dependents`, naming them, while any `index/by-input/<id>/`
        entry is PENDING, COMMITTED or WITHDRAWN. Otherwise deletes its own
        `by-input` entries, its `by-run` entry, `by-time`, the data object, `by-id`
        and, last, the record.
        """
        record = self._authored(request)
        artifact_id = record["artifact_id"]
        if dependents := self._dependents(artifact_id):
            raise ApiError(
                409, _dependents_detail(dependents), code=HAS_DEPENDENTS_CODE
            )
        keys = [f"{by_input_prefix(i)}{artifact_id}" for i in input_ids(record)]
        if (run_id := run_id_of(record)) is not None:
            keys.append(f"{by_run_prefix(run_id)}{artifact_id}")
        if record.get("finalized_at") is not None:
            keys.append(by_time_key(record["finalized_at"], artifact_id))
        keys += [
            data_key(record),
            f"{INDEX_BY_ID}{artifact_id}",
            f"{_prefix(record['author_sub'], artifact_id)}/{RECORD_FILE}",
        ]
        for key in keys:
            self.s3.delete_object(Bucket=self.bucket, Key=key)
        return json_response(200, {"state": "DELETED", "artifact_id": artifact_id})

    def _dependents(self, artifact_id: str) -> list[str]:
        """Return the ids whose by-input entry for `artifact_id` blocks a delete."""
        prefix = by_input_prefix(artifact_id)
        keys = [item["Key"] for item in self._list(prefix)]
        bodies = self._read_many(keys)
        return sorted(
            key.removeprefix(prefix)
            for key, body in zip(keys, bodies, strict=True)
            if body is not None and body.get("state") in DEPENDENT_STATES
        )

    def _locate(self, sub: str, artifact_id: str) -> dict[str, Any]:
        """Return any author's record: the author from `index/by-id/`, else `sub`.

        A version 1 record from before the index has no `by-id` entry until the 5c
        back-fill; it is found under the caller's own prefix only.
        """
        if upload_id_problem(artifact_id) is not None:
            raise ApiError(404, NOT_FOUND, code=NOT_FOUND_CODE)
        entry = self._read(f"{INDEX_BY_ID}{artifact_id}")
        author = str(entry["author_sub"]) if entry is not None else sub
        record = self._read(f"{_prefix(author, artifact_id)}/{RECORD_FILE}")
        if record is None or record.get("author_sub") != author:
            raise ApiError(404, NOT_FOUND, code=NOT_FOUND_CODE)
        return record

    def _authored(self, request: Request) -> dict[str, Any]:
        """Return the record when the caller is its author.

        Another researcher gets 403 `not_author` on a readable record and 404 on an
        unfinished one, the same answer as an absent id.
        """
        record = self._locate(request.sub, request.path_params.get("id", ""))
        if record["author_sub"] != request.sub:
            if record["state"] in READABLE:
                raise ApiError(403, NOT_AUTHOR, code=NOT_AUTHOR_CODE)
            raise ApiError(404, NOT_FOUND, code=NOT_FOUND_CODE)
        return record

    def _write_by_time(self, record: dict[str, Any]) -> None:
        """Rewrite the `by-time` summary of a finalized record; else do nothing."""
        if record.get("finalized_at") is not None:
            self._put_index(
                by_time_key(record["finalized_at"], record["artifact_id"]),
                _shared_summary(record),
            )

    def _list(self, prefix: str) -> list[dict[str, Any]]:
        """Return every object listed under `prefix`, following continuation."""
        items: list[dict[str, Any]] = []
        token = ""
        while True:
            page = self.s3.list_objects_v2(
                Bucket=self.bucket,
                Prefix=prefix,
                MaxKeys=1000,
                **({"ContinuationToken": token} if token else {}),
            )
            items += page.get("Contents", [])
            token = page.get("NextContinuationToken")
            if not token:
                return items

    def _read_many(self, keys: Sequence[str]) -> list[dict[str, Any] | None]:
        """Read JSON objects in parallel, at most `S3_POOL_CONNECTIONS` at a time."""
        if len(keys) <= 1:
            return [self._read(k) for k in keys]
        with ThreadPoolExecutor(max_workers=S3_POOL_CONNECTIONS) as pool:
            return list(pool.map(self._read, keys))

    def _check_artifact_inputs(self, inputs: list[dict[str, Any]]) -> None:
        """Refuse 400 at the first `artifact` input whose `by-id` is not COMMITTED."""
        ids = dict.fromkeys(i["id"] for i in inputs if i["kind"] == "artifact")
        for artifact_id in ids:
            entry = self._read(f"{INDEX_BY_ID}{artifact_id}")
            if entry is None or entry.get("state") != COMMITTED:
                raise ApiError(
                    400, f"artifact input {artifact_id} is not a committed result"
                )

    def _write_state_index(self, record: dict[str, Any]) -> None:
        """Write `index/by-id/<id>` and each `index/by-input/<input>/<id>` state.

        A finalized version 2 record (COMMITTED or WITHDRAWN) also writes
        `index/by-run/<run>/<id>`: first at finalize, again at withdraw.
        """
        artifact_id, state = record["artifact_id"], record["state"]
        self._put_index(
            f"{INDEX_BY_ID}{artifact_id}",
            {"author_sub": record["author_sub"], "state": state},
        )
        for input_id in input_ids(record):
            self._put_index(
                f"{by_input_prefix(input_id)}{artifact_id}", {"state": state}
            )
        run_id = run_id_of(record)
        if run_id is not None and state in READABLE:
            self._put_index(f"{by_run_prefix(run_id)}{artifact_id}", {"state": state})

    def _own(self, sub: str, artifact_id: str) -> dict[str, Any]:
        """Return the caller's record; another sub's or an absent one is a 404."""
        if upload_id_problem(artifact_id) is not None:
            raise ApiError(404, NOT_FOUND)
        record = self._read(f"{_prefix(sub, artifact_id)}/{RECORD_FILE}")
        if record is None or record.get("author_sub") != sub:
            raise ApiError(404, NOT_FOUND)
        return record

    def _read(self, key: str) -> dict[str, Any] | None:
        try:
            got = self.s3.get_object(Bucket=self.bucket, Key=key)
        except ClientError as err:
            if err.response["Error"]["Code"] in S3_NOT_FOUND:
                return None
            raise
        record: dict[str, Any] = json.loads(got["Body"].read())
        return record

    def _put_index(self, key: str, body: dict[str, Any]) -> None:
        self.s3.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=json.dumps(body).encode(),
            ContentType="application/json",
        )

    def _write(self, sub: str, record: dict[str, Any]) -> None:
        self.s3.put_object(
            Bucket=self.bucket,
            Key=f"{_prefix(sub, record['artifact_id'])}/{RECORD_FILE}",
            Body=json.dumps(record).encode(),
            ContentType="application/json",
        )
