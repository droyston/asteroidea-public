"""The raw bucket's own principal deny.

Defence in depth behind ResearcherRole's explicit deny. The policy names its two
writers by fixed role name, so these tests read the policy, then read the names back off
the real roles in ServicesStack and check the three roles by name: CommitRole and
SessionApiRole pass the deny for exactly the calls they make, everyone else does not.
"""

from fnmatch import fnmatchcase
from typing import Any, Final

import pytest
from aws_cdk import App, Environment
from aws_cdk.assertions import Template

from asteroidea_infra.app import ServicesStack
from asteroidea_infra.data_stack import DataStack
from asteroidea_infra.synth_asserts import flat

pytestmark = pytest.mark.synth

ACCOUNT: Final = "000000000000"
ENV: Final = Environment(account=ACCOUNT, region="us-east-1")
COMMIT: Final = f"arn:aws:iam::{ACCOUNT}:role/asteroidea-commit-role"
SESSION: Final = f"arn:aws:iam::{ACCOUNT}:role/asteroidea-session-api-role"
IMPORT: Final = f"arn:aws:iam::{ACCOUNT}:role/asteroidea-import-role"
RESEARCHER: Final = f"arn:aws:iam::{ACCOUNT}:role/ResearcherRole"
DEVELOPER: Final = (
    f"arn:aws:iam::{ACCOUNT}:role/aws-reserved/sso.amazonaws.com/"
    "AWSReservedSSO_AsteroideaSandboxDeveloper_0123456789abcdef"
)
PUT_SID: Final = "DenyRawWritesExceptSessionApiAndCommit"
DELETE_SID: Final = "DenyRawDeletesExceptCommit"


@pytest.fixture(scope="module")
def templates() -> dict[str, Template]:
    app = App(context={"sandbox-test": "true"})
    data = DataStack(app, "DataStack", env=ENV)
    services = ServicesStack(app, "ServicesStack", env=ENV, data=data)
    return {
        "data": Template.from_stack(data),
        "ingest": Template.from_stack(services.ingest),
        "api": Template.from_stack(services.api),
    }


def _raw_policy(templates: dict[str, Template]) -> list[dict[str, Any]]:
    found = [
        v["Properties"]["PolicyDocument"]["Statement"]
        for k, v in templates["data"].find_resources("AWS::S3::BucketPolicy").items()
        if k.startswith("RawBucket")
    ]
    assert len(found) == 1
    return list(found[0])


def _statement(templates: dict[str, Template], sid: str) -> dict[str, Any]:
    (found,) = [s for s in _raw_policy(templates) if s.get("Sid") == sid]
    return found


def _listed(value: str | list[str]) -> list[str]:
    return [value] if isinstance(value, str) else list(value)


def _denied(templates: dict[str, Template], principal: str, action: str) -> bool:
    """Return whether a Deny statement of the raw policy applies to the request."""
    for s in _raw_policy(templates):
        if s["Effect"] != "Deny" or "Condition" not in s:
            continue
        if not any(fnmatchcase(action, a) for a in _listed(s["Action"])):
            continue
        cond = s["Condition"].get("ArnNotEquals")
        if cond is not None and principal not in flat(cond["aws:PrincipalArn"]):
            return True
    return False


def _role_name(template: Template, function: str = "") -> str:
    """Return the name of the Lambda role whose logical id starts with `function`."""
    (role,) = [
        r["Properties"]
        for lid, r in template.find_resources("AWS::IAM::Role").items()
        if "lambda.amazonaws.com" in str(r["Properties"]["AssumeRolePolicyDocument"])
        and lid.startswith(function)
    ]
    return str(role["RoleName"])


def _allowed(template: Template, action: str, key: str, function: str = "") -> bool:
    """Return whether the role's own policy allows `action` on a key under `key`."""
    statements = [
        s
        for lid, p in template.find_resources("AWS::IAM::Policy").items()
        if lid.startswith(function)
        for s in p["Properties"]["PolicyDocument"]["Statement"]
    ]
    return any(
        s["Effect"] == "Allow"
        and any(fnmatchcase(action, a) for a in _listed(s["Action"]))
        and key in str(s["Resource"])
        for s in statements
    )


