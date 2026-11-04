"""Browse calls: datasets, subjects, sessions and assets as Arrow tables.

Each call is one fixed query; a filter left as `None` matches every row, and a
given value is bound as a parameter, never spliced into the text.
"""

from typing import TYPE_CHECKING

from asteroidea import _sql

if TYPE_CHECKING:
    import pyarrow as pa

    from asteroidea._auth import Auth

_DATASETS = "SELECT * FROM datasets ORDER BY dataset"
_SUBJECTS = (
    "SELECT * FROM subject_index "
    "WHERE (CAST(? AS VARCHAR) IS NULL OR dataset = ?) "
    "ORDER BY dataset, subject"
)
_SESSIONS = (
    "SELECT * FROM sessions "
    "WHERE (CAST(? AS VARCHAR) IS NULL OR dataset = ?) "
    "AND (CAST(? AS VARCHAR) IS NULL OR subject = ?) "
    "ORDER BY dataset, subject, bids_session"
)
_ASSETS = (
    "SELECT * FROM files "
    "WHERE (CAST(? AS VARCHAR) IS NULL OR dataset = ?) "
    "AND (CAST(? AS VARCHAR) IS NULL OR subject = ?) "
    "AND (CAST(? AS VARCHAR) IS NULL OR bids_session = ?) "
    "AND (CAST(? AS VARCHAR) IS NULL OR format = ?) "
    "ORDER BY dataset, s3_key"
)


def _twice(*values: str | None) -> list[str | None]:
    """Return each value twice, in order, as `[a, a, b, b]`."""
    return [item for value in values for item in (value, value)]


def datasets(auth: Auth) -> pa.Table:
    """Return every dataset row."""
    return _sql.sql(auth, _DATASETS)


def subjects(auth: Auth, *, dataset: str | None = None) -> pa.Table:
    """Return subject rows with `subject_key` (curated key, else dataset/subject)."""
    return _sql.sql(auth, _SUBJECTS, _twice(dataset))


def sessions(
    auth: Auth, *, dataset: str | None = None, subject: str | None = None
) -> pa.Table:
    """Return session rows."""
    return _sql.sql(auth, _SESSIONS, _twice(dataset, subject))


def assets(
    auth: Auth,
    *,
    dataset: str | None = None,
    subject: str | None = None,
    bids_session: str | None = None,
    file_format: str | None = None,
) -> pa.Table:
    """Return asset rows joined to their file headers."""
    return _sql.sql(auth, _ASSETS, _twice(dataset, subject, bids_session, file_format))
