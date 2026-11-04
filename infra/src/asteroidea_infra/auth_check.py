"""Sign-in and deny service checks (live only).

Run `python -m asteroidea_infra.auth_check pkce|pkce-exchange|deny`.

`pkce` builds a PKCE authorize URL for the `sdk` client, probes that Cognito accepts the
out-of-band redirect, and keeps the verifier and state in a 0600 file. Secrets are never
printed.
"""

import argparse
import base64
import contextlib
import hashlib
import json
import os
import secrets
import sys
import time
import urllib.parse
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import boto3
import duckdb
import polars as pl
import requests
from botocore.exceptions import ClientError

from asteroidea_infra import athena, config, schema
from asteroidea_infra.admin import session_for, stack_outputs
from asteroidea_infra.identity import SDK_CALLBACK
from asteroidea_infra.sandbox_guard import require_sandbox

if TYPE_CHECKING:
    from boto3 import Session

TIMEOUT: Final = 30
SCOPES: Final = "openid email profile"
STATE_MODE: Final = 0o600


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def write_private(path: Path, content: str) -> None:
    """Write `content` to `path` with mode 0600, never wider."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, STATE_MODE)
    with os.fdopen(fd, "w") as handle:
        handle.write(content)
    path.chmod(STATE_MODE)


def authorize_url(base: str, client_id: str, state: str, challenge: str) -> str:
    """Return the managed-login authorize URL for the PKCE S256 flow."""
    query = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "response_type": "code",
            "scope": SCOPES,
            "redirect_uri": SDK_CALLBACK,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        },
        quote_via=urllib.parse.quote,
    )
    return f"{base}/oauth2/authorize?{query}"


def pkce_start(state_file: Path) -> None:
    """Probe the authorize request, save the verifier and state, print the URL."""
    profile, account = require_sandbox()
    out = stack_outputs(session_for(profile, account))
    verifier = _b64(secrets.token_bytes(48))
    challenge = _b64(hashlib.sha256(verifier.encode()).digest())
    state = _b64(secrets.token_bytes(16))
    url = authorize_url(
        out["ManagedLoginBaseUrl"].rstrip("/"), out["SdkClientId"], state, challenge
    )
    probe = requests.get(url, allow_redirects=False, timeout=TIMEOUT)
    location = probe.headers.get("Location", "")
    mismatch = "redirect_mismatch" in location or "redirect_mismatch" in probe.text
    sys.stderr.write(
        f"Authorize probe: status {probe.status_code}, location "
        f"{urllib.parse.urlsplit(location).path or '(none)'}, "
        f"redirect_mismatch={mismatch}\n"
    )
    if mismatch or probe.status_code not in {302, 200}:
        sys.stderr.write("STOP: managed login rejected the redirect\n")
        raise SystemExit(1)
    write_private(
        state_file,
        json.dumps(
            {"code_verifier": verifier, "state": state, "client_id": out["SdkClientId"]}
        ),
    )
    sys.stdout.write(f"READY authorize-url {url}\n")


def _password() -> str:
    return "Aa1!" + secrets.token_urlsafe(18)


def _ensure_user(
    session: Session,
    pool: str,
    email: str,
    password: str,
    *,
    group: bool,
) -> None:
    """Create a test user with a permanent password; no email is sent."""
    cognito = session.client("cognito-idp")
    with contextlib.suppress(cognito.exceptions.UsernameExistsException):
        cognito.admin_create_user(
            UserPoolId=pool,
            Username=email,
            UserAttributes=[
                {"Name": "email", "Value": email},
                {"Name": "email_verified", "Value": "true"},
            ],
            MessageAction="SUPPRESS",
        )
    cognito.admin_set_user_password(
        UserPoolId=pool, Username=email, Password=password, Permanent=True
    )
    if group:
        cognito.admin_add_user_to_group(
            UserPoolId=pool, Username=email, GroupName="researchers"
        )


def _credentials(
    session: Session, out: dict[str, str], email: str, password: str
) -> dict[str, Any]:
    """Cognito login, then identity-pool credentials; raises ClientError if refused."""
    cognito = session.client("cognito-idp")
    auth = cognito.admin_initiate_auth(
        UserPoolId=out["UserPoolId"],
        ClientId=out["SandboxTestClientId"],
        AuthFlow="ADMIN_USER_PASSWORD_AUTH",
        AuthParameters={"USERNAME": email, "PASSWORD": password},
    )
    provider = f"cognito-idp.{config.REGION}.amazonaws.com/{out['UserPoolId']}"
    logins = {provider: auth["AuthenticationResult"]["IdToken"]}
    identity = session.client("cognito-identity")
    identity_id = identity.get_id(IdentityPoolId=out["IdentityPoolId"], Logins=logins)[
        "IdentityId"
    ]
    return identity.get_credentials_for_identity(IdentityId=identity_id, Logins=logins)[
        "Credentials"
    ]


def _creds_session(creds: dict[str, Any]) -> Session:
    """Return a boto3 session that signs with the identity-pool credentials."""
    return boto3.Session(
        region_name=config.REGION,
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretKey"],
        aws_session_token=creds["SessionToken"],
    )


class _Report:
    """Print PASS or FAIL per criterion and remember whether all passed."""

    def __init__(self) -> None:
        self.ok = True

    def __call__(self, name: str, *, passed: bool, detail: str) -> None:
        self.ok = self.ok and passed
        sys.stdout.write(f"{'PASS' if passed else 'FAIL'} {name}: {detail}\n")


def _deny_login(
    session: Session,
    out: dict[str, str],
    users: dict[str, tuple[str, str]],
    report: _Report,
) -> dict[str, Any]:
    """Criterion 1: login yields ResearcherRole; the group-less user is refused."""
    creds = _credentials(session, out, *users["researcher"])
    who = _creds_session(creds).client("sts").get_caller_identity()["Arn"]
    role_ok = "ResearcherRole" in who
    try:
        _credentials(session, out, *users["groupless"])
        refused = "credentials were issued"
        group_refused = False
    except ClientError as err:
        refused = err.response["Error"]["Code"]
        group_refused = True
    report(
        "1 login yields ResearcherRole; group-less refused",
        passed=role_ok and group_refused,
        detail=f"arn={who.split('assumed-role/')[-1]}; group-less={refused}",
    )
    return creds


def _deny_read(creds: dict[str, Any], report: _Report) -> None:
    """Criterion 2: DuckDB and Polars read the `assets` Iceberg table."""
    glue = _creds_session(creds).client("glue")
    table = glue.get_table(DatabaseName=schema.DATABASE, Name=schema.TABLE)["Table"]
    metadata = table["Parameters"]["metadata_location"]
    try:
        con = duckdb.connect()
        con.execute("INSTALL iceberg; LOAD iceberg; INSTALL httpfs; LOAD httpfs;")
        con.execute(
            "CREATE SECRET (TYPE S3, KEY_ID ?, SECRET ?, SESSION_TOKEN ?, REGION ?)",
            [
                creds["AccessKeyId"],
                creds["SecretKey"],
                creds["SessionToken"],
                config.REGION,
            ],
        )
        duck = con.execute(DUCKDB_COUNT_SQL, [metadata]).fetchone()
        frame = pl.scan_iceberg(
            metadata,
            storage_options={
                "s3.access-key-id": creds["AccessKeyId"],
                "s3.secret-access-key": creds["SecretKey"],
                "s3.session-token": creds["SessionToken"],
                "s3.region": config.REGION,
            },
        ).collect()
        report(
            "2 DuckDB and Polars read assets",
            passed=duck is not None and duck[0] == frame.height,
            detail=f"duckdb rows={duck[0] if duck is not None else None}, "
            f"polars rows={frame.height}",
        )
    except (duckdb.Error, pl.exceptions.PolarsError, OSError) as err:
        report(
            "2 DuckDB and Polars read assets",
            passed=False,
            detail=f"{type(err).__name__}: {err}",
        )


def _deny_athena(creds: dict[str, Any], report: _Report) -> None:
    """Criterion 3: an Athena count query succeeds as ResearcherRole."""
    athena_client = _creds_session(creds).client("athena")
    try:
        qid = athena_client.start_query_execution(
            QueryString=ATHENA_COUNT_SQL,
            QueryExecutionContext={"Database": schema.DATABASE},
            WorkGroup=athena.WORKGROUP,
        )["QueryExecutionId"]
        for _ in range(POLL_LIMIT):
            st = athena_client.get_query_execution(QueryExecutionId=qid)[
                "QueryExecution"
            ]["Status"]
            if st["State"] in {"SUCCEEDED", "FAILED", "CANCELLED"}:
                break
            time.sleep(2)
        rows = (
            athena_client.get_query_results(QueryExecutionId=qid)["ResultSet"]["Rows"]
            if st["State"] == "SUCCEEDED"
            else []
        )
        value = rows[1]["Data"][0].get("VarCharValue") if len(rows) > 1 else None
        report(
            "3 Athena query as ResearcherRole",
            passed=st["State"] == "SUCCEEDED" and value is not None,
            detail=(
                f"state={st['State']} count={value} {st.get('StateChangeReason', '')}"
            ),
        )
    except ClientError as err:
        report("3 Athena query as ResearcherRole", passed=False, detail=str(err))


def _deny_writes(
    session: Session, out: dict[str, str], creds: dict[str, Any], report: _Report
) -> None:
    """Criterion 4: all five writes into raw/ and landing/ are denied."""
    bucket = out.get("RawBucketName") or _raw_bucket(session)
    s3 = _creds_session(creds).client("s3")
    attempts = {
        "PutObject raw": lambda: s3.put_object(
            Bucket=bucket, Key="raw/deny-check/deny.txt", Body=b"x"
        ),
        "DeleteObject raw": lambda: s3.delete_object(
            Bucket=bucket, Key="raw/deny-check/deny.txt"
        ),
        "DeleteObjectVersion raw": lambda: s3.delete_object(
            Bucket=bucket, Key="raw/deny-check/deny.txt", VersionId="null"
        ),
        "PutObject landing": lambda: s3.put_object(
            Bucket=bucket, Key="landing/deny-check/deny.txt", Body=b"x"
        ),
        "CopyObject into raw": lambda: s3.copy_object(
            Bucket=bucket,
            Key="raw/deny-check/copy.txt",
            CopySource={"Bucket": bucket, "Key": "landing/deny-check/deny.txt"},
        ),
    }
    results: list[str] = []
    denied = 0
    for name, call in attempts.items():
        try:
            call()
            results.append(f"{name}=ALLOWED")
        except ClientError as err:
            code = err.response["Error"]["Code"]
            denied += code == "AccessDenied"
            results.append(f"{name}={code}")
    report(
        "4 all 5 writes denied",
        passed=denied == len(attempts),
        detail="; ".join(results),
    )


def deny_checks(secret_file: Path) -> bool:
    """Run the deny checks; print PASS or FAIL per criterion (passwords: 0600 file)."""
    profile, account = require_sandbox()
    session = session_for(profile, account)
    out = stack_outputs(session)
    users = {
        "researcher": (
            f"deny-researcher-{secrets.token_hex(3)}@example.invalid",
            _password(),
        ),
        "groupless": (
            f"deny-groupless-{secrets.token_hex(3)}@example.invalid",
            _password(),
        ),
    }
    write_private(secret_file, json.dumps(users))
    _ensure_user(session, out["UserPoolId"], *users["researcher"], group=True)
    _ensure_user(session, out["UserPoolId"], *users["groupless"], group=False)
    report = _Report()
    creds = _deny_login(session, out, users, report)
    _deny_read(creds, report)
    _deny_athena(creds, report)
    _deny_writes(session, out, creds, report)
    return report.ok


POLL_LIMIT: Final = 30
DUCKDB_COUNT_SQL: Final = "SELECT count(*) FROM iceberg_scan(?)"
ATHENA_COUNT_SQL: Final = schema.ASSETS_COUNT_SQL


def _raw_bucket(session: Session) -> str:
    """Return the raw bucket name from the DataStack resources."""
    cfn = session.client("cloudformation")
    for res in cfn.describe_stack_resources(StackName="DataStack")["StackResources"]:
        if (
            res["LogicalResourceId"].startswith("RawBucket")
            and res["ResourceType"] == "AWS::S3::Bucket"
        ):
            return str(res["PhysicalResourceId"])
    msg = "raw bucket not found in DataStack"
    raise SystemExit(msg)


def _token_request(base: str, form: dict[str, str]) -> dict[str, Any]:
    resp = requests.post(
        f"{base}/oauth2/token",
        data=form,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=TIMEOUT,
    )
    if resp.status_code != requests.codes.ok:
        sys.stderr.write(
            f"token endpoint {resp.status_code}: {resp.json().get('error')}\n"
        )
        raise SystemExit(1)
    return dict(resp.json())


def _claims(token: str) -> dict[str, Any]:
    payload = token.split(".")[1]
    return dict(
        json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    )


def pkce_exchange(state_file: Path, pasted: str, cache_dir: Path) -> None:
    """Exchange a pasted redirect URL, cache tokens, refresh, then get credentials."""
    profile, account = require_sandbox()
    session = session_for(profile, account)
    out = stack_outputs(session)
    base = out["ManagedLoginBaseUrl"].rstrip("/")
    saved = json.loads(state_file.read_text())
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(pasted).query)
    if query.get("state", [""])[0] != saved["state"]:
        sys.stderr.write("STOP: state mismatch\n")
        raise SystemExit(1)
    tokens = _token_request(
        base,
        {
            "grant_type": "authorization_code",
            "client_id": saved["client_id"],
            "code": query["code"][0],
            "redirect_uri": SDK_CALLBACK,
            "code_verifier": saved["code_verifier"],
        },
    )
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_dir.chmod(0o700)
    cache = cache_dir / "tokens.json"
    write_private(cache, json.dumps(tokens))
    sys.stdout.write(
        f"Code exchange (no browser on this machine; pasted URL exchanged): PASS, "
        f"keys={sorted(tokens)}\n"
        f"cache mode={oct(cache.stat().st_mode & 0o777)} "
        f"dir mode={oct(cache_dir.stat().st_mode & 0o777)}\n"
    )
    refreshed = _token_request(
        base,
        {
            "grant_type": "refresh_token",
            "client_id": saved["client_id"],
            "refresh_token": tokens["refresh_token"],
        },
    )
    new = _claims(refreshed["access_token"])
    old = _claims(tokens["access_token"])
    verdict = (
        "differs" if refreshed["access_token"] != tokens["access_token"] else "SAME"
    )
    sys.stdout.write(
        f"refresh: new access token {verdict}; exp={new['exp']} "
        f"(first exp={old['exp']}); groups={new.get('cognito:groups')}\n"
    )
    tokens.update(refreshed)
    write_private(cache, json.dumps(tokens))
    provider = f"cognito-idp.{config.REGION}.amazonaws.com/{out['UserPoolId']}"
    logins = {provider: refreshed["id_token"]}
    identity = session.client("cognito-identity")
    identity_id = identity.get_id(IdentityPoolId=out["IdentityPoolId"], Logins=logins)[
        "IdentityId"
    ]
    creds = identity.get_credentials_for_identity(
        IdentityId=identity_id, Logins=logins
    )["Credentials"]
    arn = _creds_session(creds).client("sts").get_caller_identity()["Arn"]
    sys.stdout.write(f"caller={arn.split('assumed-role/')[-1]}\n")


def main() -> None:
    """Dispatch the check."""
    parser = argparse.ArgumentParser()
    parser.add_argument("check", choices=["pkce", "pkce-exchange", "deny"])
    parser.add_argument("--state-file", type=Path, required=True)
    parser.add_argument("--pasted", default="")
    parser.add_argument("--cache-dir", type=Path, default=Path("tokens"))
    args = parser.parse_args()
    if args.check == "pkce":
        pkce_start(args.state_file)
    elif args.check == "pkce-exchange":
        pkce_exchange(args.state_file, args.pasted, args.cache_dir)
    elif not deny_checks(args.state_file):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
