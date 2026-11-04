"""DataStack: stateful resources, the user pool among them. Termination-protected."""

from typing import TYPE_CHECKING

from aws_cdk import CfnOutput, Environment, Stack

from asteroidea_infra import athena, catalog, config, storage
from asteroidea_infra.identity import (
    Identity,
    IdentityBuckets,
    export_name,
    sandbox_test_enabled,
)
from asteroidea_infra.web_hosting import WebHosting

if TYPE_CHECKING:
    from constructs import Construct


class DataStack(Stack):
    """Buckets, sessions table, catalog and identity (user pool RETAIN)."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        env: Environment,
    ) -> None:
        """Create the stack with termination protection on."""
        super().__init__(scope, construct_id, env=env, termination_protection=True)
        # The raw bucket's CORS rule names the hosted URL: web hosting comes first.
        self.web = WebHosting(self, "Web")
        self.raw_bucket = storage.raw_bucket(self, web_url=self.web.url)
        commit_role = self._role_arn(config.COMMIT_ROLE_NAME)
        storage.deny_raw_writes_except(
            self.raw_bucket,
            writers=[
                self._role_arn(config.SESSION_API_ROLE_NAME),
                commit_role,
                self._role_arn(config.IMPORT_ROLE_NAME),  # landing/ only, by its IAM
            ],
            deleter=commit_role,
        )
        self.artifacts_bucket = storage.artifacts_bucket(
            self,
            platform_role=self._role_arn(config.PLATFORM_ROLE_NAME),
        )
        self.derived_bucket = storage.derived_bucket(
            self,
            lfp_writer=self._role_arn(config.LFP_BUILD_ROLE_NAME),
            rows_writer=self._role_arn(config.DERIVE_ROLE_NAME),
            web_url=self.web.url,
        )
        self.athena_results_bucket = storage.athena_results_bucket(self)
        self.warehouse_bucket = storage.warehouse_bucket(self)
        self.sessions_table = storage.sessions_table(self)
        self.owners_table = storage.dataset_owners_table(self)
        self.labels_table = storage.labels_table(self)
        self.import_runs_table = storage.import_runs_table(self)
        self.notes_table = storage.notes_table(self)
        self.database = catalog.glue_database(self)
        self.workgroup = athena.workgroup(self, self.athena_results_bucket)
        self.identity = Identity(
            self,
            "Identity",
            buckets=IdentityBuckets(
                raw=self.raw_bucket,
                warehouse=self.warehouse_bucket,
                athena_results=self.athena_results_bucket,
                artifacts=self.artifacts_bucket,
            ),
            sandbox_test=sandbox_test_enabled(self),
            web_url=self.web.url,
        )
        for key, value in self.identity.outputs().items():
            CfnOutput(self, key, value=value, export_name=export_name(key))
        # The first deployed ServicesStack imports this export (its Identity nested
        # stack
        # granted ResearcherRole the results bucket). Nothing imports it after this
        # deploy, but CloudFormation refuses to delete an export still in use, and
        # DataStack deploys first; keep it until a later deploy has none importing it.
        self.export_value(self.athena_results_bucket.bucket_arn)
        CfnOutput(self, "WebUrl", value=self.web.url, export_name=export_name("WebUrl"))
        CfnOutput(self, "SiteBucketName", value=self.web.bucket.bucket_name)
        CfnOutput(
            self,
            "DistributionId",
            value=self.web.distribution.distribution_id,
        )
        CfnOutput(self, "ArtifactsBucketName", value=self.artifacts_bucket.bucket_name)
        CfnOutput(self, "DerivedBucketName", value=self.derived_bucket.bucket_name)
        CfnOutput(self, "WarehouseBucketName", value=self.warehouse_bucket.bucket_name)
        CfnOutput(
            self,
            "AthenaResultsBucketName",
            value=self.athena_results_bucket.bucket_name,
        )

    def _role_arn(self, name: str) -> str:
        """Return the ARN of the role `name` in this account (it need not exist yet)."""
        return self.format_arn(
            service="iam",
            region="",
            resource="role",
            resource_name=name,
        )
