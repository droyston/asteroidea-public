"""The hosting construct's pinned properties."""

from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from aws_cdk.assertions import Template

pytestmark = pytest.mark.synth

CLOUDFRONT = "cloudfront.amazonaws.com"


def _site_bucket(template: Template) -> dict[str, Any]:
    found = [
        v
        for k, v in template.find_resources("AWS::S3::Bucket").items()
        if k.startswith("WebSiteBucket")
    ]
    assert len(found) == 1, found
    return dict(found[0])


def _distribution(template: Template) -> dict[str, Any]:
    (dist,) = template.find_resources("AWS::CloudFront::Distribution").values()
    return dist["Properties"]["DistributionConfig"]


def _site_policy_statements(template: Template) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for pol in template.find_resources("AWS::S3::BucketPolicy").values():
        if pol["Properties"]["Bucket"]["Ref"].startswith("WebSiteBucket"):
            out.extend(pol["Properties"]["PolicyDocument"]["Statement"])
    return out


def _is_cloudfront(stmt: dict[str, Any]) -> bool:
    return bool(stmt.get("Principal") == {"Service": CLOUDFRONT})


def _suffixes(stmt: dict[str, Any]) -> list[str]:
    """Return the key pattern of each resource, e.g. `/*` or `/handout/*`."""
    resources = stmt["Resource"]
    if not isinstance(resources, list):
        resources = [resources]
    return [r["Fn::Join"][1][-1] for r in resources]


def _is_handout_deny(stmt: dict[str, Any]) -> bool:
    return bool(
        stmt["Effect"] == "Deny"
        and _is_cloudfront(stmt)
        and stmt["Action"] == "s3:GetObject"
        and _suffixes(stmt) == ["/handout/*"],
    )


class TestHandoutDeny:
    def test_deny_exists(self, data_template: Template) -> None:
        denies = [
            s for s in _site_policy_statements(data_template) if _is_handout_deny(s)
        ]
        assert len(denies) == 1, _site_policy_statements(data_template)

    def test_cloudfront_allow_is_paired_with_the_deny(
        self,
        data_template: Template,
    ) -> None:
        statements = _site_policy_statements(data_template)
        allows = [s for s in statements if s["Effect"] == "Allow" and _is_cloudfront(s)]
        assert allows, "the OAC Allow is expected"
        covers = [
            a for a in allows if any(x in {"/*", "/handout/*"} for x in _suffixes(a))
        ]
        assert not covers or any(_is_handout_deny(s) for s in statements)


class TestDistribution:
    def test_origin_path(self, data_template: Template) -> None:
        (origin,) = _distribution(data_template)["Origins"]
        assert origin["OriginPath"] == "/site"

    def test_error_responses(self, data_template: Template) -> None:
        got = {
            e["ErrorCode"]: (e["ResponseCode"], e["ResponsePagePath"])
            for e in _distribution(data_template)["CustomErrorResponses"]
        }
        assert got == {403: (200, "/index.html"), 404: (200, "/index.html")}

    def test_price_class_root_and_protocol(self, data_template: Template) -> None:
        cfg = _distribution(data_template)
        assert cfg["PriceClass"] == "PriceClass_100"
        assert cfg["DefaultRootObject"] == "index.html"
        behavior = cfg["DefaultCacheBehavior"]
        assert behavior["ViewerProtocolPolicy"] == "redirect-to-https"
        assert behavior["ResponseHeadersPolicyId"]

    def test_no_waf(self, data_template: Template) -> None:
        assert "WebACLId" not in _distribution(data_template)


class TestOutputsAndBucket:
    def test_outputs(self, data_template: Template) -> None:
        outputs = data_template.to_json()["Outputs"]
        assert outputs["WebUrl"]["Export"]["Name"] == "asteroidea-WebUrl"
        assert "SiteBucketName" in outputs
        assert "DistributionId" in outputs

    def test_bucket_settings(self, data_template: Template) -> None:
        res = _site_bucket(data_template)
        assert res["DeletionPolicy"] == "Retain"
        props = res["Properties"]
        assert props["PublicAccessBlockConfiguration"] == {
            "BlockPublicAcls": True,
            "BlockPublicPolicy": True,
            "IgnorePublicAcls": True,
            "RestrictPublicBuckets": True,
        }
        assert "VersioningConfiguration" not in props
        enc = props["BucketEncryption"]["ServerSideEncryptionConfiguration"]
        assert enc[0]["ServerSideEncryptionByDefault"] == {"SSEAlgorithm": "AES256"}
