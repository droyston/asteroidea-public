"""Import planning core and ImportPlanFunction.

The planning core moved here from `asteroidea_infra.curate` so the web import
path and `make import` plan a source the same way: list the archive, resolve the
dataset's title and licence before anything is invoked, choose each file's
mode from its source bucket's region, refuse a second version and skip files
already in the catalog. `curate.py` imports these names back; its behaviour is
unchanged.

ImportPlanFunction (the commit image with a CMD override, as ImportFunction) runs
`handler`: given `{run_id, source, include, caller}` it plans the run, refuses it with
the reason written to the run item (no title or licence, a second version, over
`MAX_RUN_FILES` files, nothing selected, an unknown source), else invokes
ImportFunction asynchronously once per file to import, all at once,
and records each file's session id. Catalog writes stay behind the FIFOs:
the planner only invokes ImportFunction, and ImportFunction claims the dataset for
admin.
"""

import fnmatch
import http.client
import json
import os
import re
import time
import urllib.parse
from dataclasses import dataclass, field
from http import HTTPStatus
from typing import TYPE_CHECKING, Any, Final, Protocol

import boto3
from botocore import UNSIGNED
from botocore.config import Config
from botocore.exceptions import ClientError
from pyiceberg.catalog import load_catalog
from pyiceberg.expressions import EqualTo

from asteroidea_infra import schema
from asteroidea_ingest import import_file, sessions
from asteroidea_ingest.import_outcome import DONE, PLANNING, RUNNING
from asteroidea_ingest.uploads_api import SANDBOX_TIER

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from pyiceberg.catalog import Catalog

    from asteroidea_ingest.aws_types import Invoker, RunsTable

DANDI_API: Final = "https://api.dandiarchive.org/api"
OPENNEURO_GRAPHQL: Final = "https://openneuro.org/crn/graphql"
S3_ENDPOINT: Final = "https://s3.amazonaws.com"
OPENNEURO_BUCKET: Final = "openneuro.org"
# Bucket bookkeeping keys at a dataset root that are not snapshot files: the
# annex object `annex-uuid` sits beside the data, so the snapshot check ignores it.
OPENNEURO_BOOKKEEPING: Final = frozenset({"annex-uuid"})
HTTP_TIMEOUT_SECONDS: Final = 60
EXIT_FAIL: Final = 1
EXIT_REFUSED: Final = 2
SECOND_VERSION: Final = "second version: one version per dataset"


class CurateError(Exception):
    """A refusal with its exit code; the message names the cause."""

    def __init__(self, code: int, message: str) -> None:
        """Keep the exit code."""
        super().__init__(message)
        self.code = code


class Web(Protocol):
    """The anonymous calls the CLI makes (injected so the tests stay offline)."""

    def get_json(self, url: str) -> dict[str, Any]:
        """GET a JSON object."""
        ...

    def post_json(self, url: str, body: dict[str, Any]) -> dict[str, Any]:
        """POST a JSON body; return the JSON object answer."""
        ...

    def list_objects(self, bucket: str, prefix: str) -> dict[str, int]:
        """List a public bucket anonymously: key -> size."""
        ...

    def bucket_region(self, bucket: str) -> str:
        """HEAD the bucket root and return `x-amz-bucket-region`."""
        ...


