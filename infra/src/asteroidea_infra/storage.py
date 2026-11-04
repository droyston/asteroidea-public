"""Buckets (SSE-S3, no public access, SSL); sessions, owners, labels, runs, notes."""

from typing import TYPE_CHECKING

from aws_cdk import Duration, RemovalPolicy
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_iam as iam
from aws_cdk import aws_s3 as s3

if TYPE_CHECKING:
    from collections.abc import Sequence

    from constructs import Construct

TTL_ATTRIBUTE = "expires_at"
PARTITION_KEY = "session_id"
OWNER_INDEX = "by_owner"
OWNERS_PARTITION_KEY = "dataset"
LABELS_PARTITION_KEY = "asset_id"
LABELS_SORT_KEY = "label_id"
RUNS_PARTITION_KEY = "run_id"
RUNS_CALLER_INDEX = "by_caller"
NOTES_PARTITION_KEY = "note_id"
WEB_DEV_ORIGIN = "http://localhost:5173"  # `make web` (the registered callback)


def _bucket(
    scope: Construct,
    construct_id: str,
    *,
    versioned: bool = False,
    lifecycle_rules: list[s3.LifecycleRule] | None = None,
    object_lock: bool = False,
) -> s3.Bucket:
    return s3.Bucket(
        scope,
        construct_id,
        versioned=versioned,
        block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
        encryption=s3.BucketEncryption.S3_MANAGED,
        enforce_ssl=True,
        lifecycle_rules=lifecycle_rules,
        object_lock_enabled=object_lock or None,
        removal_policy=RemovalPolicy.RETAIN,
    )


def raw_bucket(scope: Construct, *, web_url: str) -> s3.Bucket:
    """Versioned raw bucket with Object Lock; `landing/` expires in 3 d, noncurrent 1 d.

    CDK 2.274.0 emits `ObjectLockConfiguration` only with a default retention
    (`parseObjectLockConfig`, `aws-s3/lib/bucket.js`), so the property is set by
    override with no rule. Turning Object Lock on this way works for an existing
    versioned bucket, without replacement. With no default retention
    `landing/` stays deletable; the commit Lambda locks each `raw/` version.

    One CORS rule: the hosted app and the dev server may GET and HEAD with a `Range`
    header, so the browser reads a presigned URL directly.
    """
    bucket = _bucket(
        scope,
        "RawBucket",
        versioned=True,
        lifecycle_rules=[
            s3.LifecycleRule(
                id="landing-expiry",
                prefix="landing/",
                expiration=Duration.days(3),
                noncurrent_version_expiration=Duration.days(1),
                abort_incomplete_multipart_upload_after=Duration.days(1),
            ),
        ],
        object_lock=True,
    )
    bucket.add_cors_rule(
        allowed_methods=[s3.HttpMethods.GET, s3.HttpMethods.HEAD],
        allowed_origins=[web_url, WEB_DEV_ORIGIN],
        allowed_headers=["Range"],
        exposed_headers=["Content-Range", "Accept-Ranges", "ETag", "Content-Length"],
        max_age=3000,
    )
    cfn = bucket.node.default_child
    if not isinstance(cfn, s3.CfnBucket):  # pragma: no cover
        msg = "RawBucket has no CfnBucket default child"
        raise TypeError(msg)
    cfn.add_property_override(
        "ObjectLockConfiguration", {"ObjectLockEnabled": "Enabled"}
    )
    return bucket


def deny_raw_writes_except(
    bucket: s3.IBucket,
    *,
    writers: Sequence[str],
    deleter: str,
) -> None:
    """Add the principal-deny statements to the raw bucket policy.

    - Deny `s3:PutObject*` and `s3:RestoreObject` unless the principal is one of the
      `writers` (SessionApiRole by presigned PUT, CommitRole).
    - Deny `s3:DeleteObject*` unless the principal is `deleter` (CommitRole, which
      deletes only to roll back a failed commit).

    Principals are role ARNs, matched on `aws:PrincipalArn`, which is the role ARN for
    an assumed role and for a presigned URL signed by one. The non-TLS deny is the
    bucket's `enforce_ssl`. This is defence in depth behind ResearcherRole's own deny.
    """
    _deny_objects_except(
        bucket,
        sid="DenyRawWritesExceptSessionApiAndCommit",
        actions=["s3:PutObject*", "s3:RestoreObject"],
        principals=writers,
    )
    _deny_objects_except(
        bucket,
        sid="DenyRawDeletesExceptCommit",
        actions=["s3:DeleteObject*"],
        principals=[deleter],
    )


def _deny_objects_except(
    bucket: s3.IBucket,
    *,
    sid: str,
    actions: list[str],
    principals: Sequence[str],
) -> None:
    """Deny `actions` on every object unless the principal ARN is in `principals`."""
    statement = iam.PolicyStatement(
        sid=sid,
        effect=iam.Effect.DENY,
        actions=actions,
        resources=[bucket.arn_for_objects("*")],
        conditions={"ArnNotEquals": {"aws:PrincipalArn": list(principals)}},
    )
    statement.add_any_principal()
    bucket.add_to_resource_policy(statement)


