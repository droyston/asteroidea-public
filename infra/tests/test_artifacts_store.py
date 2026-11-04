"""The artifacts bucket and who may touch it."""

from typing import Any, Final

import pytest
from aws_cdk import App, Environment
from aws_cdk.assertions import Template

from asteroidea_infra.app import ServicesStack
from asteroidea_infra.data_stack import DataStack
from asteroidea_infra.synth_asserts import flat

pytestmark = pytest.mark.synth

ACCOUNT: Final = "000000000000"
PLATFORM: Final = f"arn:aws:iam::{ACCOUNT}:role/asteroidea-platform-role"
SID: Final = "DenyArtifactAccessExceptPlatform"
ALLOWED: Final = {"s3:PutObject", "s3:GetObject", "s3:DeleteObject"}


@pytest.fixture(scope="module")
def templates() -> dict[str, Template]:
    app = App(context={"sandbox-test": "true"})
    env = Environment(account=ACCOUNT, region="us-east-1")
    data = DataStack(app, "DataStack", env=env)
    services = ServicesStack(app, "ServicesStack", env=env, data=data)
    return {
        "data": Template.from_stack(data),
        "ingest": Template.from_stack(services.ingest),
        "api": Template.from_stack(services.api),
    }


def _bucket(templates: dict[str, Template]) -> tuple[str, dict[str, Any]]:
    (found,) = [
        (k, v["Properties"])
        for k, v in templates["data"].find_resources("AWS::S3::Bucket").items()
        if k.startswith("ArtifactsBucket")
    ]
    return found


def test_bucket_settings(templates: dict[str, Template]) -> None:
    """Block-all, SSE-S3 and SSL are in test_data_storage; the rest is here."""
    key, props = _bucket(templates)
    resource = templates["data"].to_json()["Resources"][key]
    assert resource["DeletionPolicy"] == "Retain"
    assert resource["UpdateReplacePolicy"] == "Retain"
    assert "VersioningConfiguration" not in props
    assert "ObjectLockConfiguration" not in props
    (rule,) = props["LifecycleConfiguration"]["Rules"]
    assert rule["AbortIncompleteMultipartUpload"] == {"DaysAfterInitiation": 1}
    assert rule["Status"] == "Enabled"
    assert "ExpirationInDays" not in rule


def test_bucket_name_is_output(templates: dict[str, Template]) -> None:
    assert "ArtifactsBucketName" in templates["data"].to_json()["Outputs"]


def test_deny_statement_names_platform_role_only(
    templates: dict[str, Template],
) -> None:
    (policy,) = [
        v["Properties"]["PolicyDocument"]["Statement"]
        for k, v in templates["data"].find_resources("AWS::S3::BucketPolicy").items()
        if k.startswith("ArtifactsBucket")
    ]
    (deny,) = [s for s in policy if s.get("Sid") == SID]
    assert deny["Effect"] == "Deny"
    assert deny["Principal"] == {"AWS": "*"}
    assert deny["Action"] == ["s3:GetObject*", "s3:PutObject*", "s3:DeleteObject*"]
    assert flat(deny["Condition"]["ArnNotEquals"]["aws:PrincipalArn"]) == [PLATFORM]
    assert set(deny["Condition"]) == {"ArnNotEquals"}


def test_only_platform_role_gains_artifacts_access(
    templates: dict[str, Template],
) -> None:
    """Only PlatformFunction (Allow) and ResearcherRole (Deny) name the bucket."""
    named: set[str] = set()
    for key in ("data", "ingest", "api"):
        for lid, pol in templates[key].find_resources("AWS::IAM::Policy").items():
            for s in pol["Properties"]["PolicyDocument"]["Statement"]:
                if "ArtifactsBucket" in str(s["Resource"]):
                    named.add(f"{key}:{lid.split('-')[0]}")
                    if s["Effect"] == "Allow":
                        assert key == "api"
                        assert lid.startswith("PlatformFunction")
    assert {n.split(":")[0] for n in named} == {"data", "api"}
    assert not any(n.startswith("ingest") for n in named)