def _object(value: object, url: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CurateError(EXIT_FAIL, f"{url}: expected a JSON object")
    return {str(k): v for k, v in value.items()}


def https(
    method: str, url: str, body: bytes | None = None
) -> tuple[int, http.client.HTTPMessage, bytes]:
    """One anonymous request over TLS (https only; redirects are not followed)."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or parts.hostname is None:
        raise CurateError(EXIT_FAIL, f"refused non-TLS URL {url}")
    target = parts.path or "/"
    if parts.query:
        target = f"{target}?{parts.query}"
    headers = {"content-type": "application/json"} if body is not None else {}
    conn = http.client.HTTPSConnection(
        parts.hostname, parts.port, timeout=HTTP_TIMEOUT_SECONDS
    )
    try:
        conn.request(method, target, body=body, headers=headers)
        resp = conn.getresponse()
        return resp.status, resp.headers, resp.read()
    finally:
        conn.close()


@dataclass(frozen=True, slots=True)
class AnonymousWeb:
    """`Web` over `http.client` (TLS) and an unsigned boto3 S3 client.

    `region` is the platform region the unsigned S3 client is built for: the CLI
    passes `asteroidea_infra.config.REGION`, ImportPlanFunction its own `AWS_REGION`
    (`asteroidea_infra.config` is not in the commit image).
    """

    region: str

    @staticmethod
    def _json(method: str, url: str, body: bytes | None = None) -> dict[str, Any]:
        status, _, data = https(method, url, body)
        if status != HTTPStatus.OK:
            raise CurateError(EXIT_FAIL, f"{method} {url}: HTTP {status}")
        return _object(json.loads(data), url)

    def get_json(self, url: str) -> dict[str, Any]:
        """GET a JSON object."""
        return self._json("GET", url)

    def post_json(self, url: str, body: dict[str, Any]) -> dict[str, Any]:
        """POST a JSON body."""
        return self._json("POST", url, json.dumps(body).encode())

    def list_objects(self, bucket: str, prefix: str) -> dict[str, int]:
        """Anonymous ListObjectsV2 of `prefix`, every page."""
        client = boto3.client(
            "s3", region_name=self.region, config=Config(signature_version=UNSIGNED)
        )
        out: dict[str, int] = {}
        for page in client.get_paginator("list_objects_v2").paginate(
            Bucket=bucket, Prefix=prefix
        ):
            out.update({o["Key"]: int(o["Size"]) for o in page.get("Contents", [])})
        return out

    def bucket_region(self, bucket: str) -> str:
        """HEAD the bucket root; another region answers 301 and still names itself.

        Measured: a HEAD of an object URL carries no region header, so the root.
        """
        _, headers, _ = https("HEAD", f"{S3_ENDPOINT}/{bucket}")
        region = headers.get("x-amz-bucket-region")
        if region is None:
            raise CurateError(EXIT_FAIL, f"bucket {bucket}: no x-amz-bucket-region")
        return str(region)


@dataclass(frozen=True, slots=True)
class Source:
    """`<archive>:<id>@<version>` and the platform dataset name."""

    archive: str
    dataset_id: str
    version: str

    @property
    def dataset(self) -> str:
        """Return `<archive>-<id>`, for example `dandi-000623`."""
        return f"{self.archive}-{self.dataset_id}".lower()

    @classmethod
    def parse(cls, text: str) -> Source:
        """Parse `dandi:000623@0.240227.2023`; exit 2 for an unknown archive."""
        archive, _, rest = text.partition(":")
        dataset_id, _, version = rest.partition("@")
        if archive not in {"dandi", "openneuro"} or not dataset_id or not version:
            want = "dandi:ID@VERSION or openneuro:ID@VERSION"
            msg = f"unknown source {text!r}: want {want}"
            raise CurateError(EXIT_REFUSED, msg)
        return cls(archive, dataset_id, version)


@dataclass(slots=True)
class Listed:
    """One file to import, before its mode is chosen."""

    relpath: str
    size: int
    source: dict[str, str]
    bucket: str | None = None
    ancillary: bool = False


@dataclass(slots=True)
class ImportPlan:
    """A resolved source: dataset metadata and the files to import."""

    source: Source
    dataset_meta: dict[str, str]
    files: list[Listed] = field(default_factory=list)


def _require_meta(meta: Mapping[str, object], where: str) -> dict[str, str]:
    """Refuse a dataset with no title or licence before any invoke."""
    missing = [k for k in ("title", "license") if not meta.get(k)]
    if missing:
        msg = f"{where}: no {' or '.join(missing)}; nothing invoked"
        raise CurateError(EXIT_FAIL, msg)
    return {k: str(v) for k, v in meta.items() if v}


def included(relpath: str, include: Sequence[str]) -> bool:
    """Return whether `relpath` matches one INCLUDE glob (all when none is given)."""
    return not include or any(fnmatch.fnmatchcase(relpath, g) for g in include)


# -- DANDI ---------------------------------------------------------------------


def _s3_url_parts(url: str) -> tuple[str, str] | None:
    parts = urllib.parse.urlsplit(url)
    suffix = ".s3.amazonaws.com"
    if parts.scheme == "https" and parts.netloc.endswith(suffix):
        return parts.netloc.removesuffix(suffix), parts.path.lstrip("/")
    return None


def _dandi_file(source: Source, asset: Mapping[str, Any]) -> Listed:
    metadata = asset.get("metadata") or {}
    s3_urls = [p for u in metadata.get("contentUrl", []) if (p := _s3_url_parts(u))]
    if not s3_urls:
        raise CurateError(EXIT_FAIL, f"{asset['path']}: no S3 contentUrl")
    bucket, key = s3_urls[0]
    src = {
        "archive": "dandi",
        "dataset": source.dataset_id,
        "version": source.version,
        "path": str(asset["path"]),
        "url": f"https://{bucket}.s3.amazonaws.com/{key}",
        "bucket": bucket,
        "key": key,
    }
    digest = (metadata.get("digest") or {}).get("dandi:sha2-256")
    if digest:
        src["digest"] = f"sha2-256:{digest}"
    return Listed(str(asset["path"]), int(asset["size"]), src, bucket)


def dandi_plan(web: Web, source: Source) -> ImportPlan:
    """List a DANDI version: `/info/` for the metadata, `/assets/` for the files."""
    base = f"{DANDI_API}/dandisets/{source.dataset_id}/versions/{source.version}"
    info = web.get_json(f"{base}/info/")
    metadata = dict(info.get("metadata") or {})
    licenses = [str(x).removeprefix("spdx:") for x in metadata.get("license") or []]
    meta = _require_meta(
        {
            "title": info.get("name"),
            "license": ", ".join(licenses),
            "doi": metadata.get("doi"),
            "url": metadata.get("url"),
        },
        f"dandi {source.dataset_id}@{source.version}",
    )
    plan = ImportPlan(source, meta)
    url: str | None = f"{base}/assets/?page_size=100&metadata=true"
    while url is not None:
        page = web.get_json(url)
        plan.files.extend(_dandi_file(source, a) for a in page.get("results", []))
        following = page.get("next")
        url = following if isinstance(following, str) else None
    return plan


# -- OpenNeuro -----------------------------------------------------------------


def _snapshot(web: Web, source: Source, fields: str) -> dict[str, Any]:
    query = (
        f'query {{ snapshot(datasetId: "{source.dataset_id}", tag: "{source.version}") '
        f"{{ {fields} }} }}"
    )
    answer = web.post_json(OPENNEURO_GRAPHQL, {"query": query})
    snapshot = (answer.get("data") or {}).get("snapshot")
    if not snapshot:
        msg = f"openneuro {source.dataset_id}@{source.version}: no such snapshot"
        raise CurateError(EXIT_REFUSED, msg)
    return dict(snapshot)


def _dir_may_match(path: str, include: Sequence[str]) -> bool:
    return not include or any(
        g.startswith(f"{path}/")
        or fnmatch.fnmatchcase(f"{path}/", g.split("*")[0] + "*")
        for g in include
    )


def snapshot_sizes(web: Web, source: Source, include: Sequence[str]) -> dict[str, int]:
    """Return relpath -> size for the snapshot files that `include` selects."""
    sizes: dict[str, int] = {}

    def walk(tree: str | None, prefix: str) -> None:
        """List one snapshot tree, recursing where `include` may match."""
        files = "files" if tree is None else f'files(tree: "{tree}")'
        entries = _snapshot(web, source, f"{files} {{ id filename size directory }}")
        for entry in entries["files"]:
            path = f"{prefix}{entry['filename']}"
            if entry["directory"]:
                if _dir_may_match(path, include):
                    walk(str(entry["id"]), f"{path}/")
            elif included(path, include):
                sizes[path] = int(entry["size"])

    walk(None, "")
    return sizes


def openneuro_plan(web: Web, source: Source, include: Sequence[str]) -> ImportPlan:
    """List an OpenNeuro snapshot from S3, checked against the GraphQL snapshot.

    OpenNeuro's bucket holds the latest snapshot, not a pinned one, so the included
    files' count and sizes must equal the snapshot's (else exit 1, nothing invoked).
    The bucket's root-level `annex-uuid` is bookkeeping, not a snapshot file, so it
    is dropped from the listing before the comparison.
    The dataset metadata is the snapshot's `dataset_description.json` (GraphQL).
    """
    description = _snapshot(web, source, "description { Name License }")["description"]
    meta = _require_meta(
        {
            "title": (description or {}).get("Name"),
            "license": (description or {}).get("License"),
            "url": f"https://openneuro.org/datasets/{source.dataset_id}"
            f"/versions/{source.version}",
        },
        f"openneuro {source.dataset_id}@{source.version}",
    )
    prefix = f"{source.dataset_id}/"
    listed = {
        key.removeprefix(prefix): size
        for key, size in web.list_objects(OPENNEURO_BUCKET, prefix).items()
        if not key.endswith("/")
        and key.removeprefix(prefix) not in OPENNEURO_BOOKKEEPING
    }
    chosen = {p: s for p, s in listed.items() if included(p, include)}
    expected = snapshot_sizes(web, source, include)
    if chosen != expected:
        only_s3 = sorted(set(chosen) - set(expected))[:5]
        only_snap = sorted(set(expected) - set(chosen))[:5]
        sized = sorted(
            p for p in set(chosen) & set(expected) if chosen[p] != expected[p]
        )
        msg = (
            f"OpenNeuro snapshot check failed for {source.dataset_id}"
            f"@{source.version}: "
            f"{len(chosen)} listed vs {len(expected)} in the snapshot; only in S3 "
            f"{only_s3}, only in the snapshot {only_snap}, size differs {sized[:5]}"
        )
        raise CurateError(EXIT_FAIL, msg)
    plan = ImportPlan(source, meta)
    for relpath, size in sorted(chosen.items()):
        key = f"{prefix}{relpath}"
        src = {
            "archive": "openneuro",
            "dataset": source.dataset_id,
            "version": source.version,
            "path": relpath,
            "url": f"{S3_ENDPOINT}/{OPENNEURO_BUCKET}/{key}",
            "bucket": OPENNEURO_BUCKET,
            "key": key,
        }
        plan.files.append(Listed(relpath, size, src, OPENNEURO_BUCKET))
    return plan


# -- ancillary files --------------------------------------------------


def extra_files(manifest: Mapping[str, Any]) -> list[Listed]:
    """Read an EXTRA manifest: pinned HTTPS files stored under the dataset."""
    out: list[Listed] = []
    for entry in manifest.get("files", []):
        src = {
            "archive": "https",
            "dataset": str(manifest["dataset"]),
            "version": str(entry["version"]),
            "path": str(entry["path"]),
            "url": str(entry["url"]),
        }
        if entry.get("sha256"):
            src["digest"] = f"sha2-256:{entry['sha256']}"
        if entry.get("license"):
            src["license"] = str(entry["license"])
        out.append(Listed(str(entry["path"]), int(entry["size"]), src, ancillary=True))
    return out


# -- mode, manifest, version refusal -------------------------------------------


def choose_modes(web: Web, files: Sequence[Listed], region: str) -> dict[str, str]:
    """Return relpath -> mode: `s3` when the bucket is in the platform `region`.

    Ancillary files have no bucket and always stream (`https`). The region is read
    once per bucket.
    """
    regions: dict[str, str] = {}
    modes: dict[str, str] = {}
    for f in files:
        if f.bucket is None:
            modes[f.relpath] = import_file.MODE_HTTPS
            continue
        if f.bucket not in regions:
            regions[f.bucket] = web.bucket_region(f.bucket)
        same = regions[f.bucket] == region
        modes[f.relpath] = import_file.MODE_S3 if same else import_file.MODE_HTTPS
    return modes


def events(
    plan: ImportPlan, files: Sequence[Listed], modes: Mapping[str, str], caller: str
) -> list[dict[str, Any]]:
    """Return the ImportFunction event per file, each with its session id."""
    out = []
    for f in files:
        event = {
            "mode": modes[f.relpath],
            "dataset": plan.source.dataset,
            "relpath": f.relpath,
            "size": f.size,
            "source": f.source,
            "dataset_meta": plan.dataset_meta,
            "caller": caller,
            "ancillary": f.ancillary,
        }
        event["session_id"] = import_file.session_id(
            plan.source.dataset, f.relpath, f.source["version"]
        )
        out.append(event)
    return out


def split_imported(
    dataset: str,
    planned: Sequence[dict[str, Any]],
    existing: Mapping[str, str | None],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Return (to import, already imported); exit 2 on a second version.

    `existing` maps each committed relpath of the dataset to its `source_version`.
    """
    todo: list[dict[str, Any]] = []
    done: list[str] = []
    conflicts: list[str] = []
    for event in planned:
        relpath, version = event["relpath"], event["source"]["version"]
        if relpath not in existing:
            todo.append(event)
        elif existing[relpath] == version:
            done.append(relpath)
        else:
            conflicts.append(
                f"{relpath} (imported {existing[relpath]}, asked {version})"
            )
    if conflicts:
        msg = f"{SECOND_VERSION}: {dataset}: {', '.join(conflicts[:5])}"
        raise CurateError(EXIT_REFUSED, msg)
    return todo, done


@dataclass(frozen=True, slots=True)
class Planned:
    """A planned source: every selected file's event, to import and already done."""

    plan: ImportPlan
    events: list[dict[str, Any]]
    todo: list[dict[str, Any]]
    done: list[str]


@dataclass(frozen=True, slots=True)
class PlanInputs:
    """What one plan needs besides the source: the caller and the catalog's state."""

    caller: str
    region: str
    existing: Callable[[str], Mapping[str, str | None]]
    extra: Mapping[str, Any] | None = None
    max_files: int | None = None
    sandbox: bool = False


def plan_source(
    web: Web, source: Source, include: Sequence[str], inputs: PlanInputs
) -> Planned:
    """List, resolve metadata, select, cap, choose modes and split off imported files.

    Raises `CurateError` before anything is invoked: no title or licence, an
    OpenNeuro snapshot mismatch, nothing selected, more than `inputs.max_files` files
    (`make import` passes no cap) or a second version.
    """
    if source.archive == "dandi":
        plan = dandi_plan(web, source)
        files = [f for f in plan.files if included(f.relpath, include)]
    else:
        plan = openneuro_plan(web, source, include)
        files = list(plan.files)
    if inputs.sandbox:
        plan.dataset_meta = {**plan.dataset_meta, "tier": SANDBOX_TIER}
    if inputs.extra is not None:
        files.extend(extra_files({**inputs.extra, "dataset": source.dataset_id}))
    if not files:
        raise CurateError(EXIT_FAIL, f"INCLUDE {list(include)} selects no file")
    if inputs.max_files is not None and len(files) > inputs.max_files:
        msg = (
            f"over {inputs.max_files} files: INCLUDE {list(include)} selects "
            f"{len(files)}; nothing invoked"
        )
        raise CurateError(EXIT_REFUSED, msg)
    planned = events(
        plan, files, choose_modes(web, files, inputs.region), inputs.caller
    )
    todo, done = split_imported(
        source.dataset, planned, inputs.existing(source.dataset)
    )
    return Planned(plan, planned, todo, done)


# -- session outcomes ----------------------------------------------------------


def session_status(item: Mapping[str, Any] | None) -> str:
    """Describe a session that is still moving (the timed-out FAIL line)."""
    if item is None:
        return "no session yet"
    keys = ("state", "extract_state", "commit_error", "extract_error")
    return " ".join(f"{k}={item.get(k)}" for k in keys if item.get(k) is not None)


# -- the catalog's versions ----------------------------------------------------


def existing_from_catalog(catalog: Catalog) -> Callable[[str], dict[str, str | None]]:
    """Return dataset -> {relpath: source_version} over the `assets` table."""

    def existing(dataset: str) -> dict[str, str | None]:
        """Return the dataset's catalogued files as path -> source version."""
        table = catalog.load_table(f"{schema.DATABASE}.{schema.TABLE}")
        rows = table.scan(
            row_filter=EqualTo(term="dataset", value=dataset),
            selected_fields=("s3_key", "source_version"),
        ).to_arrow()
        prefix = f"raw/{dataset}/"
        return {
            str(r["s3_key"]).removeprefix(prefix): r["source_version"]
            for r in rows.to_pylist()
        }

    return existing


# -- ImportPlanFunction ---------------

# "50 files, all at once". A run that selects more is refused, nothing invoked.
MAX_RUN_FILES: Final = 50
REFUSED: Final = "REFUSED"
NOTHING_TO_DO: Final = "NOTHING_TO_DO"
BAD_EVENT: Final = "the run needs source (text) and include (a list of globs)"


@dataclass(frozen=True, slots=True)
class RunStore:
    """The import-runs table (PK `run_id`, GSI `by_caller`) over a DynamoDB client."""

    ddb: RunsTable
    name: str

    def _update(
        self, run_id: str, values: Mapping[str, Any], condition: str, *, now: int
    ) -> bool:
        """SET `values` and `updated_at` under `condition`; False when it fails."""
        sets = {**values, "updated_at": now}
        expression = ", ".join(f"#{k} = :{k}" for k in sets)
        update = (
            f"SET {expression}, #created_at = if_not_exists(#created_at, :created_at)"
        )
        # DynamoDB rejects a name no expression uses, so list only those that appear.
        used = set(re.findall(r"#\w+", f"{update} {condition}"))
        names = {f"#{k}": k for k in (*sets, "run_id", "created_at") if f"#{k}" in used}
        attr_values = {f":{k}": v for k, v in sets.items()}
        attr_values[":planning"] = PLANNING
        attr_values[":created_at"] = now
        try:
            self.ddb.update_item(
                TableName=self.name,
                Key=sessions.marshal({"run_id": run_id}),
                UpdateExpression=update,
                ConditionExpression=condition,
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=sessions.marshal(attr_values),
            )
        except ClientError as err:
            if err.response["Error"]["Code"] == sessions.CONDITIONAL_FAILED:
                return False
            raise
        return True

    def start(self, run_id: str, fields: Mapping[str, Any], *, now: int) -> bool:
        """Write the run as PLANNING unless it has moved past planning (a retry)."""
        return self._update(
            run_id,
            {**fields, "state": PLANNING},
            "attribute_not_exists(#run_id) OR #state = :planning",
            now=now,
        )

    def finish(self, run_id: str, fields: Mapping[str, Any], *, now: int) -> bool:
        """Move a PLANNING run to its planned state (`fields` holds `state`)."""
        return self._update(run_id, fields, "#state = :planning", now=now)


@dataclass(frozen=True, slots=True)
class PlannerDeps:
    """ImportPlanFunction's outside world (tests pass fakes and stubbed clients)."""

    web: Web
    existing: Callable[[str], Mapping[str, str | None]]
    runs: RunStore
    lam: Invoker
    import_function: str
    region: str
    clock: Callable[[], float] = time.time


def _required(event: Mapping[str, Any], key: str) -> str:
    value = event.get(key)
    if not isinstance(value, str) or not value:
        msg = f"import plan event needs {key}"
        raise ValueError(msg)
    return value


def _request(event: Mapping[str, Any]) -> tuple[str, tuple[str, ...]] | None:
    """Return (source text, include globs), or None when their shape is wrong."""
    source, include = event.get("source"), event.get("include", [])
    if not isinstance(source, str) or not isinstance(include, list):
        return None
    globs = tuple(g for g in include if isinstance(g, str))
    return (source, globs) if len(globs) == len(include) else None


def _plan(
    request: tuple[str, tuple[str, ...]] | None,
    caller: str,
    deps: PlannerDeps,
    *,
    sandbox: bool = False,
) -> tuple[Source, Planned]:
    """Plan a run's request with the 50-file cap; raise `CurateError` to refuse."""
    if request is None:
        raise CurateError(EXIT_REFUSED, BAD_EVENT)
    source_text, include = request
    source = Source.parse(source_text)
    inputs = PlanInputs(
        caller=caller,
        region=deps.region,
        existing=deps.existing,
        max_files=MAX_RUN_FILES,
        sandbox=sandbox,
    )
    return source, plan_source(deps.web, source, include, inputs)


def plan_run(event: Mapping[str, Any], deps: PlannerDeps) -> dict[str, Any]:
    """Plan one run, then invoke ImportFunction once per file to import, all at once.

    A refusal is written to the run (`state` REFUSED, `refusal` the reason) and
    nothing is invoked. A run whose every file is already imported is DONE at once.
    A retried delivery of a run already past PLANNING does nothing.
    """
    run_id, caller = _required(event, "run_id"), _required(event, "caller")
    now = int(deps.clock())
    request = _request(event)
    source_text, include = request or (str(event.get("source")), ())
    fields = {"source": source_text, "include": list(include), "caller_sub": caller}
    if not deps.runs.start(run_id, fields, now=now):
        return {"run_id": run_id, "outcome": NOTHING_TO_DO}
    try:
        source, planned = _plan(
            request, caller, deps, sandbox=event.get("sandbox") is True
        )
    except CurateError as err:
        deps.runs.finish(run_id, {"state": REFUSED, "refusal": str(err)}, now=now)
        return {"run_id": run_id, "state": REFUSED, "refusal": str(err)}
    for ev in planned.todo:
        deps.lam.invoke(
            FunctionName=deps.import_function,
            InvocationType="Event",  # never a synchronous, retried call
            Payload=json.dumps(
                {k: v for k, v in ev.items() if k != "session_id"}
            ).encode(),
        )
    files = [
        {"relpath": e["relpath"], "session_id": e["session_id"]} for e in planned.todo
    ]
    state = RUNNING if planned.todo else DONE
    deps.runs.finish(
        run_id,
        {
            "state": state,
            "dataset": source.dataset,
            "files": files,
            "already_imported": planned.done,
        },
        now=int(deps.clock()),
    )
    return {
        "run_id": run_id,
        "state": state,
        "invoked": len(files),
        "already_imported": len(planned.done),
    }


def handler(event: dict[str, Any], _context: object) -> dict[str, Any]:
    """Lambda entry point (asynchronous invoke from ImportsFunction)."""
    region = os.environ["AWS_REGION"]
    deps = PlannerDeps(
        web=AnonymousWeb(region),
        existing=existing_from_catalog(load_catalog("glue", type="glue")),
        runs=RunStore(boto3.client("dynamodb"), os.environ["RUNS_TABLE"]),
        lam=boto3.client("lambda"),
        import_function=os.environ["IMPORT_FUNCTION_NAME"],
        region=region,
    )
    return plan_run(event, deps)
