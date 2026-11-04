"""The function registry: one plain row per Lambda function and the builder that loops.

Each row names the handler, the zip or image code, the fixed role name, memory,
timeout, the environment and the role's IAM statements. Statements are plain
`iam.PolicyStatement` lists, returned in template order by one small function per
role.

Nested-stack split rule:

1. A deployed function never changes `construct_id` or its tuple. Either change makes
   CloudFormation create a new function and role before it deletes the old ones, and
   the fixed role name makes the create fail.
2. Budget per template: 300 resources, 150 outputs, 150 parameters, 800,000 bytes
   (the stack-budget gate fails above them).
3. When a stack would pass its budget, new functions go into a new nested stack with
   its own tuple, in a labelled infrastructure PR with a deploy. Existing functions
   never move.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from aws_cdk import Duration, NestedStack
from aws_cdk import aws_ecr_assets as ecr_assets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_lambda_event_sources as event_sources

from asteroidea_infra import athena, config, schema
from asteroidea_infra.identity import QueryGrant, query_read_statements
from asteroidea_infra.roles import name_execution_role
from asteroidea_infra.storage import (
    DERIVED_SUMMARY_PREFIX,
    OWNER_INDEX,
    RUNS_CALLER_INDEX,
)
from asteroidea_infra.web_hosting import HANDOUT_PREFIX
from asteroidea_ingest import catalog_api, timing

if TYPE_CHECKING:
    from collections.abc import Callable

    from aws_cdk import aws_sqs as sqs
    from constructs import Construct

    from asteroidea_infra.api import ApiStack
    from asteroidea_infra.ingest import IngestStack

REPO_ROOT: Final = Path(__file__).resolve().parents[3]
INGEST_SRC: Final = REPO_ROOT / "ingest" / "src"
COMPUTE_DOCKERFILE: Final = "ingest/Dockerfile.compute"
MEMORY_MB: Final = 256
COMPUTE_MEMORY_MB: Final = 2048
COMMIT_DOCKERFILE: Final = "ingest/Dockerfile"
EXTRACT_DOCKERFILE: Final = "ingest/Dockerfile.extract"
# One message per invocation for every queue-fed function.
BATCH_SIZE: Final = 1

ATHENA_READ_ACTIONS: Final = (
    "athena:StartQueryExecution",
    "athena:GetQueryExecution",
    "athena:GetQueryResults",
    "athena:StopQueryExecution",
)
GLUE_READ_ACTIONS: Final = ("glue:GetDatabase", "glue:GetTable")
CATALOG_TABLES: Final = (
    "assets",
    "datasets",
    "asset_headers",
    "subjects",
    "sessions",
    "series",
    "electrodes",
    "units",
    "intervals",
    "events",
    "subject_links",
    "studies",
    "study_datasets",
)
COMPUTE_TABLES: Final = (
    "assets",
    "series",
    "units",
    "unit_spikes",
    "intervals",
    "events",
)
# LabelsRole's Athena read: the asset row and the three tables a label is
# drawn against. Each is granted with its Iceberg `$snapshots` metadata table.
LABELS_TABLES: Final = ("assets", "intervals", "events", "asset_headers")
# LfpBuildRole's Athena read for the summary kinds: the dataset summary's nine
# tables and, for the per-session parts through the compute API, `unit_spikes`.
SUMMARY_TABLES: Final = (
    "assets",
    "subjects",
    "sessions",
    "asset_headers",
    "series",
    "units",
    "unit_spikes",
    "electrodes",
    "intervals",
    "events",
)


@dataclass(frozen=True)
class Image:
    """A container image built from the repo root."""

    # "ingest/Dockerfile", "ingest/Dockerfile.extract" or "ingest/Dockerfile.compute".
    dockerfile: str
    # CMD override; None keeps the Dockerfile's CMD.
    cmd: str | None = None


@dataclass(frozen=True)
class FunctionSpec[S: NestedStack]:
    """One Lambda function: everything the builder needs to create it."""

    # "CatalogFunction": the logical-id root. Never change it for a deployed function.
    construct_id: str
    # "asteroidea_ingest.catalog_api.handler"; for an image with cmd=None it must
    # equal the Dockerfile's CMD (tested in test_registry).
    handler: str
    # Fixed physical role name from config.
    role_name: str
    memory_mb: int
    timeout_seconds: int
    # The environment, read from the stack being built.
    env: Callable[[S], dict[str, str]]
    # The role's statements, in template order.
    statements: Callable[[S], list[iam.PolicyStatement]]
    # None: the ingest/src zip (Python 3.14, arm64).
    image: Image | None = None
    # The queue an SqsEventSource (batch size 1) reads, added before the statements.
    queue: Callable[[S], sqs.IQueue] | None = None
    # Adds COLD_NONCE from the cold-nonce context (`cold_start_nonce`).
    cold_nonce: bool = False
    # API functions: "Integration" for SessionFunction (legacy), "<Name>Integration"
    # for the others.
    integration_id: str | None = None


def cold_start_nonce(scope: Construct) -> dict[str, str]:
    """Return `COLD_NONCE` when the `cold-nonce` context is set, else nothing.

    A changed environment value makes Lambda retire every warm environment, so the next
    invocation is a cold start. `make deploy COLD_NONCE=<n>` forces one per deploy; the
    cold-start measurements use it. Unset, the function is unchanged.
    """
    value = scope.node.try_get_context("cold-nonce")
    return {} if value is None else {"COLD_NONCE": str(value)}


def raw_retention_days(scope: Construct) -> str:
    """Return the `raw-retention-days` context as the commit function's value.

    An integer of at least 1, default 1 (the sandbox value). `make deploy
    RAW_RETENTION_DAYS=<n>` sets it; a real deployment records its value in its runbook.
    """
    value = scope.node.try_get_context("raw-retention-days")
    days = 1 if value is None else int(value)
    if days < 1:
        msg = f"raw-retention-days must be an integer of at least 1, got {value!r}"
        raise ValueError(msg)
    return str(days)


def build_function[S: NestedStack](stack: S, spec: FunctionSpec[S]) -> lambda_.Function:
    """Create `spec`'s function, name its role, add its queue, then statements."""
    environment = spec.env(stack)
    if spec.cold_nonce:
        environment |= cold_start_nonce(stack)
    memory_size = spec.memory_mb
    timeout = Duration.seconds(spec.timeout_seconds)
    if spec.image is None:
        fn = lambda_.Function(
            stack,
            spec.construct_id,
            runtime=lambda_.Runtime.PYTHON_3_14,
            architecture=lambda_.Architecture.ARM_64,
            handler=spec.handler,
            code=lambda_.Code.from_asset(
                str(INGEST_SRC),
                exclude=["**/__pycache__", "**/*.pyc"],
            ),
            memory_size=memory_size,
            timeout=timeout,
            environment=environment,
        )
    else:
        cmd = None if spec.image.cmd is None else [spec.image.cmd]
        fn = lambda_.DockerImageFunction(
            stack,
            spec.construct_id,
            code=lambda_.DockerImageCode.from_image_asset(
                str(REPO_ROOT),
                file=spec.image.dockerfile,
                platform=ecr_assets.Platform.LINUX_ARM64,
                cmd=cmd,
            ),
            architecture=lambda_.Architecture.ARM_64,
            memory_size=memory_size,
            timeout=timeout,
            environment=environment,
        )
    name_execution_role(fn, spec.role_name)
    # The event source mapping grants the consume actions; its statement comes first.
    if spec.queue is not None:
        fn.add_event_source(
            event_sources.SqsEventSource(spec.queue(stack), batch_size=BATCH_SIZE)
        )
    for statement in spec.statements(stack):
        fn.add_to_role_policy(statement)
    return fn


