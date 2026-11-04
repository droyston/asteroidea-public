"""Identity pins (clients, callbacks, group, role mapping, ResearcherRole).

The identity resources live in DataStack.
"""

import json
import re
from typing import Any

import pytest
from aws_cdk import App, Environment
from aws_cdk.assertions import Match, Template

from asteroidea_infra.data_stack import DataStack
from asteroidea_infra.synth_asserts import actions, policy_statements

pytestmark = pytest.mark.synth

ENV = Environment(account="000000000000", region="us-east-1")


def _template(*, sandbox_test: bool) -> Template:
    context = {"sandbox-test": "true"} if sandbox_test else {}
    app = App(context=context)
    return Template.from_stack(DataStack(app, "DataStack", env=ENV))


@pytest.fixture(scope="module")
def on() -> Template:
    return _template(sandbox_test=True)


@pytest.fixture(scope="module")
def off() -> Template:
    return _template(sandbox_test=False)


def _hosted(path: str) -> Any:
    """Return the matcher for `https://<distribution domain><path>`."""
    domain = {
        "Fn::GetAtt": [Match.string_like_regexp("^WebDistribution"), "DomainName"]
    }
    return {"Fn::Join": ["", ["https://", domain, path]]}


def _clients(t: Template) -> dict[str, dict[str, Any]]:
    found = t.find_resources("AWS::Cognito::UserPoolClient")
    return {v["Properties"]["ClientName"]: v["Properties"] for v in found.values()}


def test_sdk_client_has_the_exact_loopback_callback(on: Template) -> None:
    sdk = _clients(on)["sdk"]
    assert sdk["CallbackURLs"] == ["http://localhost:8765/callback"]
    assert sdk["AllowedOAuthFlows"] == ["code"]
    assert "GenerateSecret" not in sdk or sdk["GenerateSecret"] is False


def test_web_client_has_the_in_app_callback_exactly(on: Template) -> None:
    web = _clients(on)["web"]
    result = Match.object_equals(
        {
            "callbacks": [
                "http://localhost:5173/auth/callback",
                _hosted("/auth/callback"),
            ],
            "logouts": ["http://localhost:5173/", _hosted("/")],
        },
    ).test({"callbacks": web["CallbackURLs"], "logouts": web["LogoutURLs"]})
    assert not result.has_failed()
    assert web["AllowedOAuthFlows"] == ["code"]


def test_sdk_client_logout_is_unchanged(on: Template) -> None:
    assert _clients(on)["sdk"]["LogoutURLs"] == ["http://localhost:8765/callback"]


def test_invitation_names_the_hosted_url_and_the_temporary_password(
    on: Template,
) -> None:
    (pool,) = on.find_resources("AWS::Cognito::UserPool").values()
    invite = pool["Properties"]["AdminCreateUserConfig"]["InviteMessageTemplate"]
    assert invite["EmailSubject"] == "Your asteroidea account"
    body = json.dumps(invite["EmailMessage"])
    assert "{username}" in body
    assert "{####}" in body
    assert "valid for 7 days" in body
    assert '"Fn::GetAtt": ["WebDistribution' in body
    assert '"DomainName"' in body


def test_sandbox_test_client_only_with_the_flag(on: Template, off: Template) -> None:
    assert set(_clients(on)) == {"sdk", "web", "sandbox-test"}
    assert set(_clients(off)) == {"sdk", "web"}
    test_client = _clients(on)["sandbox-test"]
    assert test_client["ExplicitAuthFlows"] == [
        "ALLOW_ADMIN_USER_PASSWORD_AUTH",
        "ALLOW_REFRESH_TOKEN_AUTH",
    ]
    assert "CallbackURLs" not in test_client


def test_group_researchers_has_the_researcher_role(on: Template) -> None:
    on.has_resource_properties(
        "AWS::Cognito::UserPoolGroup",
        {"GroupName": "researchers", "Precedence": 10, "RoleArn": Match.any_value()},
    )


def test_group_curators_has_no_role_arn(on: Template) -> None:
    """A role on `curators` would take over a two-group user's identity role."""
    on.has_resource_properties(
        "AWS::Cognito::UserPoolGroup",
        {"GroupName": "curators", "Precedence": 20, "RoleArn": Match.absent()},
    )
    groups = on.find_resources("AWS::Cognito::UserPoolGroup")
    assert sorted(g["Properties"]["GroupName"] for g in groups.values()) == [
        "curators",
        "researchers",
    ]


def test_sign_up_is_closed_and_domain_is_managed_login(on: Template) -> None:
    on.has_resource_properties(
        "AWS::Cognito::UserPool",
        {"AdminCreateUserConfig": {"AllowAdminCreateUserOnly": True}},
    )
    on.has_resource_properties(
        "AWS::Cognito::UserPoolDomain", {"ManagedLoginVersion": 2}
    )


