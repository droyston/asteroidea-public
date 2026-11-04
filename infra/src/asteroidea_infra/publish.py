"""`make web-publish` (live): build and upload the hosted app and the SDK handout.

Steps, each printing `PASS <step>` or
`FAIL <step>: <reason>`, stopping at the first FAIL (exit 1):
index-schema, outputs, build-web, upload-site, platform-json, build-wheel,
upload-handout, manifest, invalidate.
Only the `site/` and `handout/` prefixes of the site bucket are ever written or deleted.
`handout/manifest.json` is read by the platform Lambda
(`asteroidea_ingest.get_started_api`); its `pins` come from
`notebook.NOTEBOOK_PINS`, the one source.
"""

import hashlib
import json
import os
import re
import sys
import time
import tomllib
from typing import TYPE_CHECKING, Any, Final, NotRequired, Protocol, TypedDict, Unpack

from botocore.exceptions import BotoCoreError, ClientError

from asteroidea_infra.admin import sdk_config, session_for, stack_outputs
from asteroidea_infra.notebook import NOTEBOOK, NOTEBOOK_PINS
from asteroidea_infra.runner import REPO, execute
from asteroidea_infra.sandbox_guard import require_sandbox

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from boto3 import Session

SITE_PREFIX: Final = "site"
HANDOUT_PREFIX: Final = "handout"
MANIFEST_KEY: Final = f"{HANDOUT_PREFIX}/manifest.json"
PLATFORM_KEY: Final = f"{SITE_PREFIX}/platform.json"
# Public wheel copies (`site/sdk/<wheel>`): the Get started command names one by URL.
SDK_PREFIX: Final = f"{SITE_PREFIX}/sdk/"
DIST: Final = REPO / "web" / "dist"
INDEX_JSON: Final = REPO / "web" / "public" / "index.json"
INDEX_SCHEMA_TS: Final = REPO / "web" / "src" / "index" / "schema.ts"
INDEX_SCHEMA_RE: Final = re.compile(r'export const INDEX_SCHEMA\s*=\s*"([^"]+)"')
WHEEL_DIR: Final = REPO / "dist"
SDK_PYPROJECT: Final = REPO / "sdk" / "pyproject.toml"
BUILD_TIMEOUT_S: Final = 600.0
NO_CACHE: Final = "no-cache"
IMMUTABLE: Final = "public, max-age=31536000, immutable"
NO_CACHE_FILES: Final = frozenset({"index.html", "index.json"})
CONTENT_TYPES: Final = {
    ".js": "text/javascript",
    ".css": "text/css",
    ".html": "text/html",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".whl": "application/zip",
    ".ipynb": "application/json",
}
DEFAULT_TYPE: Final = "application/octet-stream"
DATA_OUTPUTS: Final = ("WebUrl", "SiteBucketName", "DistributionId")
SERVICES_OUTPUTS: Final = ("WebClientId", "ManagedLoginBaseUrl", "ApiUrl")
DELETE_BATCH: Final = 1000


class StepError(Exception):
    """A publish step failed; the message is the reason, verbatim."""


class PutArgs(TypedDict):
    """Keyword arguments of `S3.PutObject` as used here."""

    Bucket: str
    Key: str
    Body: bytes
    ContentType: str
    CacheControl: NotRequired[str]


class ListArgs(TypedDict):
    """Keyword arguments of `S3.ListObjectsV2` as used here."""

    Bucket: str
    Prefix: str
    ContinuationToken: NotRequired[str]


class DeleteArgs(TypedDict):
    """Keyword arguments of `S3.DeleteObjects` as used here."""

    Bucket: str
    Delete: dict[str, Any]


class S3(Protocol):
    """The S3 client methods this module calls."""

    def put_object(self, **kwargs: Unpack[PutArgs]) -> dict[str, Any]:
        """Upload one object."""
        ...

    def list_objects_v2(self, **kwargs: Unpack[ListArgs]) -> dict[str, Any]:
        """List one page of keys."""
        ...

    def delete_objects(self, **kwargs: Unpack[DeleteArgs]) -> dict[str, Any]:
        """Delete up to 1000 keys."""
        ...


