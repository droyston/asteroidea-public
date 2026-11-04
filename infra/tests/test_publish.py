"""`make web-publish`: fake S3 and CloudFront, no AWS."""

import dataclasses
import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest

from asteroidea._config import PlatformConfig
from asteroidea_infra import admin, publish
from asteroidea_infra.notebook import NOTEBOOK_PINS

if TYPE_CHECKING:
    from collections.abc import Sequence

    from boto3 import Session

REPO = Path(__file__).resolve().parents[2]
BUCKET = "site-bucket"
SCHEMA_TS = (REPO / "web" / "src" / "index" / "schema.ts").read_text()
INDEX_SCHEMA = next(
    line.split('"')[1] for line in SCHEMA_TS.splitlines() if "INDEX_SCHEMA =" in line
)
DATA = {"WebUrl": "https://d1.cloudfront.net", "SiteBucketName": BUCKET}
DATA["DistributionId"] = "EDIST"
SERVICES = {
    "WebClientId": "webclient",
    "ManagedLoginBaseUrl": "https://asteroidea-1.auth.us-east-1.amazoncognito.com",
    "ApiUrl": "https://api.example",
    "UserPoolId": "pool",
    "SdkClientId": "sdkclient",
    "IdentityPoolId": "idpool",
}


class FakeS3:
    def __init__(self, existing: dict[str, bytes] | None = None) -> None:
        self.objects: dict[str, dict[str, Any]] = {
            k: {"Body": v} for k, v in (existing or {}).items()
        }
        self.deleted: list[str] = []

    def put_object(self, **kw: Any) -> dict[str, Any]:
        assert kw["Bucket"] == BUCKET
        self.objects[kw["Key"]] = kw
        return {}

    def list_objects_v2(self, **kw: Any) -> dict[str, Any]:
        keys = sorted(k for k in self.objects if k.startswith(kw["Prefix"]))
        return {"Contents": [{"Key": k} for k in keys], "IsTruncated": False}

    def delete_objects(self, **kw: Any) -> dict[str, Any]:
        for obj in kw["Delete"]["Objects"]:
            self.deleted.append(obj["Key"])
            del self.objects[obj["Key"]]
        return {}


class FakeCloudFront:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def create_invalidation(self, **kw: Any) -> dict[str, Any]:
        self.calls.append(kw)
        return {"Invalidation": {"Id": "INV1"}}


class FakeSession:
    def client(self, name: str) -> Any:
        assert name == "cloudformation"
        return self

    def describe_stack_resources(self, **kw: str) -> dict[str, Any]:
        assert kw["StackName"] == "DataStack"
        return {
            "StackResources": [
                {
                    "LogicalResourceId": "RawBucketAB12",
                    "ResourceType": "AWS::S3::Bucket",
                    "PhysicalResourceId": "raw-bucket",
                }
            ]
        }

    def describe_stacks(self, **kw: str) -> dict[str, Any]:
        out = DATA if kw["StackName"] == "DataStack" else SERVICES
        return {
            "Stacks": [
                {
                    "Outputs": [
                        {"OutputKey": k, "OutputValue": v} for k, v in out.items()
                    ]
                }
            ]
        }


@pytest.fixture
def tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    dist = tmp_path / "web" / "dist"
    (dist / "assets").mkdir(parents=True)
    files = {
        "index.html": "<html>",
        "index.json": "[]",
        "favicon.svg": "<svg/>",
        "logo.png": "png",
        "assets/app-abc.js": "js",
        "assets/app-abc.css": "css",
    }
    for rel, body in files.items():
        (dist / rel).write_text(body)
    wheels = tmp_path / "dist"
    wheels.mkdir()
    (wheels / "asteroidea-0.1.0-py3-none-any.whl").write_bytes(b"wheel-bytes")
    nb = tmp_path / "exemplar.ipynb"
    nb.write_text("{}")
    public = tmp_path / "web" / "public"
    public.mkdir(parents=True)
    (public / "index.json").write_text(json.dumps({"schema": INDEX_SCHEMA}))
    monkeypatch.setattr(publish, "INDEX_JSON", public / "index.json")
    monkeypatch.setattr(publish, "DIST", dist)
    monkeypatch.setattr(publish, "WHEEL_DIR", wheels)
    monkeypatch.setattr(publish, "NOTEBOOK", nb)
    return tmp_path


def run(
    s3: FakeS3, cf: FakeCloudFront, *, built: list[Any] | None = None
) -> tuple[int, str]:
    lines: list[str] = []

    def build(cmd: Sequence[str], env: dict[str, str] | None) -> None:
        if built is not None:
            built.append((list(cmd), env))

    code = publish.publish(
        cast("Session", FakeSession()),
        build=build,
        s3=s3,
        cloudfront=cf,
        out=lines.append,
    )
    return code, "".join(lines)


