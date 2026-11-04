"""`make catalog-init`: the table specs, create, evolve and refuse.

The evolve tests run against PyIceberg's local SQL catalog (SQLite, file warehouse).
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pyarrow as pa
import pytest
from pyiceberg.catalog.sql import SqlCatalog

from asteroidea_infra import schema
from asteroidea_infra.catalog_init import (
    SchemaConflictError,
    Step,
    create_table,
    create_table_at,
    iceberg_schema,
    plan,
    run,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from pyiceberg.catalog import Catalog

REQUIRED = {
    "asset_id",
    "session_id",
    "dataset",
    "s3_key",
    "s3_version_id",
    "size_bytes",
    "checksum_crc64nvme",
    "extension",
    "committed_at",
}
PROVENANCE = {
    "upload_id",
    "ingest_method",
    "format",
    "checksum_sha256",
    "source_archive",
    "source_dataset",
    "source_version",
    "source_path",
    "source_url",
    "source_digest",
    "license",
    "ingested_by",
}
BASE_SPEC = schema.TableSpec(schema.TABLE, schema.BASE_ASSETS_COLUMNS, ("session_id",))
BASE_ROW: dict[str, Any] = {
    "asset_id": "a1",
    "session_id": "s1",
    "dataset": "ds1",
    "s3_key": "raw/ds1/sub-01/anat/sub-01_T1w.nii",
    "s3_version_id": "v1",
    "size_bytes": 352,
    "checksum_crc64nvme": "AAAAAAAAAAA=",
    "subject": "sub-01",
    "bids_session": None,
    "datatype": "anat",
    "suffix": "T1w",
    "extension": ".nii",
    "committed_at": datetime(2026, 10, 23, tzinfo=UTC).replace(tzinfo=None),
}


class _FakeCatalog:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    def create_table_if_not_exists(self, *args: Any, **kwargs: Any) -> str:
        self.calls.append((args, kwargs))
        return "table"


@pytest.fixture
def catalog(tmp_path: Path) -> Iterator[SqlCatalog]:
    sql = SqlCatalog(
        "test",
        uri=f"sqlite:///{tmp_path}/catalog.db",
        warehouse=f"file://{tmp_path}/warehouse",
    )
    sql.create_namespace(schema.DATABASE)
    yield sql
    sql.engine.dispose()


@pytest.fixture
def root(tmp_path: Path) -> str:
    return f"file://{tmp_path}/warehouse"


def _state(catalog: Catalog) -> dict[str, tuple[Any, ...]]:
    out: dict[str, tuple[Any, ...]] = {}
    for name in schema.TABLE_NAMES:
        table = catalog.load_table(f"{schema.DATABASE}.{name}")
        snapshot = table.current_snapshot()
        out[name] = (
            table.metadata_location,
            table.metadata.current_schema_id,
            None if snapshot is None else snapshot.snapshot_id,
        )
    return out


def test_assets_spec_is_base_plus_optional_provenance() -> None:
    """The 13 base columns unchanged, plus 12 optional provenance columns."""
    fields = iceberg_schema().fields
    assert len(fields) == 25
    assert [f.name for f in fields[:13]] == [c.name for c in schema.BASE_ASSETS_COLUMNS]
    assert {f.name for f in fields if f.required} == REQUIRED
    assert {f.name for f in fields[13:]} == PROVENANCE
    assert {f.name: str(f.field_type) for f in fields}["size_bytes"] == "long"


def test_fourteen_specs_and_the_grant_names_derive_from_them() -> None:
    assert tuple(spec.name for spec in schema.TABLES) == schema.TABLE_NAMES
    assert len(set(schema.TABLE_NAMES)) == 14
    assert schema.PROBE_TABLE not in schema.TABLE_NAMES
    for spec in schema.TABLES:
        names = [c.name for c in spec.columns]
        assert len(names) == len(set(names)), spec.name
        assert set(spec.key) <= {c.name for c in spec.columns if c.required}


def test_table_is_format_v2_under_the_warehouse_prefix() -> None:
    fake = _FakeCatalog()
    create_table(cast("Catalog", fake), "wh-bucket", "assets")
    ((args, kwargs),) = fake.calls
    assert args[0] == "asteroidea.assets"
    assert kwargs["properties"] == {"format-version": "2"}
    assert kwargs["location"] == "s3://wh-bucket/iceberg/assets"


def test_count_sql_names_the_assets_table() -> None:
    """The Athena count reads `assets`, which ResearcherRole may read."""
    assert schema.ASSETS_COUNT_SQL.endswith(f" FROM {schema.TABLE}")


def test_fresh_create_makes_all_fourteen_tables(catalog: SqlCatalog, root: str) -> None:
    steps = run(catalog, root, dry_run=False)
    assert steps == [Step(name) for name in schema.TABLE_NAMES]
    for spec in schema.TABLES:
        table = catalog.load_table(f"{schema.DATABASE}.{spec.name}")
        assert table.metadata.format_version == 2
        got = {f.name: (str(f.field_type), f.required) for f in table.schema().fields}
        assert got == {c.name: (c.type, c.required) for c in spec.columns}


def test_dry_run_changes_nothing(
    catalog: SqlCatalog, root: str, capsys: pytest.CaptureFixture[str]
) -> None:
    steps = run(catalog, root, dry_run=True)
    assert len(steps) == 14
    assert catalog.list_tables(schema.DATABASE) == []
    assert "plan: create asteroidea.units (14 columns)" in capsys.readouterr().out


def test_evolve_adds_provenance_to_a_legacy_assets_table(
    catalog: SqlCatalog, root: str
) -> None:
    legacy = create_table_at(catalog, root, schema.TABLE, iceberg_schema(BASE_SPEC))
    legacy.append(pa.Table.from_pylist([BASE_ROW], schema=legacy.schema().as_arrow()))
    steps = run(catalog, root, dry_run=False)
    added = {s.column.name for s in steps if s.table == schema.TABLE and s.column}
    assert added == PROVENANCE
    assets = catalog.load_table(f"{schema.DATABASE}.{schema.TABLE}")
    assert [f.name for f in assets.schema().fields] == [
        c.name for c in schema.ASSETS_COLUMNS
    ]
    (row,) = assets.scan().to_arrow().to_pylist()
    assert row["asset_id"] == "a1"
    assert all(row[name] is None for name in PROVENANCE)  # a legacy row reads null


def test_rerun_is_idempotent(catalog: SqlCatalog, root: str) -> None:
    legacy = create_table_at(catalog, root, schema.TABLE, iceberg_schema(BASE_SPEC))
    legacy.append(pa.Table.from_pylist([BASE_ROW], schema=legacy.schema().as_arrow()))
    run(catalog, root, dry_run=False)
    before = _state(catalog)
    assert plan(catalog) == []
    assert run(catalog, root, dry_run=False) == []
    assert _state(catalog) == before  # no new metadata, schema id or snapshot


def test_type_conflict_is_refused_naming_table_and_column(
    catalog: SqlCatalog, root: str
) -> None:
    columns = tuple(
        schema.Column(c.name, "long", c.required) if c.name == "upload_id" else c
        for c in schema.ASSETS_COLUMNS
    )
    conflicted = schema.TableSpec(schema.TABLE, columns, ("session_id",))
    create_table_at(catalog, root, schema.TABLE, iceberg_schema(conflicted))
    with pytest.raises(SchemaConflictError, match=r"asteroidea\.assets\.upload_id"):
        run(catalog, root, dry_run=False)
    assert catalog.list_tables(schema.DATABASE) == [(schema.DATABASE, schema.TABLE)]


def test_missing_required_column_is_refused(catalog: SqlCatalog, root: str) -> None:
    columns = tuple(c for c in schema.ASSETS_COLUMNS if c.name != "dataset")
    short = schema.TableSpec(schema.TABLE, columns, ("session_id",))
    create_table_at(catalog, root, schema.TABLE, iceberg_schema(short))
    with pytest.raises(SchemaConflictError, match=r"asteroidea\.assets\.dataset"):
        plan(catalog)


# -- what `make catalog-init DRY=1` must print -----------

TODAY = Path(__file__).parent / "fixtures" / "catalog" / "schema-recorded.json"


def _today_specs() -> list[schema.TableSpec]:
    """Return the deployed catalog's specs, frozen before the new tables."""
    tables = json.loads(TODAY.read_text())["tables"]
    return [
        schema.TableSpec(
            name,
            tuple(schema.Column(n, t, required=r) for n, t, r in spec["columns"]),
            tuple(spec["key"]),
        )
        for name, spec in tables.items()
    ]