def test_domain_prefix_is_the_new_pools(on: Template) -> None:
    """Decision 3a: a new pool, so a prefix unlike the former `asteroidea-<account>`."""
    on.has_resource_properties(
        "AWS::Cognito::UserPoolDomain", {"Domain": "asteroidea-id-000000000000"}
    )


def test_identity_pool_has_no_unauthenticated_identities(on: Template) -> None:
    on.has_resource_properties(
        "AWS::Cognito::IdentityPool", {"AllowUnauthenticatedIdentities": False}
    )


def test_role_mapping_is_token_with_deny_for_every_client(on: Template) -> None:
    (attachment,) = on.find_resources(
        "AWS::Cognito::IdentityPoolRoleAttachment"
    ).values()
    mappings = attachment["Properties"]["RoleMappings"]
    assert set(mappings) == {"sdk", "web", "sandbox-test"}
    for mapping in mappings.values():
        assert mapping["Type"] == "Token"
        assert mapping["AmbiguousRoleResolution"] == "Deny"


def test_default_authenticated_role_has_no_policy(on: Template) -> None:
    policies = on.find_resources("AWS::IAM::Policy")
    assert len(policies) == 1  # ResearcherRole's only
    (policy,) = policies.values()
    assert "ResearcherRole" in str(policy["Properties"]["Roles"])


def test_trust_requires_the_pool_audience_and_authenticated_amr(on: Template) -> None:
    roles = on.find_resources("AWS::IAM::Role")
    assert len(roles) == 2
    for role in roles.values():
        (stmt,) = role["Properties"]["AssumeRolePolicyDocument"]["Statement"]
        assert stmt["Action"] == "sts:AssumeRoleWithWebIdentity"
        assert stmt["Principal"] == {"Federated": "cognito-identity.amazonaws.com"}
        cond = stmt["Condition"]
        assert "cognito-identity.amazonaws.com:aud" in cond["StringEquals"]
        assert cond["ForAnyValue:StringLike"] == {
            "cognito-identity.amazonaws.com:amr": "authenticated"
        }


def test_researcher_role_denies_raw_writes_and_deletes(on: Template) -> None:
    (deny,) = [
        s
        for s in policy_statements(on, exactly_one=True)
        if s["Effect"] == "Deny" and actions(s) == {"s3:PutObject*", "s3:DeleteObject*"}
    ]
    assert "Fn::ImportValue" in str(deny["Resource"]) or "Fn::GetAtt" in str(
        deny["Resource"]
    )


def test_researcher_role_denies_everything_on_the_artifacts_bucket(
    on: Template,
) -> None:
    """The second Deny is `s3:*` on the bucket and its objects."""
    denies = [
        s for s in policy_statements(on, exactly_one=True) if s["Effect"] == "Deny"
    ]
    assert len(denies) == 2
    (deny,) = [s for s in denies if actions(s) == {"s3:*"}]
    resources = deny["Resource"]
    assert len(resources) == 2  # the bucket ARN and `<arn>/*`
    assert "ArtifactsBucket" in str(resources)
    assert str(resources[1]).count("/*") == 1


def test_researcher_role_allows_no_write_to_raw(on: Template) -> None:
    for s in policy_statements(on, exactly_one=True):
        if s["Effect"] != "Allow":
            continue
        for action in actions(s):
            if action.startswith(("s3:Put", "s3:Delete")):
                assert "athena/*" in str(s["Resource"]), s  # results bucket only
            assert action != "s3:*"
            assert "*" not in action.removeprefix("s3:").removeprefix("glue:")


def test_researcher_role_reads_raw_and_warehouse(on: Template) -> None:
    gets = [
        s
        for s in policy_statements(on, exactly_one=True)
        if "s3:GetObjectVersion" in actions(s)
    ]
    assert len(gets) == 1
    assert "raw/*" in str(gets[0]["Resource"])
    athena = [
        s
        for s in policy_statements(on, exactly_one=True)
        if "athena:StartQueryExecution" in actions(s)
    ]
    assert len(athena) == 1
    assert "workgroup/asteroidea" in str(athena[0]["Resource"])


CATALOG_TABLES = {
    "assets",
    "datasets",
    "subjects",
    "subject_links",
    "sessions",
    "asset_headers",
    "series",
    "electrodes",
    "units",
    "intervals",
    "unit_spikes",
    "events",
    "studies",
    "study_datasets",
}


def test_researcher_role_reads_exactly_the_catalog_glue_tables(on: Template) -> None:
    """The researcher grant, with the meaning the catalog design gives it.

    Replaces `test_researcher_role_reads_only_the_assets_glue_table`. The grant
    names exactly the ten catalog tables: no wildcard, no `s1_probe`.
    """
    (glue,) = [
        s
        for s in policy_statements(on, exactly_one=True)
        if "glue:GetTable" in actions(s)
    ]
    text = str(glue["Resource"])
    assert "s1_probe" not in text
    named = re.findall(r":table/asteroidea/([^'\"\s]+)", text)
    assert sorted(named) == sorted(CATALOG_TABLES)  # each once, nothing else
    assert "*" not in "".join(named)