@pytest.mark.usefixtures("tree")
def test_content_types_and_cache_control() -> None:
    s3 = FakeS3()
    code, text = run(s3, FakeCloudFront())
    assert code == 0, text
    want = {
        "site/index.html": ("text/html", "no-cache"),
        "site/index.json": ("application/json", "no-cache"),
        "site/favicon.svg": ("image/svg+xml", "no-cache"),
        "site/logo.png": ("image/png", "no-cache"),
        "site/assets/app-abc.js": ("text/javascript", publish.IMMUTABLE),
        "site/assets/app-abc.css": ("text/css", publish.IMMUTABLE),
    }
    for key, (ctype, cache) in want.items():
        assert s3.objects[key]["ContentType"] == ctype, key
        assert s3.objects[key]["CacheControl"] == cache, key
    assert publish.IMMUTABLE == "public, max-age=31536000, immutable"
    wheel = s3.objects["handout/asteroidea-0.1.0-py3-none-any.whl"]
    assert wheel["ContentType"] == "application/zip"
    assert s3.objects["handout/exemplar.ipynb"]["ContentType"] == "application/json"


@pytest.mark.usefixtures("tree")
def test_wheel_is_also_served_publicly_from_site_sdk() -> None:
    s3 = FakeS3()
    code, _ = run(s3, FakeCloudFront())
    assert code == 0
    public = s3.objects["site/sdk/asteroidea-0.1.0-py3-none-any.whl"]
    assert public["ContentType"] == "application/zip"
    handout = s3.objects["handout/asteroidea-0.1.0-py3-none-any.whl"]
    assert public["Body"] == handout["Body"]


@pytest.mark.usefixtures("tree")
def test_old_public_wheels_survive_the_stale_sweep() -> None:
    s3 = FakeS3({"site/sdk/x.whl": b"old", "site/gone.html": b"x"})
    code, _ = run(s3, FakeCloudFront())
    assert code == 0
    assert "site/gone.html" in s3.deleted
    assert "site/sdk/x.whl" not in s3.deleted
    assert "site/sdk/x.whl" in s3.objects


@pytest.mark.usefixtures("tree")
def test_manifest_has_sha256_and_pins_from_the_single_source() -> None:
    s3 = FakeS3()
    code, _ = run(s3, FakeCloudFront())
    assert code == 0
    body = s3.objects["handout/manifest.json"]
    manifest = json.loads(body["Body"])
    assert manifest == {
        "wheel": {
            "filename": "asteroidea-0.1.0-py3-none-any.whl",
            "sha256": hashlib.sha256(b"wheel-bytes").hexdigest(),
        },
        "notebook": {"filename": "exemplar.ipynb"},
        "pins": list(NOTEBOOK_PINS),
    }
    assert len(manifest["pins"]) > 0
    assert body["ContentType"] == "application/json"
    assert body["CacheControl"] == "no-cache"


@pytest.mark.usefixtures("tree")
def test_platform_json_is_public_and_matches_sdk_config() -> None:
    s3 = FakeS3()
    code, text = run(s3, FakeCloudFront())
    assert code == 0
    assert "PASS platform-json: 8 keys" in text
    obj = s3.objects["site/platform.json"]
    body = json.loads(obj["Body"])
    assert body == admin.sdk_config(cast("Session", FakeSession()))
    assert set(body) == {f.name for f in dataclasses.fields(PlatformConfig)}
    assert body["api_url"] == "https://api.example"
    assert obj["ContentType"] == "application/json"
    assert obj["CacheControl"] == "no-cache"


@pytest.mark.usefixtures("tree")
def test_platform_json_survives_the_stale_sweep() -> None:
    s3 = FakeS3({"site/platform.json": b"{}", "site/gone.html": b"x"})
    code, _ = run(s3, FakeCloudFront())
    assert code == 0
    assert "site/gone.html" in s3.deleted
    assert "site/platform.json" in s3.objects
    assert json.loads(s3.objects["site/platform.json"]["Body"])["region"]


@pytest.mark.usefixtures("tree")
def test_stale_site_keys_deleted_and_handout_untouched() -> None:
    s3 = FakeS3(
        {
            "site/assets/old-111.js": b"x",
            "site/gone.html": b"x",
            "handout/old-wheel.whl": b"keep",
            "elsewhere/file": b"keep",
        }
    )
    code, _ = run(s3, FakeCloudFront())
    assert code == 0
    assert sorted(s3.deleted) == ["site/assets/old-111.js", "site/gone.html"]
    assert "handout/old-wheel.whl" in s3.objects
    assert "elsewhere/file" in s3.objects


@pytest.mark.usefixtures("tree")
def test_vite_manifest_is_not_uploaded_and_a_stale_copy_is_swept() -> None:
    (publish.DIST / ".vite").mkdir()
    (publish.DIST / ".vite" / "manifest.json").write_text("{}")
    s3 = FakeS3({"site/.vite/manifest.json": b"old"})
    code, _ = run(s3, FakeCloudFront())
    assert code == 0
    assert not any(".vite" in k for k in s3.objects)
    assert "site/.vite/manifest.json" in s3.deleted
    assert "site/index.html" in s3.objects


