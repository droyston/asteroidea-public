"""ImportFunction: import one source file and finalize its upload session.

The curator CLI (`make import`) invokes this asynchronously once per file, with
the mode it chose from the source bucket's region: `s3` (same region: CopyObject) or
`https` (another region, or an ancillary file: an anonymous streamed GET into one
PutObject). The handler never picks the mode from the archive name.

Idempotent per file: the session id is `uuid5(IMPORT_NS, "<dataset>/<relpath>
@<source_version>")` and the first write is a conditional PutItem; a repeat delivery
continues from the state it finds (OPEN: move again and finalize; FINALIZING: re-send
the commit message with the same dedup id; later: nothing).
"""

import base64
import hashlib
import http.client
import io
import os
import tempfile
import time
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from typing import IO, TYPE_CHECKING, Any, Final, override

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from asteroidea_ingest import owners, queues, sessions
from asteroidea_ingest.commit import NOT_FOUND_CODES, SHA256
from asteroidea_ingest.sessions import SessionTable, Transition
from asteroidea_ingest.upload_rules import DATASET_RE, MAX_SIZE, path_problem

if TYPE_CHECKING:
    from collections.abc import Callable

    from asteroidea_ingest.aws_types import S3, Sqs

IMPORT_NS: Final = uuid.UUID("6a1d3c52-5b0e-4f7e-9d43-2f8c1e7b9a60")
MODE_S3: Final = "s3"
MODE_HTTPS: Final = "https"
ANCILLARY_MAX_SIZE: Final = 100 * 1024**2
READ_CHUNK: Final = 1024**2
HTTP_TIMEOUT_SECONDS: Final = 60
REFUSED: Final = "REFUSED"
NOTHING_TO_DO: Final = "NOTHING_TO_DO"
DIGEST_MISMATCH: Final = "source digest mismatch"
NOT_YOURS: Final = "dataset owned by a researcher"
# botocore's SigV4 seeks the body to hash it unless payload signing is off, and a
# streamed body cannot be rewound for a retry. `max_attempts: 1` is not enough, because
# botocore counts it as retries (the client resolves it to total_max_attempts 2), so one
# attempt in total is spelt here.
STREAM_CLIENT_CONFIG: Final = Config(
    s3={"payload_signing_enabled": False},
    retries={"total_max_attempts": 1, "mode": "standard"},
)


class ImportRefusedError(ValueError):
    """The event cannot be imported; the message names why. No session is created."""


def session_id(dataset: str, relpath: str, source_version: str) -> str:
    """Return the deterministic import session id: the same file always maps to it."""
    return str(uuid.uuid5(IMPORT_NS, f"{dataset}/{relpath}@{source_version}"))


def b64_sha256(hex_digest: str) -> str:
    """Convert a publisher's hex SHA-256 to S3's base64 form."""
    return base64.b64encode(bytes.fromhex(hex_digest)).decode()


@dataclass(frozen=True, slots=True)
class ImportEvent:
    """One file to import, as the curator CLI sends it."""

    mode: str
    dataset: str
    relpath: str
    size: int
    source: dict[str, str]
    dataset_meta: dict[str, str]
    caller: str
    ancillary: bool

    @classmethod
    def parse(cls, event: dict[str, Any]) -> ImportEvent:
        """Validate the event; raise `ImportRefusedError` naming the problem."""
        mode = event.get("mode")
        if mode not in {MODE_S3, MODE_HTTPS}:
            msg = f"mode must be {MODE_S3!r} or {MODE_HTTPS!r}, got {mode!r}"
            raise ImportRefusedError(msg)
        dataset, relpath = event.get("dataset"), event.get("relpath")
        if not isinstance(dataset, str) or not DATASET_RE.match(dataset):
            msg = "dataset must match ^[a-z0-9][a-z0-9_-]{0,62}$"
            raise ImportRefusedError(msg)
        problem = path_problem(relpath)
        if problem is not None or not isinstance(relpath, str):
            raise ImportRefusedError(problem or "relpath must be a string")
        ancillary = event.get("ancillary") is True
        cap = ANCILLARY_MAX_SIZE if ancillary else MAX_SIZE
        size = event.get("size")
        if isinstance(size, bool) or not isinstance(size, int) or not 1 <= size <= cap:
            msg = f"size must be an integer from 1 B to {cap} B"
            raise ImportRefusedError(msg)
        source = {str(k): str(v) for k, v in dict(event.get("source") or {}).items()}
        if "version" not in source:
            msg = "source.version is required (it keys the session id)"
            raise ImportRefusedError(msg)
        if mode == MODE_S3 and not {"bucket", "key"} <= set(source):
            msg = "mode s3 needs source.bucket and source.key"
            raise ImportRefusedError(msg)
        if mode == MODE_HTTPS and not source.get("url", "").startswith("https://"):
            msg = "mode https needs an https:// source.url"
            raise ImportRefusedError(msg)
        meta = {
            str(k): str(v) for k, v in dict(event.get("dataset_meta") or {}).items()
        }
        return cls(
            mode=str(mode),
            dataset=dataset,
            relpath=relpath,
            size=size,
            source=source,
            dataset_meta=meta,
            caller=str(event.get("caller") or "unknown"),
            ancillary=ancillary,
        )

    @property
    def digest(self) -> str | None:
        """Return the publisher's base64 SHA-256 when the listing gives one."""
        digest = self.source.get("digest", "")
        prefix = "sha2-256:"
        return (
            b64_sha256(digest.removeprefix(prefix))
            if digest.startswith(prefix)
            else None
        )


