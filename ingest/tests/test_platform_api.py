"""Platform API: GET /get-started."""

import io
import json
import shlex
from dataclasses import dataclass, field
from typing import Any, override

import pytest
from botocore.exceptions import ClientError

from asteroidea_infra.identity import SDK_CALLBACK, WEB_CALLBACK
from asteroidea_ingest import get_started_api
from asteroidea_ingest.get_started_api import PlatformApi

from .conftest import jwt_event

BUCKET = "site-bucket"
SDK_CLIENT = "sdk-client-id"
PLATFORM = {
    "region": "us-east-1",
    "user_pool_id": "us-east-1_pool",
    "client_id": SDK_CLIENT,
    "login_base_url": "https://login.example",
    "redirect_uri": SDK_CALLBACK,
    "identity_pool_id": "us-east-1:ident",
    "raw_bucket": "raw-bucket",
}
WHEEL = "asteroidea-0.1.0-py3-none-any.whl"
MANIFEST = {
    "wheel": {"filename": WHEEL, "sha256": "ab" * 32},
    "notebook": {"filename": "exemplar.ipynb"},
    "pins": ["numpy==2.5.4", "pandas==3.1.0"],
}
ROUTE = "GET /get-started"
DOMAIN = "abc.execute-api.us-east-1.amazonaws.com"
NOW = 1_000


@dataclass
class FakeS3:
    manifest: dict[str, Any] | None = field(default_factory=lambda: MANIFEST)
    presigned: list[dict[str, Any]] = field(default_factory=list)
    reads: list[str] = field(default_factory=list)

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        self.reads.append(f"{kwargs['Bucket']}/{kwargs['Key']}")
        if self.manifest is None:
            raise ClientError(
                {"Error": {"Code": "NoSuchKey", "Message": "missing"}}, "GetObject"
            )
        return {"Body": io.BytesIO(json.dumps(self.manifest).encode())}

    def list_objects_v2(self, **_kwargs: Any) -> dict[str, Any]:
        return {}

    def generate_presigned_url(self, **kwargs: Any) -> str:
        self.presigned.append(kwargs)
        return f"https://signed.example/{kwargs['Params']['Key']}"


def api(s3: Any, web_url: str = "https://d1.cloudfront.net") -> PlatformApi:
    return PlatformApi(
        s3=s3,
        bucket=BUCKET,
        platform=dict(PLATFORM),
        web_url=web_url,
    )


def test_200_body_shape() -> None:
    s3 = FakeS3()
    resp = api(s3).handle(jwt_event(ROUTE, sub="u", domainName=DOMAIN), now=NOW)
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert set(body) == {"platform", "wheel", "notebook", "pins", "install"}
    assert set(body["platform"]) == {*PLATFORM, "api_url"}
    assert len(body["platform"]) == 8
    assert body["platform"]["api_url"] == (
        "https://abc.execute-api.us-east-1.amazonaws.com"
    )
    assert body["wheel"] == {
        "url": f"https://signed.example/handout/{WHEEL}",
        "filename": WHEEL,
        "sha256": "ab" * 32,
        "expires_at": NOW + 3600,
    }
    assert body["notebook"] == {
        "url": "https://signed.example/handout/exemplar.ipynb",
        "filename": "exemplar.ipynb",
        "expires_at": NOW + 3600,
    }
    assert body["pins"] == MANIFEST["pins"]
    assert s3.reads == [f"{BUCKET}/handout/manifest.json"]


def test_install_is_the_short_command_text() -> None:
    resp = api(FakeS3()).handle(jwt_event(ROUTE, sub="u", domainName=DOMAIN), now=NOW)
    install = json.loads(resp["body"])["install"]
    wheel_url = f"https://d1.cloudfront.net/sdk/{WHEEL}"
    assert install.splitlines() == [
        ("Install uv first if it is missing: " + get_started_api.UV_INSTALL_URL),
        "Then run:",
        f"uvx --from {wheel_url} asteroidea-setup https://d1.cloudfront.net",
    ]
    assert "docs.astral.sh/uv/getting-started/installation/" in install
    assert "curl" not in install
    assert "|" not in install


class TokenS3(FakeS3):
    """A presign that carries a temporary-credential token, as the Lambda's does."""

    @override
    def generate_presigned_url(self, **kwargs: Any) -> str:
        self.presigned.append(kwargs)
        return (
            f"https://b.s3.amazonaws.com/{kwargs['Params']['Key']}"
            "?X-Amz-Credential=ASIAX&X-Amz-Security-Token=tok&X-Amz-Signature=s"
        )


def test_install_text_carries_no_credential_and_names_the_public_wheel() -> None:
    resp = api(TokenS3(), "https://app.example/").handle(
        jwt_event(ROUTE, sub="u", domainName=DOMAIN), now=NOW
    )
    install = json.loads(resp["body"])["install"]
    assert "X-Amz-" not in install
    wheel_url = shlex.quote(f"https://app.example/sdk/{WHEEL}")
    assert install.splitlines()[-1] == (
        f"uvx --from {wheel_url} asteroidea-setup https://app.example/"
    )


def test_install_command_quotes_the_filename_and_both_arguments() -> None:
    text = get_started_api.install_command(
        wheel_filename="a b+c-1.0-py3-none-any.whl", web_url="https://app.example"
    )
    last = text.splitlines()[-1]
    assert shlex.split(last) == [
        "uvx",
        "--from",
        "https://app.example/sdk/a%20b%2Bc-1.0-py3-none-any.whl",
        "asteroidea-setup",
        "https://app.example",
    ]


