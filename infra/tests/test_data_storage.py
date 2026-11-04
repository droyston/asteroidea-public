"""The storage, table and catalog properties the design fixes."""

from typing import Any

import pytest
from aws_cdk.assertions import Match, Template

from asteroidea_infra.synth_asserts import flat

pytestmark = pytest.mark.synth

BUCKETS = (
    "RawBucket",
    "AthenaResultsBucket",
    "WarehouseBucket",
    "ArtifactsBucket",
    "DerivedBucket",
)


def find_bucket(template: Template, logical_prefix: str) -> tuple[str, dict[str, Any]]:
    """Return (logical id, properties) of the one bucket with this id prefix."""
    found = {
        k: v["Properties"]
        for k, v in template.find_resources("AWS::S3::Bucket").items()
        if k.startswith(logical_prefix)
    }
    assert len(found) == 1, found
    return next(iter(found.items()))


def _lifecycle(template: Template, prefix: str) -> list[dict[str, Any]]:
    _, props = find_bucket(template, prefix)
    return props.get("LifecycleConfiguration", {}).get("Rules", [])


class TestRawBucket:
    def test_versioned(self, data_template: Template) -> None:
        _, props = find_bucket(data_template, "RawBucket")
        assert props["VersioningConfiguration"] == {"Status": "Enabled"}

    def test_landing_lifecycle(self, data_template: Template) -> None:
        (rule,) = _lifecycle(data_template, "RawBucket")
        assert rule["Prefix"] == "landing/"
        assert rule["Status"] == "Enabled"
        assert rule["ExpirationInDays"] == 3
        assert rule["NoncurrentVersionExpiration"] == {"NoncurrentDays": 1}

    def test_object_lock_is_on_with_no_default_retention(
        self, data_template: Template
    ) -> None:
        """Lock on, no `Rule`, so `landing/` stays deletable."""
        _, props = find_bucket(data_template, "RawBucket")
        assert props["ObjectLockEnabled"] is True
        assert props["ObjectLockConfiguration"] == {"ObjectLockEnabled": "Enabled"}
        assert "Rule" not in props["ObjectLockConfiguration"]

    def test_cors_rule_lets_the_hosted_app_range_read(
        self, data_template: Template
    ) -> None:
        """One rule, GET and HEAD with `Range`, two origins."""
        _, props = find_bucket(data_template, "RawBucket")
        (rule,) = props["CorsConfiguration"]["CorsRules"]
        assert rule["AllowedMethods"] == ["GET", "HEAD"]
        assert rule["AllowedHeaders"] == ["Range"]
        assert rule["ExposedHeaders"] == [
            "Content-Range",
            "Accept-Ranges",
            "ETag",
            "Content-Length",
        ]
        assert rule["MaxAge"] == 3000
        assert "http://localhost:5173" in rule["AllowedOrigins"]
        assert len(rule["AllowedOrigins"]) == 2

    @pytest.mark.parametrize("prefix", ["AthenaResultsBucket", "WarehouseBucket"])
    def test_other_buckets_have_no_object_lock(
        self, data_template: Template, prefix: str
    ) -> None:
        _, props = find_bucket(data_template, prefix)
        assert "ObjectLockEnabled" not in props
        assert "ObjectLockConfiguration" not in props


class TestBucketHardening:
    @pytest.mark.parametrize("prefix", BUCKETS)
    def test_block_public_access(self, data_template: Template, prefix: str) -> None:
        _, props = find_bucket(data_template, prefix)
        assert props["PublicAccessBlockConfiguration"] == {
            "BlockPublicAcls": True,
            "BlockPublicPolicy": True,
            "IgnorePublicAcls": True,
            "RestrictPublicBuckets": True,
        }

    @pytest.mark.parametrize("prefix", BUCKETS)
    def test_sse_s3(self, data_template: Template, prefix: str) -> None:
        _, props = find_bucket(data_template, prefix)
        (rule,) = props["BucketEncryption"]["ServerSideEncryptionConfiguration"]
        assert rule["ServerSideEncryptionByDefault"] == {"SSEAlgorithm": "AES256"}

    @pytest.mark.parametrize("prefix", BUCKETS)
    def test_ssl_enforced(self, data_template: Template, prefix: str) -> None:
        logical, _ = find_bucket(data_template, prefix)
        data_template.has_resource_properties(
            "AWS::S3::BucketPolicy",
            {
                "Bucket": {"Ref": logical},
                "PolicyDocument": {
                    "Statement": Match.array_with(
                        [
                            Match.object_like(
                                {
                                    "Effect": "Deny",
                                    "Condition": {
                                        "Bool": {"aws:SecureTransport": "false"},
                                    },
                                },
                            ),
                        ],
                    ),
                },
            },
        )