def _query_read(stack: ApiStack, grant: QueryGrant) -> list[iam.PolicyStatement]:
    """Return the shared Athena read over the stack's warehouse and results buckets."""
    inputs = stack.inputs
    return query_read_statements(
        stack,
        warehouse=inputs.warehouse_bucket,
        results=inputs.athena_results_bucket,
        grant=grant,
    )


def session_env(stack: ApiStack) -> dict[str, str]:
    """Return SessionFunction's environment."""
    inputs = stack.inputs
    return {
        "SESSIONS_TABLE": inputs.sessions_table.table_name,
        "OWNERS_TABLE": inputs.owners_table.table_name,
        "RAW_BUCKET": inputs.raw_bucket.bucket_name,
        "COMMIT_QUEUE_URL": inputs.commit_queue.queue_url,
    }


def session_statements(stack: ApiStack) -> list[iam.PolicyStatement]:
    """Return SessionApiRole's statements."""
    inputs = stack.inputs
    sessions_table, raw_bucket = inputs.sessions_table, inputs.raw_bucket
    return [
        iam.PolicyStatement(
            actions=["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem"],
            resources=[sessions_table.table_arn],
        ),
        # Dataset ownership: the create check reads and claims.
        iam.PolicyStatement(
            actions=["dynamodb:GetItem", "dynamodb:PutItem"],
            resources=[inputs.owners_table.table_arn],
        ),
        iam.PolicyStatement(
            actions=["dynamodb:Query"],
            resources=[f"{sessions_table.table_arn}/index/{OWNER_INDEX}"],
        ),
        iam.PolicyStatement(
            actions=["s3:PutObject"],
            resources=[raw_bucket.arn_for_objects("landing/*")],
        ),
        iam.PolicyStatement(
            actions=["s3:GetObject"],
            resources=[raw_bucket.arn_for_objects("raw/*")],
        ),
        iam.PolicyStatement(
            actions=["s3:ListBucket"],
            resources=[raw_bucket.bucket_arn],
            conditions={"StringLike": {"s3:prefix": ["raw/*"]}},
        ),
        # Finalize queues the commit; it no longer invokes the function.
        iam.PolicyStatement(
            actions=["sqs:SendMessage"],
            resources=[inputs.commit_queue.queue_arn],
        ),
    ]


def platform_env(stack: ApiStack) -> dict[str, str]:
    """Return PlatformFunction's environment, the PLATFORM_* values last."""
    inputs = stack.inputs
    return {
        "SITE_BUCKET": inputs.site_bucket.bucket_name,
        "ARTIFACTS_BUCKET": inputs.artifacts_bucket.bucket_name,
        "WEB_URL": inputs.web_url,
        "NOTES_TABLE": inputs.notes_table.table_name,
        **inputs.platform_env,
    }