class ForwardOnlyBody:
    """A stream botocore can PUT without rewinding (the measured wrapper).

    `tell()` reports the bytes read and `seek` is allowed only to the current
    position; reading more than `limit` bytes raises, so a source longer than its
    listing never reaches S3 in full.
    """

    def __init__(self, raw: IO[bytes], limit: int) -> None:
        """Wrap `raw`; `limit` is the listed size."""
        self._raw = raw
        self._limit = limit
        self._pos = 0

    def seekable(self) -> bool:
        """Return True, as the measured wrapper does; only no-op seeks work."""
        return True

    def tell(self) -> int:
        """Return the number of bytes read so far."""
        return self._pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        """Allow only a no-op seek to the current position."""
        target = offset if whence == io.SEEK_SET else self._pos + offset
        if whence not in {io.SEEK_SET, io.SEEK_CUR} or target != self._pos:
            msg = "forward-only body: seek is allowed only to the current position"
            raise io.UnsupportedOperation(msg)
        return self._pos

    def read(self, size: int = -1) -> bytes:
        """Read up to `size` bytes, refusing a source longer than the listing."""
        chunk = self._raw.read(size if size > 0 else READ_CHUNK)
        self._pos += len(chunk)
        if self._pos > self._limit:
            msg = f"source is longer than its listed size {self._limit} B"
            raise ValueError(msg)
        return chunk


