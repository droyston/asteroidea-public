"""Athena workgroup `asteroidea`: results go to the results bucket under `athena/`."""

from typing import TYPE_CHECKING

from aws_cdk import aws_athena as athena

if TYPE_CHECKING:
    from aws_cdk import aws_s3 as s3
    from constructs import Construct

WORKGROUP = "asteroidea"


def workgroup(scope: Construct, results: s3.IBucket) -> athena.CfnWorkGroup:
    """Create the workgroup; SSE-S3 results and enforced configuration."""
    return athena.CfnWorkGroup(
        scope,
        "Workgroup",
        name=WORKGROUP,
        work_group_configuration=athena.CfnWorkGroup.WorkGroupConfigurationProperty(
            enforce_work_group_configuration=True,
            publish_cloud_watch_metrics_enabled=True,
            result_configuration=athena.CfnWorkGroup.ResultConfigurationProperty(
                output_location=f"s3://{results.bucket_name}/athena/",
                encryption_configuration=athena.CfnWorkGroup.EncryptionConfigurationProperty(
                    encryption_option="SSE_S3",
                ),
            ),
        ),
    )