class TestDerivedBucket:
    """The derived store; LfpBuildRole `lfp/*`, DeriveRole `rows/*`."""

    def test_unversioned_retained_no_lock_rows_expire_in_seven_days(
        self, data_template: Template
    ) -> None:
        logical, props = find_bucket(data_template, "DerivedBucket")
        assert "VersioningConfiguration" not in props  # rebuilt by re-extract
        assert "ObjectLockEnabled" not in props
        # rows/ bundles are needed only until their commit; lfp/ never expires.
        assert props["LifecycleConfiguration"] == {
            "Rules": [
                {
                    "ExpirationInDays": 7,
                    "Id": "rows-expiry",
                    "Prefix": "rows/",
                    "Status": "Enabled",
                }
            ]
        }
        resource = data_template.to_json()["Resources"][logical]
        assert resource["DeletionPolicy"] == "Retain"
        assert "DerivedBucketName" in data_template.to_json()["Outputs"]

    def test_cors_lets_the_hosted_app_get_a_presigned_overview(
        self, data_template: Template
    ) -> None:
        """One GET-only rule with two origins (no Range, no write method)."""
        _, props = find_bucket(data_template, "DerivedBucket")
        (rule,) = props["CorsConfiguration"]["CorsRules"]
        assert rule["AllowedMethods"] == ["GET"]
        assert "AllowedHeaders" not in rule
        assert len(rule["AllowedOrigins"]) == 2
        assert "http://localhost:5173" in rule["AllowedOrigins"]

    def test_two_platform_writers_each_limited_to_its_own_prefix(
        self, data_template: Template
    ) -> None:
        """Lfp/*, summary/* LfpBuildRole; rows/* DeriveRole; no one."""
        (statements,) = [
            v["Properties"]["PolicyDocument"]["Statement"]
            for k, v in data_template.find_resources("AWS::S3::BucketPolicy").items()
            if k.startswith("DerivedBucket")
        ]
        writes = ["s3:PutObject*", "s3:DeleteObject*"]
        # Only Deny statements: the policy grants nothing to anyone.
        assert {s["Effect"] for s in statements} == {"Deny"}
        by_sid = {s["Sid"]: s for s in statements if "Sid" in s}
        assert set(by_sid) == {
            "DenyDerivedLfpWritesExceptItsWriter",
            "DenyDerivedRowsWritesExceptItsWriter",
            "DenyDerivedSummaryWritesExceptItsWriter",
            "DenyDerivedWritesOutsideTheWriterPrefixes",
        }
        writers: set[str] = set()
        for sid, prefix, role in (
            ("DenyDerivedLfpWritesExceptItsWriter", "lfp", "lfp-build"),
            ("DenyDerivedRowsWritesExceptItsWriter", "rows", "derive"),
            ("DenyDerivedSummaryWritesExceptItsWriter", "summary", "lfp-build"),
        ):
            deny = by_sid[sid]
            assert deny["Principal"] == {"AWS": "*"}
            assert deny["Action"] == writes
            assert flat(deny["Resource"]).endswith(f"/{prefix}/*"), sid
            (writer,) = deny["Condition"]["ArnNotEquals"]["aws:PrincipalArn"]
            assert flat(writer).endswith(f":role/asteroidea-{role}-role"), sid
            writers.add(flat(writer))
        assert len(writers) == 2  # still two platform writers
        outside = by_sid["DenyDerivedWritesOutsideTheWriterPrefixes"]
        assert outside["Principal"] == {"AWS": "*"}
        assert outside["Action"] == writes
        assert "Condition" not in outside  # no writer outside the three prefixes
        assert "Resource" not in outside
        lfp, rows, summary = (flat(r) for r in outside["NotResource"])
        assert lfp.endswith("/lfp/*")
        assert rows.endswith("/rows/*")
        assert summary.endswith("/summary/*")

    def test_no_data_stack_role_is_granted_the_derived_bucket(
        self, data_template: Template
    ) -> None:
        """ResearcherRole (and every DataStack role) has no grant."""
        for policy in data_template.find_resources("AWS::IAM::Policy").values():
            assert "DerivedBucket" not in str(policy["Properties"]["PolicyDocument"])
        for role in data_template.find_resources("AWS::IAM::Role").values():
            assert "DerivedBucket" not in str(role["Properties"].get("Policies", []))


