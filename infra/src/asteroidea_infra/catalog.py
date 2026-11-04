"""Glue database `asteroidea`.

The Iceberg table `assets` is not created here: CloudFormation rejected the Iceberg
v2 `AWS::Glue::Table`).
`make catalog-init` creates it through PyIceberg instead.
"""

from typing import TYPE_CHECKING

from aws_cdk import Stack
from aws_cdk import aws_glue as glue

from asteroidea_infra import schema

if TYPE_CHECKING:
    from constructs import Construct


def glue_database(scope: Construct) -> glue.CfnDatabase:
    """Create the Glue database in this account's default catalog."""
    return glue.CfnDatabase(
        scope,
        "Database",
        catalog_id=Stack.of(scope).account,
        database_input=glue.CfnDatabase.DatabaseInputProperty(name=schema.DATABASE),
    )
