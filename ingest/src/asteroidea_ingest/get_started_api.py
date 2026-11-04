"""Platform Lambda: `GET /get-started`.

The JWT authorizer (access token) runs before this code and answers 401. This module
adds the group check (403) and the missing-manifest refusal (503).

`handout/manifest.json` (site bucket, top-level `handout/` prefix; `make web-publish`
writes it) is a JSON object:

    {"wheel": {"filename": "<wheel file name>", "sha256": "<hex digest>"},
     "notebook": {"filename": "<notebook file name>"},
     "pins": ["<requirement>", ...]}

`pins` is `notebook.NOTEBOOK_PINS` (exact requirements for the notebook venv);
the response carries them from the manifest only. The hosted app address
(`asteroidea-setup`'s argument) is the function's `WEB_URL` environment
value. Each file sits at `handout/<filename>`. Extra manifest fields are ignored here.

The install command names the public wheel copy, `<WEB_URL>/sdk/<wheel filename>`
(`make web-publish` puts it under `site/sdk/`). The command carries no signed URL,
so no temporary credential reaches shell history or the clipboard. The `wheel` and
`notebook` parts of the response still carry presigned `handout/` links.
"""

import json
import os
import shlex
import urllib.parse
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

import boto3
from botocore.exceptions import ClientError

from asteroidea_ingest.api_common import (
    ApiError,
    Invocation,
    Request,
    Response,
    Timing,
    json_response,
    serve,
)
from asteroidea_ingest.artifacts import S3_CLIENT_CONFIG, ArtifactsApi
from asteroidea_ingest.aws_types import S3_NOT_FOUND
from asteroidea_ingest.notes_api import handler_api as notes_api

if TYPE_CHECKING:
    from collections.abc import Mapping

    from asteroidea_ingest.aws_types import S3

HANDOUT_PREFIX: Final = "handout"
MANIFEST_KEY: Final = f"{HANDOUT_PREFIX}/manifest.json"
URL_SECONDS: Final = 3600
# PlatformFunction's other routes: `/artifacts` (`artifacts.DISPATCH`) and `/notes`
# (`notes_api.DISPATCH`).
DISPATCH: Final = {"GET /get-started": "get_started"}
UV_INSTALL_URL: Final = "https://docs.astral.sh/uv/getting-started/installation/"
STALE_MANIFEST: Final = (
    "the SDK download is out of date (no pins); "
    "ask the administrator to run make web-publish"
)
NOT_PUBLISHED: Final = (
    "the SDK download is not published yet; "
    "ask the administrator to run make web-publish"
)
# Function environment value -> `platform.json` key (set by CDK; the SDK's loader
# reads exactly these eight keys). `api_url` is not here: it comes from the request.
PLATFORM_ENV: Final = {
    "region": "PLATFORM_REGION",
    "user_pool_id": "PLATFORM_USER_POOL_ID",
    "client_id": "PLATFORM_SDK_CLIENT_ID",
    "login_base_url": "PLATFORM_LOGIN_BASE_URL",
    "redirect_uri": "PLATFORM_SDK_CALLBACK",
    "identity_pool_id": "PLATFORM_IDENTITY_POOL_ID",
    "raw_bucket": "PLATFORM_RAW_BUCKET",
}


