"""Deny test: a researcher cannot write the raw bucket.

Run through `make live T=tests/live/test_raw_bucket_deny.py`. It signs a researcher in
through the `sandbox-test` client (the `auth` fixture) and asserts, with the
identity-pool credentials ResearcherRole issues:

- the five writes are denied: PutObject raw, DeleteObject raw,
  DeleteObjectVersion raw, PutObject landing, CopyObject into raw;
- a user who is in no group is refused credentials.

Red proof: `ASTEROIDEA_DENY_REDPROOF=1` runs the same writes with the administrator
SSO profile instead of the researcher, which may write. The test then fails.
The switch never touches the deployed role, and it changes two things so nothing lands
in `raw/` or is deleted: every probe key is `landing/redproof-<token>/...` (the key is
printed; `landing/` expires in 3 days), and the two delete cases are skipped.
The red proof only works while the raw bucket's principal deny is not
deployed: once it is, the developer profile is denied too and the switch passes.
"""

import os
import secrets
import sys
from typing import TYPE_CHECKING, Any, Final

import pytest
from botocore.exceptions import ClientError

from asteroidea._creds import session
from asteroidea_infra.admin import raw_bucket_name, stack_outputs

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    import boto3

    from asteroidea._auth import Auth

RED_PROOF: Final = os.environ.get("ASTEROIDEA_DENY_REDPROOF") == "1"
PROBE: Final = f"landing/redproof-{secrets.token_hex(4)}"
DENIED: Final = "AccessDenied"
REFUSED: Final = "NotAuthorizedException"
DELETES: Final = frozenset({"DeleteObject raw", "DeleteObjectVersion raw"})
NAMES: Final = (
    "PutObject raw",
    "DeleteObject raw",
    "DeleteObjectVersion raw",
    "PutObject landing",
    "CopyObject into raw",
)


def _key(real: str) -> str:
    """Return the key the call targets: the fixed key, or a probe key (red proof)."""
    return f"{PROBE}/{real.replace('/', '-')}" if RED_PROOF else real


def _calls(s3: Any, bucket: str) -> dict[str, Callable[[], object]]:
    """Return the five writes by name."""
    landing = _key("landing/deny-check/deny.txt")
    if RED_PROOF:
        # The copy needs a source; seed it with the same (allowed) profile.
        s3.put_object(Bucket=bucket, Key=f"{PROBE}/source.txt", Body=b"x")
        source = f"{PROBE}/source.txt"
    else:
        source = landing
    return {
        "PutObject raw": lambda: s3.put_object(
            Bucket=bucket, Key=_key("raw/deny-check/deny.txt"), Body=b"x"
        ),
        "DeleteObject raw": lambda: s3.delete_object(
            Bucket=bucket, Key=_key("raw/deny-check/deny.txt")
        ),
        "DeleteObjectVersion raw": lambda: s3.delete_object(
            Bucket=bucket, Key=_key("raw/deny-check/deny.txt"), VersionId="null"
        ),
        "PutObject landing": lambda: s3.put_object(
            Bucket=bucket, Key=landing, Body=b"x"
        ),
        "CopyObject into raw": lambda: s3.copy_object(
            Bucket=bucket,
            Key=_key("raw/deny-check/copy.txt"),
            CopySource={"Bucket": bucket, "Key": source},
        ),
    }


@pytest.fixture(scope="module")
def bucket(admin_session: boto3.Session) -> str:
    return raw_bucket_name(admin_session)


@pytest.fixture(scope="module")
def writer(admin_session: boto3.Session, auth: Auth) -> boto3.Session:
    """Return the session whose writes are tried (the red proof swaps it)."""
    return admin_session if RED_PROOF else session(auth)


@pytest.mark.parametrize("name", NAMES)
def test_the_write_is_denied(name: str, writer: boto3.Session, bucket: str) -> None:
    if RED_PROOF and name in DELETES:
        pytest.skip("the red proof never deletes")
    s3 = writer.client("s3")
    if RED_PROOF:
        sys.stdout.write(f"red proof probe key prefix: s3://{bucket}/{PROBE}/\n")
    call = _calls(s3, bucket)[name]
    with pytest.raises(ClientError) as raised:
        call()
    assert raised.value.response["Error"]["Code"] == DENIED


def test_the_writer_is_the_researcher_role(writer: boto3.Session) -> None:
    """Guard: the denials above come from the policy, not from dead credentials."""
    if RED_PROOF:
        pytest.skip("the red proof signs as the developer profile")
    arn = writer.client("sts").get_caller_identity()["Arn"]
    assert "assumed-role/" in arn
    assert "ResearcherRole" in arn
    glue = writer.client("glue")
    table = glue.get_table(DatabaseName="asteroidea", Name="assets")["Table"]
    assert table["Name"] == "assets"


def test_a_user_in_no_group_is_refused_credentials(
    admin_session: boto3.Session, groupless_tokens: Mapping[str, Any]
) -> None:
    outputs = stack_outputs(admin_session)
    region = admin_session.region_name
    provider = f"cognito-idp.{region}.amazonaws.com/{outputs['UserPoolId']}"
    logins = {provider: groupless_tokens["IdToken"]}
    identity = admin_session.client("cognito-identity")
    identity_id = identity.get_id(
        IdentityPoolId=outputs["IdentityPoolId"], Logins=logins
    )["IdentityId"]
    with pytest.raises(ClientError) as raised:
        identity.get_credentials_for_identity(IdentityId=identity_id, Logins=logins)
    assert raised.value.response["Error"]["Code"] == REFUSED
