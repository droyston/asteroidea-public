"""Upload one file: create a session, PUT once, finalize, poll."""

import fnmatch
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

from asteroidea import _crc
from asteroidea._client import PlatformClient, json_of, refusal
from asteroidea._http import Reply, Transport, send
from asteroidea.errors import (
    DatasetNotYoursError,
    FinalizeTimeoutError,
    PlatformUnreachableError,
    UploadIncompleteError,
    UploadRefusedError,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from pathlib import Path

    from asteroidea._auth import Auth

VISIBLE_WITHIN: Final = 60.0  # seconds from the finalize 202
POLL_EVERY: Final = 1.0
_OK: Final = 200
_CREATED: Final = 201
_ACCEPTED: Final = 202
_BAD_REQUEST: Final = 400
_GONE: Final = 410
_CONFLICT: Final = 409
_COMMITTED: Final = "COMMITTED"
_INCOMPLETE: Final = "INCOMPLETE"
SANDBOX: Final = "sandbox"  # the one `dataset_meta.tier` a caller may send


@dataclass(frozen=True)
class UploadResult:
    """A committed upload."""

    session_id: str
    dataset: str
    path: str
    asset_id: str
    snapshot_id: int


@dataclass(frozen=True)
class DatasetUploadResult:
    """The outcome of `upload_dataset`; `failed` is (relpath, the error's message)."""

    dataset: str
    upload_id: str
    committed: list[UploadResult]
    failed: list[tuple[str, str]]


def sandbox_batch(batch: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Return `batch` with `dataset_meta.tier` "sandbox" added; `batch` is not changed.

    `Uploader.open_session(..., batch=sandbox_batch())` opens a sandbox session.
    """
    given = dict(batch or {})
    return {**given, "dataset_meta": {**given.get("dataset_meta", {}), "tier": SANDBOX}}


def relpath_of(path: Path) -> str:
    """Return the path inside the dataset.

    From the first `sub-*` folder on when there is one, else the file name.
    """
    parts = path.parts[:-1]
    for index, part in enumerate(parts):
        if part.startswith("sub-"):
            return "/".join((*parts[index:], path.name))
    return path.name


def _create(
    api: PlatformClient, body: Mapping[str, Any], dataset: str, relpath: str
) -> Reply:
    """Create an upload session and return the reply.

    Raises `UploadRefusedError` on a 409 (the file exists) or a 400, and
    `PlatformUnreachableError` on any other unexpected status.
    """
    reply = api.call("POST", "/sessions", body)
    if reply.status == _CONFLICT:
        msg = (
            f"A file already exists at {relpath} in dataset {dataset}; "
            "upload it under another dataset name."
        )
        raise UploadRefusedError(msg)
    if reply.status == _BAD_REQUEST:
        msg = f"The platform refused the upload ({refusal(reply)}); fix it, try again."
        raise UploadRefusedError(msg)
    if reply.status != _CREATED:
        raise PlatformUnreachableError
    return reply


def put_file(transport: Transport, url: str, path: Path, checksum: str) -> None:
    """PUT the file once with its checksum header.

    Raises `UploadIncompleteError` when the store refuses it.
    """
    reply = transport("PUT", url, {_crc.HEADER: checksum}, path)
    if reply.status != _OK:
        raise UploadIncompleteError


def _finalize(api: PlatformClient, session_id: str) -> Reply:
    """Ask the platform to commit a session and return the reply.

    Raises `UploadIncompleteError` on a 409, `UploadRefusedError` on a 410
    (the session expired) and `PlatformUnreachableError` on any other
    unexpected status.
    """
    reply = api.call("POST", f"/sessions/{session_id}/finalize")
    if reply.status == _CONFLICT:
        raise UploadIncompleteError
    if reply.status == _GONE:
        msg = "This upload session has expired; upload the file again."
        raise UploadRefusedError(msg)
    if reply.status not in {_OK, _ACCEPTED}:
        raise PlatformUnreachableError
    return reply


@dataclass(frozen=True)
class Session:
    """An open upload session: where to PUT and what the checksum was."""

    session_id: str
    url: str = field(repr=False)
    dataset: str
    relpath: str
    checksum: str


class Uploader:
    """Uploads files for one signed-in researcher.

    `transport`, `clock` and `sleep` are seams for tests. The step methods are
    public so that tests can drive a session part of the way.
    """

    def __init__(
        self,
        auth: Auth,
        transport: Transport = send,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Bind the sign-in and the seams."""
        self.auth = auth
        self.transport = transport
        self.clock = clock
        self.sleep = sleep

    def _api(self) -> PlatformClient:
        """Return a client signed with the current access token."""
        return PlatformClient(
            self.auth.config.api_url, self.auth.tokens().access_token, self.transport
        )

    def open_session(
        self,
        path: Path,
        dataset: str,
        relpath: str | None = None,
        checksum: str | None = None,
        batch: Mapping[str, Any] | None = None,
    ) -> Session:
        """Create a session for `path`. `checksum` replaces the computed CRC64NVME.

        `batch` adds body fields: `upload_id` and `dataset_meta`. A sandbox session
        passes `sandbox_batch(batch)`.
        """
        rel = relpath or relpath_of(path)
        crc = checksum or _crc.crc64nvme(path)
        body = {
            "dataset": dataset,
            "path": rel,
            "size": path.stat().st_size,
            "crc64nvme": crc,
            **(batch or {}),
        }
        created = json_of(_create(self._api(), body, dataset, rel))
        return Session(
            str(created["session_id"]), str(created["url"]), dataset, rel, crc
        )

    def send_file(self, session: Session, path: Path) -> None:
        """PUT the file once. Raises `UploadIncompleteError` when it is refused."""
        put_file(self.transport, session.url, path, session.checksum)

    def finalize(self, session_id: str) -> Reply:
        """Ask the platform to commit; the reply is 202, or 200 when already done."""
        return _finalize(self._api(), session_id)

    def state(self, session_id: str) -> dict[str, Any]:
        """Return the session's state record."""
        return json_of(self._api().call("GET", f"/sessions/{session_id}"))

    def check(self, session: Session) -> UploadResult | None:
        """Poll once: the result when committed, `None` while pending.

        Raises `UploadIncompleteError`, with the recorded class and message when the
        platform recorded a commit error, when the file did not arrive intact.
        """
        state = self.state(session.session_id)
        if state.get("state") == _COMMITTED:
            return _result(session, state)
        recorded = state.get("commit_error")
        if isinstance(recorded, dict):
            msg = (
                f"The file did not arrive intact ({recorded.get('class', 'error')}: "
                f"{recorded.get('message', 'no message')}); upload again."
            )
            raise UploadIncompleteError(msg)
        if state.get("state") == _INCOMPLETE:
            raise UploadIncompleteError
        return None

    def wait(self, session: Session) -> UploadResult:
        """Poll until the row is visible, at most 60 s from now (the 202)."""
        deadline = self.clock() + VISIBLE_WITHIN
        while True:
            done = self.check(session)
            if done is not None:
                return done
            if self.clock() >= deadline:
                raise FinalizeTimeoutError
            self.sleep(POLL_EVERY)

    def upload(
        self,
        path: Path,
        dataset: str,
        relpath: str | None = None,
        checksum: str | None = None,
        *,
        sandbox: bool = False,
    ) -> UploadResult:
        """Upload `path` into `dataset`; return once its row is visible.

        `sandbox` marks the dataset sandbox (a test dataset the data home folds away).

        Raises `FinalizeTimeoutError` when the row is not visible 60 s after the
        finalize call was accepted, and `UploadIncompleteError` when the file never
        arrived intact.
        """
        batch = sandbox_batch() if sandbox else None
        session = self.open_session(path, dataset, relpath, checksum, batch)
        self.send_file(session, path)
        accepted = self.finalize(session.session_id)
        if accepted.status == _OK:
            return _result(session, json_of(accepted))
        return self.wait(session)


def _result(session: Session, state: Mapping[str, Any]) -> UploadResult:
    """Return the `UploadResult` of a committed session state."""
    return UploadResult(
        session_id=session.session_id,
        dataset=session.dataset,
        path=session.relpath,
        asset_id=str(state["asset_id"]),
        snapshot_id=int(state["snapshot_id"]),
    )


def _description(root: Path) -> dict[str, str]:
    """Return the `title` and `license` of root's `dataset_description.json`, if any."""
    path = root / "dataset_description.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text())
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    found = {"title": data.get("Name"), "license": data.get("License")}
    return {key: value for key, value in found.items() if isinstance(value, str)}


def files_of(root: Path, include: Sequence[str] | None) -> list[tuple[str, Path]]:
    """Return `(relative path, path)` for each file under `root`, sorted.

    With `include`, keep only the paths that match one of its globs.
    """
    found = sorted(
        (path.relative_to(root).as_posix(), path)
        for path in root.rglob("*")
        if path.is_file()
    )
    if include is None:
        return found
    return [
        (rel, path)
        for rel, path in found
        if any(fnmatch.fnmatchcase(rel, pattern) for pattern in include)
    ]


def _send_all(
    uploader: Uploader,
    files: Sequence[tuple[str, Path]],
    dataset: str,
    batch: Mapping[str, Any],
) -> tuple[list[Session], list[UploadResult], list[tuple[str, Exception]]]:
    """Create, PUT and finalize each file in turn; return pending, done and failed."""
    pending: list[Session] = []
    done: list[UploadResult] = []
    failed: list[tuple[str, Exception]] = []
    for rel, path in files:
        try:
            session = uploader.open_session(path, dataset, rel, batch=batch)
            uploader.send_file(session, path)
            accepted = uploader.finalize(session.session_id)
        except DatasetNotYoursError:
            raise
        except (UploadRefusedError, UploadIncompleteError) as err:
            failed.append((rel, err))
            continue
        if accepted.status == _OK:
            done.append(_result(session, json_of(accepted)))
        else:
            pending.append(session)
    return pending, done, failed


def _poll_all(
    uploader: Uploader, pending: list[Session], limit: float
) -> tuple[list[UploadResult], list[tuple[str, Exception]]]:
    """Poll every session until it is committed or failed, or `limit` seconds pass."""
    deadline = uploader.clock() + limit
    done: list[UploadResult] = []
    failed: list[tuple[str, Exception]] = []
    while pending:
        for session in list(pending):
            try:
                result = uploader.check(session)
            except UploadIncompleteError as err:
                pending.remove(session)
                failed.append((session.relpath, err))
                continue
            if result is not None:
                pending.remove(session)
                done.append(result)
        if pending and uploader.clock() >= deadline:
            failed.extend((s.relpath, FinalizeTimeoutError()) for s in pending)
            break
        if pending:
            uploader.sleep(POLL_EVERY)
    return done, failed


def upload_dataset(
    uploader: Uploader,
    root: Path,
    dataset: str,
    include: Sequence[str] | None = None,
    *,
    sandbox: bool = False,
) -> DatasetUploadResult:
    """Upload every file under `root` (those matching `include`, when given).

    `sandbox` marks the dataset sandbox on every file's session.

    Each file keeps its path relative to `root`. One `upload_id` groups the batch
    and `dataset_description.json` (when present) supplies the dataset's title and
    licence. Files go one at a time; then all are polled until committed, within
    60 s plus 10 s per file. A failure is listed in `failed`; the call raises only
    when every file failed (the last error), no file matched, or the dataset name
    belongs to another researcher (`DatasetNotYoursError`, at the first file).
    """
    files = files_of(root, include)
    if not files:
        msg = "No file to upload under that folder; check the path and the filters."
        raise UploadRefusedError(msg)
    upload_id = str(uuid.uuid4())
    batch: dict[str, Any] = {"upload_id": upload_id}
    meta = _description(root)
    if sandbox:
        meta["tier"] = SANDBOX
    if meta:
        batch["dataset_meta"] = meta
    pending, committed, failed = _send_all(uploader, files, dataset, batch)
    polled, late = _poll_all(uploader, pending, VISIBLE_WITHIN + 10 * len(files))
    committed += polled
    failed += late
    if not committed:
        raise failed[-1][1]
    return DatasetUploadResult(
        dataset, upload_id, committed, [(rel, str(err)) for rel, err in failed]
    )