class SameHostRedirect(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only to the same scheme and host; refuse any other."""

    @override
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: http.client.HTTPMessage,
        newurl: str,
    ) -> urllib.request.Request | None:
        """Refuse a redirect that leaves the original https host."""
        old, new = urllib.parse.urlsplit(req.full_url), urllib.parse.urlsplit(newurl)
        if (new.scheme, new.netloc) != (old.scheme, old.netloc):
            refused = (
                f"refused redirect from {old.netloc} to {new.scheme}://{new.netloc}"
            )
            raise ImportRefusedError(refused)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_https(url: str) -> IO[bytes]:
    """Open an anonymous GET of `url` (TLS; redirects only within the same host)."""
    if not url.startswith("https://"):
        msg = f"refused non-TLS source {url}"
        raise ImportRefusedError(msg)
    opener = urllib.request.build_opener(SameHostRedirect)
    response = opener.open(url, timeout=HTTP_TIMEOUT_SECONDS)
    if not isinstance(response, http.client.HTTPResponse):
        msg = f"unexpected response type {type(response).__name__} for {url}"
        raise TypeError(msg)
    return response


class MoveFailedError(Exception):
    """The source could not be moved to landing/; the session becomes INCOMPLETE."""


@dataclass(frozen=True, slots=True)
class ImportDeps:
    """What one import needs; `fetch` opens an anonymous HTTPS source."""

    table: SessionTable
    s3: S3
    stream_s3: S3
    sqs: Sqs
    bucket: str
    commit_queue_url: str
    dataset_owners: owners.Owners
    fetch: Callable[[str], IO[bytes]] = fetch_https


def _exists(deps: ImportDeps, key: str) -> bool:
    try:
        deps.s3.head_object(Bucket=deps.bucket, Key=key)
    except ClientError as err:
        if err.response["Error"]["Code"] not in NOT_FOUND_CODES:
            raise
        return False
    return True


def _move_s3(deps: ImportDeps, ev: ImportEvent, landing_key: str) -> str:
    """CopyObject a same-region source into landing/; return its base64 SHA-256."""
    copied = deps.s3.copy_object(
        Bucket=deps.bucket,
        Key=landing_key,
        CopySource={"Bucket": ev.source["bucket"], "Key": ev.source["key"]},
        ChecksumAlgorithm=SHA256,
        MetadataDirective="COPY",
        TaggingDirective="REPLACE",  # no tags, so no tagging permission
    )
    got = copied["CopyObjectResult"].get("ChecksumSHA256")
    if got is None:
        msg = "CopyObject returned no ChecksumSHA256"
        raise MoveFailedError(msg)
    if ev.digest is not None and got != ev.digest:
        raise MoveFailedError(DIGEST_MISMATCH)
    return str(got)


def _put(deps: ImportDeps, key: str, body: object, size: int, digest: str) -> None:
    try:
        deps.stream_s3.put_object(
            Bucket=deps.bucket,
            Key=key,
            Body=body,
            ContentLength=size,
            ChecksumSHA256=digest,
        )
    except ClientError as err:
        if err.response["Error"]["Code"] == "BadDigest":
            raise MoveFailedError(DIGEST_MISMATCH) from err
        raise


def _move_https(deps: ImportDeps, ev: ImportEvent, landing_key: str) -> str:
    """Stream an HTTPS source into one PutObject; return its base64 SHA-256.

    With a publisher digest the body streams through unbuffered and S3 checks it
    (BadDigest on a mismatch). Without one (ancillary files) the source,
    at most 100 MiB, is spooled while its SHA-256 is computed, then PUT with it.
    """
    with ev_stream(deps, ev) as stream:
        if ev.digest is not None:
            try:
                _put(
                    deps,
                    landing_key,
                    ForwardOnlyBody(stream, ev.size),
                    ev.size,
                    ev.digest,
                )
            except ValueError as err:  # ForwardOnlyBody: longer than listed
                raise MoveFailedError(str(err)) from err
            return ev.digest
        if ev.size > ANCILLARY_MAX_SIZE:
            msg = f"no publisher digest for a file over {ANCILLARY_MAX_SIZE} B"
            raise MoveFailedError(msg)
        hasher = hashlib.sha256()
        with tempfile.SpooledTemporaryFile(max_size=8 * READ_CHUNK) as spool:
            total = 0
            while chunk := stream.read(READ_CHUNK):
                total += len(chunk)
                if total > ev.size:
                    break
                hasher.update(chunk)
                spool.write(chunk)
            if total != ev.size:
                msg = f"source size differs from the listed {ev.size} B"
                raise MoveFailedError(msg)
            spool.seek(0)
            digest = base64.b64encode(hasher.digest()).decode()
            _put(deps, landing_key, spool, ev.size, digest)
        return digest


def ev_stream(deps: ImportDeps, ev: ImportEvent) -> IO[bytes]:
    """Open the event's HTTPS source; a refused redirect fails the move."""
    try:
        return deps.fetch(ev.source["url"])
    except ImportRefusedError as err:
        raise MoveFailedError(str(err)) from err


def _item(ev: ImportEvent, sid: str, now: int) -> dict[str, Any]:
    return {
        "session_id": sid,
        "state": sessions.OPEN,
        "owner_sub": f"import:{ev.caller}",
        "dataset": ev.dataset,
        "relpath": ev.relpath,
        "size": ev.size,
        "landing_key": f"landing/{sid}/{ev.relpath}",
        "raw_key": f"raw/{ev.dataset}/{ev.relpath}",
        "created_at": now,
        "updated_at": now,
        "expires_at": now + sessions.OPEN_TTL_SECONDS,
        "ingest_method": "import",
        "import_mode": ev.mode,
        "dataset_meta": ev.dataset_meta,
        "source": ev.source,
    }


def _create(deps: ImportDeps, item: dict[str, Any]) -> bool:
    """Write the first item; return False when a concurrent delivery already did."""
    try:
        deps.table.ddb.put_item(
            TableName=deps.table.name,
            Item=sessions.marshal(item),
            ConditionExpression="attribute_not_exists(session_id)",
        )
    except ClientError as err:
        if err.response["Error"]["Code"] == sessions.CONDITIONAL_FAILED:
            return False
        raise
    return True


def _from_open(
    deps: ImportDeps, ev: ImportEvent, item: dict[str, Any], now: int
) -> dict[str, str]:
    sid = item["session_id"]
    move = _move_s3 if ev.mode == MODE_S3 else _move_https
    try:
        sha256 = move(deps, ev, item["landing_key"])
    except MoveFailedError as err:
        sessions.transition(
            deps.table,
            sid,
            Transition(
                expect=sessions.OPEN,
                to=sessions.INCOMPLETE,
                now=now,
                set_values={"error": {"reason": str(err)}},
            ),
        )
        return {"outcome": sessions.INCOMPLETE, "session_id": sid, "reason": str(err)}
    token = uuid.uuid4().hex
    if not sessions.transition(
        deps.table,
        sid,
        Transition(
            expect=sessions.OPEN,
            to=sessions.FINALIZING,
            now=now,
            set_values={
                "finalize_token": token,
                sessions.COMMIT_LEASE: now + sessions.COMMIT_LEASE_SECONDS,
                "expected_checksum": {"algorithm": SHA256, "value": sha256},
            },
        ),
    ):
        return {"outcome": "LOST_CLAIM", "session_id": sid}
    queues.send_commit(deps.sqs, deps.commit_queue_url, sid, token)
    return {"outcome": sessions.FINALIZING, "session_id": sid}


def run_import(
    event: dict[str, Any], deps: ImportDeps, *, now: int | None = None
) -> dict[str, str]:
    """Import one file; return `{outcome, session_id?, reason?}`.

    A refusal is an outcome (REFUSED, no session created), not an exception.
    """
    now = int(time.time()) if now is None else now
    try:
        ev = ImportEvent.parse(event)
    except ImportRefusedError as err:
        return {"outcome": REFUSED, "reason": str(err)}
    sid = session_id(ev.dataset, ev.relpath, ev.source["version"])
    item = sessions.get(deps.table, sid)
    if item is None:
        fresh = _item(ev, sid, now)
        if not owners.claim_for_admin(
            deps.dataset_owners, ev.dataset, fresh["owner_sub"], now
        ):
            # A refusal is a session the curator CLI can read (`make import` prints
            # FAIL for INCOMPLETE); no landing object is written.
            fresh["state"] = sessions.INCOMPLETE
            fresh["error"] = {"reason": NOT_YOURS}
            _create(deps, fresh)
            return {"outcome": REFUSED, "session_id": sid, "reason": NOT_YOURS}
        if _exists(deps, fresh["raw_key"]):
            return {"outcome": REFUSED, "reason": "raw key already exists"}
        item = fresh if _create(deps, fresh) else sessions.get(deps.table, sid)
        if item is None:  # pragma: no cover - the conditional write saw an item
            msg = f"session {sid} vanished after a conditional create"
            raise RuntimeError(msg)
    state = item["state"]
    if state == sessions.OPEN:
        return _from_open(deps, ev, item, now)
    if state == sessions.FINALIZING:
        queues.send_commit(deps.sqs, deps.commit_queue_url, sid, item["finalize_token"])
        return {"outcome": "RESENT", "session_id": sid}
    return {"outcome": NOTHING_TO_DO, "session_id": sid, "state": state}


def handler(event: dict[str, Any], _context: object) -> dict[str, str]:
    """Lambda entry point (asynchronous invoke from `make import`)."""
    deps = ImportDeps(
        table=SessionTable(boto3.client("dynamodb"), os.environ["SESSIONS_TABLE"]),
        s3=boto3.client("s3"),
        stream_s3=boto3.client("s3", config=STREAM_CLIENT_CONFIG),
        sqs=boto3.client("sqs"),
        bucket=os.environ["RAW_BUCKET"],
        commit_queue_url=os.environ["COMMIT_QUEUE_URL"],
        dataset_owners=owners.from_env(),
    )
    return run_import(event, deps)
