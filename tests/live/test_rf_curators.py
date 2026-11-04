"""Live test: `curators` keeps the researcher role.

Run with `make live T=tests/live/test_rf_curators.py` after
`make deploy SANDBOX_TEST=1` (DataStack). Collected only under `make live`
(`ASTEROIDEA_LIVE=1`); run by hand only.

A new sandbox-test user is added to `researchers` and `curators`. Its identity-pool
credentials must still come back (the `curators` group has no role ARN, so the
`researchers` group's ResearcherRole is the only candidate), and a fresh access
token's `cognito:groups` holds both. The user is deleted at the end.
"""

import base64
import contextlib
import json
import secrets
from typing import TYPE_CHECKING, Any, Final

import boto3
import pytest
from botocore.exceptions import ClientError

from asteroidea_infra.admin import sdk_config, stack_outputs

if TYPE_CHECKING:
    from collections.abc import Iterator

    from boto3 import Session

BOTH: Final = {"researchers", "curators"}


@pytest.fixture(scope="module")
def two_group_tokens(admin_session: Session) -> Iterator[dict[str, Any]]:
    """Yield the sign-in result of a new user in both groups; delete the user after."""
    outputs = stack_outputs(admin_session)
    pool = outputs["UserPoolId"]
    idp = admin_session.client("cognito-idp")
    email = f"rf-curators-{secrets.token_hex(3)}@example.invalid"
    password = "Aa1!" + secrets.token_urlsafe(18)
    idp.admin_create_user(
        UserPoolId=pool,
        Username=email,
        UserAttributes=[
            {"Name": "email", "Value": email},
            {"Name": "email_verified", "Value": "true"},
        ],
        MessageAction="SUPPRESS",
    )
    try:
        idp.admin_set_user_password(
            UserPoolId=pool, Username=email, Password=password, Permanent=True
        )
        for group in sorted(BOTH):
            idp.admin_add_user_to_group(
                UserPoolId=pool, Username=email, GroupName=group
            )
        result = idp.admin_initiate_auth(
            UserPoolId=pool,
            ClientId=outputs["SandboxTestClientId"],
            AuthFlow="ADMIN_USER_PASSWORD_AUTH",
            AuthParameters={"USERNAME": email, "PASSWORD": password},
        )["AuthenticationResult"]
        yield dict(result)
    finally:
        with contextlib.suppress(ClientError):
            idp.admin_delete_user(UserPoolId=pool, Username=email)


def _claims(token: str) -> dict[str, Any]:
    payload = token.split(".")[1]
    padded = payload + "=" * (-len(payload) % 4)
    return dict(json.loads(base64.urlsafe_b64decode(padded)))


def test_access_token_holds_both_groups(two_group_tokens: dict[str, Any]) -> None:
    groups = set(_claims(two_group_tokens["AccessToken"])["cognito:groups"])
    assert groups == BOTH


def test_two_group_user_still_gets_identity_pool_credentials(
    admin_session: Session, two_group_tokens: dict[str, Any]
) -> None:
    """A role on `curators` would make the mapping ambiguous and Deny the user."""
    config = sdk_config(admin_session)
    region = config["region"]
    provider = f"cognito-idp.{region}.amazonaws.com/{config['user_pool_id']}"
    identity = boto3.client("cognito-identity", region_name=region)
    logins = {provider: two_group_tokens["IdToken"]}
    identity_id = identity.get_id(
        IdentityPoolId=config["identity_pool_id"], Logins=logins
    )["IdentityId"]
    creds = identity.get_credentials_for_identity(
        IdentityId=identity_id, Logins=logins
    )["Credentials"]
    assert creds["AccessKeyId"]
    assert creds["SessionToken"]
