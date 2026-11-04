"""Admin commands through boto3 (live): add a researcher, print the SDK config.

`make researcher EMAIL=<e> [DIR=<d>]` (`researcher-add` is its alias):
create the user, then write `<d>/platform.json`. `make sdk-config > platform.json`
prints the same content. `deploy-check` is what `make deploy` runs first.
Exit codes: 2 malformed email or a refused deploy, 1 user exists or stack lookup fails.
"""

import argparse
import json
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import boto3
from botocore.exceptions import ClientError

from asteroidea_infra import config
from asteroidea_infra.identity import CURATORS_GROUP, GROUP, SDK_CALLBACK, export_name
from asteroidea_infra.sandbox_guard import require_sandbox

if TYPE_CHECKING:
    from boto3 import Session

EMAIL_RE: Final = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
EXIT_BAD_EMAIL: Final = 2
EXIT_DEPLOY_REFUSED: Final = 2
SANDBOX_TEST_EXPORT: Final = export_name("SandboxTestClientId")


def session_for(profile: str, account: str) -> Session:
    """Return a boto3 session, refusing a profile in the wrong account."""
    session = boto3.Session(profile_name=profile, region_name=config.REGION)
    got = session.client("sts").get_caller_identity()["Account"]
    if got != account:
        msg = f"profile {profile} is account {got}, expected {account}"
        raise SystemExit(msg)
    return session


def stack_outputs(session: Session, stack: str = "ServicesStack") -> dict[str, str]:
    """Return a deployed stack's outputs; exit 1 when the stack is missing."""
    try:
        described = session.client("cloudformation").describe_stacks(StackName=stack)
    except ClientError as err:
        msg = f"cannot read stack {stack}: {err}"
        raise SystemExit(msg) from err
    return {o["OutputKey"]: o["OutputValue"] for o in described["Stacks"][0]["Outputs"]}


def deploy_refusal(
    data_outputs: list[dict[str, str]],
    *,
    sandbox_test: bool,
) -> str | None:
    """Return why `make deploy` must refuse, or None to go ahead.

    `data_outputs` is the deployed DataStack's `Outputs` list (empty before the
    first deploy). A deploy without the sandbox-test client would delete DataStack's
    `SandboxTestClientId` export while the deployed ServicesStack still imports it,
    which CloudFormation refuses mid-deploy; refuse before anything starts.
    """
    exported = {o.get("ExportName") for o in data_outputs}
    if SANDBOX_TEST_EXPORT in exported and not sandbox_test:
        return (
            f"refusing: the deployed DataStack exports {SANDBOX_TEST_EXPORT} and "
            "ServicesStack imports it; deploy with SANDBOX_TEST=1"
        )
    return None


def deployed_outputs(session: Session, stack: str) -> list[dict[str, str]]:
    """Return a deployed stack's `Outputs`, or [] when the stack does not exist."""
    try:
        described = session.client("cloudformation").describe_stacks(StackName=stack)
    except ClientError as err:
        if "does not exist" in str(err):
            return []
        raise
    return list(described["Stacks"][0].get("Outputs", []))


def add_researcher(session: Session, email: str, *, resend: bool = False) -> None:
    """Create the user (Cognito emails a temporary password) and add the group.

    With `resend` the user must already exist: Cognito re-sends the invitation
    (current template, new temporary password) and the group add is idempotent.
    """
    if not EMAIL_RE.match(email):
        sys.stderr.write(f"malformed email: {email}\n")
        raise SystemExit(EXIT_BAD_EMAIL)
    pool = stack_outputs(session)["UserPoolId"]
    cognito = session.client("cognito-idp")
    extra = {"MessageAction": "RESEND"} if resend else {}
    try:
        cognito.admin_create_user(
            UserPoolId=pool,
            Username=email,
            UserAttributes=[
                {"Name": "email", "Value": email},
                {"Name": "email_verified", "Value": "true"},
            ],
            DesiredDeliveryMediums=["EMAIL"],
            **extra,
        )
    except cognito.exceptions.UsernameExistsException as err:
        msg = f"user exists: {email}"
        raise SystemExit(msg) from err
    except cognito.exceptions.UserNotFoundException as err:
        msg = f"cannot resend: no such user: {email}"
        raise SystemExit(msg) from err
    cognito.admin_add_user_to_group(UserPoolId=pool, Username=email, GroupName=GROUP)
    verb = "invitation re-sent to" if resend else "created"
    sys.stdout.write(f"{verb} {email} in group {GROUP}; temporary password emailed\n")