class InvalidationArgs(TypedDict):
    """Keyword arguments of `CloudFront.CreateInvalidation` as used here."""

    DistributionId: str
    InvalidationBatch: dict[str, Any]


class CloudFront(Protocol):
    """The CloudFront client method this module calls."""

    def create_invalidation(self, **kwargs: Unpack[InvalidationArgs]) -> dict[str, Any]:
        """Create an invalidation."""
        ...


def content_type(path: str) -> str:
    """Return the Content-Type for a file name by extension."""
    suffix = "." + path.rsplit(".", 1)[-1] if "." in path else ""
    return CONTENT_TYPES.get(suffix.lower(), DEFAULT_TYPE)


def cache_control(rel: str) -> str:
    """Return the Cache-Control for a `dist/`-relative key."""
    if rel in NO_CACHE_FILES:
        return NO_CACHE
    if rel.startswith("assets/"):
        return IMMUTABLE
    return NO_CACHE


def check_index_schema() -> str:
    """Return the schema both agree on, or raise: `index.json` must match the app.

    The app (`web/src/index/schema.ts` `INDEX_SCHEMA`) refuses any other schema
    string, so a stale harvest would publish a Datasets page that shows only an error.
    """
    found = INDEX_SCHEMA_RE.search(INDEX_SCHEMA_TS.read_text())
    if found is None:
        msg = f"no INDEX_SCHEMA constant in {INDEX_SCHEMA_TS}"
        raise StepError(msg)
    want = str(found.group(1))
    if not INDEX_JSON.is_file():
        msg = f"{INDEX_JSON} does not exist; run make harvest"
        raise StepError(msg)
    try:
        have = json.loads(INDEX_JSON.read_text()).get("schema")
    except (ValueError, AttributeError) as err:
        msg = f"{INDEX_JSON} is not an index object ({err}); run make harvest"
        raise StepError(msg) from err
    if have != want:
        msg = (
            f"index.json schema is {have!r} but the web app reads INDEX_SCHEMA "
            f"{want!r}; run make harvest"
        )
        raise StepError(msg)
    return want


def read_outputs(data: dict[str, str], services: dict[str, str]) -> dict[str, str]:
    """Return the outputs this module needs, or raise naming each missing one."""
    missing = [k for k in DATA_OUTPUTS if k not in data]
    missing += [k for k in SERVICES_OUTPUTS if k not in services]
    if missing:
        msg = f"stack outputs missing: {', '.join(missing)}"
        raise StepError(msg)
    return {k: data[k] for k in DATA_OUTPUTS} | {
        k: services[k] for k in SERVICES_OUTPUTS
    }


def web_env(outputs: dict[str, str]) -> dict[str, str]:
    """Return the Vite env names (`web/.env.example`) from the outputs."""
    domain = outputs["ManagedLoginBaseUrl"].removeprefix("https://").rstrip("/")
    return {
        "VITE_COGNITO_DOMAIN": domain,
        "VITE_COGNITO_CLIENT_ID": outputs["WebClientId"],
        "VITE_API_URL": outputs["ApiUrl"],
    }


def _list_keys(s3: S3, bucket: str, prefix: str) -> list[str]:
    keys: list[str] = []
    args: ListArgs = {"Bucket": bucket, "Prefix": f"{prefix}/"}
    while True:
        page = s3.list_objects_v2(**args)
        keys += [o["Key"] for o in page.get("Contents", [])]
        token = page.get("NextContinuationToken")
        if not page.get("IsTruncated") or not token:
            return keys
        args["ContinuationToken"] = token


def put(
    s3: S3,
    bucket: str,
    key: str,
    body: bytes,
    meta: tuple[str, str | None],
) -> None:
    """Write one object; refuse a key outside `site/` and `handout/`."""
    ctype, cache = meta
    if not key.startswith((f"{SITE_PREFIX}/", f"{HANDOUT_PREFIX}/")):
        msg = f"refusing to write outside {SITE_PREFIX}/ and {HANDOUT_PREFIX}/: {key}"
        raise StepError(msg)
    args: PutArgs = {"Bucket": bucket, "Key": key, "Body": body, "ContentType": ctype}
    if cache is not None:
        args["CacheControl"] = cache
    s3.put_object(**args)


