"""Admin commands: input checks that need no AWS."""

import json
from typing import TYPE_CHECKING, Any, cast, override

import pytest

from asteroidea_infra.admin import (
    EXIT_BAD_EMAIL,
    EXIT_DEPLOY_REFUSED,
    add_curator,
    add_researcher,
    deploy_refusal,
    researcher,
    sdk_config,
)

if TYPE_CHECKING:
    from pathlib import Path

    from boto3 import Session


@pytest.mark.parametrize("email", ["", "no-at", "a@b", "a b@c.d"])
def test_malformed_email_exits_2_before_any_aws_call(email: str) -> None:
    class NoAws:
        def client(self, *_a: Any, **_k: Any) -> None:
            msg = "no AWS call allowed"
            raise AssertionError(msg)

    with pytest.raises(SystemExit) as exc:
        add_researcher(cast("Session", NoAws()), email)
    assert exc.value.code == EXIT_BAD_EMAIL


def test_sdk_config_names_the_raw_bucket() -> None:
    outputs = dict.fromkeys(
        (
            "UserPoolId",
            "SdkClientId",
            "ManagedLoginBaseUrl",
            "IdentityPoolId",
            "ApiUrl",
        ),
        "v",
    )

    class Cfn:
        def describe_stacks(self, **_kwargs: str) -> dict[str, Any]:
            return {
                "Stacks": [
                    {
                        "Outputs": [
                            {"OutputKey": k, "OutputValue": v}
                            for k, v in outputs.items()
                        ]
                    }
                ]
            }

        def describe_stack_resources(self, **_kwargs: str) -> dict[str, Any]:
            return {
                "StackResources": [
                    {
                        "LogicalResourceId": "RawBucketAB12",
                        "ResourceType": "AWS::S3::Bucket",
                        "PhysicalResourceId": "the-raw-bucket",
                    }
                ]
            }

    class Fake:
        def client(self, _name: str) -> Cfn:
            return Cfn()

    assert sdk_config(cast("Session", Fake()))["raw_bucket"] == "the-raw-bucket"


SANDBOX_EXPORT = {
    "OutputKey": "SandboxTestClientId",
    "OutputValue": "abc",
    "ExportName": "asteroidea-SandboxTestClientId",
}
OTHER = {"OutputKey": "UserPoolId", "OutputValue": "p", "ExportName": "x"}


def test_deploy_refused_without_the_flag_when_the_export_is_deployed() -> None:
    """Dropping SANDBOX_TEST would delete an export ServicesStack imports."""
    refusal = deploy_refusal([OTHER, SANDBOX_EXPORT], sandbox_test=False)
    assert refusal is not None
    assert "asteroidea-SandboxTestClientId" in refusal
    assert "SANDBOX_TEST=1" in refusal


@pytest.mark.parametrize(
    ("outputs", "sandbox_test"),
    [
        ([OTHER, SANDBOX_EXPORT], True),
        ([OTHER], False),
        ([OTHER], True),
        ([], False),  # first deploy: no DataStack yet
    ],
)
def test_deploy_allowed_otherwise(
    outputs: list[dict[str, str]], *, sandbox_test: bool
) -> None:
    assert deploy_refusal(outputs, sandbox_test=sandbox_test) is None


def test_deploy_refusal_exit_code_is_2() -> None:
    assert EXIT_DEPLOY_REFUSED == 2


PLATFORM_OUTPUTS = {
    "UserPoolId": "us-east-1_pool",
    "SdkClientId": "sdk-client",
    "ManagedLoginBaseUrl": "https://asteroidea-id-0.auth.us-east-1.amazoncognito.com",
    "IdentityPoolId": "us-east-1:ipool",
    "ApiUrl": "https://api.example",
}


