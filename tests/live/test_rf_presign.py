"""Live test: the presign route and the raw bucket CORS.

Run with `make live T=tests/live/test_rf_presign.py` after `make deploy SANDBOX_TEST=1`
(DataStack and ServicesStack) and `make web-publish`. Collected only under `make live`
(`ASTEROIDEA_LIVE=1`); run by hand only.

Stand-in: sign-in is the headless `sandbox-test` client (the conftest
`auth` fixture). The browser is replaced by a plain HTTPS request that sends the
`Origin` header the browser would send. Nothing is written to the sandbox.

A `Content-Security-Policy` on the hosted page that blocks the S3 origin
would stop the browser fetch even though CORS passes. The CSP test writes the header
to stderr; a policy whose `connect-src` (or `default-src`) lacks `amazonaws.com` fails.
"""

import http.client
import json
import sys
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import urlsplit

import pytest

from asteroidea._http import send
from asteroidea_infra.admin import stack_outputs

if TYPE_CHECKING:
    from boto3 import Session

    from asteroidea._auth import Auth

DATASET: Final = "openneuro-ds004798"
RANGE_END: Final = 351  # a 352-byte header read
OK: Final = 200
PARTIAL: Final = 206
TIMEOUT_S: Final = 30


def _fetch(url: str, headers: dict[str, str]) -> tuple[int, dict[str, str]]:
    """GET an https URL; return the status and the lower-cased headers."""
    parts = urlsplit(url)
    assert parts.scheme == "https", url
    conn = http.client.HTTPSConnection(parts.netloc, timeout=TIMEOUT_S)
    try:
        target = parts.path + (f"?{parts.query}" if parts.query else "")
        conn.request("GET", target or "/", headers=headers)
        reply = conn.getresponse()
        reply.read(RANGE_END + 1)
        return reply.status, {k.lower(): v for k, v in reply.getheaders()}
    finally:
        conn.close()


@pytest.fixture(scope="module")
def web_url(admin_session: Session) -> str:
    """Return the hosted app URL (DataStack's `WebUrl` output)."""
    return stack_outputs(admin_session, "DataStack")["WebUrl"]


def _ranged_asset(assets: list[dict[str, Any]]) -> dict[str, Any]:
    """Return the first listed asset larger than the ranged read.

    A read of `bytes=0-RANGE_END` only answers `bytes 0-RANGE_END/size` when the
    object is larger than `RANGE_END + 1` bytes; a smaller object returns its whole
    length and the content-range assertion cannot hold.
    """
    for asset in assets:
        if int(asset["size_bytes"]) > RANGE_END + 1:
            return asset
    pytest.fail(f"no listed asset exceeds {RANGE_END + 1} bytes")


@pytest.fixture(scope="module")
def download(auth: Auth) -> dict[str, Any]:
    """Return the `/download` body of the first asset of the OpenNeuro exemplar.

    The asset is the first one in the listing whose `size_bytes` exceeds the
    ranged read (`_ranged_asset`).
    """
    base = auth.config.api_url.rstrip("/")
    headers = {"Authorization": f"Bearer {auth.tokens().access_token}"}
    listing = send("GET", f"{base}/catalog/datasets/{DATASET}/assets", headers)
    assert listing.status == OK, listing
    asset_id = _ranged_asset(json.loads(listing.body)["assets"])["asset_id"]
    reply = send("GET", f"{base}/catalog/assets/{asset_id}/download", headers)
    assert reply.status == OK, reply
    return dict(json.loads(reply.body))


def test_download_body_is_pinned_to_a_version(download: dict[str, Any]) -> None:
    assert download["expires_in"] == 300
    assert download["s3_version_id"]
    assert f"versionId={download['s3_version_id']}" in download["url"]


def test_ranged_get_from_the_hosted_origin_passes_cors(
    download: dict[str, Any], web_url: str
) -> None:
    status, headers = _fetch(
        download["url"], {"Origin": web_url, "Range": f"bytes=0-{RANGE_END}"}
    )
    assert status == PARTIAL
    assert headers["access-control-allow-origin"] == web_url
    size = download["size_bytes"]
    assert headers["content-range"] == f"bytes 0-{RANGE_END}/{size}"
    exposed = headers["access-control-expose-headers"].lower()
    assert "content-range" in exposed
    assert "accept-ranges" in exposed


def test_the_hosted_page_csp_does_not_block_s3(web_url: str) -> None:
    status, headers = _fetch(f"{web_url}/index.html", {})
    assert status == OK
    csp = headers.get("content-security-policy")
    sys.stderr.write(f"Content-Security-Policy on index.html: {csp!r}\n")
    if csp is None:
        return
    directives = {
        d.strip().split(" ", 1)[0]: d.strip() for d in csp.split(";") if d.strip()
    }
    governing = directives.get("connect-src") or directives.get("default-src")
    assert governing is None or "amazonaws.com" in governing, csp
