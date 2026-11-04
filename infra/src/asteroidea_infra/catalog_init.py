"""Create and evolve the catalog tables through PyIceberg's Glue catalog (live only).

Run with `make catalog-init` (`DRY=1` prints the plan and changes nothing).
CloudFormation could not create the Iceberg table, so this is
the way tables are created. Create each
missing table of `schema.TABLES`, add each missing optional column to an existing
table, and never drop or retype a column; a conflict refuses with exit 1.
"""

import argparse
import sys
from itertools import count
from typing import TYPE_CHECKING, Final, NamedTuple

import boto3
from pyiceberg.catalog import load_catalog
from pyiceberg.exceptions import NoSuchTableError
from pyiceberg.schema import Schema
from pyiceberg.types import (
    BooleanType,
    DoubleType,
    IcebergType,
    ListType,
    LongType,
    NestedField,
    StringType,
    TimestampType,
)

from asteroidea_infra import schema
from asteroidea_infra.sandbox_guard import require_sandbox

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

    from pyiceberg.catalog import Catalog
    from pyiceberg.table import Table

_PRIMITIVES: Final = {
    "string": StringType,
    "long": LongType,
    "double": DoubleType,
    "boolean": BooleanType,
    "timestamp": TimestampType,
}
EXIT_CONFLICT: Final = 1


class SchemaConflictError(ValueError):
    """An existing table disagrees with its spec in a way an add cannot fix."""


class Step(NamedTuple):
    """One planned change: create `table`, or add `column` to it."""

    table: str
    column: schema.Column | None = None

    def describe(self) -> str:
        """Return the plan line for this step."""
        name = f"{schema.DATABASE}.{self.table}"
        if self.column is None:
            columns = len(schema.SPECS[self.table].columns)
            return f"create {name} ({columns} columns)"
        return f"add column {name}.{self.column.name} {self.column.type} (optional)"


def iceberg_type(type_name: str, ids: Iterator[int]) -> IcebergType:
    """Return the Iceberg type for a spec type name; list element ids come from ids."""
    if type_name.startswith("list<") and type_name.endswith(">"):
        element = type_name.removeprefix("list<").removesuffix(">")
        return ListType(next(ids), _PRIMITIVES[element](), element_required=False)
    return _PRIMITIVES[type_name]()


def iceberg_schema(spec: schema.TableSpec = schema.ASSETS) -> Schema:
    """Build the PyIceberg schema of a table spec (default: `assets`)."""
    ids = count(len(spec.columns) + 1)
    return Schema(
        *(
            NestedField(i, c.name, iceberg_type(c.type, ids), required=c.required)
            for i, c in enumerate(spec.columns, start=1)
        ),
    )


def _evolve_steps(table: Table, spec: schema.TableSpec) -> list[Step]:
    """Return the add-column steps for an existing table, or refuse a conflict."""
    current = {f.name: f for f in table.schema().fields}
    steps: list[Step] = []
    for column in spec.columns:
        field = current.get(column.name)
        where = f"{schema.DATABASE}.{spec.name}.{column.name}"
        if field is None:
            if column.required:
                msg = f"refusing: {where} is required and missing; it cannot be added"
                raise SchemaConflictError(msg)
            steps.append(Step(spec.name, column))
            continue
        if str(field.field_type) != column.type:
            msg = (
                f"refusing: {where} is {field.field_type} in the catalog, "
                f"{column.type} in the spec; catalog-init never retypes a column"
            )
            raise SchemaConflictError(msg)
        if field.required != column.required:
            msg = (
                f"refusing: {where} has required={field.required} in the catalog, "
                f"required={column.required} in the spec"
            )
            raise SchemaConflictError(msg)
    return steps


def plan(
    catalog: Catalog, specs: Iterable[schema.TableSpec] = schema.TABLES
) -> list[Step]:
    """Return every step that brings the catalog to the specs; change nothing."""
    steps: list[Step] = []
    for spec in specs:
        try:
            table = catalog.load_table(f"{schema.DATABASE}.{spec.name}")
        except NoSuchTableError:
            steps.append(Step(spec.name))
            continue
        steps.extend(_evolve_steps(table, spec))
    return steps


def create_table(
    catalog: Catalog,
    bucket: str,
    name: str,
    table_schema: Schema | None = None,
) -> Table:
    """Create the format-v2 table `name` in the bucket if it does not exist."""
    return create_table_at(catalog, f"s3://{bucket}", name, table_schema)


def create_table_at(
    catalog: Catalog,
    root: str,
    name: str,
    table_schema: Schema | None = None,
) -> Table:
    """Create the format-v2 table `name` under the warehouse URI `root`."""
    return catalog.create_table_if_not_exists(
        f"{schema.DATABASE}.{name}",
        iceberg_schema() if table_schema is None else table_schema,
        location=f"{root}/{schema.ICEBERG_PREFIX}/{name}",
        properties={"format-version": "2"},
    )


def apply(catalog: Catalog, root: str, steps: list[Step]) -> None:
    """Run the steps: one create per new table, one schema update per evolved table."""
    adds: dict[str, list[schema.Column]] = {}
    for step in steps:
        if step.column is None:
            spec = schema.SPECS[step.table]
            create_table_at(catalog, root, spec.name, iceberg_schema(spec))
        else:
            adds.setdefault(step.table, []).append(step.column)
    for name, columns in adds.items():
        table = catalog.load_table(f"{schema.DATABASE}.{name}")
        ids = count(1)  # placeholders: update_schema assigns fresh ids
        with table.update_schema() as update:
            for column in columns:
                update.add_column(column.name, iceberg_type(column.type, ids))


def run(catalog: Catalog, root: str, *, dry_run: bool) -> list[Step]:
    """Plan, print the plan, and apply it unless `dry_run`; return the steps."""
    steps = plan(catalog)
    for step in steps:
        sys.stdout.write(f"{'plan' if dry_run else 'apply'}: {step.describe()}\n")
    if not steps:
        sys.stdout.write("catalog matches the specs: nothing to do\n")
    elif not dry_run:
        apply(catalog, root, steps)
    return steps


def warehouse_bucket_name(session: boto3.Session) -> str:
    """Read the warehouse bucket name from the DataStack outputs."""
    outputs = session.client("cloudformation").describe_stacks(StackName="DataStack")
    return str(
        next(
            o["OutputValue"]
            for o in outputs["Stacks"][0]["Outputs"]
            if o["OutputKey"].startswith("WarehouseBucketName")
        ),
    )


def glue_catalog(profile: str) -> Catalog:
    """Load the PyIceberg Glue catalog through the sandbox profile."""
    return load_catalog(
        "glue",
        **{
            "type": "glue",
            "client.profile-name": profile,
            "client.region": "us-east-1",
        },
    )


def main() -> None:
    """Create the database if missing, then plan and (unless --dry-run) apply."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    profile, account = require_sandbox()
    session = boto3.Session(profile_name=profile, region_name="us-east-1")
    got = session.client("sts").get_caller_identity()["Account"]
    if got != account:
        msg = f"profile {profile} is account {got}, expected {account}"
        raise SystemExit(msg)
    bucket = warehouse_bucket_name(session)
    catalog = glue_catalog(profile)
    if not args.dry_run:
        catalog.create_namespace_if_not_exists(schema.DATABASE)
    try:
        run(catalog, f"s3://{bucket}", dry_run=args.dry_run)
    except SchemaConflictError as err:
        sys.stderr.write(f"{err}\n")
        raise SystemExit(EXIT_CONFLICT) from err


if __name__ == "__main__":
    main()