def platform_statements(stack: ApiStack) -> list[iam.PolicyStatement]:
    """Return PlatformRole's statements: `handout/*` of the site bucket, artifacts/*.

    In the artifacts bucket it reads, writes and deletes `artifacts/*` (records and
    data) and `index/*` (the by-id, by-input and by-time index objects). On the notes
    table it may only PutItem: no read, so a researcher never reads another's note.
    """
    inputs = stack.inputs
    return [
        iam.PolicyStatement(
            actions=["s3:GetObject"],
            resources=[inputs.site_bucket.arn_for_objects(f"{HANDOUT_PREFIX}/*")],
        ),
        iam.PolicyStatement(
            actions=["s3:PutObject", "s3:GetObject", "s3:DeleteObject"],
            resources=[
                inputs.artifacts_bucket.arn_for_objects("artifacts/*"),
                inputs.artifacts_bucket.arn_for_objects("index/*"),
            ],
        ),
        iam.PolicyStatement(
            actions=["s3:ListBucket"],
            resources=[inputs.artifacts_bucket.bucket_arn],
        ),
        iam.PolicyStatement(
            actions=["dynamodb:PutItem"],
            resources=[inputs.notes_table.table_arn],
        ),
    ]


def catalog_env(stack: ApiStack) -> dict[str, str]:
    """Return CatalogFunction's environment."""
    return {
        "ATHENA_WORKGROUP": athena.WORKGROUP,
        "GLUE_DATABASE": schema.DATABASE,
        "RAW_BUCKET": stack.inputs.raw_bucket.bucket_name,
        "DERIVED_BUCKET": stack.inputs.derived_bucket.bucket_name,
    }


def catalog_statements(stack: ApiStack) -> list[iam.PolicyStatement]:
    """Return CatalogRole's statements: the Athena read, raw/*, dataset summaries."""
    derived = stack.inputs.derived_bucket
    return [
        *_query_read(
            stack,
            QueryGrant(
                tables=CATALOG_TABLES,
                glue_actions=GLUE_READ_ACTIONS,
                athena_actions=ATHENA_READ_ACTIONS,
            ),
        ),
        # the download route presigns a GET on one catalogued version of a
        # raw/ object; the same read ResearcherRole has (identity.py).
        iam.PolicyStatement(
            actions=["s3:GetObject", "s3:GetObjectVersion"],
            resources=[stack.inputs.raw_bucket.arn_for_objects("raw/*")],
        ),
        # GET /catalog/studies reads each dataset's stored summary, and no other
        # derived object; read-only (LfpBuildRole is the prefix's one writer).
        # ListBucket on the prefix makes a missing summary NoSuchKey, not
        # AccessDenied, so a dataset not yet summarised reads live without a warning.
        iam.PolicyStatement(
            actions=["s3:GetObject"],
            resources=[
                derived.arn_for_objects(
                    f"{DERIVED_SUMMARY_PREFIX}/*/{catalog_api.SUMMARY_FILE}"
                )
            ],
        ),
        iam.PolicyStatement(
            actions=["s3:ListBucket"],
            resources=[derived.bucket_arn],
            conditions={"StringLike": {"s3:prefix": [f"{DERIVED_SUMMARY_PREFIX}/*"]}},
        ),
    ]


def compute_env(stack: ApiStack) -> dict[str, str]:
    """Return ComputeFunction's environment."""
    inputs = stack.inputs
    return {
        "ATHENA_WORKGROUP": athena.WORKGROUP,
        "GLUE_DATABASE": schema.DATABASE,
        "RAW_BUCKET": inputs.raw_bucket.bucket_name,
        "DERIVED_BUCKET": inputs.derived_bucket.bucket_name,
        "LABELS_TABLE": inputs.labels_table.table_name,
    }


def compute_statements(stack: ApiStack) -> list[iam.PolicyStatement]:
    """Return ComputeRole's statements: tables, raw/*, lfp/*, summary/*, LabelsTable."""
    inputs = stack.inputs
    derived = (DERIVED_LFP_PREFIX, DERIVED_SUMMARY_PREFIX)
    return [
        *_query_read(
            stack,
            QueryGrant(
                tables=COMPUTE_TABLES,
                glue_actions=GLUE_READ_ACTIONS,
                athena_actions=ATHENA_READ_ACTIONS,
            ),
        ),
        # Ranged GETs of one catalogued version of a raw/ object (the fallback
        # and the `frames:` mode); the same read as CatalogRole.
        iam.PolicyStatement(
            actions=["s3:GetObject", "s3:GetObjectVersion"],
            resources=[inputs.raw_bucket.arn_for_objects("raw/*")],
        ),
        # The LFP indexes and overviews and the stored session summaries; read-only
        # (LfpBuildRole is their one writer). ListBucket on the same prefixes makes
        # S3 answer a missing key with NoSuchKey (404) instead of AccessDenied, which
        # is how the routes tell "not built yet" from a fault.
        iam.PolicyStatement(
            actions=["s3:GetObject"],
            resources=[
                inputs.derived_bucket.arn_for_objects(f"{p}/*") for p in derived
            ],
        ),
        iam.PolicyStatement(
            actions=["s3:ListBucket"],
            resources=[inputs.derived_bucket.bucket_arn],
            conditions={"StringLike": {"s3:prefix": [f"{p}/*" for p in derived]}},
        ),
        # The `label:` and `label_name:` aligns read label starts from LabelsTable;
        # read-only.
        iam.PolicyStatement(
            actions=["dynamodb:GetItem", "dynamodb:Query"],
            resources=[inputs.labels_table.table_arn],
        ),
    ]