def artifacts_bucket(scope: Construct, *, platform_role: str) -> s3.Bucket:
    """Researcher artifacts: only PlatformRole may read, write or delete.

    Unversioned; incomplete multipart uploads abort after 1 d. A presigned
    URL signed by PlatformRole carries the author check made at signing time.
    """
    bucket = _bucket(
        scope,
        "ArtifactsBucket",
        lifecycle_rules=[
            s3.LifecycleRule(
                id="abort-incomplete-multipart",
                abort_incomplete_multipart_upload_after=Duration.days(1),
            ),
        ],
    )
    _deny_objects_except(
        bucket,
        sid="DenyArtifactAccessExceptPlatform",
        actions=["s3:GetObject*", "s3:PutObject*", "s3:DeleteObject*"],
        principals=[platform_role],
    )
    return bucket


DERIVED_LFP_PREFIX = "lfp"
DERIVED_ROWS_PREFIX = "rows"
DERIVED_SUMMARY_PREFIX = "summary"
DERIVED_ROWS_EXPIRY_DAYS = 7  # needed only until their commit
DERIVED_WRITE_ACTIONS = ["s3:PutObject*", "s3:DeleteObject*"]


def derived_bucket(
    scope: Construct, *, lfp_writer: str, rows_writer: str, web_url: str
) -> s3.Bucket:
    """Platform-derived data: `lfp/` builds, `rows/` bundles and `summary/` objects.

    Keys `lfp/<asset_id>/<s3_version_id>/<series_slug>/{index.bin,overview.bin}` and
    one `manifest.json` per asset version; `rows/<asset_id>/<run_id>/` holds one
    re-extract row bundle and its manifest, which expire after 7 days;
    `summary/<dataset>/_dataset.json` and
    `summary/<dataset>/<subject>/<session>/<digest>/...` with `latest.json` hold the
    dataset and session summaries. Unversioned: every object is rebuilt from its raw
    version by re-extract. Two platform writers, each limited to its own prefixes by
    this policy: `lfp_writer` (LfpBuildRole) on `lfp/*` and `summary/*`,
    `rows_writer` (DeriveRole) on `rows/*`; every other key has no writer.
    ComputeRole reads `lfp/*` and ExtractRole `rows/*` by IAM. No researcher grant:
    researchers read through the API. One CORS rule: the hosted app
    and the dev server may GET a presigned overview object (`/compute/overview`).
    """
    bucket = _bucket(
        scope,
        "DerivedBucket",
        lifecycle_rules=[
            s3.LifecycleRule(
                id="rows-expiry",
                prefix=f"{DERIVED_ROWS_PREFIX}/",
                expiration=Duration.days(DERIVED_ROWS_EXPIRY_DAYS),
            ),
        ],
    )
    bucket.add_cors_rule(
        allowed_methods=[s3.HttpMethods.GET],
        allowed_origins=[web_url, WEB_DEV_ORIGIN],
        exposed_headers=["Content-Length", "ETag"],
        max_age=3000,
    )
    prefixes = {
        DERIVED_LFP_PREFIX: lfp_writer,
        DERIVED_ROWS_PREFIX: rows_writer,
        DERIVED_SUMMARY_PREFIX: lfp_writer,
    }
    for prefix, writer in prefixes.items():
        statement = iam.PolicyStatement(
            sid=f"DenyDerived{prefix.capitalize()}WritesExceptItsWriter",
            effect=iam.Effect.DENY,
            actions=DERIVED_WRITE_ACTIONS,
            resources=[bucket.arn_for_objects(f"{prefix}/*")],
            conditions={"ArnNotEquals": {"aws:PrincipalArn": [writer]}},
        )
        statement.add_any_principal()
        bucket.add_to_resource_policy(statement)
    outside = iam.PolicyStatement(
        sid="DenyDerivedWritesOutsideTheWriterPrefixes",
        effect=iam.Effect.DENY,
        actions=DERIVED_WRITE_ACTIONS,
        not_resources=[bucket.arn_for_objects(f"{p}/*") for p in prefixes],
    )
    outside.add_any_principal()
    bucket.add_to_resource_policy(outside)
    return bucket


def athena_results_bucket(scope: Construct) -> s3.Bucket:
    """Athena query results; everything under `athena/` expires after 1 d."""
    return _bucket(
        scope,
        "AthenaResultsBucket",
        lifecycle_rules=[
            s3.LifecycleRule(
                id="athena-expiry",
                prefix="athena/",
                expiration=Duration.days(1),
            ),
        ],
    )


def warehouse_bucket(scope: Construct) -> s3.Bucket:
    """Iceberg data and metadata under `iceberg/`; no lifecycle."""
    return _bucket(scope, "WarehouseBucket")