class TestAthenaResultsAndWarehouse:
    def test_results_expire_in_one_day(self, data_template: Template) -> None:
        (rule,) = _lifecycle(data_template, "AthenaResultsBucket")
        assert rule["Prefix"] == "athena/"
        assert rule["ExpirationInDays"] == 1

    def test_warehouse_has_no_lifecycle(self, data_template: Template) -> None:
        assert _lifecycle(data_template, "WarehouseBucket") == []


class TestSessionsTable:
    def test_key_ttl_pitr(self, data_template: Template) -> None:
        data_template.has_resource_properties(
            "AWS::DynamoDB::Table",
            {
                "KeySchema": [{"AttributeName": "session_id", "KeyType": "HASH"}],
                "BillingMode": "PAY_PER_REQUEST",
                "TimeToLiveSpecification": {
                    "AttributeName": "expires_at",
                    "Enabled": True,
                },
                "PointInTimeRecoverySpecification": {
                    "PointInTimeRecoveryEnabled": True,
                },
            },
        )

    def test_owner_index_serves_the_uploads_listing(
        self, data_template: Template
    ) -> None:
        """GSI `by_owner` is owner_sub (S), created_at (N), projection ALL."""
        data_template.has_resource_properties(
            "AWS::DynamoDB::Table",
            {
                "AttributeDefinitions": Match.array_with(
                    [
                        {"AttributeName": "owner_sub", "AttributeType": "S"},
                        {"AttributeName": "created_at", "AttributeType": "N"},
                    ]
                ),
                "GlobalSecondaryIndexes": [
                    {
                        "IndexName": "by_owner",
                        "KeySchema": [
                            {"AttributeName": "owner_sub", "KeyType": "HASH"},
                            {"AttributeName": "created_at", "KeyType": "RANGE"},
                        ],
                        "Projection": {"ProjectionType": "ALL"},
                    }
                ],
            },
        )

    def test_the_table_is_still_retained(self, data_template: Template) -> None:
        tables = data_template.find_resources("AWS::DynamoDB::Table")
        (table,) = [t for lid, t in tables.items() if lid.startswith("SessionsTable")]
        assert table["DeletionPolicy"] == "Retain"


class TestDatasetOwnersTable:
    """Its own table (the sessions GSI would list owner items)."""

    def test_key_billing_pitr(self, data_template: Template) -> None:
        data_template.has_resource_properties(
            "AWS::DynamoDB::Table",
            {
                "KeySchema": [{"AttributeName": "dataset", "KeyType": "HASH"}],
                "AttributeDefinitions": [
                    {"AttributeName": "dataset", "AttributeType": "S"}
                ],
                "BillingMode": "PAY_PER_REQUEST",
                "PointInTimeRecoverySpecification": {
                    "PointInTimeRecoveryEnabled": True,
                },
            },
        )

    def test_retained_with_no_index_and_no_ttl(self, data_template: Template) -> None:
        tables = data_template.find_resources("AWS::DynamoDB::Table")
        (owners,) = [t for lid, t in tables.items() if lid.startswith("DatasetOwners")]
        assert owners["DeletionPolicy"] == "Retain"
        assert owners["UpdateReplacePolicy"] == "Retain"
        assert "GlobalSecondaryIndexes" not in owners["Properties"]
        assert "TimeToLiveSpecification" not in owners["Properties"]