def labels_env(stack: ApiStack) -> dict[str, str]:
    """Return LabelsFunction's environment."""
    return {
        "LABELS_TABLE": stack.inputs.labels_table.table_name,
        "ATHENA_WORKGROUP": athena.WORKGROUP,
        "GLUE_DATABASE": schema.DATABASE,
    }


def labels_statements(stack: ApiStack) -> list[iam.PolicyStatement]:
    """Return LabelsRole's statements: four tables (with snapshots), LabelsTable."""
    return [
        *_query_read(
            stack,
            QueryGrant(
                tables=(*LABELS_TABLES, *(f"{t}$snapshots" for t in LABELS_TABLES[1:])),
                glue_actions=GLUE_READ_ACTIONS,
                athena_actions=ATHENA_READ_ACTIONS,
            ),
        ),
        iam.PolicyStatement(
            actions=[
                "dynamodb:GetItem",
                "dynamodb:PutItem",
                "dynamodb:UpdateItem",
                "dynamodb:DeleteItem",
                "dynamodb:Query",
            ],
            resources=[stack.inputs.labels_table.table_arn],
        ),
    ]


def imports_env(stack: ApiStack) -> dict[str, str]:
    """Return ImportsFunction's environment."""
    inputs = stack.inputs
    return {
        "RUNS_TABLE": inputs.import_runs_table.table_name,
        "SESSIONS_TABLE": inputs.sessions_table.table_name,
        "IMPORT_PLAN_FUNCTION_NAME": inputs.import_plan_function.function_name,
    }


def imports_statements(stack: ApiStack) -> list[iam.PolicyStatement]:
    """Return ImportsRole's four statements."""
    inputs = stack.inputs
    runs = inputs.import_runs_table
    return [
        iam.PolicyStatement(
            actions=["lambda:InvokeFunction"],
            resources=[inputs.import_plan_function.function_arn],
        ),
        iam.PolicyStatement(
            actions=["dynamodb:GetItem", "dynamodb:PutItem"],
            resources=[runs.table_arn],
        ),
        iam.PolicyStatement(
            actions=["dynamodb:Query"],
            resources=[f"{runs.table_arn}/index/{RUNS_CALLER_INDEX}"],
        ),
        iam.PolicyStatement(
            actions=["dynamodb:BatchGetItem"],
            resources=[inputs.sessions_table.table_arn],
        ),
    ]


# The Api nested stack's functions, in construction order. A statement or env
# function may read only what earlier rows created. Integrations and routes are
# added in this order too, after the HttpApi.
API_FUNCTIONS: Final[tuple[FunctionSpec[ApiStack], ...]] = (
    FunctionSpec(
        construct_id="SessionFunction",
        handler="asteroidea_ingest.uploads_api.handler",
        role_name=config.SESSION_API_ROLE_NAME,
        memory_mb=MEMORY_MB,
        timeout_seconds=timing.API_TIMEOUT_SECONDS,
        env=session_env,
        statements=session_statements,
        integration_id="Integration",
    ),
    FunctionSpec(
        construct_id="PlatformFunction",
        handler="asteroidea_ingest.get_started_api.handler",
        role_name=config.PLATFORM_ROLE_NAME,
        memory_mb=MEMORY_MB,
        timeout_seconds=timing.API_TIMEOUT_SECONDS,
        env=platform_env,
        statements=platform_statements,
        integration_id="PlatformIntegration",
    ),
    FunctionSpec(
        construct_id="CatalogFunction",
        handler="asteroidea_ingest.catalog_api.handler",
        role_name=config.CATALOG_ROLE_NAME,
        memory_mb=MEMORY_MB,
        timeout_seconds=timing.CATALOG_TIMEOUT_SECONDS,
        env=catalog_env,
        statements=catalog_statements,
        integration_id="CatalogIntegration",
    ),
    FunctionSpec(
        construct_id="ComputeFunction",
        handler="asteroidea_ingest.compute_api.handler",
        role_name=config.COMPUTE_ROLE_NAME,
        memory_mb=COMPUTE_MEMORY_MB,
        timeout_seconds=timing.COMPUTE_TIMEOUT_SECONDS,
        env=compute_env,
        statements=compute_statements,
        image=Image(COMPUTE_DOCKERFILE),
        integration_id="ComputeIntegration",
    ),
    FunctionSpec(
        construct_id="LabelsFunction",
        handler="asteroidea_ingest.labels_api.handler",
        role_name=config.LABELS_ROLE_NAME,
        memory_mb=MEMORY_MB,
        timeout_seconds=timing.CATALOG_TIMEOUT_SECONDS,
        env=labels_env,
        statements=labels_statements,
        integration_id="LabelsIntegration",
    ),
    FunctionSpec(
        construct_id="ImportsFunction",
        handler="asteroidea_ingest.imports_api.handler",
        role_name=config.IMPORTS_ROLE_NAME,
        memory_mb=MEMORY_MB,
        timeout_seconds=timing.API_TIMEOUT_SECONDS,
        env=imports_env,
        statements=imports_statements,
        integration_id="ImportsIntegration",
    ),
)

