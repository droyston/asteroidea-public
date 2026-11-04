"""Identity construct in DataStack: user pool, clients, identity pool, ResearcherRole.

The pool lives in the termination-protected DataStack with RETAIN, under its own domain
prefix. The `sandbox-test` client exists only when the sandbox context flag is set.
DataStack exports the identity output names; ServicesStack imports them.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from aws_cdk import Duration, RemovalPolicy, Stack
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_iam as iam
from constructs import Construct

from asteroidea_infra import athena, schema

if TYPE_CHECKING:
    from collections.abc import Iterable

    from aws_cdk import aws_s3 as s3

GROUP: Final = "researchers"
GROUP_PRECEDENCE: Final = 10
CURATORS_GROUP: Final = "curators"
CURATORS_PRECEDENCE: Final = 20
SDK_CALLBACK: Final = "http://localhost:8765/callback"
WEB_CALLBACK: Final = "http://localhost:5173/auth/callback"
WEB_LOGOUT: Final = "http://localhost:5173/"
SANDBOX_FLAG: Final = "sandbox-test"
# The first pool used `asteroidea`; the new pool needs its own prefix.
DOMAIN_PREFIX: Final = "asteroidea-id"
EXPORT_PREFIX: Final = "asteroidea-"
# Client name -> the original output name that carries its id.
CLIENT_OUTPUTS: Final = {
    "sdk": "SdkClientId",
    "web": "WebClientId",
    SANDBOX_FLAG: "SandboxTestClientId",
}
# The ServicesStack output names DataStack now exports (existing consumers read them
# from ServicesStack, which re-outputs the imported values).
POOL_OUTPUTS: Final = (
    "UserPoolId",
    "IdentityPoolId",
    "ManagedLoginBaseUrl",
    "ResearcherRoleArn",
)

RESEARCHER_GLUE_ACTIONS: Final = (
    "glue:GetDatabase",
    "glue:GetDatabases",
    "glue:GetTable",
    "glue:GetTables",
)
RESEARCHER_ATHENA_ACTIONS: Final = (
    "athena:StartQueryExecution",
    "athena:GetQueryExecution",
    "athena:GetQueryResults",
    "athena:StopQueryExecution",
    "athena:GetWorkGroup",
)


@dataclass(frozen=True)
class QueryGrant:
    """The part of the Athena read that differs per role: tables and actions."""

    tables: Iterable[str]
    glue_actions: Iterable[str]
    athena_actions: Iterable[str]


def query_read_statements(
    stack: Stack,
    *,
    warehouse: s3.IBucket,
    results: s3.IBucket,
    grant: QueryGrant,
) -> list[iam.PolicyStatement]:
    """Return the Athena read of `grant.tables`: warehouse, Glue, workgroup, results.

    ResearcherRole (every table), CatalogRole, ComputeRole and LabelsRole share it, so
    their Glue, warehouse and results statements cannot drift.
    """
    return [
        iam.PolicyStatement(
            actions=["s3:GetObject"],
            resources=[warehouse.arn_for_objects(f"{schema.ICEBERG_PREFIX}/*")],
        ),
        iam.PolicyStatement(
            actions=["s3:ListBucket", "s3:GetBucketLocation"],
            resources=[warehouse.bucket_arn],
        ),
        iam.PolicyStatement(
            actions=list(grant.glue_actions),
            resources=[
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
                    for name in grant.tables
                ),
            ],
        ),
        iam.PolicyStatement(
            actions=list(grant.athena_actions),
            resources=[
                stack.format_arn(
                    service="athena",
                    resource="workgroup",
                    resource_name=athena.WORKGROUP,
                ),
            ],
        ),
        iam.PolicyStatement(
            actions=[
                "s3:PutObject",
                "s3:GetObject",
                "s3:AbortMultipartUpload",
                "s3:ListMultipartUploadParts",
            ],
            resources=[results.arn_for_objects("athena/*")],
        ),
        iam.PolicyStatement(
            actions=["s3:ListBucket", "s3:GetBucketLocation"],
            resources=[results.bucket_arn],
        ),
    ]


def add_query_read_policy(
    grantee: iam.IGrantable,
    stack: Stack,
    *,
    warehouse: s3.IBucket,
    results: s3.IBucket,
    grant: QueryGrant,
) -> None:
    """Add `query_read_statements` to `grantee`'s policy, in order."""
    principal = grantee.grant_principal
    for statement in query_read_statements(
        stack, warehouse=warehouse, results=results, grant=grant
    ):
        principal.add_to_principal_policy(statement)


