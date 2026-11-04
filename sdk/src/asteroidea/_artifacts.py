"""Saved results (artifacts): save, list, open, download and withdraw.

Every researcher lists, opens and downloads any author's finished result;
only its author withdraws it. Inside a `run()`, a result opened or downloaded is
recorded as an `artifact` input of that run.
"""

import importlib.metadata
import mimetypes
import urllib.parse
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from asteroidea import _crc, _run
from asteroidea._auth import default_auth
from asteroidea._client import PlatformClient, json_of, refusal
from asteroidea._http import Downloader, Transport, download, send
from asteroidea._upload import put_file
from asteroidea.errors import (
    ArtifactNotFoundError,
    ArtifactRefusedError,
    PlatformUnreachableError,
    UploadIncompleteError,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

    from asteroidea._auth import Auth
    from asteroidea._query import FileRef

_OK: Final = 200
_CREATED: Final = 201
_BAD_REQUEST: Final = 400
_NOT_FOUND: Final = 404
_CONFLICT: Final = 409
_COMMITTED: Final = "COMMITTED"
_WITHDRAWN: Final = "WITHDRAWN"
# The states any researcher can open or download.
_READABLE: Final = frozenset({_COMMITTED, _WITHDRAWN})
SCOPES: Final = ("mine", "all")
DEFAULT_CONTENT_TYPE: Final = "application/octet-stream"
DISTRIBUTION: Final = "asteroidea"


def sdk_version() -> str | None:
    """Return the installed SDK's version, or None when it is not installed."""
    try:
        return importlib.metadata.version(DISTRIBUTION)
    except importlib.metadata.PackageNotFoundError:
        return None


def content_type(path: Path) -> str:
    """Return the media type guessed from the file name, else octet-stream."""
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or DEFAULT_CONTENT_TYPE


@dataclass(frozen=True)
class ArtifactResult:
    """A saved result."""

    artifact_id: str
    name: str
    size: int
    snapshot_id: int | None


@dataclass(frozen=True)
class ArtifactInfo:
    """One saved result in the list; `state` is COMMITTED once the file is saved.

    `snapshot_id` is None for a version 2 record saved without one (its table
    snapshots are in the record's inputs). `author_email` is set on the rows of
    `scope="all"` and of an `input_id` listing, and None on your own listing.
    """

    artifact_id: str
    name: str
    size: int
    content_type: str
    snapshot_id: int | None
    state: str
    created_at: int
    finalized_at: int | None
    author_email: str | None = None


@dataclass(frozen=True)
class ArtifactRecord:
    """One opened result: its summary fields and the whole saved record.

    `record` is the provenance JSON as the platform stores it (`schema`, `run`,
    `inputs` and `sources` for a version 2 record; no `schema` for version 1).
    `state` is COMMITTED or WITHDRAWN.
    """

    artifact_id: str
    name: str
    state: str
    author_email: str | None
    note: str | None
    record: Mapping[str, Any]


def _expect(status: int, expected: int) -> None:
    """Raise `PlatformUnreachableError` when `status` is not the expected one."""
    if status != expected:
        raise PlatformUnreachableError


def _info(row: dict[str, Any]) -> ArtifactInfo:
    """Return the `ArtifactInfo` of one API artifact record."""
    finalized = row.get("finalized_at")
    snapshot = row.get("snapshot_id")
    email = row.get("author_email")
    return ArtifactInfo(
        artifact_id=str(row["artifact_id"]),
        name=str(row["name"]),
        size=int(row["size"]),
        content_type=str(row["content_type"]),
        snapshot_id=None if snapshot is None else int(snapshot),
        state=str(row["state"]),
        created_at=int(row["created_at"]),
        finalized_at=None if finalized is None else int(finalized),
        author_email=None if email is None else str(email),
    )


def _quoted(artifact_id: str) -> str:
    """Return the artifact id percent-encoded for use in a route."""
    return urllib.parse.quote(artifact_id, safe="")


class Artifacts:
    """Saved results of one signed-in researcher.

    `transport` and `downloader` are seams for tests.
    """

    def __init__(
        self,
        auth: Auth,
        transport: Transport = send,
        downloader: Downloader = download,
    ) -> None:
        """Bind the sign-in and the seams."""
        self.auth = auth
        self.transport = transport
        self.downloader = downloader

    def _api(self, token: str | None = None) -> PlatformClient:
        """Return a `PlatformClient` signed with `token`, else the access token."""
        return PlatformClient(
            self.auth.config.api_url,
            token or self.auth.tokens().access_token,
            self.transport,
        )

    def save(
        self,
        path: Path,
        *,
        sources: Sequence[FileRef],
        snapshot_id: int,
        note: str | None = None,
    ) -> ArtifactResult:
        """Save the file at `path` as a result every researcher can read.

        `sources` are the catalog files it was computed from and `snapshot_id`
        the catalog snapshot they were read at. Raises `ArtifactRefusedError`
        when the platform refuses the request and `UploadIncompleteError` when
        the file did not arrive intact. A version 1 body, sent with the access
        token.
        """
        record = {
            "snapshot_id": snapshot_id,
            "sources": [
                {"asset_id": ref.asset_id, "s3_version_id": ref.s3_version_id}
                for ref in sources
            ],
            "note": note,
        }
        return self._send(path, record, create_api=self._api())

    def save_record(
        self, path: Path, record: Mapping[str, Any], note: str | None = None
    ) -> ArtifactResult:
        """Save the file at `path` with a version 2 record (`schema`, `run`, inputs).

        The create goes with the ID token as the bearer, so the platform reads
        the author's email from it; the finalize goes with the access token.
        Raises `IdTokenRequiredError` when the platform still finds no email.
        """
        create_api = self._api(self.auth.tokens().id_token)
        return self._send(path, {**record, "note": note}, create_api=create_api)

    def _send(
        self, path: Path, record: Mapping[str, Any], *, create_api: PlatformClient
    ) -> ArtifactResult:
        """Create, PUT and finalize one artifact file; return its result.

        Raises `PlatformUnreachableError` on an unexpected status and
        `UploadIncompleteError` when the file did not arrive intact.
        """
        size = path.stat().st_size
        checksum = _crc.crc64nvme(path)
        body = {
            "name": path.name,
            "size": size,
            "crc64nvme": checksum,
            "content_type": content_type(path),
            "sdk_version": sdk_version(),
            **record,
        }
        created = create_api.call("POST", "/artifacts", body)
        if created.status == _BAD_REQUEST:
            msg = (
                f"The platform refused the result ({refusal(created)}); "
                "fix it and try again."
            )
            raise ArtifactRefusedError(msg)
        _expect(created.status, _CREATED)
        made = json_of(created)
        artifact_id = str(made["artifact_id"])
        put_file(self.transport, str(made["url"]), path, checksum)
        done = self._api().call("POST", f"/artifacts/{artifact_id}/finalize")
        if done.status == _CONFLICT:
            raise UploadIncompleteError
        if done.status == _NOT_FOUND:
            raise ArtifactNotFoundError
        _expect(done.status, _OK)
        snapshot = record.get("snapshot_id")
        return ArtifactResult(
            artifact_id, path.name, size, None if snapshot is None else int(snapshot)
        )

    def listing(
        self, scope: str = "mine", input_id: str | None = None
    ) -> list[ArtifactInfo]:
        """Return saved results, newest first (at most 100).

        `scope="mine"` (the default) is your own results of every state; `"all"`
        is the newest finished results of every author. `input_id` keeps only the
        results made from that file (asset id) or result (artifact id): yours, or
        every author's finished ones with `"all"`. Raises `ArtifactRefusedError`
        with the platform's reason for a scope other than these two.
        """
        query: dict[str, str] = {}
        if scope != "mine":
            query["scope"] = scope
        if input_id is not None:
            query["input"] = input_id
        route = "/artifacts"
        if query:
            route += "?" + urllib.parse.urlencode(query)
        reply = self._api().call("GET", route)
        if reply.status == _BAD_REQUEST:
            msg = f"The platform refused the listing ({refusal(reply)}); fix it."
            raise ArtifactRefusedError(msg)
        _expect(reply.status, _OK)
        return [_info(row) for row in json_of(reply).get("artifacts", [])]

    def _get(self, artifact_id: str) -> tuple[dict[str, Any], str]:
        """Return the artifact record and its download URL.

        Raises `ArtifactNotFoundError` when the id is unknown or the
        artifact is not in a readable state.
        """
        reply = self._api().call("GET", f"/artifacts/{_quoted(artifact_id)}")
        if reply.status == _NOT_FOUND:
            raise ArtifactNotFoundError
        _expect(reply.status, _OK)
        found = json_of(reply)
        record = found.get("record")
        if not isinstance(record, dict) or record.get("state") not in _READABLE:
            raise ArtifactNotFoundError
        return record, str(found.get("url", ""))

    def open(self, artifact_id: str) -> ArtifactRecord:
        """Return any author's finished result (COMMITTED or WITHDRAWN) by id.

        Inside a `run()`, the result is recorded as an `artifact` input. Raises
        `ArtifactNotFoundError` when no finished result has this id.
        """
        record, _ = self._get(artifact_id)
        _run.record_artifact(str(record["artifact_id"]))
        note = record.get("note")
        email = record.get("author_email")
        return ArtifactRecord(
            artifact_id=str(record["artifact_id"]),
            name=str(record["name"]),
            state=str(record["state"]),
            author_email=None if email is None else str(email),
            note=None if note is None else str(note),
            record=record,
        )

    def download(self, artifact_id: str, dest: Path) -> Path:
        """Save the file of any author's finished result to `dest`; return the path.

        `dest` is the file to write, or a folder, in which case the result's
        own name is used. Inside a `run()`, the result is recorded as an
        `artifact` input. Raises `ArtifactNotFoundError` when no finished result
        has this id.
        """
        record, url = self._get(artifact_id)
        target = dest / str(record["name"]) if dest.is_dir() else dest
        _expect(self.downloader(url, target), _OK)
        _run.record_artifact(str(record["artifact_id"]))
        return target

    def withdraw(self, artifact_id: str) -> str:
        """Withdraw your finished result: it stays readable, marked WITHDRAWN.

        Returns the new state. Raises `NotTheAuthorError` for another author's
        result, `ArtifactNotFoundError` when no result has this id and
        `ArtifactRefusedError` with the platform's reason for an unfinished one.
        """
        reply = self._api().call("POST", f"/artifacts/{_quoted(artifact_id)}/withdraw")
        if reply.status == _NOT_FOUND:
            raise ArtifactNotFoundError
        if reply.status == _CONFLICT:
            msg = f"The platform did not withdraw the result ({refusal(reply)})."
            raise ArtifactRefusedError(msg)
        _expect(reply.status, _OK)
        return str(json_of(reply).get("state", _WITHDRAWN))


def save_artifact(
    path: Path, *, sources: Sequence[FileRef], snapshot_id: int, note: str | None = None
) -> ArtifactResult:
    """Save `path` as a result every researcher can read (see `Artifacts.save`)."""
    return Artifacts(default_auth()).save(
        path, sources=sources, snapshot_id=snapshot_id, note=note
    )


def artifacts(scope: str = "mine", input_id: str | None = None) -> list[ArtifactInfo]:
    """Return saved results, newest first (see `Artifacts.listing`)."""
    return Artifacts(default_auth()).listing(scope, input_id)


def open_artifact(artifact_id: str) -> ArtifactRecord:
    """Return any author's finished result by id (see `Artifacts.open`)."""
    return Artifacts(default_auth()).open(artifact_id)


def download_artifact(artifact_id: str, dest: Path) -> Path:
    """Save a finished result's file to `dest` (a file or a folder)."""
    return Artifacts(default_auth()).download(artifact_id, dest)


def withdraw_artifact(artifact_id: str) -> str:
    """Withdraw your finished result (see `Artifacts.withdraw`)."""
    return Artifacts(default_auth()).withdraw(artifact_id)