# Ingest stack constants.
COMMIT_MEMORY_MB: Final = 2048
IMPORT_MEMORY_MB: Final = 1024
IMPORT_HANDLER: Final = "asteroidea_ingest.import_file.handler"
# ImportPlanFunction: the commit image with a CMD override, as
# ImportFunction; PyIceberg's "already imported" scan needs the commit image.
# Memory as ImportFunction: it reads one dataset's `s3_key` and `source_version`.
IMPORT_PLAN_HANDLER: Final = "asteroidea_ingest.import_plan.handler"
IMPORT_PLAN_MEMORY_MB: Final = 1024
# The one same-region archive bucket ImportFunction copies from (DANDI is
# fetched anonymously over HTTPS, so there is no dandiarchive grant).
OPENNEURO_OBJECTS: Final = "arn:aws:s3:::openneuro.org/*"
# The commit appends `assets` rows and `datasets` rows.
COMMIT_TABLES: Final = (schema.TABLE, schema.DATASETS)
# The nine tables ExtractFunction appends to, named one by one (no wildcard).
EXTRACT_TABLES: Final = (
    "subjects",
    "sessions",
    "asset_headers",
    "series",
    "electrodes",
    "units",
    "intervals",
    "unit_spikes",
    "events",
)
EXTRACT_MEMORY_MB: Final = 2048
# LFP index and overview builder: the compute image with a CMD
# override, fed by its own standard queue. Memory: a measured run showed a
# 279 MiB process peak and a 53 MiB overview build; the h5py open adds BlockFile's
# cache, at most 64 x 4 MiB = 256 MiB, freed before the overviews. About 590 MiB,
# under 1024 MB with 1.7x headroom.
LFP_BUILD_MEMORY_MB: Final = 1024
LFP_BUILD_HANDLER: Final = "asteroidea_ingest.lfp_build.handler"
DERIVED_LFP_PREFIX: Final = "lfp"
# Re-extract read/derive task: the extract image with a CMD override.
# Memory as ExtractFunction: it runs the same extractors over the same files.
DERIVE_HANDLER: Final = "asteroidea_ingest.extract.derive.handler"
DERIVE_MEMORY_MB: Final = EXTRACT_MEMORY_MB
DERIVED_ROWS_PREFIX: Final = "rows"


def _glue_arns(stack: IngestStack, tables: tuple[str, ...]) -> list[str]:
    """Return the Glue catalog, the database and each named table's ARN."""
    return [
        stack.format_arn(service="glue", resource="catalog"),
        stack.format_arn(
            service="glue",
            resource="database",
            resource_name=schema.DATABASE,
        ),
        *(
            stack.format_arn(
                service="glue",
                resource="table",
                resource_name=f"{schema.DATABASE}/{name}",
            )
            for name in tables
        ),
    ]


def commit_env(stack: IngestStack) -> dict[str, str]:
    """Return CommitFunction's environment (the builder adds COLD_NONCE)."""
    return {
        "SESSIONS_TABLE": stack.tables.sessions.table_name,
        "RAW_BUCKET": stack.buckets.raw.bucket_name,
        "EXTRACT_QUEUE_URL": stack.extract_queue.queue_url,
        "RAW_RETENTION_DAYS": raw_retention_days(stack),
    }


def commit_statements(stack: IngestStack) -> list[iam.PolicyStatement]:
    """Return CommitRole's statements, after the commit queue's mapping."""
    raw, warehouse = stack.buckets.raw, stack.buckets.warehouse
    return [
        iam.PolicyStatement(
            actions=["sqs:SendMessage"],
            resources=[stack.extract_queue.queue_arn],
        ),
        iam.PolicyStatement(
            actions=["dynamodb:GetItem", "dynamodb:UpdateItem"],
            resources=[stack.tables.sessions.table_arn],
        ),
        iam.PolicyStatement(
            actions=["s3:GetObject", "s3:GetObjectVersion"],
            resources=[raw.arn_for_objects("landing/*")],
        ),
        # Without ListBucket S3 answers 403, not 404, for a missing landing object
        # or (on a resume's adoption HEAD) a missing raw object.
        iam.PolicyStatement(
            actions=["s3:ListBucket"],
            resources=[raw.bucket_arn],
            conditions={"StringLike": {"s3:prefix": ["landing/*", "raw/*"]}},
        ),
        # A resume HEADs the raw key to adopt an earlier run's copy.
        iam.PolicyStatement(
            actions=["s3:GetObject"],
            resources=[raw.arn_for_objects("raw/*")],
        ),
        iam.PolicyStatement(
            actions=["s3:PutObject", "s3:DeleteObjectVersion"],
            resources=[raw.arn_for_objects("raw/*")],
        ),
        # Lock each verified raw version. No role gets
        # s3:BypassGovernanceRetention.
        iam.PolicyStatement(
            actions=["s3:PutObjectRetention", "s3:GetObjectRetention"],
            resources=[raw.arn_for_objects("raw/*")],
        ),
        iam.PolicyStatement(
            actions=["glue:GetDatabase", "glue:GetTable", "glue:UpdateTable"],
            resources=_glue_arns(stack, COMMIT_TABLES),
        ),
        iam.PolicyStatement(
            actions=["s3:PutObject", "s3:GetObject"],
            resources=[warehouse.arn_for_objects(f"{schema.ICEBERG_PREFIX}/*")],
        ),
        # PyIceberg HeadObjects the metadata key it is about to write; without
        # ListBucket S3 answers 403 for the missing key and the append fails.
        iam.PolicyStatement(
            actions=["s3:ListBucket"],
            resources=[warehouse.bucket_arn],
        ),
    ]


