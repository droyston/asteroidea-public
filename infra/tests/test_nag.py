"""cdk-nag passes the real app and fails a bucket open to the public."""

import pytest
from aws_cdk import App, Environment, RemovalPolicy, Stack
from aws_cdk import aws_s3 as s3

from asteroidea_infra.app import build_app
from asteroidea_infra.nag import Acknowledgements, apply_nag

pytestmark = pytest.mark.synth


def _bucket_app(
    *,
    block_public_access: bool,
    acknowledgements: Acknowledgements = (),
) -> App:
    app = App()
    stack = Stack(
        app, "Probe", env=Environment(account="000000000000", region="us-east-1")
    )
    s3.Bucket(
        stack,
        "Bucket",
        removal_policy=RemovalPolicy.DESTROY,
        block_public_access=s3.BlockPublicAccess.BLOCK_ALL
        if block_public_access
        else s3.BlockPublicAccess(
            block_public_acls=False,
            ignore_public_acls=False,
            block_public_policy=False,
            restrict_public_buckets=False,
        ),
        public_read_access=not block_public_access,
    )
    apply_nag(app, acknowledgements)
    return app


def test_positive_app_synthesises_clean() -> None:
    build_app().synth()


def test_negative_app_fails_with_s2() -> None:
    with pytest.raises(Exception, match=r"AwsSolutions-S2"):
        _bucket_app(block_public_access=False).synth()


def test_acknowledgement_silences_a_finding() -> None:
    reason = "test: logging and SSL are out of scope for this probe"
    acks = (
        ("Probe", "AwsSolutions::AwsSolutions-S1", reason),
        ("Probe", "AwsSolutions::AwsSolutions-S10", reason),
    )
    app = _bucket_app(block_public_access=True, acknowledgements=acks)
    app.synth()