def add_curator(session: Session, email: str) -> None:
    """Add an existing researcher to `curators`; refuse a user outside `researchers`."""
    if not EMAIL_RE.match(email):
        sys.stderr.write(f"malformed email: {email}\n")
        raise SystemExit(EXIT_BAD_EMAIL)
    pool = stack_outputs(session)["UserPoolId"]
    cognito = session.client("cognito-idp")
    try:
        listed = cognito.admin_list_groups_for_user(UserPoolId=pool, Username=email)
    except cognito.exceptions.UserNotFoundException as err:
        msg = f"no such user: {email}"
        raise SystemExit(msg) from err
    names = {g["GroupName"] for g in listed.get("Groups", [])}
    if GROUP not in names:
        msg = f"{email} is not in the {GROUP} group; run make researcher first"
        raise SystemExit(msg)
    cognito.admin_add_user_to_group(
        UserPoolId=pool, Username=email, GroupName=CURATORS_GROUP
    )
    sys.stdout.write(f"added {email} to group {CURATORS_GROUP}\n")


def raw_bucket_name(session: Session) -> str:
    """Return the raw bucket's name from the DataStack resources."""
    cfn = session.client("cloudformation")
    for res in cfn.describe_stack_resources(StackName="DataStack")["StackResources"]:
        if (
            res["LogicalResourceId"].startswith("RawBucket")
            and res["ResourceType"] == "AWS::S3::Bucket"
        ):
            return str(res["PhysicalResourceId"])
    msg = "raw bucket not found in DataStack"
    raise SystemExit(msg)


def sdk_config(session: Session) -> dict[str, Any]:
    """Return the SDK's `platform.json` content from the stack outputs."""
    out = stack_outputs(session)
    return {
        "region": config.REGION,
        "user_pool_id": out["UserPoolId"],
        "client_id": out["SdkClientId"],
        "login_base_url": out["ManagedLoginBaseUrl"],
        "redirect_uri": SDK_CALLBACK,
        "identity_pool_id": out["IdentityPoolId"],
        "api_url": out["ApiUrl"],
        "raw_bucket": raw_bucket_name(session),
    }


def researcher(
    session: Session, email: str, directory: Path, *, resend: bool = False
) -> Path:
    """Create the researcher, then write `directory/platform.json`; return its path.

    The config is read first (stack lookups only), so a missing stack exits before
    any user is created.
    """
    platform = sdk_config(session)
    add_researcher(session, email, resend=resend)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "platform.json"
    path.write_text(json.dumps(platform, indent=2) + "\n")
    sys.stdout.write(f"wrote {path}\n")
    return path


def main() -> None:
    """Dispatch `add`, `researcher`, `curator`, `config` or `deploy-check`."""
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    add = sub.add_parser("add")
    add.add_argument("email")
    new = sub.add_parser("researcher")
    new.add_argument("email")
    new.add_argument("--dir", type=Path, required=True)
    new.add_argument("--resend", action="store_true")
    cur = sub.add_parser("curator")
    cur.add_argument("email")
    sub.add_parser("config")
    check = sub.add_parser("deploy-check")
    check.add_argument("--sandbox-test", choices=("0", "1"), required=True)
    args = parser.parse_args()
    profile, account = require_sandbox()
    session = session_for(profile, account)
    if args.command == "add":
        add_researcher(session, args.email)
    elif args.command == "researcher":
        researcher(session, args.email, args.dir.expanduser(), resend=args.resend)
    elif args.command == "curator":
        add_curator(session, args.email)
    elif args.command == "deploy-check":
        refusal = deploy_refusal(
            deployed_outputs(session, "DataStack"),
            sandbox_test=args.sandbox_test == "1",
        )
        if refusal is not None:
            sys.stderr.write(refusal + "\n")
            raise SystemExit(EXIT_DEPLOY_REFUSED)
    else:
        sys.stdout.write(json.dumps(sdk_config(session), indent=2) + "\n")


if __name__ == "__main__":
    main()