def export_name(output: str) -> str:
    """Return the CloudFormation export name DataStack gives the output `output`."""
    return f"{EXPORT_PREFIX}{output}"


def output_names(*, sandbox_test: bool) -> tuple[str, ...]:
    """Return every identity output name for this context, pool values first."""
    clients = [
        name
        for client, name in CLIENT_OUTPUTS.items()
        if sandbox_test or client != SANDBOX_FLAG
    ]
    return (*POOL_OUTPUTS, *clients)


def sandbox_test_enabled(scope: Construct) -> bool:
    """Return whether the `sandbox-test` context flag is on for this app."""
    return str(scope.node.try_get_context(SANDBOX_FLAG)).lower() == "true"


@dataclass(frozen=True)
class IdentityBuckets:
    """The data buckets ResearcherRole is granted access to."""

    raw: s3.IBucket
    warehouse: s3.IBucket
    athena_results: s3.IBucket
    artifacts: s3.IBucket


class Identity(Construct):
    """User pool (RETAIN), app clients, identity pool, ResearcherRole."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        buckets: IdentityBuckets,
        web_url: str,
        sandbox_test: bool = False,
    ) -> None:
        """Create the pool, clients, identity pool and roles."""
        super().__init__(scope, construct_id)
        self.stack = Stack.of(self)
        self.user_pool = cognito.UserPool(
            self,
            "UserPool",
            self_sign_up_enabled=False,
            sign_in_aliases=cognito.SignInAliases(email=True),
            feature_plan=cognito.FeaturePlan.ESSENTIALS,
            password_policy=cognito.PasswordPolicy(
                min_length=12,
                require_lowercase=True,
                require_uppercase=True,
                require_digits=True,
                require_symbols=True,
                temp_password_validity=Duration.days(7),
            ),
            account_recovery=cognito.AccountRecovery.EMAIL_ONLY,
            user_invitation=cognito.UserInvitationConfig(
                email_subject="Your asteroidea account",
                email_body=(
                    "Your asteroidea account is ready. Sign in at "
                    f"{web_url}/ with the username {{username}} and the temporary "
                    "password {####}. The temporary password is valid for 7 days. "
                    "You will be asked to choose a new password the first time you "
                    "sign in."
                ),
            ),
            removal_policy=RemovalPolicy.RETAIN,
        )
        self.domain = self.user_pool.add_domain(
            "Domain",
            cognito_domain=cognito.CognitoDomainOptions(
                domain_prefix=f"{DOMAIN_PREFIX}-{self.stack.account}",
            ),
            managed_login_version=cognito.ManagedLoginVersion.NEWER_MANAGED_LOGIN,
        )
        self.clients: dict[str, cognito.UserPoolClient] = {
            "sdk": self._oauth_client("SdkClient", "sdk", [SDK_CALLBACK]),
            "web": self._oauth_client(
                "WebClient",
                "web",
                [WEB_CALLBACK, f"{web_url}/auth/callback"],
                [WEB_LOGOUT, f"{web_url}/"],
            ),
        }
        if sandbox_test:
            self.clients[SANDBOX_FLAG] = self.user_pool.add_client(
                "SandboxTestClient",
                user_pool_client_name=SANDBOX_FLAG,
                generate_secret=False,
                disable_o_auth=True,
                auth_flows=cognito.AuthFlow(admin_user_password=True),
                prevent_user_existence_errors=True,
            )
        for name, client in self.clients.items():
            if name == SANDBOX_FLAG:
                continue
            cognito.CfnManagedLoginBranding(
                self,
                f"Branding-{name}",
                user_pool_id=self.user_pool.user_pool_id,
                client_id=client.user_pool_client_id,
                use_cognito_provided_values=True,
            )
        self.identity_pool = cognito.CfnIdentityPool(
            self,
            "IdentityPool",
            allow_unauthenticated_identities=False,
            allow_classic_flow=False,
            cognito_identity_providers=[
                cognito.CfnIdentityPool.CognitoIdentityProviderProperty(
                    client_id=client.user_pool_client_id,
                    provider_name=self.user_pool.user_pool_provider_name,
                    server_side_token_check=True,
                )
                for client in self.clients.values()
            ],
        )
        self.default_authenticated_role = self._federated_role("DefaultAuthRole")
        self.researcher_role = self._federated_role("ResearcherRole")
        self._researcher_policy(buckets)
        self._attach_roles()
        cognito.CfnUserPoolGroup(
            self,
            "ResearchersGroup",
            user_pool_id=self.user_pool.user_pool_id,
            group_name=GROUP,
            precedence=GROUP_PRECEDENCE,
            role_arn=self.researcher_role.role_arn,
        )
        # No role_arn: a user in both groups must keep ResearcherRole from
        # the researchers group under Token mapping with Deny on ambiguity.
        cognito.CfnUserPoolGroup(
            self,
            "CuratorsGroup",
            user_pool_id=self.user_pool.user_pool_id,
            group_name=CURATORS_GROUP,
            precedence=CURATORS_PRECEDENCE,
        )

    def outputs(self) -> dict[str, str]:
        """Return output name -> value for every identity output."""
        values = {
            "UserPoolId": self.user_pool.user_pool_id,
            "IdentityPoolId": self.identity_pool.ref,
            "ManagedLoginBaseUrl": self.domain.base_url(),
            "ResearcherRoleArn": self.researcher_role.role_arn,
        }
        for name, client in self.clients.items():
            values[CLIENT_OUTPUTS[name]] = client.user_pool_client_id
        return values

    def _attach_roles(self) -> None:
        """Token role mapping: literal map keys, token-bearing provider values."""
        region, pool_id = self.stack.region, self.user_pool.user_pool_id
        provider = f"cognito-idp.{region}.amazonaws.com/{pool_id}"
        cognito.CfnIdentityPoolRoleAttachment(
            self,
            "RoleAttachment",
            identity_pool_id=self.identity_pool.ref,
            roles={"authenticated": self.default_authenticated_role.role_arn},
            role_mappings={
                name: cognito.CfnIdentityPoolRoleAttachment.RoleMappingProperty(
                    type="Token",
                    ambiguous_role_resolution="Deny",
                    identity_provider=f"{provider}:{client.user_pool_client_id}",
                )
                for name, client in self.clients.items()
            },
        )

    def _oauth_client(
        self,
        construct_id: str,
        name: str,
        callbacks: list[str],
        logouts: list[str] | None = None,
    ) -> cognito.UserPoolClient:
        """Return a public authorization-code client with an exact callback list."""
        return self.user_pool.add_client(
            construct_id,
            user_pool_client_name=name,
            generate_secret=False,
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(authorization_code_grant=True),
                scopes=[
                    cognito.OAuthScope.OPENID,
                    cognito.OAuthScope.EMAIL,
                    cognito.OAuthScope.PROFILE,
                ],
                callback_urls=callbacks,
                logout_urls=logouts or callbacks,
            ),
            supported_identity_providers=[
                cognito.UserPoolClientIdentityProvider.COGNITO,
            ],
            prevent_user_existence_errors=True,
            enable_token_revocation=True,
            access_token_validity=Duration.hours(1),
            id_token_validity=Duration.hours(1),
            refresh_token_validity=Duration.days(30),
        )

    def _federated_role(self, construct_id: str) -> iam.Role:
        """Return a role trusted by the identity pool for authenticated identities."""
        return iam.Role(
            self,
            construct_id,
            assumed_by=iam.FederatedPrincipal(
                "cognito-identity.amazonaws.com",
                conditions={
                    "StringEquals": {
                        "cognito-identity.amazonaws.com:aud": self.identity_pool.ref,
                    },
                    "ForAnyValue:StringLike": {
                        "cognito-identity.amazonaws.com:amr": "authenticated",
                    },
                },
                assume_role_action="sts:AssumeRoleWithWebIdentity",
            ),
        )

    def _researcher_policy(
        self,
        buckets: IdentityBuckets,
    ) -> None:
        raw, warehouse, results = buckets.raw, buckets.warehouse, buckets.athena_results
        role = self.researcher_role
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["s3:GetObject", "s3:GetObjectVersion"],
                resources=[raw.arn_for_objects("raw/*")],
            ),
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["s3:ListBucket"],
                resources=[raw.bucket_arn],
                conditions={"StringLike": {"s3:prefix": ["raw/*"]}},
            ),
        )
        add_query_read_policy(
            role,
            self.stack,
            warehouse=warehouse,
            results=results,
            grant=QueryGrant(
                tables=schema.TABLE_NAMES,
                glue_actions=RESEARCHER_GLUE_ACTIONS,
                athena_actions=RESEARCHER_ATHENA_ACTIONS,
            ),
        )
        role.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.DENY,
                actions=["s3:PutObject*", "s3:DeleteObject*"],
                resources=[raw.arn_for_objects("*")],
            ),
        )
        role.add_to_policy(  # artifacts are PlatformRole's alone
            iam.PolicyStatement(
                effect=iam.Effect.DENY,
                actions=["s3:*"],
                resources=[
                    buckets.artifacts.bucket_arn,
                    buckets.artifacts.arn_for_objects("*"),
                ],
            ),
        )