def import_env(stack: IngestStack) -> dict[str, str]:
    """Return ImportFunction's environment."""
    return {
        "SESSIONS_TABLE": stack.tables.sessions.table_name,
        "OWNERS_TABLE": stack.tables.owners.table_name,
        "RAW_BUCKET": stack.buckets.raw.bucket_name,
        "COMMIT_QUEUE_URL": stack.commit_queue.queue_url,
    }


def import_statements(stack: IngestStack) -> list[iam.PolicyStatement]:
    """Return ImportRole's statements: the archive read, landing/, sessions, owners."""
    raw = stack.buckets.raw
    return [
        iam.PolicyStatement(actions=["s3:GetObject"], resources=[OPENNEURO_OBJECTS]),
        iam.PolicyStatement(
            actions=["s3:PutObject"],
            resources=[raw.arn_for_objects("landing/*")],
        ),
        # The raw-key HEAD refusal; ListBucket makes a missing key a 404.
        iam.PolicyStatement(
            actions=["s3:GetObject"],
            resources=[raw.arn_for_objects("raw/*")],
        ),
        iam.PolicyStatement(
            actions=["s3:ListBucket"],
            resources=[raw.bucket_arn],
            conditions={"StringLike": {"s3:prefix": ["landing/*", "raw/*"]}},
        ),
        iam.PolicyStatement(
            actions=["dynamodb:PutItem", "dynamodb:GetItem", "dynamodb:UpdateItem"],
            resources=[stack.tables.sessions.table_arn],
        ),
        # The admin claim on a dataset name.
        iam.PolicyStatement(
            actions=["dynamodb:GetItem", "dynamodb:PutItem"],
            resources=[stack.tables.owners.table_arn],
        ),
        iam.PolicyStatement(
            actions=["sqs:SendMessage"],
            resources=[stack.commit_queue.queue_arn],
        ),
    ]


def import_plan_env(stack: IngestStack) -> dict[str, str]:
    """Return ImportPlanFunction's environment; ImportFunction is built first."""
    return {
        "RUNS_TABLE": stack.tables.import_runs.table_name,
        "IMPORT_FUNCTION_NAME": stack.functions["ImportFunction"].function_name,
    }


def import_plan_statements(stack: IngestStack) -> list[iam.PolicyStatement]:
    """Return ImportPlanRole's statements.

    It invokes ImportFunction, writes the runs table and reads the `assets` table for
    the "already imported" check; the archives are read anonymously over HTTPS (no
    VPC). Catalog writes stay behind the FIFOs.
    """
    return [
        iam.PolicyStatement(
            actions=["lambda:InvokeFunction"],
            resources=[stack.functions["ImportFunction"].function_arn],
        ),
        # `RunStore` writes the run with conditional UpdateItem calls only.
        iam.PolicyStatement(
            actions=["dynamodb:UpdateItem"],
            resources=[stack.tables.import_runs.table_arn],
        ),
        # PyIceberg's `load_table` of `assets` (GetTable only, no update).
        iam.PolicyStatement(
            actions=["glue:GetTable"],
            resources=_glue_arns(stack, (schema.TABLE,)),
        ),
        # The table's metadata, manifests and data files: read only.
        iam.PolicyStatement(
            actions=["s3:GetObject"],
            resources=[
                stack.buckets.warehouse.arn_for_objects(f"{schema.ICEBERG_PREFIX}/*"),
            ],
        ),
    ]


def extract_env(stack: IngestStack) -> dict[str, str]:
    """Return ExtractFunction's environment (the builder adds COLD_NONCE)."""
    return {
        "SESSIONS_TABLE": stack.tables.sessions.table_name,
        "RAW_BUCKET": stack.buckets.raw.bucket_name,
        "LFP_BUILD_QUEUE_URL": stack.lfp_build_queue.queue_url,
        "DERIVED_BUCKET": stack.buckets.derived.bucket_name,
    }


