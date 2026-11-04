"""CatalogWriter.replace: one commit per table, old rows or new rows."""

from typing import TYPE_CHECKING, Any, Final

import pyarrow as pa
import pytest
from pyiceberg.catalog.sql import SqlCatalog
from pyiceberg.exceptions import CommitFailedException

from asteroidea_infra import schema
from asteroidea_infra.catalog_init import iceberg_schema
from asteroidea_ingest.catalog_writer import (
    MAX_FAILED_RETRIES,
    NAMESPACE,
    CatalogWriter,
    UnknownColumnError,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from pyiceberg.catalog import Catalog

TABLE: Final = "electrodes"
FULL: Final = f"{NAMESPACE}.{TABLE}"


@pytest.fixture
def catalog(tmp_path: Path) -> Iterator[Catalog]:
    cat = SqlCatalog(
        "test",
        uri=f"sqlite:///{tmp_path}/catalog.db",
        warehouse=f"file://{tmp_path}/warehouse",
    )
    cat.create_namespace(NAMESPACE)
    cat.create_table(FULL, iceberg_schema(schema.SPECS[TABLE]))
    yield cat
    cat.engine.dispose()


def _row(asset: str, index: int, location: str) -> dict[str, Any]:
    return {
        "asset_id": asset,
        "dataset": "ds1",
        "electrode_index": index,
        "location": location,
    }


def _rows(
    catalog: Catalog, snapshot_id: int | None = None
) -> list[tuple[str, int, str]]:
    table = catalog.load_table(FULL)
    scan = (
        table.scan(snapshot_id=snapshot_id) if snapshot_id is not None else table.scan()
    )
    data = scan.to_arrow().to_pylist()
    return sorted((r["asset_id"], r["electrode_index"], r["location"]) for r in data)


def _count_commits(catalog: Catalog, monkeypatch: pytest.MonkeyPatch) -> list[int]:
    calls: list[int] = []
    original = catalog.commit_table

    def counting(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(catalog, "commit_table", counting)
    return calls


def test_a_second_extraction_without_replace_doubles_the_rows(catalog: Catalog) -> None:
    writer = CatalogWriter(catalog)
    rows = [_row("a1", 0, "L0"), _row("a1", 1, "L1")]
    writer.append_if_absent(TABLE, {"asset_id": "a1"}, rows)
    # a second extraction that only appends (no key check) doubles the rows
    table = catalog.load_table(FULL)
    table.append(pa.Table.from_pylist(rows, schema=table.schema().as_arrow()))
    assert len(_rows(catalog)) == 4


def test_replace_leaves_exactly_the_new_rows_and_other_assets_alone(
    catalog: Catalog,
) -> None:
    writer = CatalogWriter(catalog)
    writer.append_if_absent(TABLE, {"asset_id": "a1"}, [_row("a1", 0, "old")])
    writer.append_if_absent(TABLE, {"asset_id": "a2"}, [_row("a2", 0, "other")])
    new = [_row("a1", 0, "new0"), _row("a1", 1, "new1")]
    writer.replace(TABLE, {"asset_id": "a1"}, new)
    assert _rows(catalog) == [
        ("a1", 0, "new0"),
        ("a1", 1, "new1"),
        ("a2", 0, "other"),
    ]


def test_replace_twice_with_the_same_rows_does_not_double(catalog: Catalog) -> None:
    writer = CatalogWriter(catalog)
    rows = [_row("a1", 0, "L0"), _row("a1", 1, "L1")]
    for _ in range(2):
        writer.replace(TABLE, {"asset_id": "a1"}, rows)
    assert len(_rows(catalog)) == 2


def test_a_reader_of_the_previous_snapshot_still_sees_the_old_rows(
    catalog: Catalog,
) -> None:
    writer = CatalogWriter(catalog)
    old_snapshot = writer.append_if_absent(
        TABLE, {"asset_id": "a1"}, [_row("a1", 0, "old")]
    )
    writer.replace(TABLE, {"asset_id": "a1"}, [_row("a1", 0, "new")])
    assert _rows(catalog, old_snapshot) == [("a1", 0, "old")]
    assert _rows(catalog) == [("a1", 0, "new")]


def test_replace_is_one_metadata_commit_holding_delete_and_append(
    catalog: Catalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer = CatalogWriter(catalog)
    writer.append_if_absent(TABLE, {"asset_id": "a1"}, [_row("a1", 0, "old")])
    before = len(catalog.load_table(FULL).metadata.snapshots)
    commits = _count_commits(catalog, monkeypatch)
    writer.replace(TABLE, {"asset_id": "a1"}, [_row("a1", 0, "new")])
    assert len(commits) == 1  # one catalog commit for the table
    snapshots = catalog.load_table(FULL).metadata.snapshots
    assert len(snapshots) - before == 2  # delete, then append
    operations = [
        s.summary.operation.value for s in snapshots[before:] if s.summary is not None
    ]
    assert operations == ["delete", "append"]


def test_replace_with_no_rows_deletes_only(
    catalog: Catalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer = CatalogWriter(catalog)
    writer.append_if_absent(TABLE, {"asset_id": "a1"}, [_row("a1", 0, "old")])
    commits = _count_commits(catalog, monkeypatch)
    writer.replace(TABLE, {"asset_id": "a1"}, [])
    assert _rows(catalog) == []
    assert len(commits) == 1


def test_an_unknown_column_changes_nothing(catalog: Catalog) -> None:
    writer = CatalogWriter(catalog)
    writer.append_if_absent(TABLE, {"asset_id": "a1"}, [_row("a1", 0, "old")])
    with pytest.raises(UnknownColumnError):
        writer.replace(TABLE, {"asset_id": "a1"}, [{"asset_id": "a1", "nope": 1}])
    assert _rows(catalog) == [("a1", 0, "old")]


def test_commit_failed_every_time_raises_after_the_bound(
    catalog: Catalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []

    def always_fail(*_args: Any, **_kwargs: Any) -> None:
        calls.append(1)
        raise CommitFailedException

    monkeypatch.setattr(catalog, "commit_table", always_fail)
    with pytest.raises(CommitFailedException):
        CatalogWriter(catalog).replace(TABLE, {"asset_id": "a1"}, [_row("a1", 0, "x")])
    # PyIceberg's transaction commit retries a CommitFailedException itself
    # (commit.retry.num-retries, 4 by default, so 5 tries) before our loop reloads.
    assert len(calls) == (MAX_FAILED_RETRIES + 1) * 5