@dataclass(frozen=True)
class PlatformApi:
    """`GET /get-started` over an S3 client and the platform values."""

    s3: S3
    bucket: str
    platform: dict[str, str]
    web_url: str

    def handle(
        self,
        event: dict[str, Any],
        *,
        now: int | None = None,
        invocation: Invocation | None = None,
    ) -> Response:
        """Answer one HTTP API (payload v2) event."""
        return serve(
            event,
            api=self,
            dispatch=DISPATCH,
            invocation=(invocation or Invocation()).at(now),
        )

    def get_started(self, request: Request, _timing: Timing) -> Response:
        """GET /get-started: the platform values, the handout links, the command."""
        manifest = self._manifest()
        if manifest is None:
            raise ApiError(503, NOT_PUBLISHED)
        if not isinstance(manifest.get("pins"), list):
            raise ApiError(503, STALE_MANIFEST)
        platform = {**self.platform, "api_url": f"https://{request.domain}"}
        return json_response(200, self.body(platform, manifest, request.now))

    def body(
        self, platform: dict[str, str], manifest: dict[str, Any], now: int
    ) -> dict[str, Any]:
        """Return the response body; the extension point for further fields."""
        expires_at = now + URL_SECONDS
        wheel = str(manifest["wheel"]["filename"])
        notebook = str(manifest["notebook"]["filename"])
        shown = {key: platform[key] for key in sorted(platform)}
        wheel_part = {
            "url": self._presign(wheel),
            "filename": wheel,
            "sha256": str(manifest["wheel"]["sha256"]),
            "expires_at": expires_at,
        }
        notebook_part = {
            "url": self._presign(notebook),
            "filename": notebook,
            "expires_at": expires_at,
        }
        return {
            "platform": shown,
            "wheel": wheel_part,
            "notebook": notebook_part,
            "pins": [str(pin) for pin in manifest["pins"]],
            "install": install_command(wheel_filename=wheel, web_url=self.web_url),
        }

    def _presign(self, filename: str) -> str:
        return self.s3.generate_presigned_url(
            ClientMethod="get_object",
            Params={"Bucket": self.bucket, "Key": f"{HANDOUT_PREFIX}/{filename}"},
            ExpiresIn=URL_SECONDS,
        )

    def _manifest(self) -> dict[str, Any] | None:
        try:
            got = self.s3.get_object(Bucket=self.bucket, Key=MANIFEST_KEY)
        except ClientError as err:
            if err.response.get("Error", {}).get("Code") in S3_NOT_FOUND:
                return None
            raise
        manifest: dict[str, Any] = json.loads(got["Body"].read())
        return manifest


def install_command(*, wheel_filename: str, web_url: str) -> str:
    """Return the short command text the Get started page shows.

    It names the uv install page (no pipe-to-shell line) and the one `uvx` command.
    The wheel URL is `<web_url>/sdk/<filename>`, a public path with no credential;
    both arguments are shell-quoted.
    """
    q = shlex.quote
    wheel_url = f"{web_url.rstrip('/')}/sdk/{urllib.parse.quote(wheel_filename)}"
    return (
        f"Install uv first if it is missing: {UV_INSTALL_URL}\n"
        f"Then run:\n"
        f"uvx --from {q(wheel_url)} asteroidea-setup {q(web_url)}"
    )


def platform_from_env(environ: Mapping[str, str]) -> dict[str, str]:
    """Return the seven environment-backed `platform.json` values."""
    return {key: environ[name] for key, name in PLATFORM_ENV.items()}


def handler(event: dict[str, Any], context: object) -> Response:
    """Lambda entry point: `/artifacts` to `ArtifactsApi`, `/notes` to `NotesApi`.

    Every other route is get-started. The S3 client is s3v4 with a 32-connection
    pool, for the parallel listing.
    """
    invocation = Invocation.of(context)
    path = event["routeKey"].split(" ", 1)[1]
    if path == "/notes":
        return notes_api().handle(event, invocation=invocation)
    s3 = boto3.client("s3", config=S3_CLIENT_CONFIG)
    if path.startswith("/artifacts"):
        artifacts = ArtifactsApi(s3=s3, bucket=os.environ["ARTIFACTS_BUCKET"])
        return artifacts.handle(event, invocation=invocation)
    api = PlatformApi(
        s3=s3,
        bucket=os.environ["SITE_BUCKET"],
        platform=platform_from_env(os.environ),
        web_url=os.environ["WEB_URL"],
    )
    return api.handle(event, invocation=invocation)