class TestLabelsTable:
    """Researchers' labels in their own table, outside the catalog."""

    def test_keys_billing_pitr(self, data_template: Template) -> None:
        data_template.has_resource_properties(
            "AWS::DynamoDB::Table",
            {
                "KeySchema": [
                    {"AttributeName": "asset_id", "KeyType": "HASH"},
                    {"AttributeName": "label_id", "KeyType": "RANGE"},
                ],
                "AttributeDefinitions": [
                    {"AttributeName": "asset_id", "AttributeType": "S"},
                    {"AttributeName": "label_id", "AttributeType": "S"},
                ],
                "BillingMode": "PAY_PER_REQUEST",
                "PointInTimeRecoverySpecification": {
                    "PointInTimeRecoveryEnabled": True,
                },
            },
        )

    def test_retained_with_no_index_and_no_ttl(self, data_template: Template) -> None:
        tables = data_template.find_resources("AWS::DynamoDB::Table")
        (labels,) = [t for lid, t in tables.items() if lid.startswith("LabelsTable")]
        assert labels["DeletionPolicy"] == "Retain"
        assert labels["UpdateReplacePolicy"] == "Retain"
        assert "GlobalSecondaryIndexes" not in labels["Properties"]
        assert "TimeToLiveSpecification" not in labels["Properties"]

    def test_the_stack_holds_exactly_five_tables(self, data_template: Template) -> None:
        # Sessions, owners, labels, import runs and notes.
        data_template.resource_count_is("AWS::DynamoDB::Table", 5)


class TestNotesTable:
    """App-dev notes, PK `note_id`, on demand, PITR, retained, no index."""

    def test_key_billing_pitr_and_no_index_or_ttl(
        self, data_template: Template
    ) -> None:
        tables = data_template.find_resources("AWS::DynamoDB::Table")
        (notes,) = [t for lid, t in tables.items() if lid.startswith("NotesTable")]
        props = notes["Properties"]
        assert props["KeySchema"] == [{"AttributeName": "note_id", "KeyType": "HASH"}]
        assert props["AttributeDefinitions"] == [
            {"AttributeName": "note_id", "AttributeType": "S"}
        ]
        assert props["BillingMode"] == "PAY_PER_REQUEST"
        assert props["PointInTimeRecoverySpecification"] == {
            "PointInTimeRecoveryEnabled": True
        }
        assert "GlobalSecondaryIndexes" not in props
        assert "TimeToLiveSpecification" not in props
        assert notes["DeletionPolicy"] == "Retain"
        assert notes["UpdateReplacePolicy"] == "Retain"


class TestImportRunsTable:
    """Web import runs, PK `run_id`, GSI `by_caller`."""

    def test_key_billing_pitr_and_caller_index(self, data_template: Template) -> None:
        tables = data_template.find_resources("AWS::DynamoDB::Table")
        (runs,) = [t for lid, t in tables.items() if lid.startswith("ImportRunsTable")]
        props = runs["Properties"]
        assert props["KeySchema"] == [{"AttributeName": "run_id", "KeyType": "HASH"}]
        assert props["AttributeDefinitions"] == [
            {"AttributeName": "run_id", "AttributeType": "S"},
            {"AttributeName": "caller_sub", "AttributeType": "S"},
            {"AttributeName": "created_at", "AttributeType": "N"},
        ]
        assert props["BillingMode"] == "PAY_PER_REQUEST"
        assert props["PointInTimeRecoverySpecification"] == {
            "PointInTimeRecoveryEnabled": True
        }
        assert props["GlobalSecondaryIndexes"] == [
            {
                "IndexName": "by_caller",
                "KeySchema": [
                    {"AttributeName": "caller_sub", "KeyType": "HASH"},
                    {"AttributeName": "created_at", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }
        ]

    def test_retained_with_no_ttl(self, data_template: Template) -> None:
        tables = data_template.find_resources("AWS::DynamoDB::Table")
        (runs,) = [t for lid, t in tables.items() if lid.startswith("ImportRunsTable")]
        assert runs["DeletionPolicy"] == "Retain"
        assert runs["UpdateReplacePolicy"] == "Retain"
        assert "TimeToLiveSpecification" not in runs["Properties"]


class TestCatalog:
    """`assets` is not in the stack (CFN rejected it); see test_catalog_schema."""

    def test_database(self, data_template: Template) -> None:
        data_template.has_resource_properties(
            "AWS::Glue::Database",
            {"DatabaseInput": {"Name": "asteroidea"}},
        )

    def test_workgroup_results_location(self, data_template: Template) -> None:
        data_template.has_resource_properties(
            "AWS::Athena::WorkGroup",
            {
                "Name": "asteroidea",
                "WorkGroupConfiguration": Match.object_like(
                    {
                        "EnforceWorkGroupConfiguration": True,
                        "ResultConfiguration": Match.object_like(
                            {"OutputLocation": Match.any_value()},
                        ),
                    },
                ),
            },
        )
