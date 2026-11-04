"""SQL over the catalog: DuckDB views on each table's Iceberg metadata."""

import re
import threading
from typing import TYPE_CHECKING, Final, Protocol, cast

import duckdb
from botocore.exceptions import BotoCoreError, ClientError

from asteroidea import _creds, _run
from asteroidea.errors import PlatformUnreachableError, QueryError

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    import pyarrow as pa

    from asteroidea._auth import Auth

DATABASE: Final = "asteroidea"
# A copy of infra's schema.py TABLE_NAMES (the SDK must not import infra);
# tests/test_workspace.py holds the two equal.
TABLE_NAMES: Final = (
    "assets",
    "datasets",
    "subjects",
    "subject_links",
    "sessions",
    "asset_headers",
    "series",
    "electrodes",
    "units",
    "intervals",
    "unit_spikes",
    "events",
    "studies",
    "study_datasets",
)
# The convenience views and the tables they read.
COMPOSITE_INPUTS: Final = {
    "subject_index": ("subjects", "subject_links"),
    "files": ("assets", "asset_headers"),
}
_WORD: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_SUBJECT_INDEX: Final = (
    "SELECT s.*, coalesce(l.subject_key, s.dataset || '/' || s.subject) AS subject_key "
    "FROM subjects s LEFT JOIN subject_links l "
    "ON s.dataset = l.dataset AND s.subject = l.subject"
)
_FILES: Final = (
    "SELECT a.*, h.* EXCLUDE (asset_id, dataset, format) "
    "FROM assets a LEFT JOIN asset_headers h ON a.asset_id = h.asset_id"
)


class GlueClient(Protocol):
    """The one catalog call this module makes."""

    def get_table(self, **kwargs: str) -> Mapping[str, object]:
        """Return the table description for `DatabaseName` and `Name`."""
        ...


def iceberg_scan(
    con: duckdb.DuckDBPyConnection, location: str
) -> duckdb.DuckDBPyRelation:
    """Return the Iceberg table whose metadata file is at `location`."""
    return con.sql("SELECT * FROM iceberg_scan(?)", params=[location])


def referenced(query: str, params: Sequence[str | None] | None = None) -> set[str]:
    """Return the known table and view names that `query` or `params` may use.

    Every identifier-like word is compared, case-insensitively, with the known
    names, so a name inside a string literal, a quoted identifier or a comment
    also counts. An over-match only costs time; an under-match would be wrong.
    """
    known = {*TABLE_NAMES, *COMPOSITE_INPUTS}
    text = " ".join([query, *(p for p in params or [] if p is not None)])
    return {word.lower() for word in _WORD.findall(text)} & known


def tables_needed(query: str, params: Sequence[str | None] | None = None) -> list[str]:
    """Return the base tables `query` reads, with those its convenience views read."""
    names = referenced(query, params)
    for view, inputs in COMPOSITE_INPUTS.items():
        if view in names:
            names.update(inputs)
    return [name for name in TABLE_NAMES if name in names]


def metadata_locations(glue: GlueClient, names: Sequence[str]) -> dict[str, str]:
    """Return the current metadata location of each of `names`, one `GetTable` each."""
    found: dict[str, str] = {}
    for name in names:
        try:
            reply = glue.get_table(DatabaseName=DATABASE, Name=name)
        except ClientError as err:
            msg = f"The catalog table {name} could not be read: {err}"
            raise QueryError(msg) from err
        except BotoCoreError as err:
            raise PlatformUnreachableError from err
        table = reply.get("Table")
        parameters = table.get("Parameters") if isinstance(table, dict) else None
        location = (
            parameters.get("metadata_location")
            if isinstance(parameters, dict)
            else None
        )
        if not isinstance(location, str) or not location:
            msg = f"The catalog table {name} has no data yet; ask your administrator."
            raise QueryError(msg)
        found[name] = location
    return found