def upload_site(s3: S3, bucket: str, dist: Path) -> tuple[int, int]:
    """Upload `dist/` to `site/` and delete `site/` keys no longer in it.

    `site/platform.json` is written by its own step and is never deleted here. Keys
    under `site/sdk/` (the public wheel copies) are never deleted here either: an
    older wheel stays, so a command copied earlier keeps working.

    Returns (uploaded, deleted).
    """
    # Vite's `.vite/manifest.json` is build metadata, not site content.
    files = sorted(
        p
        for p in dist.rglob("*")
        if p.is_file() and ".vite" not in p.relative_to(dist).parts
    )
    present = {p.relative_to(dist).as_posix() for p in files}
    for name, hint in (("index.html", ""), ("index.json", "; run make harvest first")):
        if name not in present:
            msg = f"{dist} has no {name}{hint}"
            raise StepError(msg)
    wanted: set[str] = {PLATFORM_KEY}
    for path in files:
        rel = path.relative_to(dist).as_posix()
        key = f"{SITE_PREFIX}/{rel}"
        wanted.add(key)
        put(
            s3,
            bucket,
            key,
            path.read_bytes(),
            (content_type(rel), cache_control(rel)),
        )
    stale = [
        k
        for k in _list_keys(s3, bucket, SITE_PREFIX)
        if k not in wanted and not k.startswith(SDK_PREFIX)
    ]
    for start in range(0, len(stale), DELETE_BATCH):
        batch = stale[start : start + DELETE_BATCH]
        s3.delete_objects(
            Bucket=bucket,
            Delete={"Objects": [{"Key": k} for k in batch], "Quiet": True},
        )
    return len(files), len(stale)


def upload_platform_json(s3: S3, bucket: str, platform: dict[str, Any]) -> list[str]:
    """Write `site/platform.json`, public under the app's origin; return its keys.

    The content is `admin.sdk_config`'s, the keys the SDK's config loader accepts.
    """
    put(
        s3,
        bucket,
        PLATFORM_KEY,
        (json.dumps(platform, indent=2, sort_keys=True) + "\n").encode(),
        (content_type(PLATFORM_KEY), NO_CACHE),
    )
    return sorted(platform)


def sdk_version() -> str:
    """Return the SDK's current version from `sdk/pyproject.toml`, the one source."""
    return str(tomllib.loads(SDK_PYPROJECT.read_text())["project"]["version"])


def find_wheel(wheel_dir: Path, version: str) -> tuple[Path, list[str]]:
    """Return the one SDK wheel of `version` and the names of other wheels ignored.

    Raises when that version's wheel is missing or appears more than once.
    """
    wheels = sorted(wheel_dir.glob("asteroidea-*.whl"))
    current = [w for w in wheels if w.name.startswith(f"asteroidea-{version}-")]
    if len(current) != 1:
        msg = (
            f"expected one asteroidea-{version}-*.whl in {wheel_dir}, "
            f"found {len(current)}"
        )
        raise StepError(msg)
    return current[0], [w.name for w in wheels if w not in current]


def upload_handout(s3: S3, bucket: str, files: tuple[Path, Path]) -> dict[str, Any]:
    """Upload the wheel and notebook (`files`) to `handout/`; write the manifest.

    The wheel is also put at `site/sdk/<name>`, which CloudFront serves without a
    credential; the Get started command names that URL. `handout/` stays denied.
    """
    wheel, notebook = files
    wheel_bytes = wheel.read_bytes()
    put(
        s3,
        bucket,
        f"{HANDOUT_PREFIX}/{wheel.name}",
        wheel_bytes,
        (content_type(wheel.name), None),
    )
    put(
        s3,
        bucket,
        f"{SDK_PREFIX}{wheel.name}",
        wheel_bytes,
        (content_type(wheel.name), None),
    )
    put(
        s3,
        bucket,
        f"{HANDOUT_PREFIX}/{notebook.name}",
        notebook.read_bytes(),
        (content_type(notebook.name), None),
    )
    manifest: dict[str, Any] = {
        "wheel": {
            "filename": wheel.name,
            "sha256": hashlib.sha256(wheel_bytes).hexdigest(),
        },
        "notebook": {"filename": notebook.name},
        "pins": list(NOTEBOOK_PINS),
    }
    put(
        s3,
        bucket,
        MANIFEST_KEY,
        (json.dumps(manifest, indent=2) + "\n").encode(),
        (content_type(MANIFEST_KEY), NO_CACHE),
    )
    return manifest