class _StubSession:
    """CloudFormation and Cognito stubs that record every call in order."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def client(self, name: str) -> Any:
        session = self

        class Client:
            def __getattr__(self, method: str) -> Any:
                def call(**kwargs: Any) -> dict[str, Any]:
                    session.calls.append((f"{name}.{method}", kwargs))
                    if method == "describe_stacks":
                        rows = [
                            {"OutputKey": k, "OutputValue": v}
                            for k, v in PLATFORM_OUTPUTS.items()
                        ]
                        return {"Stacks": [{"Outputs": rows}]}
                    if method == "describe_stack_resources":
                        bucket = {
                            "LogicalResourceId": "RawBucketAB12",
                            "ResourceType": "AWS::S3::Bucket",
                            "PhysicalResourceId": "the-raw-bucket",
                        }
                        return {"StackResources": [bucket]}
                    return {}

                return call

        return Client()


def test_researcher_creates_the_user_then_writes_platform_json(
    tmp_path: Path,
) -> None:
    """`make researcher` = add_researcher, then platform.json in DIR."""
    stub = _StubSession()
    target = tmp_path / "cfg" / "asteroidea"
    path = researcher(cast("Session", stub), "r@example.org", target)
    assert path == target / "platform.json"
    written = json.loads(path.read_text())
    assert written == sdk_config(cast("Session", _StubSession()))
    assert written["user_pool_id"] == "us-east-1_pool"
    assert written["raw_bucket"] == "the-raw-bucket"
    cognito = [(m, k) for m, k in stub.calls if m.startswith("cognito-idp.")]
    assert [m for m, _ in cognito] == [
        "cognito-idp.admin_create_user",
        "cognito-idp.admin_add_user_to_group",
    ]
    assert cognito[1][1] == {
        "UserPoolId": "us-east-1_pool",
        "Username": "r@example.org",
        "GroupName": "researchers",
    }


def test_researcher_with_a_bad_email_writes_nothing(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exc:
        researcher(cast("Session", _StubSession()), "no-at", tmp_path / "out")
    assert exc.value.code == EXIT_BAD_EMAIL
    assert not (tmp_path / "out").exists()


class NoSuchUserError(Exception):
    pass


def _resend_stub(*, user_exists: bool) -> _StubSession:
    """Build a stub whose Cognito client carries `exceptions` and can lack the user."""

    class Stub(_StubSession):
        @override
        def client(self, name: str) -> Any:
            inner = super().client(name)
            session = self

            class Exc:
                UsernameExistsException = type(
                    "UsernameExistsException", (Exception,), {}
                )
                UserNotFoundException = NoSuchUserError

            class Wrapped:
                exceptions = Exc

                def __getattr__(self, method: str) -> Any:
                    if method == "admin_create_user" and not user_exists:

                        def boom(**kwargs: Any) -> None:
                            session.calls.append((f"{name}.{method}", kwargs))
                            raise NoSuchUserError

                        return boom
                    return getattr(inner, method)

            return Wrapped()

    return Stub()


def _create_kwargs(stub: _StubSession) -> dict[str, Any]:
    return next(k for m, k in stub.calls if m == "cognito-idp.admin_create_user")


def test_resend_sends_message_action_and_the_same_attributes() -> None:
    plain, resend = _resend_stub(user_exists=True), _resend_stub(user_exists=True)
    add_researcher(cast("Session", plain), "r@example.org")
    add_researcher(cast("Session", resend), "r@example.org", resend=True)
    base, got = _create_kwargs(plain), _create_kwargs(resend)
    assert "MessageAction" not in base
    assert got.pop("MessageAction") == "RESEND"
    assert got == base
    assert [m for m, _ in resend.calls if m.startswith("cognito-idp.")][-1] == (
        "cognito-idp.admin_add_user_to_group"
    )


def test_resend_on_a_missing_user_refuses_naming_the_email() -> None:
    stub = _resend_stub(user_exists=False)
    with pytest.raises(SystemExit) as exc:
        add_researcher(cast("Session", stub), "gone@example.org", resend=True)
    assert "gone@example.org" in str(exc.value)
    assert not any(m.endswith("admin_add_user_to_group") for m, _ in stub.calls)


class _CuratorStub(_StubSession):
    """Cognito stub: `groups` for the user, or no such user when `groups` is None."""

    def __init__(self, groups: list[str] | None) -> None:
        super().__init__()
        self.groups = groups

    @override
    def client(self, name: str) -> Any:
        inner = super().client(name)
        outer = self

        class Exc:
            UserNotFoundException = NoSuchUserError

        class Wrapped:
            exceptions = Exc

            def __getattr__(self, method: str) -> Any:
                if method == "admin_list_groups_for_user":

                    def listed(**kwargs: Any) -> dict[str, Any]:
                        outer.calls.append((f"{name}.{method}", kwargs))
                        if outer.groups is None:
                            raise NoSuchUserError
                        return {"Groups": [{"GroupName": g} for g in outer.groups]}

                    return listed
                return getattr(inner, method)

        return Wrapped()


def test_curator_adds_a_researcher_to_curators() -> None:
    stub = _CuratorStub(["researchers"])
    add_curator(cast("Session", stub), "r@example.org")
    adds = [k for m, k in stub.calls if m.endswith("admin_add_user_to_group")]
    assert adds == [
        {
            "UserPoolId": "us-east-1_pool",
            "Username": "r@example.org",
            "GroupName": "curators",
        }
    ]


@pytest.mark.parametrize("groups", [[], ["curators"]])
def test_curator_refuses_a_user_outside_researchers(groups: list[str]) -> None:
    stub = _CuratorStub(groups)
    with pytest.raises(SystemExit) as exc:
        add_curator(cast("Session", stub), "r@example.org")
    assert exc.value.code not in (0, None)
    assert "r@example.org" in str(exc.value)
    assert "researchers" in str(exc.value)
    assert not any(m.endswith("admin_add_user_to_group") for m, _ in stub.calls)


def test_curator_refuses_a_missing_user() -> None:
    stub = _CuratorStub(None)
    with pytest.raises(SystemExit) as exc:
        add_curator(cast("Session", stub), "gone@example.org")
    assert "gone@example.org" in str(exc.value)
    assert not any(m.endswith("admin_add_user_to_group") for m, _ in stub.calls)


def test_curator_with_a_bad_email_makes_no_aws_call() -> None:
    stub = _CuratorStub(["researchers"])
    with pytest.raises(SystemExit) as exc:
        add_curator(cast("Session", stub), "no-at")
    assert exc.value.code == EXIT_BAD_EMAIL
    assert stub.calls == []
