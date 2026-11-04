"""A run: what one analysis read, saved with its result as a version 2 record.

`with run(name, params) as r:` opens a run for the current context. While it is
open the SDK records, without being asked:

- every `sql()` table, as a `table_snapshot` input whose version is the snapshot
  id in the table's current Iceberg metadata file (Glue `metadata_location`, then
  one object read of that JSON per location; the same method);
- every `open_file`, `open_nwb` and `open_image` asset, and every file that
  `load_session` or a `Session` reads (role `session`), as an `asset` input
  whose version is the catalogued object version that was opened;
- every `query()` file, as a source (`asset_id`, `s3_version_id`), and its
  snapshot, as a `table_snapshot` input of `asteroidea.assets`;
- every result opened or downloaded (`record_artifact`), as an `artifact` input;
- every `Session.rates` and `Session.tuning` call, as a `defaults` input
  (`analysis_defaults/<dataset>`) and a `recipe` input (`recipes/<dataset>`), each
  with its version; `run.defaults_version` is set when the run used one version.

`r.save_artifact(path)` sends that record (`schema` `asteroidea.provenance/2`)
with the ID token as the bearer. A run inside an open run is refused
(`NestedRunError`): one run is one analysis. The record lives in a context
variable, so a thread started inside the run records nothing.
"""

import contextvars
import gzip
import hashlib
import json
import platform
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Protocol, Self

from botocore.exceptions import BotoCoreError, ClientError

