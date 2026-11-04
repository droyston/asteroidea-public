from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pyarrow as pa
import pytest
from pyiceberg.catalog.sql import SqlCatalog

from asteroidea import _query
from asteroidea.errors import AssetNotFoundError, SnapshotGoneError, UnknownFilterError
from asteroidea_infra.catalog_init import iceberg_schema

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from pyiceberg.table import Table

A1 = "11111111-1111-4111-8111-111111111111"
A2 = "22222222-2222-4222-8222-222222222222"
A3 = "33333333-3333-4333-8333-333333333333"


def row(asset: str, dataset: str, when: int, **over: Any) -> dict[str, Any]:
    base = {
        "asset_id": asset,
        "session_id": f"s-{asset[:2]}",
        "dataset": dataset,
        "s3_key": f"raw/{dataset}/sub-01/anat/sub-01_T1w.nii",
        "s3_version_id": f"v-{asset[:2]}",
        "size_bytes": 100,
        "checksum_crc64nvme": "AAAAAAAAAAA=",
        "subject": "sub-01",
        "bids_session": None,
        "datatype": "anat",
        "suffix": "T1w",
        "extension": ".nii",
        "committed_at": datetime(2026, 10, 23, 12, 0, when, tzinfo=UTC).replace(
            tzinfo=None
        ),
    }
    return base | over


@pytest.fixture
def table(tmp_path: Path) -> Iterator[Table]:
    catalog = SqlCatalog(
        "test",
        uri=f"sqlite:///{tmp_path}/catalog.db",
        warehouse=f"file://{tmp_path}/warehouse",
    )
    catalog.create_namespace("asteroidea")
    assets = catalog.create_table("asteroidea.assets", iceberg_schema())
    yield assets
    catalog.engine.dispose()


def append(table: Table, *rows: dict[str, Any]) -> None:
    table.append(pa.Table.from_pylist(list(rows), schema=table.schema().as_arrow()))


def test_empty_catalog_gives_no_rows_and_no_snapshot(table: Table) -> None:
    result = _query.read_rows(table, {})
    assert (result.rows, result.files, result.snapshot_id) == ([], [], None)


def test_filters_rows_files_and_snapshot(table: Table) -> None:
    append(table, row(A1, "ds1", 1))
    append(table, row(A2, "ds2", 2, subject="sub-02", datatype="func"))
    result = _query.read_rows(table, {"datatype": "anat", "subject": "01"})
    assert [r["asset_id"] for r in result.rows] == [A1]
    assert result.files[0].path == "sub-01/anat/sub-01_T1w.nii"
    assert result.files[0].s3_version_id == "v-11"
    current = table.current_snapshot()
    assert current is not None
    assert result.snapshot_id == current.snapshot_id


def test_unknown_filter_lists_the_valid_keys(table: Table) -> None:
    with pytest.raises(UnknownFilterError) as caught:
        _query.read_rows(table, {"color": "red"})
    for key in _query.FILTERS:
        assert key in str(caught.value)


def test_snapshot_id_reads_the_catalog_as_it_was(table: Table) -> None:
    append(table, row(A1, "ds1", 1))
    first = _query.read_rows(table, {})
    append(table, row(A2, "ds2", 2))
    now = _query.read_rows(table, {})
    then = _query.read_rows(table, {}, first.snapshot_id)
    assert [r["dataset"] for r in now.rows] == ["ds1", "ds2"]
    assert then.rows == first.rows
    assert [r["dataset"] for r in then.rows] == ["ds1"]
    assert then.snapshot_id == first.snapshot_id != now.snapshot_id


def test_a_snapshot_that_does_not_exist_is_gone(table: Table) -> None:
    append(table, row(A1, "ds1", 1))
    with pytest.raises(SnapshotGoneError, match="no longer exists"):
        _query.read_rows(table, {}, 123456789)


def test_quote_in_a_filter_value_is_not_an_injection(table: Table) -> None:
    append(table, row(A1, "ds1", 1))
    assert _query.read_rows(table, {"dataset": "x' OR '1'='1"}).rows == []


def test_find_asset(table: Table) -> None:
    append(table, row(A1, "ds1", 1), row(A2, "ds2", 2))
    assert _query.find_asset(table, A2).dataset == "ds2"
    with pytest.raises(AssetNotFoundError):
        _query.find_asset(table, A3)
    with pytest.raises(AssetNotFoundError):
        _query.find_asset(table, "not-a-uuid")
