"""Query the catalog with PyIceberg scans under the researcher's credentials."""

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from pyiceberg.catalog import load_catalog

from asteroidea import _creds
from asteroidea.errors import AssetNotFoundError, SnapshotGoneError, UnknownFilterError

if TYPE_CHECKING:
    from pyiceberg.catalog import Catalog
    from pyiceberg.table import Table

    from asteroidea._auth import Auth

TABLE: Final = "asteroidea.assets"
FILTERS: Final = (
    "dataset",
    "subject",
    "bids_session",
    "datatype",
    "suffix",
    "extension",
)
_PREFIXES: Final = {"subject": "sub-", "bids_session": "ses-", "extension": "."}
RAW_PREFIX: Final = "raw/"


@dataclass(frozen=True)
class FileRef:
    """One catalogued file."""

    asset_id: str
    dataset: str
    path: str
    s3_key: str
    s3_version_id: str
    size_bytes: int


@dataclass(frozen=True)
class QueryResult:
    """Rows of the catalog at one snapshot (`None` while the catalog is empty)."""

    rows: list[dict[str, Any]]
    files: list[FileRef]
    snapshot_id: int | None


def open_catalog(auth: Auth) -> Catalog:
    """Return the Glue catalog reached with the researcher's current credentials."""
    held = _creds.credentials(auth)
    region = auth.config.region
    return load_catalog(
        "glue",
        **{
            "type": "glue",
            "client.region": region,
            "client.access-key-id": held.access_key_id,
            "client.secret-access-key": held.secret_key,
            "client.session-token": held.session_token,
            "s3.region": region,
            "s3.access-key-id": held.access_key_id,
            "s3.secret-access-key": held.secret_key,
            "s3.session-token": held.session_token,
        },
    )


def _quoted(value: str) -> str:
    """Return `value` as a SQL string literal with quotes doubled."""
    return "'" + value.replace("'", "''") + "'"


def _expression(filters: dict[str, str]) -> str:
    """Return the SQL condition for `filters`.

    Raises `UnknownFilterError` for a key that is not a known filter.
    """
    unknown = [key for key in filters if key not in FILTERS]
    if unknown:
        msg = (
            f"Unknown filter {', '.join(unknown)}; "
            f"the filters you can use are {', '.join(FILTERS)}."
        )
        raise UnknownFilterError(msg)
    terms = []
    for key, value in filters.items():
        prefix = _PREFIXES.get(key, "")
        wanted = value if value.startswith(prefix) else prefix + value
        terms.append(f"{key} = {_quoted(wanted)}")
    return " AND ".join(terms) or "true"


def file_ref(row: dict[str, Any]) -> FileRef:
    """Build the file reference of one catalog row."""
    key = str(row["s3_key"])
    return FileRef(
        asset_id=str(row["asset_id"]),
        dataset=str(row["dataset"]),
        path=key.removeprefix(f"{RAW_PREFIX}{row['dataset']}/"),
        s3_key=key,
        s3_version_id=str(row["s3_version_id"]),
        size_bytes=int(row["size_bytes"]),
    )


def read_rows(
    table: Table, filters: dict[str, str], snapshot_id: int | None = None
) -> QueryResult:
    """Scan `table` with the filters, at `snapshot_id` or the current snapshot.

    Raises `UnknownFilterError` for a filter that is not a column to filter on and
    `SnapshotGoneError` when `snapshot_id` is not in the table.
    """
    expression = _expression(filters)
    try:
        scan = table.scan(row_filter=expression, snapshot_id=snapshot_id)
        snapshot = scan.snapshot()
        arrow = scan.to_arrow()
    except ValueError as err:
        if snapshot_id is None:
            raise
        raise SnapshotGoneError from err
    rows = sorted(arrow.to_pylist(), key=lambda r: (r["committed_at"], r["asset_id"]))
    return QueryResult(
        rows=rows,
        files=[file_ref(row) for row in rows],
        snapshot_id=None if snapshot is None else snapshot.snapshot_id,
    )


def find_asset(table: Table, asset_id: str) -> FileRef:
    """Return the file of `asset_id`, or raise `AssetNotFoundError`."""
    try:
        wanted = str(uuid.UUID(asset_id))
    except ValueError as err:
        raise AssetNotFoundError from err
    scan = table.scan(row_filter=f"asset_id = {_quoted(wanted)}")
    rows = scan.to_arrow().to_pylist()
    if not rows:
        raise AssetNotFoundError
    return file_ref(rows[0])


def query(auth: Auth, *, snapshot_id: int | None = None, **filters: str) -> QueryResult:
    """Query the catalog; see `asteroidea.query`."""
    _expression(filters)  # refuse an unknown filter before any network call
    table = open_catalog(auth).load_table(TABLE)
    return read_rows(table, filters, snapshot_id)