def invalidate(cloudfront: CloudFront, distribution_id: str) -> str:
    """Invalidate `/*`; return the invalidation id without waiting."""
    made = cloudfront.create_invalidation(
        DistributionId=distribution_id,
        InvalidationBatch={
            "Paths": {"Quantity": 1, "Items": ["/*"]},
            "CallerReference": f"web-publish-{time.time_ns()}",
        },
    )
    return str(made["Invalidation"]["Id"])


def run_command(cmd: Sequence[str], env: dict[str, str] | None = None) -> None:
    """Run a build command in the repo; raise with its stderr on failure."""
    full_env = None if env is None else {**os.environ, **env}
    done = execute(cmd, cwd=REPO, env=full_env, timeout=BUILD_TIMEOUT_S)
    if done.code != 0:
        detail = (done.err or done.out).strip()
        msg = f"{' '.join(cmd)} exited {done.code}: {detail}"
        raise StepError(msg)


def publish(
    session: Session,
    *,
    build: Callable[[Sequence[str], dict[str, str] | None], None] = run_command,
    s3: S3 | None = None,
    cloudfront: CloudFront | None = None,
    out: Callable[[str], object] = sys.stdout.write,
) -> int:
    """Run every step; return 0, or 1 at the first FAIL."""
    step = "index-schema"
    try:
        out(f"PASS {step}: {check_index_schema()}\n")
        step = "outputs"
        outputs = read_outputs(
            stack_outputs(session, "DataStack"),
            stack_outputs(session, "ServicesStack"),
        )
        out(f"PASS {step}\n")
        client_s3: S3 = s3 or session.client("s3")
        client_cf: CloudFront = cloudfront or session.client("cloudfront")
        bucket = outputs["SiteBucketName"]

        step = "build-web"
        build(["npm", "run", "build", "--prefix", "web"], web_env(outputs))
        out(f"PASS {step}\n")

        step = "upload-site"
        up, gone = upload_site(client_s3, bucket, DIST)
        out(f"PASS {step}: {up} uploaded, {gone} stale deleted\n")

        step = "platform-json"
        keys = upload_platform_json(client_s3, bucket, sdk_config(session))
        out(f"PASS {step}: {len(keys)} keys\n")

        step = "build-wheel"
        build(
            ["uv", "build", "--package", "asteroidea", "--wheel", "--out-dir", "dist"],
            None,
        )
        wheel, ignored = find_wheel(WHEEL_DIR, sdk_version())
        note = f" (ignored other versions: {', '.join(ignored)})" if ignored else ""
        out(f"PASS {step}: {wheel.name}{note}\n")

        step = "upload-handout"
        manifest = upload_handout(client_s3, bucket, (wheel, NOTEBOOK))
        out(f"PASS {step}\n")

        step = "manifest"
        digest = manifest["wheel"]["sha256"]
        out(f"PASS {step}: sha256 {digest}, {len(manifest['pins'])} pins\n")

        step = "invalidate"
        out(f"PASS {step}: {invalidate(client_cf, outputs['DistributionId'])}\n")
        out(f"published {outputs['WebUrl']}\n")
    except (StepError, ClientError, BotoCoreError, OSError, SystemExit) as err:
        out(f"FAIL {step}: {err}\n")
        return 1
    return 0


def main() -> None:
    """Entry point of `make web-publish`."""
    profile, account = require_sandbox()
    raise SystemExit(publish(session_for(profile, account)))


if __name__ == "__main__":
    main()