class TestPolicyText:
    def test_put_deny_is_the_pinned_statement(
        self, templates: dict[str, Template]
    ) -> None:
        s = _statement(templates, PUT_SID)
        assert s["Effect"] == "Deny"
        assert s["Principal"] == {"AWS": "*"}
        assert s["Action"] == ["s3:PutObject*", "s3:RestoreObject"]
        assert set(s["Condition"]) == {"ArnNotEquals"}
        assert set(s["Condition"]["ArnNotEquals"]) == {"aws:PrincipalArn"}
        # The writer list gains ImportRole (landing/ only, by IAM).
        assert flat(s["Condition"]["ArnNotEquals"]["aws:PrincipalArn"]) == [
            SESSION,
            COMMIT,
            IMPORT,
        ]
        assert "/*" in str(s["Resource"])

    def test_delete_deny_is_the_pinned_statement(
        self, templates: dict[str, Template]
    ) -> None:
        s = _statement(templates, DELETE_SID)
        assert s["Effect"] == "Deny"
        assert s["Principal"] == {"AWS": "*"}
        assert s["Action"] == "s3:DeleteObject*"
        assert flat(s["Condition"]["ArnNotEquals"]["aws:PrincipalArn"]) == [COMMIT]
        assert "/*" in str(s["Resource"])

    def test_non_tls_deny_is_still_there(self, templates: dict[str, Template]) -> None:
        tls = [
            s
            for s in _raw_policy(templates)
            if s.get("Condition") == {"Bool": {"aws:SecureTransport": "false"}}
        ]
        assert len(tls) == 1
        assert tls[0]["Effect"] == "Deny"

    def test_exactly_three_statements(self, templates: dict[str, Template]) -> None:
        assert len(_raw_policy(templates)) == 3  # TLS, put, delete


class TestRolesAreNamedForTheirWriters:
    def test_commit_role_name_matches_the_policy(
        self, templates: dict[str, Template]
    ) -> None:
        name = _role_name(templates["ingest"], "CommitFunction")
        assert f"arn:aws:iam::{ACCOUNT}:role/{name}" == COMMIT

    def test_import_role_name_matches_the_policy(
        self, templates: dict[str, Template]
    ) -> None:
        name = _role_name(templates["ingest"], "ImportFunction")
        assert f"arn:aws:iam::{ACCOUNT}:role/{name}" == IMPORT

    def test_session_api_role_name_matches_the_policy(
        self, templates: dict[str, Template]
    ) -> None:
        name = _role_name(templates["api"], "SessionFunction")
        assert f"arn:aws:iam::{ACCOUNT}:role/{name}" == SESSION


class TestWritersAreNotBlocked:
    """The deny must let through every call the two writers make (by name)."""

    def test_commit_role_copies_into_raw(self, templates: dict[str, Template]) -> None:
        assert _allowed(templates["ingest"], "s3:PutObject", "raw/*")
        assert not _denied(templates, COMMIT, "s3:PutObject")

    def test_commit_role_rolls_back_by_version(
        self, templates: dict[str, Template]
    ) -> None:
        assert _allowed(templates["ingest"], "s3:DeleteObjectVersion", "raw/*")
        assert not _denied(templates, COMMIT, "s3:DeleteObjectVersion")

    def test_session_function_presigns_a_landing_put(
        self, templates: dict[str, Template]
    ) -> None:
        """The presigned PUT is signed by SessionApiRole, so it is that principal."""
        assert _allowed(templates["api"], "s3:PutObject", "landing/*")
        assert not _denied(templates, SESSION, "s3:PutObject")


class TestImportRoleWritesLandingOnly:
    def test_import_role_puts_into_landing(
        self, templates: dict[str, Template]
    ) -> None:
        ingest = templates["ingest"]
        assert _allowed(ingest, "s3:PutObject", "landing/*", "ImportFunction")
        assert not _allowed(ingest, "s3:PutObject", "raw/*", "ImportFunction")
        assert not _denied(templates, IMPORT, "s3:PutObject")
        assert _denied(templates, IMPORT, "s3:DeleteObject")


class TestEveryoneElseIsBlocked:
    @pytest.mark.parametrize("principal", [RESEARCHER, DEVELOPER])
    @pytest.mark.parametrize(
        "action",
        ["s3:PutObject", "s3:PutObjectAcl", "s3:RestoreObject"],
    )
    def test_writes(
        self, templates: dict[str, Template], principal: str, action: str
    ) -> None:
        assert _denied(templates, principal, action)

    @pytest.mark.parametrize("principal", [RESEARCHER, DEVELOPER, SESSION])
    @pytest.mark.parametrize("action", ["s3:DeleteObject", "s3:DeleteObjectVersion"])
    def test_deletes(
        self, templates: dict[str, Template], principal: str, action: str
    ) -> None:
        assert _denied(templates, principal, action)

    def test_reads_are_not_touched(self, templates: dict[str, Template]) -> None:
        for principal in (RESEARCHER, DEVELOPER, SESSION, COMMIT):
            assert not _denied(templates, principal, "s3:GetObject")
            assert not _denied(templates, principal, "s3:ListBucket")