def _state_of(catalog: Catalog, names: list[str]) -> dict[str, tuple[Any, ...]]:
    out: dict[str, tuple[Any, ...]] = {}
    for name in names:
        table = catalog.load_table(f"{schema.DATABASE}.{name}")
        out[name] = (table.metadata_location, table.metadata.current_schema_id)
    return out


def test_studies_tables_and_added_columns_match_the_schema() -> None:
    """Two new tables; every added column is optional."""
    studies = schema.SPECS["studies"]
    assert studies.key == ("study",)
    assert {c.name: (c.type, c.required) for c in studies.columns} == {
        "study": ("string", True),
        "title": ("string", True),
        "authors": ("string", False),
        "lab": ("string", False),
        "citation": ("string", False),
        "doi": ("string", False),
        "note": ("string", False),
        "curated_by": ("string", True),
        "created_at": ("timestamp", True),
    }
    members = schema.SPECS["study_datasets"]
    assert members.key == ("study", "dataset")
    assert {c.name: (c.type, c.required) for c in members.columns} == {
        "study": ("string", True),
        "dataset": ("string", True),
        "role": ("string", False),
        "curated_by": ("string", True),
        "created_at": ("timestamp", True),
    }


def test_catalog_init_plan_against_todays_catalog_is_two_tables_five_columns(
    catalog: SqlCatalog, root: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """Pin the DRY check offline: exactly 2 creates and 5 optional adds."""
    today = [spec.name for spec in _today_specs()]
    for spec in _today_specs():
        create_table_at(catalog, root, spec.name, iceberg_schema(spec))
    before = _state_of(catalog, today)
    steps = run(catalog, root, dry_run=True)
    assert capsys.readouterr().out.splitlines() == [
        "plan: add column asteroidea.datasets.tier string (optional)",
        "plan: add column asteroidea.subject_links.status string (optional)",
        "plan: add column asteroidea.subject_links.linked_by string (optional)",
        "plan: add column asteroidea.subject_links.linked_at timestamp (optional)",
        "plan: add column asteroidea.subject_links.basis string (optional)",
        "plan: create asteroidea.studies (9 columns)",
        "plan: create asteroidea.study_datasets (5 columns)",
    ]
    assert [s.table for s in steps if s.column is None] == ["studies", "study_datasets"]
    assert [s.column.required for s in steps if s.column is not None] == [False] * 5
    assert _state_of(catalog, today) == before  # DRY changed nothing
    run(catalog, root, dry_run=False)
    assert plan(catalog) == []