# A module import: `_artifacts` imports this module back (`record_artifact`).
from asteroidea import _artifacts
from asteroidea._auth import default_auth
from asteroidea._http import Transport, send
from asteroidea.errors import (
    ArtifactRefusedError,
    NestedRunError,
    PlatformUnreachableError,
    QueryError,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from types import TracebackType

    from asteroidea._artifacts import ArtifactResult
    from asteroidea._auth import Auth
    from asteroidea._query import QueryResult

SCHEMA: Final = "asteroidea.provenance/2"
DATABASE: Final = "asteroidea"
QUERY_TABLE: Final = "asteroidea.assets"
GZIP_MAGIC: Final = b"\x1f\x8b"
# Iceberg writes -1 (or leaves the field out) for a table with no snapshot yet.
NO_SNAPSHOT: Final = -1
MAX_PARAMETERS_BYTES: Final = 64 * 1024
ANALYSIS_FILE: Final = Path(__file__).with_name("analysis.py")

_active: contextvars.ContextVar[Run | None] = contextvars.ContextVar(
    "asteroidea_run", default=None
)


class ObjectReader(Protocol):
    """The one object-store call a run makes."""

    def get_object(self, **kwargs: str) -> Mapping[str, Any]:
        """Return the object at `Bucket` and `Key`; its `Body` has `read()`."""
        ...


def snapshot_of(s3: ObjectReader, name: str, location: str) -> int | None:
    """Return the current snapshot id in the Iceberg metadata file at `location`.

    None for a table with no snapshot yet. Raises `QueryError` when the file
    cannot be read or parsed and `PlatformUnreachableError` on a network failure.
    """
    bucket, _, key = location.removeprefix("s3://").partition("/")
    if not (bucket and key):
        msg = f"The catalog table {name} has no metadata file; ask your administrator."
        raise QueryError(msg)
    try:
        data = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    except ClientError as err:
        msg = f"The snapshot of catalog table {name} could not be read: {err}"
        raise QueryError(msg) from err
    except BotoCoreError as err:
        raise PlatformUnreachableError from err
    try:
        if data[:2] == GZIP_MAGIC:
            data = gzip.decompress(data)
        current = json.loads(data).get("current-snapshot-id")
    except (OSError, ValueError, AttributeError) as err:
        msg = f"The metadata of catalog table {name} could not be parsed: {err}"
        raise QueryError(msg) from err
    if current is None or current == NO_SNAPSHOT:
        return None
    return int(current)


def _analysis_sha256() -> str | None:
    """Return the SHA-256 of the analysis module, or `None` if unreadable."""
    try:
        return hashlib.sha256(ANALYSIS_FILE.read_bytes()).hexdigest()
    except OSError:
        return None


class Run:
    """One analysis: its name, parameters and every input it read.

    `auth` and `transport` are used only by `save_artifact`; `auth` defaults to
    the saved sign-in then.
    """

    def __init__(
        self,
        name: str,
        params: Mapping[str, Any] | None = None,
        *,
        auth: Auth | None = None,
        transport: Transport = send,
    ) -> None:
        """Check the parameters are a JSON object of at most 64 KiB."""
        parameters = dict(params or {})
        try:
            encoded = json.dumps(parameters, separators=(",", ":"), ensure_ascii=False)
        except (TypeError, ValueError) as err:
            msg = f"run parameters must be JSON values ({err}); fix them and try again."
            raise ArtifactRefusedError(msg) from err
        if len(encoded.encode()) > MAX_PARAMETERS_BYTES:
            msg = "run parameters must be at most 64 KiB of JSON; try again with fewer."
            raise ArtifactRefusedError(msg)
        self.name = name
        self.parameters = parameters
        self.run_id = str(uuid.uuid4())
        self.auth = auth
        self.transport = transport
        self.sources: dict[tuple[str, str], dict[str, str]] = {}
        self.inputs: dict[tuple[str, str, object], dict[str, Any]] = {}
        self.query_snapshots: list[int] = []
        self.defaults_versions: list[str] = []
        self._snapshots: dict[str, int | None] = {}
        self._token: contextvars.Token[Run | None] | None = None

    def __enter__(self) -> Self:
        """Make this the open run; refuse when another run is open."""
        if _active.get() is not None:
            raise NestedRunError
        self._token = _active.set(self)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Close the run; what it recorded stays readable and savable."""
        if self._token is not None:
            _active.reset(self._token)
            self._token = None

    def add_input(
        self, kind: str, input_id: str, version: object, role: str | None = None
    ) -> None:
        """Record one input once (by kind, id and version), keeping the first role."""
        key = (kind, input_id, version)
        if key not in self.inputs:
            entry: dict[str, Any] = {"kind": kind, "id": input_id}
            if version is not None:
                entry["version"] = version
            if role is not None:
                entry["role"] = role
            self.inputs[key] = entry

    def add_tables(
        self, s3: Callable[[], ObjectReader], locations: Mapping[str, str]
    ) -> None:
        """Record each table's snapshot; one object read per new metadata location."""
        if not locations:
            return
        client: ObjectReader | None = None
        for name, location in locations.items():
            if location not in self._snapshots:
                client = client or s3()
                self._snapshots[location] = snapshot_of(client, name, location)
            snapshot = self._snapshots[location]
            if snapshot is not None:
                self.add_input("table_snapshot", f"{DATABASE}.{name}", snapshot, "sql")

    def add_settings(
        self, dataset: str, defaults_version: str, recipe_version: str, role: str
    ) -> None:
        """Record the analysis defaults and event recipe versions a statistic used."""
        self.add_input(
            "defaults", f"analysis_defaults/{dataset}", defaults_version, role
        )
        self.add_input("recipe", f"recipes/{dataset}", recipe_version, role)
        if defaults_version not in self.defaults_versions:
            self.defaults_versions.append(defaults_version)

    def add_query(self, result: QueryResult) -> None:
        """Record a `query()` result's files as sources and its snapshot."""
        for ref in result.files:
            self.sources.setdefault(
                (ref.asset_id, ref.s3_version_id),
                {"asset_id": ref.asset_id, "s3_version_id": ref.s3_version_id},
            )
        if result.snapshot_id is not None:
            self.query_snapshots.append(result.snapshot_id)
            self.add_input("table_snapshot", QUERY_TABLE, result.snapshot_id, "query")

    def record(self) -> dict[str, Any]:
        """Return the version 2 fields: `schema`, sources, inputs, run, snapshot_id.

        `snapshot_id` is present only when every `query()` in the run read one and
        the same snapshot; each snapshot is in `inputs` either way.
        """
        body: dict[str, Any] = {
            "schema": SCHEMA,
            "sources": list(self.sources.values()),
            "inputs": list(self.inputs.values()),
            "run": {
                "run_id": self.run_id,
                "function": self.name,
                "parameters": self.parameters,
                "sdk_version": _artifacts.sdk_version(),
                "analysis_sha256": _analysis_sha256(),
                "environment": {"python": platform.python_version()},
            },
        }
        if len(self.defaults_versions) == 1:
            body["run"]["defaults_version"] = self.defaults_versions[0]
        if len(set(self.query_snapshots)) == 1:
            body["snapshot_id"] = self.query_snapshots[0]
        return body

    def save_artifact(
        self, path: Path | str, note: str | None = None
    ) -> ArtifactResult:
        """Save the file at `path` with this run's record, readable by every researcher.

        Raises `ArtifactRefusedError` when the run read nothing to record.
        """
        record = self.record()
        if not record["sources"] and not record["inputs"]:
            msg = (
                "This run read nothing to record; read the data inside the run "
                "with query(), sql(), open_file() or open_nwb() and try again."
            )
            raise ArtifactRefusedError(msg)
        auth = self.auth if self.auth is not None else default_auth()
        client = _artifacts.Artifacts(auth, self.transport)
        return client.save_record(Path(path), record, note)


def active() -> Run | None:
    """Return the open run of this context, or None."""
    return _active.get()


def record_tables(s3: Callable[[], ObjectReader], locations: Mapping[str, str]) -> None:
    """Record `sql()` table snapshots in the open run; nothing when none is open."""
    current = _active.get()
    if current is not None:
        current.add_tables(s3, locations)


def record_asset(asset_id: str, version_id: str, role: str) -> None:
    """Record an opened asset version in the open run; nothing when none is open."""
    current = _active.get()
    if current is not None:
        current.add_input("asset", asset_id, version_id, role)


def record_query(result: QueryResult) -> None:
    """Record a `query()` result in the open run; nothing when none is open."""
    current = _active.get()
    if current is not None:
        current.add_query(result)


def record_artifact(artifact_id: str) -> None:
    """Record an opened result in the open run; nothing when none is open."""
    current = _active.get()
    if current is not None:
        current.add_input("artifact", artifact_id, None, "opened")


def record_settings(
    dataset: str, defaults_version: str, recipe_version: str, role: str
) -> None:
    """Record a statistic's defaults and recipe versions in the open run, if any."""
    current = _active.get()
    if current is not None:
        current.add_settings(dataset, defaults_version, recipe_version, role)
