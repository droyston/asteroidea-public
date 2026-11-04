"""Live test: web import runs.

Run with `make live T=tests/live/test_rf_imports.py` after `make deploy SANDBOX_TEST=1`
of the stacks. Collected only under `make live` (`ASTEROIDEA_LIVE=1`); it needs
the sandbox environment.

The file: `dandi:000027@0.210831.2033`, `sub-RAT123/sub-RAT123.nwb`, 18,792 bytes (one
asset, CC0, "Test dataset for testing dandi-cli"). Nothing else in the platform uses
dandiset 000027, so importing it costs nothing a later test needs. Whether the extract
step accepts this small test NWB is unverified: a `FAIL extraction ...` outcome is a
real finding about the file, not about the route.

Re-runnable: the first run imports the file and the second finds it in the catalog.
The planner then invokes nothing and the run reads DONE with the file listed as
`already_imported` and outcome PASS, so both runs end with one file and one PASS.

Stand-ins: the curator is a new sandbox-test researcher added to
`curators` with `admin.add_curator` (not the browser sign-in), and its token comes from
the headless `sandbox-test` client. The user is deleted at the end. The run is polled
at most 10 minutes (the import itself takes the commit and extract queues).
"""

import contextlib
import json
import secrets
import time
from typing import TYPE_CHECKING, Any, Final

import pytest
from botocore.exceptions import ClientError

from asteroidea._http import send
from asteroidea_infra.admin import add_curator, stack_outputs

if TYPE_CHECKING:
    from collections.abc import Iterator

    from boto3 import Session

    from asteroidea._auth import Auth

SOURCE: Final = "dandi:000027@0.210831.2033"
RELPATH: Final = "sub-RAT123/sub-RAT123.nwb"
FILE_BYTES: Final = 18_792
BUDGET_SECONDS: Final = 600
POLL_SECONDS: Final = 5
ACCEPTED: Final = 202
FORBIDDEN: Final = 403
NOT_FOUND: Final = 404
BAD_REQUEST: Final = 400
TERMINAL: Final = {"DONE", "FAILED", "REFUSED"}


def _call(
    api_url: str,
    token: str,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    headers = {"Authorization": f"Bearer {token}"}
    payload = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        payload = json.dumps(body).encode()
    reply = send(method, f"{api_url.rstrip('/')}{path}", headers, payload)
    return reply.status, json.loads(reply.body) if reply.body else {}


@pytest.fixture(scope="module")
def curator_token(admin_session: Session) -> Iterator[str]:
    """Yield the access token of a new curator; delete the user after."""
    outputs = stack_outputs(admin_session)
    pool = outputs["UserPoolId"]
    idp = admin_session.client("cognito-idp")
    email = f"rf-imports-{secrets.token_hex(3)}@example.invalid"
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
        idp.admin_add_user_to_group(
            UserPoolId=pool, Username=email, GroupName="researchers"
        )
        add_curator(admin_session, email)  # the helper under test
        result = idp.admin_initiate_auth(
            UserPoolId=pool,
            ClientId=outputs["SandboxTestClientId"],
            AuthFlow="ADMIN_USER_PASSWORD_AUTH",
            AuthParameters={"USERNAME": email, "PASSWORD": password},
        )["AuthenticationResult"]
        yield str(result["AccessToken"])
    finally:
        with contextlib.suppress(ClientError):
            idp.admin_delete_user(UserPoolId=pool, Username=email)


def test_a_non_curator_gets_403_on_every_route(auth: Auth) -> None:
    url, token = auth.config.api_url, auth.tokens().access_token
    status, body = _call(
        url, token, "POST", "/imports", {"source": SOURCE, "sandbox": True}
    )
    assert status == FORBIDDEN, body
    assert _call(url, token, "GET", "/imports")[0] == FORBIDDEN
    assert _call(url, token, "GET", "/imports/anything")[0] == FORBIDDEN


def test_a_curator_imports_one_small_file_and_it_reads_pass(
    auth: Auth, curator_token: str
) -> None:
    url = auth.config.api_url
    status, body = _call(
        url,
        curator_token,
        "POST",
        "/imports",
        {"source": SOURCE, "include": [RELPATH], "sandbox": True},
    )
    assert status == ACCEPTED, body
    run_id = body["run_id"]

    deadline = time.monotonic() + BUDGET_SECONDS
    run: dict[str, Any] = {}
    while time.monotonic() < deadline:
        status, run = _call(url, curator_token, "GET", f"/imports/{run_id}")
        assert status == 200, run
        if run["state"] in TERMINAL:
            break
        time.sleep(POLL_SECONDS)
    assert run["state"] == "DONE", run
    (file,) = run["files"]
    assert file["relpath"] == RELPATH
    assert file["outcome"] == "PASS", file  # imported now, or already_imported: True

    listed = _call(url, curator_token, "GET", "/imports")[1]["runs"]
    assert run_id in [r["run_id"] for r in listed]


def test_a_bad_source_is_400_and_an_unknown_run_is_404(
    auth: Auth, curator_token: str
) -> None:
    url = auth.config.api_url
    status, body = _call(
        url,
        curator_token,
        "POST",
        "/imports",
        {"source": "nope", "include": [], "sandbox": True},
    )
    assert status == BAD_REQUEST, body
    assert _call(url, curator_token, "GET", "/imports/not-a-run")[0] == NOT_FOUND
