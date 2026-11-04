"""DataStack is termination-protected and holds the user pool (RETAIN).

`make destroy-services` must not reach the
pool, so ServicesStack holds no Cognito resource and imports the pool's exports.
"""

from typing import Any

import pytest
from aws_cdk import App, Environment
from aws_cdk.assertions import Template

from asteroidea_infra.app import ServicesStack, build_app
from asteroidea_infra.data_stack import DataStack

pytestmark = pytest.mark.synth

ENV = Environment(account="000000000000", region="us-east-1")
CLIENT_EXPORTS = {
    False: {"asteroidea-SdkClientId", "asteroidea-WebClientId"},
    True: {
        "asteroidea-SdkClientId",
        "asteroidea-WebClientId",
        "asteroidea-SandboxTestClientId",
    },
}
# Every export the deployed DataStack carries (read from a synth, both
# contexts). The ServicesStack imports each; CloudFormation refuses to delete an
# export in use, and DataStack deploys first, so each must survive this deploy.
BASE_EXPORTS = {
    "DataStack:ExportsOutputFnGetAttAthenaResultsBucket879938FAArn609FA1B4",
    "DataStack:ExportsOutputFnGetAttRawBucket0C3EE094ArnD2F95F99",
    "DataStack:ExportsOutputFnGetAttSessionsTable7C302024Arn24FF8012",
    "DataStack:ExportsOutputFnGetAttWarehouseBucket0E783396ArnD66BFFC9",
    "DataStack:ExportsOutputRefRawBucket0C3EE094D6333DF9",
    "DataStack:ExportsOutputRefSessionsTable7C3020246E1CCFDD",
}
BASE_OUTPUTS = {
    "UserPoolId",
    "SdkClientId",
    "WebClientId",
    "IdentityPoolId",
    "ManagedLoginBaseUrl",
    "ResearcherRoleArn",
    "ApiUrl",
    "CommitFunctionName",
}
# The writer queue outputs, added beside these names.
QUEUE_OUTPUTS = {"CommitQueueUrl", "ExtractQueueUrl", "CommitDlqUrl", "ExtractDlqUrl"}
# `make import` invokes ImportFunction by this output.
IMPORT_OUTPUTS = {"ImportFunctionName"}
# The LFP build queue, its DLQ and its function.
LFP_OUTPUTS = {"LfpBuildQueueUrl", "LfpBuildDlqUrl", "LfpBuildFunctionName"}
REEXTRACT_OUTPUTS = {"ReextractStateMachineArn"}  # `make reextract` starts it


def _app(*, sandbox_test: bool) -> tuple[DataStack, ServicesStack]:
    app = App(context={"sandbox-test": "true"} if sandbox_test else {})
    data = DataStack(app, "DataStack", env=ENV)
    return data, ServicesStack(app, "ServicesStack", env=ENV, data=data)


def _exports(template: Template) -> dict[str, Any]:
    """Return export name -> exported value."""
    outputs = template.to_json().get("Outputs", {})
    return {o["Export"]["Name"]: o["Value"] for o in outputs.values() if "Export" in o}


def _imports(node: Any) -> list[str]:
    """Return every Fn::ImportValue name under `node`, in order."""
    if isinstance(node, dict):
        if set(node) == {"Fn::ImportValue"}:
            return [str(node["Fn::ImportValue"])]
        return [name for value in node.values() for name in _imports(value)]
    if isinstance(node, list):
        return [name for value in node for name in _imports(value)]
    return []


def _client_ref(value: Any) -> bool:
    ref = str(value.get("Ref", "")) if isinstance(value, dict) else ""
    return ref.startswith("Identity") and "Client" in ref


def test_data_stack_has_termination_protection() -> None:
    stacks = {s.stack_name: s for s in build_app().synth().stacks}
    assert stacks["DataStack"].termination_protection is True
    assert stacks["ServicesStack"].termination_protection is not True


@pytest.mark.parametrize("sandbox_test", [False, True])
def test_user_pool_is_retained_in_data_stack(*, sandbox_test: bool) -> None:
    data, _ = _app(sandbox_test=sandbox_test)
    pools = Template.from_stack(data).find_resources("AWS::Cognito::UserPool")
    assert len(pools) == 1, "DataStack must hold the one user pool"
    (pool,) = pools.values()
    assert pool["DeletionPolicy"] == "Retain", "DataStack user pool is not RETAIN"
    assert pool["UpdateReplacePolicy"] == "Retain"


@pytest.mark.parametrize("sandbox_test", [False, True])
def test_services_stack_holds_no_cognito_resource(*, sandbox_test: bool) -> None:
    _, services = _app(sandbox_test=sandbox_test)
    for stack in (services, services.ingest, services.api):
        resources = Template.from_stack(stack).to_json()["Resources"].values()
        cognito = {r["Type"] for r in resources if "Cognito" in r["Type"]}
        assert not cognito, f"ServicesStack ({stack.node.path}) holds {cognito}"


@pytest.mark.parametrize("sandbox_test", [False, True])
def test_authorizer_audience_imports_equal_the_client_exports(
    *, sandbox_test: bool
) -> None:
    """No import of an export that the same context's DataStack lacks."""
    data, services = _app(sandbox_test=sandbox_test)
    exports = _exports(Template.from_stack(data))
    clients = {name for name, value in exports.items() if _client_ref(value)}
    assert clients == CLIENT_EXPORTS[sandbox_test]
    api = Template.from_stack(services.api)
    (auth,) = api.find_resources("AWS::ApiGatewayV2::Authorizer").values()
    audience = _imports(auth["Properties"]["JwtConfiguration"]["Audience"])
    assert len(audience) == len(set(audience))
    assert set(audience) == clients
    every_import = _imports(Template.from_stack(services).to_json())
    every_import += _imports(api.to_json())
    assert set(every_import) <= set(exports), "an import names a missing export"


@pytest.mark.parametrize("sandbox_test", [False, True])
def test_every_deployed_export_survives(*, sandbox_test: bool) -> None:
    data, _ = _app(sandbox_test=sandbox_test)
    assert set(_exports(Template.from_stack(data))) >= BASE_EXPORTS


@pytest.mark.parametrize("sandbox_test", [False, True])
def test_services_stack_keeps_every_deployed_output_name(*, sandbox_test: bool) -> None:
    _, services = _app(sandbox_test=sandbox_test)
    outputs = set(Template.from_stack(services).to_json()["Outputs"])
    expected = (
        BASE_OUTPUTS | QUEUE_OUTPUTS | IMPORT_OUTPUTS | LFP_OUTPUTS | REEXTRACT_OUTPUTS
    )
    expected |= {"SandboxTestClientId"} if sandbox_test else set()
    assert outputs == expected