def sessions_table(scope: Construct) -> dynamodb.Table:
    """On-demand sessions table: PK `session_id`, TTL `expires_at`, PITR on.

    The GSI `by_owner` (`owner_sub` S, `created_at` N, projection ALL) serves
    GET /uploads. Both keys exist on every session item: no
    backfill. A re-extraction tracking item (
    `reextract:<run_id>:<asset_id>`) carries neither: the GSI is sparse, so
    `GET /uploads` never lists it, and it uses `reextract_state`, never `state` or
    `extract_state`, so `make reclaim` ignores it.
    """
    table = dynamodb.Table(
        scope,
        "SessionsTable",
        partition_key=dynamodb.Attribute(
            name=PARTITION_KEY,
            type=dynamodb.AttributeType.STRING,
        ),
        billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
        time_to_live_attribute=TTL_ATTRIBUTE,
        point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
            point_in_time_recovery_enabled=True,
        ),
        removal_policy=RemovalPolicy.RETAIN,
    )
    table.add_global_secondary_index(
        index_name=OWNER_INDEX,
        partition_key=dynamodb.Attribute(
            name="owner_sub",
            type=dynamodb.AttributeType.STRING,
        ),
        sort_key=dynamodb.Attribute(
            name="created_at",
            type=dynamodb.AttributeType.NUMBER,
        ),
        projection_type=dynamodb.ProjectionType.ALL,
    )
    return table


def dataset_owners_table(scope: Construct) -> dynamodb.Table:
    """On-demand dataset owners table: PK `dataset`, PITR on, RETAIN.

    Its own table, not the sessions table: the sessions GSI `by_owner` would list an
    owner item in GET /uploads.
    """
    return dynamodb.Table(
        scope,
        "DatasetOwnersTable",
        partition_key=dynamodb.Attribute(
            name=OWNERS_PARTITION_KEY,
            type=dynamodb.AttributeType.STRING,
        ),
        billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
        point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
            point_in_time_recovery_enabled=True,
        ),
        removal_policy=RemovalPolicy.RETAIN,
    )


def labels_table(scope: Construct) -> dynamodb.Table:
    """On-demand labels table: PK `asset_id`, SK `label_id`, PITR on, RETAIN.

    Researchers' labels, outside the catalog. No index, no
    TTL: a label is read by asset and removed only by its author.
    """
    return dynamodb.Table(
        scope,
        "LabelsTable",
        partition_key=dynamodb.Attribute(
            name=LABELS_PARTITION_KEY,
            type=dynamodb.AttributeType.STRING,
        ),
        sort_key=dynamodb.Attribute(
            name=LABELS_SORT_KEY,
            type=dynamodb.AttributeType.STRING,
        ),
        billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
        point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
            point_in_time_recovery_enabled=True,
        ),
        removal_policy=RemovalPolicy.RETAIN,
    )


def import_runs_table(scope: Construct) -> dynamodb.Table:
    """On-demand import-runs table: PK `run_id`, PITR on, RETAIN.

    One item per web import run: `source`, `include`, `caller_sub`, `state`
    (PLANNING, RUNNING, DONE, REFUSED), `refusal`, `files` [{relpath, session_id}],
    `created_at`, `updated_at`. The GSI `by_caller` (`caller_sub` S, `created_at` N,
    projection ALL) serves a curator's last runs. No TTL: a run is a record.
    """
    table = dynamodb.Table(
        scope,
        "ImportRunsTable",
        partition_key=dynamodb.Attribute(
            name=RUNS_PARTITION_KEY,
            type=dynamodb.AttributeType.STRING,
        ),
        billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
        point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
            point_in_time_recovery_enabled=True,
        ),
        removal_policy=RemovalPolicy.RETAIN,
    )
    table.add_global_secondary_index(
        index_name=RUNS_CALLER_INDEX,
        partition_key=dynamodb.Attribute(
            name="caller_sub",
            type=dynamodb.AttributeType.STRING,
        ),
        sort_key=dynamodb.Attribute(
            name="created_at",
            type=dynamodb.AttributeType.NUMBER,
        ),
        projection_type=dynamodb.ProjectionType.ALL,
    )
    return table


def notes_table(scope: Construct) -> dynamodb.Table:
    """On-demand notes table: PK `note_id`, PITR on, RETAIN; no index, no TTL.

    App-dev feedback notes from the web app (`POST /notes`). An item holds
    `created_at` N, `author_sub`, `author_email` (or `author_email_missing`),
    `status` (`open` or `done`), `text`, `url` and `view` (compact JSON text).
    `make notes` scans it and sorts the open notes newest first: the table stays
    small, so no index. Only PlatformRole writes it, with PutItem.
    """
    return dynamodb.Table(
        scope,
        "NotesTable",
        partition_key=dynamodb.Attribute(
            name=NOTES_PARTITION_KEY,
            type=dynamodb.AttributeType.STRING,
        ),
        billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
        point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
            point_in_time_recovery_enabled=True,
        ),
        removal_policy=RemovalPolicy.RETAIN,
    )
