"""Append rows to an Iceberg table, at most once per key.

The catalog is injected, so tests use a local SQL catalog and the Lambda uses Glue.
"""

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

import pyarrow as pa
from pyiceberg.exceptions import CommitFailedException, CommitStateUnknownException
from pyiceberg.expressions import And, BooleanExpression, EqualTo

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from pyiceberg.catalog import Catalog
    from pyiceberg.table import Table

NAMESPACE: Final = "asteroidea"
MAX_UNKNOWN_RETRIES: Final = 1
MAX_FAILED_RETRIES: Final = 3


class UnknownColumnError(ValueError):
    """A row names a column the table does not have; nothing is appended."""


def _key_filter(key: Mapping[str, Any]) -> BooleanExpression:
    terms: list[BooleanExpression] = [EqualTo(n, v) for n, v in key.items()]
    if not terms:
        msg = "key must name at least one column"
        raise ValueError(msg)
    expr = terms[0]
    for term in terms[1:]:
        expr = And(expr, term)
    return expr


class CatalogWriter:
    """Check-before-append writer over an injected PyIceberg catalog."""

    def __init__(self, catalog: Catalog) -> None:
        """Keep the catalog; the table is loaded per attempt."""
        self._catalog = catalog

    @staticmethod
    def _has_key(table: Table, key: Mapping[str, Any]) -> bool:
        scan = table.scan(row_filter=_key_filter(key), selected_fields=tuple(key))
        return scan.to_arrow().num_rows > 0

    @staticmethod
    def _snapshot_id(table: Table) -> int:
        snapshot = table.current_snapshot()
        if snapshot is None:  # pragma: no cover - a row exists, so a snapshot does
            msg = "table has rows but no current snapshot"
            raise RuntimeError(msg)
        return snapshot.snapshot_id

    @staticmethod
    def _batch(table: Table, rows: Sequence[Mapping[str, Any]]) -> pa.Table:
        columns = {field.name for field in table.schema().fields}
        unknown = sorted({name for row in rows for name in row} - columns)
        if unknown:
            msg = f"rows name columns {unknown} that the table does not have"
            raise UnknownColumnError(msg)
        return pa.Table.from_pylist(
            [dict(row) for row in rows], schema=table.schema().as_arrow()
        )

    def append_if_absent(
        self,
        table_name: str,
        key: Mapping[str, Any],
        rows: Sequence[Mapping[str, Any]],
    ) -> int:
        """Append `rows` unless a row matching `key` exists; return the snapshot id.

        `key` maps each key column to its value (all must match). When a row
        exists, the current snapshot id is returned and nothing is appended.
        After `CommitStateUnknownException` (at most `MAX_UNKNOWN_RETRIES` times)
        or `CommitFailedException` (at most `MAX_FAILED_RETRIES` times) the table
        is reloaded and re-checked before the append is tried again.
        """
        unknown = failed = 0
        while True:
            table = self._catalog.load_table(f"{NAMESPACE}.{table_name}")
            if self._has_key(table, key):
                return self._snapshot_id(table)
            batch = self._batch(table, rows)
            try:
                table.append(batch)
            except CommitStateUnknownException:
                unknown += 1
                if unknown > MAX_UNKNOWN_RETRIES:
                    raise
                continue
            except CommitFailedException:
                failed += 1
                if failed > MAX_FAILED_RETRIES:
                    raise
                continue
            return self._snapshot_id(table)

    def replace(
        self,
        table_name: str,
        key: Mapping[str, Any],
        rows: Sequence[Mapping[str, Any]],
    ) -> int | None:
        """Replace the rows matching `key` with `rows`; return the snapshot id.

        The id is None when `rows` is empty and the table has no snapshot yet.

        One PyIceberg transaction: delete the rows matching `key`, then append
        `rows` (none: the delete alone), committed once. A reader sees the old
        rows or the new rows, never neither. Retries as
        `append_if_absent` does; a retry repeats the whole transaction on a
        reloaded table, which ends in the same state.
        """
        expr = _key_filter(key)
        unknown = failed = 0
        while True:
            table = self._catalog.load_table(f"{NAMESPACE}.{table_name}")
            batch = self._batch(table, rows) if rows else None
            exists = self._has_key(table, key)
            try:
                with table.transaction() as tx:
                    if exists:  # a delete that matches nothing warns
                        tx.delete(expr)
                    if batch is not None:
                        tx.append(batch)
            except CommitStateUnknownException:
                unknown += 1
                if unknown > MAX_UNKNOWN_RETRIES:
                    raise
                continue
            except CommitFailedException:
                failed += 1
                if failed > MAX_FAILED_RETRIES:
                    raise
                continue
            return (
                None if table.current_snapshot() is None else self._snapshot_id(table)
            )


def utc_now_naive() -> datetime:
    """Return the current UTC time without tzinfo (the `timestamp` column type)."""
    return datetime.now(UTC).replace(tzinfo=None)