def test_a_manifest_without_pins_gets_503() -> None:
    manifest = {k: v for k, v in MANIFEST.items() if k != "pins"}
    s3 = FakeS3(manifest=manifest)
    resp = api(s3).handle(jwt_event(ROUTE, sub="u", domainName=DOMAIN), now=NOW)
    assert resp["statusCode"] == 503
    assert json.loads(resp["body"]) == {
        "error": "unavailable",
        "detail": get_started_api.STALE_MANIFEST,
    }
    assert not s3.presigned


def test_presign_names_the_handout_key_for_3600_seconds() -> None:
    s3 = FakeS3()
    api(s3).handle(jwt_event(ROUTE, sub="u", domainName=DOMAIN), now=NOW)
    assert s3.presigned == [
        {
            "ClientMethod": "get_object",
            "Params": {"Bucket": BUCKET, "Key": f"handout/{WHEEL}"},
            "ExpiresIn": 3600,
        },
        {
            "ClientMethod": "get_object",
            "Params": {"Bucket": BUCKET, "Key": "handout/exemplar.ipynb"},
            "ExpiresIn": 3600,
        },
    ]


@pytest.mark.parametrize("groups", ["[admins]", ""])
def test_caller_outside_researchers_gets_403(groups: str) -> None:
    s3 = FakeS3()
    resp = api(s3).handle(
        jwt_event(ROUTE, sub="u", groups=groups, domainName=DOMAIN), now=NOW
    )
    assert resp["statusCode"] == 403
    assert json.loads(resp["body"]) == {
        "error": "forbidden",
        "detail": "caller is not in the researchers group",
    }
    assert not s3.reads
    assert not s3.presigned


def test_missing_manifest_gets_503() -> None:
    s3 = FakeS3(manifest=None)
    resp = api(s3).handle(jwt_event(ROUTE, sub="u", domainName=DOMAIN), now=NOW)
    assert resp["statusCode"] == 503
    assert json.loads(resp["body"]) == {
        "error": "unavailable",
        "detail": "the SDK download is not published yet; "
        "ask the administrator to run make web-publish",
    }
    assert not s3.presigned


def test_other_s3_errors_are_a_logged_500(capsys: pytest.CaptureFixture[str]) -> None:
    class Denied(FakeS3):
        @override
        def get_object(self, **_kwargs: Any) -> dict[str, Any]:
            raise ClientError(
                {"Error": {"Code": "AccessDenied", "Message": "no"}}, "GetObject"
            )

    resp = api(Denied()).handle(jwt_event(ROUTE, sub="u", domainName=DOMAIN), now=NOW)
    assert resp["statusCode"] == 500
    assert json.loads(resp["body"])["error"] == "internal_error"
    assert '{"unhandled":"ClientError"' in capsys.readouterr().out


def test_platform_uses_the_sdk_client_and_callback_not_the_web_client() -> None:
    """The env mapping carries the SDK client id and callback, not the web one."""
    env = {name: f"v-{key}" for key, name in get_started_api.PLATFORM_ENV.items()}
    env["PLATFORM_SDK_CLIENT_ID"] = SDK_CLIENT
    env["PLATFORM_SDK_CALLBACK"] = SDK_CALLBACK
    platform = get_started_api.platform_from_env(env)
    assert platform["client_id"] == SDK_CLIENT
    assert platform["redirect_uri"] == SDK_CALLBACK
    assert WEB_CALLBACK not in platform.values()
    assert set(platform) | {"api_url"} == {*PLATFORM, "api_url"}


def test_handler_sends_artifact_routes_to_the_artifacts_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`/artifacts` routes use ARTIFACTS_BUCKET; `/get-started` keeps SITE_BUCKET."""
    s3 = FakeS3(manifest=MANIFEST)
    monkeypatch.setattr(get_started_api.boto3, "client", lambda *_a, **_k: s3)
    monkeypatch.setenv("SITE_BUCKET", BUCKET)
    monkeypatch.setenv("ARTIFACTS_BUCKET", "artifacts-bucket")
    monkeypatch.setenv("WEB_URL", "https://d1.cloudfront.net")
    for name in get_started_api.PLATFORM_ENV.values():
        monkeypatch.setenv(name, f"v-{name}")
    resp = get_started_api.handler(jwt_event("GET /artifacts", sub="u"), None)
    assert resp["statusCode"] == 200
    assert json.loads(resp["body"]) == {"artifacts": []}
    assert s3.reads == []


def test_handler_builds_its_s3_client_with_the_32_connection_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The "All" listing reads in parallel."""
    built: list[dict[str, Any]] = []

    def client(*args: Any, **kwargs: Any) -> FakeS3:
        built.append({"args": args, **kwargs})
        return FakeS3(manifest=MANIFEST)

    monkeypatch.setattr(get_started_api.boto3, "client", client)
    monkeypatch.setenv("SITE_BUCKET", BUCKET)
    monkeypatch.setenv("ARTIFACTS_BUCKET", "artifacts-bucket")
    monkeypatch.setenv("WEB_URL", "https://d1.cloudfront.net")
    for name in get_started_api.PLATFORM_ENV.values():
        monkeypatch.setenv(name, f"v-{name}")
    get_started_api.handler(jwt_event("GET /artifacts", sub="u"), None)
    (call,) = built
    assert call["args"] == ("s3",)
    # botocore's stubs do not declare Config's attributes; they are instance fields
    config = vars(call["config"])
    assert config["max_pool_connections"] == 32
    assert config["signature_version"] == "s3v4"