def extract_statements(stack: IngestStack) -> list[iam.PolicyStatement]:
    """Return ExtractRole's statements, after the extract queue's mapping."""
    raw, warehouse = stack.buckets.raw, stack.buckets.warehouse
    derived = stack.buckets.derived
    return [
        # The presigned GET URL carries the role's read of the raw version.
        iam.PolicyStatement(
            actions=["s3:GetObject", "s3:GetObjectVersion"],
            resources=[raw.arn_for_objects("raw/*")],
        ),
        iam.PolicyStatement(
            actions=["s3:ListBucket"],
            resources=[raw.bucket_arn],
            conditions={"StringLike": {"s3:prefix": ["raw/*"]}},
        ),
        iam.PolicyStatement(
            actions=["glue:GetDatabase", "glue:GetTable", "glue:UpdateTable"],
            resources=_glue_arns(stack, EXTRACT_TABLES),
        ),
        iam.PolicyStatement(
            actions=["s3:PutObject", "s3:GetObject"],
            resources=[warehouse.arn_for_objects(f"{schema.ICEBERG_PREFIX}/*")],
        ),
        iam.PolicyStatement(
            actions=["s3:ListBucket"],
            resources=[warehouse.bucket_arn],
        ),
        iam.PolicyStatement(
            actions=["dynamodb:GetItem", "dynamodb:UpdateItem"],
            resources=[stack.tables.sessions.table_arn],
        ),
        # One LFP build message per NWB extraction.
        iam.PolicyStatement(
            actions=["sqs:SendMessage"],
            resources=[stack.lfp_build_queue.queue_arn],
        ),
        # A rows-by-pointer message's manifest and bundle (read rows/
        # only). ListBucket on the prefix makes a missing manifest NoSuchKey,
        # not AccessDenied, so the FAILED message names the real cause.
        iam.PolicyStatement(
            actions=["s3:GetObject"],
            resources=[derived.arn_for_objects(f"{DERIVED_ROWS_PREFIX}/*")],
        ),
        iam.PolicyStatement(
            actions=["s3:ListBucket"],
            resources=[derived.bucket_arn],
            conditions={"StringLike": {"s3:prefix": [f"{DERIVED_ROWS_PREFIX}/*"]}},
        ),
    ]


def lfp_build_env(stack: IngestStack) -> dict[str, str]:
    """Return LfpBuildFunction's environment: the buckets, then the summary kinds'."""
    return {
        "RAW_BUCKET": stack.buckets.raw.bucket_name,
        "DERIVED_BUCKET": stack.buckets.derived.bucket_name,
        "ATHENA_WORKGROUP": athena.WORKGROUP,
        "GLUE_DATABASE": schema.DATABASE,
        "LFP_BUILD_QUEUE_URL": stack.lfp_build_queue.queue_url,
    }


def lfp_build_statements(stack: IngestStack) -> list[iam.PolicyStatement]:
    """Return LfpBuildRole's statements, after the LFP build queue's mapping.

    The LFP kind reads raw/ and writes lfp/. The summary kinds also read the catalog
    through Athena, read LFP manifests and the stored summary, write summary/ and
    send the per-session messages to this function's own queue.
    """
    derived = stack.buckets.derived
    prefixes = (DERIVED_LFP_PREFIX, DERIVED_SUMMARY_PREFIX)
    objects = [derived.arn_for_objects(f"{p}/*") for p in prefixes]
    return [
        # The size HEAD and the ranged GETs of one raw/ version.
        iam.PolicyStatement(
            actions=["s3:GetObject", "s3:GetObjectVersion"],
            resources=[stack.buckets.raw.arn_for_objects("raw/*")],
        ),
        # The derived store's lfp/ and summary/ writer; the bucket policy denies
        # others.
        iam.PolicyStatement(actions=["s3:PutObject"], resources=objects),
        # LFP manifests (the overview states) and the stored summary, read before
        # each conditional write.
        iam.PolicyStatement(actions=["s3:GetObject"], resources=objects),
        # ListBucket on the same prefixes makes a missing key NoSuchKey (404), not
        # AccessDenied, so an empty bucket reads "no summary yet".
        iam.PolicyStatement(
            actions=["s3:ListBucket"],
            resources=[derived.bucket_arn],
            conditions={"StringLike": {"s3:prefix": [f"{p}/*" for p in prefixes]}},
        ),
        # A dataset summary fans out one session message per session.
        iam.PolicyStatement(
            actions=["sqs:SendMessage"],
            resources=[stack.lfp_build_queue.queue_arn],
        ),
        *query_read_statements(
            stack,
            warehouse=stack.buckets.warehouse,
            results=stack.buckets.results,
            grant=QueryGrant(
                tables=SUMMARY_TABLES,
                glue_actions=GLUE_READ_ACTIONS,
                athena_actions=ATHENA_READ_ACTIONS,
            ),
        ),
    ]


def derive_env(stack: IngestStack) -> dict[str, str]:
    """Return DeriveFunction's environment."""
    return {
        "SESSIONS_TABLE": stack.tables.sessions.table_name,
        "RAW_BUCKET": stack.buckets.raw.bucket_name,
        "DERIVED_BUCKET": stack.buckets.derived.bucket_name,
    }