def load_extensions(con: duckdb.DuckDBPyConnection) -> None:
    """Install and load the Iceberg and S3 extensions (once per process)."""
    con.execute("INSTALL iceberg; LOAD iceberg; INSTALL httpfs; LOAD httpfs;")


def make_secret(
    region: str,
) -> Callable[[duckdb.DuckDBPyConnection, object], None]:
    """Return the step that sets the S3 secret from a `Credentials`."""

    def secret(con: duckdb.DuckDBPyConnection, credential: object) -> None:
        """Create the DuckDB S3 secret from the researcher's credentials."""
        held = cast("_creds.Credentials", credential)
        con.execute(
            "CREATE OR REPLACE SECRET (TYPE S3, KEY_ID ?, SECRET ?, "
            "SESSION_TOKEN ?, REGION ?)",
            [held.access_key_id, held.secret_key, held.session_token, region],
        )

    return secret


class Engine:
    """One DuckDB connection with its views, reused across calls (process cache).

    `setup(con)` runs once, when the engine is made (loading the extensions).
    `secret(con, credential)` runs again whenever the credential changes. A view
    is bound to a table's metadata location and rebound only when that location
    changes, so a call that finds the same location pays no binding cost.
    """

    def __init__(
        self,
        *,
        scan: Callable[[duckdb.DuckDBPyConnection, str], duckdb.DuckDBPyRelation],
        setup: Callable[[duckdb.DuckDBPyConnection], None],
        secret: Callable[[duckdb.DuckDBPyConnection, object], None],
    ) -> None:
        """Open the connection and run `setup` on it."""
        self._scan = scan
        self._secret = secret
        self._lock = threading.Lock()
        self._bound: dict[str, str] = {}
        self._credential: object = None
        try:
            self._con = duckdb.connect()
            setup(self._con)
        except duckdb.Error as err:
            raise QueryError(str(err)) from err

    def run(
        self,
        query: str,
        params: Sequence[str | None] | None,
        locations: Mapping[str, str],
        credential: object,
    ) -> pa.Table:
        """Run `query`; `locations` holds the metadata location of each table needed."""
        with self._lock:
            try:
                if credential != self._credential:
                    self._secret(self._con, credential)
                    self._credential = credential
                for name, location in locations.items():
                    if self._bound.get(name) != location:
                        self._scan(self._con, location).create_view(name)
                        self._bound[name] = location
                wanted = referenced(query, params)
                if "subject_index" in wanted:
                    self._con.execute(
                        "CREATE OR REPLACE VIEW subject_index AS " + _SUBJECT_INDEX
                    )
                if "files" in wanted:
                    self._con.execute("CREATE OR REPLACE VIEW files AS " + _FILES)
                return self._con.execute(query, list(params or [])).to_arrow_table()
            except duckdb.Error as err:
                raise QueryError(str(err)) from err


_engine_lock = threading.Lock()
_engines: dict[str, Engine] = {}


def _engine(region: str) -> Engine:
    """Return the shared query engine for `region`, creating it on first use."""
    with _engine_lock:
        engine = _engines.get(region)
        if engine is None:
            engine = Engine(
                scan=iceberg_scan, setup=load_extensions, secret=make_secret(region)
            )
            _engines[region] = engine
        return engine


def reset_cache() -> None:
    """Drop the process's cached connections; the next call starts cold."""
    with _engine_lock:
        _engines.clear()


def sql(auth: Auth, query: str, params: Sequence[str | None] | None = None) -> pa.Table:
    """Run `query` with the researcher's credentials.

    Views exist for the tables the query names (and those behind `subject_index`
    and `files`), each at the metadata location read at the start of this call.
    Inside a `run()`, each table's snapshot at that location is recorded.
    """
    held = _creds.credentials(auth)
    session = _creds.session(auth)
    glue = cast("GlueClient", session.client("glue"))
    locations = metadata_locations(glue, tables_needed(query, params))
    _run.record_tables(
        lambda: cast("_run.ObjectReader", session.client("s3")), locations
    )
    engine = _engine(auth.config.region)
    return engine.run(query, params, locations, held)