@pytest.mark.usefixtures("tree")
def test_no_key_outside_the_two_prefixes() -> None:
    s3 = FakeS3()
    run(s3, FakeCloudFront())
    assert s3.objects
    assert all(k.startswith(("site/", "handout/")) for k in s3.objects)
    with pytest.raises(publish.StepError, match="outside"):
        publish.put(s3, BUCKET, "other/x", b"", ("text/html", None))
    assert "other/x" not in s3.objects


@pytest.mark.usefixtures("tree")
def test_build_env_and_invalidation() -> None:
    cf = FakeCloudFront()
    built: list[Any] = []
    code, text = run(FakeS3(), cf, built=built)
    assert code == 0
    cmd, env = built[0]
    assert cmd[:2] == ["npm", "run"]
    assert env == {
        "VITE_COGNITO_DOMAIN": "asteroidea-1.auth.us-east-1.amazoncognito.com",
        "VITE_COGNITO_CLIENT_ID": "webclient",
        "VITE_API_URL": "https://api.example",
    }
    assert cf.calls[0]["DistributionId"] == "EDIST"
    assert cf.calls[0]["InvalidationBatch"]["Paths"]["Items"] == ["/*"]
    assert "PASS invalidate: INV1" in text


def test_first_fail_stops_with_exit_1(tree: Path) -> None:
    (tree / "web" / "dist" / "index.html").unlink()
    s3 = FakeS3()
    cf = FakeCloudFront()
    code, text = run(s3, cf)
    assert code == 1
    assert "FAIL upload-site: " in text
    assert not cf.calls
    assert not any(k.startswith("handout/") for k in s3.objects)


def test_missing_index_json_refuses_and_uploads_nothing(tree: Path) -> None:
    (tree / "web" / "dist" / "index.json").unlink()
    s3 = FakeS3()
    cf = FakeCloudFront()
    code, text = run(s3, cf)
    assert code == 1
    assert "FAIL upload-site: " in text
    assert "has no index.json; run make harvest first" in text
    assert not any(k.startswith("site/") for k in s3.objects)
    assert not cf.calls


def test_guard_refuses_without_the_sandbox_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ASTEROIDEA_PROFILE", raising=False)
    monkeypatch.delenv("ASTEROIDEA_ACCOUNT", raising=False)
    with pytest.raises(SystemExit) as exc:
        publish.main()
    assert exc.value.code == 4
    recipe = (REPO / "Makefile").read_text().split("\nweb-publish:", 1)[1]
    assert "$(SANDBOX_GUARD)" in recipe.split("\n\n", 1)[0]


def test_find_wheel_picks_the_current_version_and_names_the_others(
    tmp_path: Path,
) -> None:
    (tmp_path / "asteroidea-0.0.0-py3-none-any.whl").write_bytes(b"old")
    (tmp_path / "asteroidea-0.1.0-py3-none-any.whl").write_bytes(b"new")
    wheel, ignored = publish.find_wheel(tmp_path, "0.1.0")
    assert wheel.name == "asteroidea-0.1.0-py3-none-any.whl"
    assert ignored == ["asteroidea-0.0.0-py3-none-any.whl"]


def test_find_wheel_refuses_a_missing_or_duplicated_current_wheel(
    tmp_path: Path,
) -> None:
    (tmp_path / "asteroidea-0.0.0-py3-none-any.whl").write_bytes(b"old")
    with pytest.raises(publish.StepError, match=r"found 0"):
        publish.find_wheel(tmp_path, "0.1.0")
    (tmp_path / "asteroidea-0.1.0-py3-none-any.whl").write_bytes(b"a")
    (tmp_path / "asteroidea-0.1.0-py3-none-macosx.whl").write_bytes(b"b")
    with pytest.raises(publish.StepError, match=r"found 2"):
        publish.find_wheel(tmp_path, "0.1.0")


def test_sdk_version_is_read_from_the_sdk_pyproject() -> None:
    expected = (REPO / "sdk" / "pyproject.toml").read_text()
    assert f'version = "{publish.sdk_version()}"' in expected


def test_a_stale_index_schema_refuses_before_any_aws_call(tree: Path) -> None:
    (tree / "web" / "public" / "index.json").write_text(
        json.dumps({"schema": "asteroidea.dataset-index/1"})
    )
    s3 = FakeS3()
    cf = FakeCloudFront()
    built: list[Any] = []
    code, text = run(s3, cf, built=built)
    assert code == 1
    assert text.startswith("FAIL index-schema: ")
    assert "asteroidea.dataset-index/1" in text
    assert INDEX_SCHEMA in text
    assert "make harvest" in text
    assert not s3.objects
    assert not cf.calls
    assert not built


def test_a_missing_public_index_refuses_naming_make_harvest(tree: Path) -> None:
    (tree / "web" / "public" / "index.json").unlink()
    code, text = run(FakeS3(), FakeCloudFront())
    assert code == 1
    assert "FAIL index-schema: " in text
    assert "make harvest" in text


@pytest.mark.usefixtures("tree")
def test_a_matching_index_schema_passes_the_guard() -> None:
    code, text = run(FakeS3(), FakeCloudFront())
    assert code == 0
    assert f"PASS index-schema: {INDEX_SCHEMA}\n" in text