def derive_statements(stack: IngestStack) -> list[iam.PolicyStatement]:
    """Return DeriveRole's statements.

    It reads one raw version, writes that asset's row bundle and manifest under
    `rows/` and, on a failure, sets the tracking item FAILED. No catalog grant: the
    FIFO's handler commits.
    """
    raw = stack.buckets.raw
    return [
        # The presigned NWB URL and the ranged GETs of one raw version.
        iam.PolicyStatement(
            actions=["s3:GetObject", "s3:GetObjectVersion"],
            resources=[raw.arn_for_objects("raw/*")],
        ),
        iam.PolicyStatement(
            actions=["s3:ListBucket"],
            resources=[raw.bucket_arn],
            conditions={"StringLike": {"s3:prefix": ["raw/*"]}},
        ),
        # The rows/ writer; the bucket policy denies it every other key.
        iam.PolicyStatement(
            actions=["s3:PutObject"],
            resources=[
                stack.buckets.derived.arn_for_objects(f"{DERIVED_ROWS_PREFIX}/*")
            ],
        ),
        # `sessions.set_reextract`: the FAILED write (UpdateItem only).
        iam.PolicyStatement(
            actions=["dynamodb:UpdateItem"],
            resources=[stack.tables.sessions.table_arn],
        ),
    ]


def _commit_queue(stack: IngestStack) -> sqs.IQueue:
    """Return the commit FIFO: one group, batch size 1, one commit at a time."""
    return stack.commit_queue


def _extract_queue(stack: IngestStack) -> sqs.IQueue:
    """Return the extract FIFO: one group, batch size 1, one extraction at a time."""
    return stack.extract_queue


def _lfp_build_queue(stack: IngestStack) -> sqs.IQueue:
    """Return the LFP build queue: one file per invocation, one timeout per build."""
    return stack.lfp_build_queue


# The Ingest nested stack's functions, in construction order, after its queues. A
# statement or env function may read only what earlier rows (and the queues) created.
INGEST_FUNCTIONS: Final[tuple[FunctionSpec[IngestStack], ...]] = (
    FunctionSpec(
        construct_id="CommitFunction",
        handler="asteroidea_ingest.commit.handler",
        role_name=config.COMMIT_ROLE_NAME,
        memory_mb=COMMIT_MEMORY_MB,
        timeout_seconds=timing.COMMIT_TIMEOUT_SECONDS,
        env=commit_env,
        statements=commit_statements,
        image=Image(COMMIT_DOCKERFILE),
        queue=_commit_queue,
        cold_nonce=True,
    ),
    FunctionSpec(
        construct_id="ImportFunction",
        handler=IMPORT_HANDLER,
        role_name=config.IMPORT_ROLE_NAME,
        memory_mb=IMPORT_MEMORY_MB,
        timeout_seconds=timing.IMPORT_TIMEOUT_SECONDS,
        env=import_env,
        statements=import_statements,
        image=Image(COMMIT_DOCKERFILE, cmd=IMPORT_HANDLER),
    ),
    FunctionSpec(
        construct_id="ImportPlanFunction",
        handler=IMPORT_PLAN_HANDLER,
        role_name=config.IMPORT_PLAN_ROLE_NAME,
        memory_mb=IMPORT_PLAN_MEMORY_MB,
        timeout_seconds=timing.IMPORT_PLAN_TIMEOUT_SECONDS,
        env=import_plan_env,
        statements=import_plan_statements,
        image=Image(COMMIT_DOCKERFILE, cmd=IMPORT_PLAN_HANDLER),
    ),
    FunctionSpec(
        construct_id="ExtractFunction",
        handler="asteroidea_ingest.extract.handler.handler",
        role_name=config.EXTRACT_ROLE_NAME,
        memory_mb=EXTRACT_MEMORY_MB,
        timeout_seconds=timing.EXTRACT_TIMEOUT_SECONDS,
        env=extract_env,
        statements=extract_statements,
        image=Image(EXTRACT_DOCKERFILE),
        queue=_extract_queue,
        cold_nonce=True,
    ),
    FunctionSpec(
        construct_id="LfpBuildFunction",
        handler=LFP_BUILD_HANDLER,
        role_name=config.LFP_BUILD_ROLE_NAME,
        memory_mb=LFP_BUILD_MEMORY_MB,
        timeout_seconds=timing.LFP_BUILD_TIMEOUT_SECONDS,
        env=lfp_build_env,
        statements=lfp_build_statements,
        image=Image(COMPUTE_DOCKERFILE, cmd=LFP_BUILD_HANDLER),
        queue=_lfp_build_queue,
    ),
    FunctionSpec(
        construct_id="DeriveFunction",
        handler=DERIVE_HANDLER,
        role_name=config.DERIVE_ROLE_NAME,
        memory_mb=DERIVE_MEMORY_MB,
        timeout_seconds=timing.DERIVE_TIMEOUT_SECONDS,
        env=derive_env,
        statements=derive_statements,
        image=Image(EXTRACT_DOCKERFILE, cmd=DERIVE_HANDLER),
    ),
)
FUNCTIONS: Final[tuple[FunctionSpec[Any], ...]] = (*API_FUNCTIONS, *INGEST_FUNCTIONS)
