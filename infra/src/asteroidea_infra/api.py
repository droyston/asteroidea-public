"""Api nested stack: HTTP API with a Cognito JWT authorizer.

DataStack's Identity owns the user pool and clients; this stack takes their ids, which
ServicesStack imports from DataStack's exports. The functions come from the registry
(`functions.API_FUNCTIONS`) and the routes from `asteroidea_ingest.routes.ROUTES`.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from aws_cdk import Duration, NestedStack
from aws_cdk import aws_apigatewayv2 as apigw
from aws_cdk import aws_apigatewayv2_authorizers as authorizers
from aws_cdk import aws_apigatewayv2_integrations as integrations

from asteroidea_infra.functions import API_FUNCTIONS, build_function
from asteroidea_ingest.routes import ROUTES

if TYPE_CHECKING:
    from aws_cdk import aws_dynamodb as dynamodb
    from aws_cdk import aws_lambda as lambda_
    from aws_cdk import aws_s3 as s3
    from aws_cdk import aws_sqs as sqs
    from constructs import Construct

# The web app's dev origin; the hosted origin comes from ApiInputs.web_url.
WEB_ORIGIN = "http://localhost:5173"
# The methods the CORS preflight allows; every route's method is one of them.
CORS_METHODS = ("GET", "POST", "PATCH", "DELETE")


@dataclass(frozen=True)
class ApiInputs:
    """The resources the Api stack takes from the Data and Ingest stacks."""

    user_pool_id: str
    web_url: str
    client_ids: list[str]
    raw_bucket: s3.IBucket
    sessions_table: dynamodb.ITable
    owners_table: dynamodb.ITable
    labels_table: dynamodb.ITable  # LabelsRole reads and writes it
    commit_queue: sqs.IQueue
    site_bucket: s3.IBucket
    artifacts_bucket: s3.IBucket
    platform_env: dict[str, str]  # PlatformFunction's PLATFORM_* values
    warehouse_bucket: s3.IBucket  # CatalogRole reads it through Athena
    athena_results_bucket: s3.IBucket
    derived_bucket: s3.IBucket  # ComputeRole reads lfp/*
    import_runs_table: dynamodb.ITable  # ImportsRole creates and reads runs
    notes_table: dynamodb.ITable  # PlatformRole puts notes
    import_plan_function: lambda_.Function  # ImportsRole invokes it, async


class ApiStack(NestedStack):
    """HTTP API, its six functions and their routes."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        inputs: ApiInputs,
    ) -> None:
        """Create the functions, the authorizer and one route per `ROUTES` row.

        The audience is every client that signs researchers in (sdk, web and, when
        enabled, sandbox-test: a token from a client outside it gets a 401).
        """
        super().__init__(scope, construct_id)
        self.inputs = inputs
        built: dict[str, lambda_.Function] = {
            spec.construct_id: build_function(self, spec) for spec in API_FUNCTIONS
        }
        self.session_function = built["SessionFunction"]
        self.platform_function = built["PlatformFunction"]
        self.catalog_function = built["CatalogFunction"]
        self.compute_function = built["ComputeFunction"]
        self.labels_function = built["LabelsFunction"]
        self.imports_function = built["ImportsFunction"]
        authorizer = authorizers.HttpJwtAuthorizer(
            "Authorizer",
            f"https://cognito-idp.{self.region}.amazonaws.com/{inputs.user_pool_id}",
            jwt_audience=list(inputs.client_ids),
        )
        self.http_api = apigw.HttpApi(
            self,
            "HttpApi",
            default_authorizer=authorizer,
            # The web app calls GET /uploads from the browser with a bearer token.
            cors_preflight=apigw.CorsPreflightOptions(
                allow_origins=[WEB_ORIGIN, inputs.web_url],
                allow_methods=[apigw.CorsHttpMethod[m] for m in CORS_METHODS],
                allow_headers=["authorization", "content-type"],
                max_age=Duration.days(1),
            ),
        )
        for spec in API_FUNCTIONS:
            if spec.integration_id is None:
                msg = f"{spec.construct_id} is an API function with no integration_id"
                raise ValueError(msg)
            integration = integrations.HttpLambdaIntegration(
                spec.integration_id, built[spec.construct_id]
            )
            for route in ROUTES:
                if route.function == spec.construct_id:
                    self.http_api.add_routes(
                        path=route.path,
                        methods=[apigw.HttpMethod[route.method]],
                        integration=integration,
                    )
