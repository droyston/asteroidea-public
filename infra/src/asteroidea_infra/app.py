"""CDK app entry point: `python -m asteroidea_infra.app` (see `cdk.json`)."""

from typing import TYPE_CHECKING

from aws_cdk import App, CfnOutput, Environment, Fn, Stack, Tags

from asteroidea_infra import config
from asteroidea_infra.api import ApiInputs, ApiStack
from asteroidea_infra.data_stack import DataStack
from asteroidea_infra.identity import (
    CLIENT_OUTPUTS,
    SDK_CALLBACK,
    export_name,
    output_names,
    sandbox_test_enabled,
)
from asteroidea_infra.ingest import IngestBuckets, IngestStack, IngestTables
from asteroidea_infra.nag import apply_nag

if TYPE_CHECKING:
    from constructs import Construct


class ServicesStack(Stack):
    """Stateless services: Ingest and Api nested stacks, plus outputs.

    The user pool and its clients live in DataStack (RETAIN); this stack imports
    their exports and re-outputs them under their original names, so every reader of
    ServicesStack's outputs is unchanged.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        env: Environment,
        data: DataStack,
    ) -> None:
        """Create the nested stacks over the DataStack resources."""
        super().__init__(scope, construct_id, env=env)
        self.add_stack_dependency(data)  # the identity imports name no token
        self.identity_values = {
            key: Fn.import_value(export_name(key))
            for key in output_names(sandbox_test=sandbox_test_enabled(self))
        }
        self.ingest = IngestStack(
            self,
            "Ingest",
            buckets=IngestBuckets(
                raw=data.raw_bucket,
                warehouse=data.warehouse_bucket,
                derived=data.derived_bucket,
                results=data.athena_results_bucket,
            ),
            tables=IngestTables(
                data.sessions_table, data.owners_table, data.import_runs_table
            ),
        )
        self.api = ApiStack(
            self,
            "Api",
            inputs=ApiInputs(
                user_pool_id=self.identity_values["UserPoolId"],
                web_url=Fn.import_value(export_name("WebUrl")),
                client_ids=[
                    self.identity_values[name]
                    for name in CLIENT_OUTPUTS.values()
                    if name in self.identity_values
                ],
                raw_bucket=data.raw_bucket,
                sessions_table=data.sessions_table,
                owners_table=data.owners_table,
                labels_table=data.labels_table,
                commit_queue=self.ingest.commit_queue,
                site_bucket=data.web.bucket,
                artifacts_bucket=data.artifacts_bucket,
                warehouse_bucket=data.warehouse_bucket,
                athena_results_bucket=data.athena_results_bucket,
                derived_bucket=data.derived_bucket,
                import_runs_table=data.import_runs_table,
                notes_table=data.notes_table,
                import_plan_function=self.ingest.import_plan_function,
                platform_env=self._platform_env(data),
            ),
        )
        self._outputs()

    def _platform_env(self, data: DataStack) -> dict[str, str]:
        """Return PlatformFunction's environment: the `platform.json` values.

        `client_id` is the SDK client and `redirect_uri` the SDK callback, never the
        web client; `api_url` comes from the request, so no cycle through the API.
        """
        values = self.identity_values
        return {
            "PLATFORM_REGION": config.REGION,
            "PLATFORM_USER_POOL_ID": values["UserPoolId"],
            "PLATFORM_SDK_CLIENT_ID": values[CLIENT_OUTPUTS["sdk"]],
            "PLATFORM_LOGIN_BASE_URL": values["ManagedLoginBaseUrl"],
            "PLATFORM_SDK_CALLBACK": SDK_CALLBACK,
            "PLATFORM_IDENTITY_POOL_ID": values["IdentityPoolId"],
            "PLATFORM_RAW_BUCKET": data.raw_bucket.bucket_name,
        }

    def _outputs(self) -> None:
        values = {
            **self.identity_values,
            "ApiUrl": self.api.http_api.api_endpoint,
            "CommitFunctionName": self.ingest.commit_function.function_name,
            "ImportFunctionName": self.ingest.import_function.function_name,
            "CommitQueueUrl": self.ingest.commit_queue.queue_url,
            "ExtractQueueUrl": self.ingest.extract_queue.queue_url,
            "ReextractStateMachineArn": (
                self.ingest.reextract_state_machine.state_machine_arn
            ),
            "CommitDlqUrl": self.ingest.commit_dlq.queue_url,
            "ExtractDlqUrl": self.ingest.extract_dlq.queue_url,
            "LfpBuildQueueUrl": self.ingest.lfp_build_queue.queue_url,
            "LfpBuildDlqUrl": self.ingest.lfp_build_dlq.queue_url,
            "LfpBuildFunctionName": self.ingest.lfp_build_function.function_name,
        }
        for key, value in values.items():
            CfnOutput(self, key, value=value)


def build_app() -> App:
    """Build the app: DataStack, ServicesStack, tags and cdk-nag."""
    app = App()
    env = Environment(account=config.account(), region=config.REGION)
    data = DataStack(app, "DataStack", env=env)
    ServicesStack(app, "ServicesStack", env=env, data=data)
    for key, value in config.TAGS.items():
        Tags.of(app).add(key, value)
    apply_nag(app)
    return app


if __name__ == "__main__":
    build_app().synth()
