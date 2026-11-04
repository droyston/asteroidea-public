"""`GET /catalog/session-summary` over the derived store.

The store fake answers as the derived bucket answers ComputeRole: a GET under a
prefix the role reads returns the object, or NoSuchKey when the role may also list
that prefix (else AccessDenied, as S3 answers a missing key without ListBucket); a
GET outside those prefixes and every write are AccessDenied. The round trip builds
the summary with the writer and reads it through the route. The golden body
`web/e2e/fixtures/ux/session-summary.golden.json` stores three parts the web parses
with its live parsers: the resolver's CS62 events fixture (`events`), the CS62 R2
units table (`units`) and the rates fixture (`rates-scene_cut`); regenerate with
`GOLDEN_WRITE=1`, then `npx biome format --write e2e/fixtures/ux/` in `web/`.
"""

import io
import json
from typing import Any, Final
from urllib.parse import parse_qs, urlsplit

import boto3
import pytest
from botocore.config import Config
from botocore.exceptions import ClientError

from asteroidea_ingest import compute_api, dataset_summary, summary_read
from asteroidea_ingest.compute_api import ComputeApi

from .conftest import jwt_event
from .test_catalog_studies import GOLDEN_DIR, check_golden
from .test_compute_events import STAMPS, _write
from .test_dataset_summary import (
    DATASET,
    DERIVED,
    SESSION,
    SUBJECT,
    DerivedStore,
    make_world,
)

ROUTE: Final = "GET /catalog/session-summary"
# The derived prefixes ComputeRole reads and lists (functions.py :: compute_statements;
# infra/tests/test_services_synth.py pins the statements).
READ_PREFIXES: Final = ("lfp/", "summary/")
RATES: Final = GOLDEN_DIR / "rates-cs62-scene_cut-display.json"
EVENTS: Final = GOLDEN_DIR.parent / "synthesis-events.json"
UNITS: Final = GOLDEN_DIR / "units-cs62-r2.json"
G_ASSET: Final = "asset-cs62-r2"
GOLDEN: Final = "session-summary.golden.json"
G_DATASET, G_SUBJECT, G_SESSION = "dandi-000623", "sub-CS62", "ses-2"
DIGEST: Final = "0" * 63 + "1"
GET, PUT = "GetObject", "PutObject"


def _denied(operation: str, key: str) -> ClientError:
    error: Any = {"Error": {"Code": "AccessDenied", "Message": f"Access Denied {key}"}}
    return ClientError(error, operation)


class ComputeRoleView:
    """The derived bucket as ComputeRole sees it through IAM and the bucket policy."""

    def __init__(
        self, store: DerivedStore, *, list_prefixes: tuple[str, ...] = READ_PREFIXES
    ) -> None:
        self.store = store
        self.list_prefixes = list_prefixes
        self.gets: list[str] = []

    def get_object(self, **kwargs: Any) -> Any:
        assert kwargs["Bucket"] == DERIVED
        key = str(kwargs["Key"])
        self.gets.append(key)
        if not key.startswith(READ_PREFIXES):
            raise _denied(GET, key)
        if key not in self.store.objects:
            if key.startswith(self.list_prefixes):
                error: Any = {"Error": {"Code": "NoSuchKey", "Message": key}}
                raise ClientError(error, GET)
            raise _denied(GET, key)
        return {"Body": io.BytesIO(self.store.objects[key])}

    def put_object(self, **kwargs: Any) -> dict[str, Any]:
        raise _denied(PUT, str(kwargs["Key"]))


class NoAthena:
    """An Athena client the route must not call: any call fails the test."""

    def __getattr__(self, name: str) -> Any:
        msg = f"the session-summary route called Athena: {name}"
        raise AssertionError(msg)


NO_ATHENA: Any = NoAthena()


def _presigner() -> Any:
    s3 = boto3.client(
        "s3",
        region_name="us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
        config=Config(signature_version="s3v4"),
    )

    def presign(key: str, seconds: int) -> str:
        return str(
            s3.generate_presigned_url(
                ClientMethod="get_object",
                Params={"Bucket": DERIVED, "Key": key},
                ExpiresIn=seconds,
            )
        )

    return presign


def _call(
    view: ComputeRoleView,
    query: dict[str, str] | None,
    *,
    groups: str = "[researchers]",
) -> tuple[int, dict[str, Any]]:
    api = ComputeApi(
        athena=NO_ATHENA,
        workgroup="wg",
        database="db",
        s3=view,
        raw_bucket="raw-bucket",
        clock=lambda: 0.0,
        derived_bucket=DERIVED,
        presign=_presigner(),
    )
    event = {**jwt_event(ROUTE, groups=groups), "queryStringParameters": query}
    response = api.handle(event, 28_000)
    return response["statusCode"], json.loads(response["body"])


def _query(
    dataset: str = DATASET, subject: str = SUBJECT, session: str = SESSION
) -> dict[str, str]:
    return {"dataset": dataset, "subject": subject, "session": session}


@pytest.fixture(scope="module")
def nwb_bytes(tmp_path_factory: pytest.TempPathFactory) -> bytes:
    path = tmp_path_factory.mktemp("cs62-summary-route") / "cs62.nwb"
    _write(path, STAMPS)
    return path.read_bytes()


# --- the round trip with the writer ---


def test_the_route_answers_what_the_writer_stored_byte_for_byte(
    nwb_bytes: bytes,
) -> None:
    world = make_world(nwb_bytes)
    built = dataset_summary.summarize_session(DATASET, SUBJECT, SESSION, world.deps)
    assert built["outcome"] == "built"
    view = ComputeRoleView(world.store)
    status, body = _call(view, _query())
    assert status == 200, body
    assert body["status"] == "built"
    assert body["delivery"] == "inline"
    prefix = f"summary/{DATASET}/{SUBJECT}/{SESSION}/{built['digest']}"
    manifest = world.store.json(f"{prefix}/manifest.json")
    assert body["manifest"] == manifest
    assert list(body["parts"]) == [
        "events",
        "units",
        "rates-scene_cut",
        "tuning-scene_cut-cut_kind",
        "rates-recognition_onset",
        "tuning-recognition_onset-familiarity",
    ]
    for name, part in body["parts"].items():
        stored = world.store.objects[f"{prefix}/{name}.json"]
        text = json.dumps(part, allow_nan=False, separators=(",", ":"))
        assert text.encode() == stored, name  # the live route's encoding (`_encode`)
    # latest, the manifest, then each part in manifest order; nothing else.
    assert view.gets == [
        dataset_summary.latest_key(DATASET, SUBJECT, SESSION),
        f"{prefix}/manifest.json",
        *(f"{prefix}/{n}" for n in manifest["parts"]),
    ]


def test_no_latest_pointer_is_missing_with_200(nwb_bytes: bytes) -> None:
    world = make_world(nwb_bytes)
    status, body = _call(ComputeRoleView(world.store), _query())
    assert (status, body) == (200, {"status": "missing"})


def test_access_denied_is_never_read_as_missing() -> None:
    """Without ListBucket S3 answers a missing key 403: a fault, not "not built"."""
    view = ComputeRoleView(DerivedStore(), list_prefixes=("lfp/",))
    status, body = _call(view, _query())
    assert status == 502
    assert body["error"] == "summary_unreadable"
    assert "AccessDenied" in body["detail"]
    assert dataset_summary.latest_key(DATASET, SUBJECT, SESSION) in body["detail"]


def test_a_pointer_to_no_manifest_is_unreadable(nwb_bytes: bytes) -> None:
    world = make_world(nwb_bytes)
    dataset_summary.summarize_session(DATASET, SUBJECT, SESSION, world.deps)
    latest = world.store.json(dataset_summary.latest_key(DATASET, SUBJECT, SESSION))
    del world.store.objects[latest["manifest"]]
    status, body = _call(ComputeRoleView(world.store), _query())
    assert status == 502
    assert body["error"] == "summary_unreadable"
    assert "does not exist" in body["detail"]


def test_a_listed_part_that_is_gone_is_unreadable(nwb_bytes: bytes) -> None:
    world = make_world(nwb_bytes)
    built = dataset_summary.summarize_session(DATASET, SUBJECT, SESSION, world.deps)
    prefix = f"summary/{DATASET}/{SUBJECT}/{SESSION}/{built['digest']}"
    del world.store.objects[f"{prefix}/rates-scene_cut.json"]
    status, body = _call(ComputeRoleView(world.store), _query())
    assert status == 502
    assert f"{prefix}/rates-scene_cut.json" in body["detail"]


def test_a_pointer_outside_the_session_is_unreadable() -> None:
    store = DerivedStore()
    key = dataset_summary.latest_key(DATASET, SUBJECT, SESSION)
    other = f"summary/{DATASET}/sub-other/{SESSION}/{DIGEST}/manifest.json"
    store.objects[key] = json.dumps({"digest": DIGEST, "manifest": other}).encode()
    status, body = _call(ComputeRoleView(store), _query())
    assert status == 502
    assert other in body["detail"]


# --- the body cap ---


def _store_session(
    store: DerivedStore,
    names: tuple[str, str, str],
    parts: dict[str, dict[str, Any]],
    *,
    stored_bytes: int | None = None,
    total_units: int = 44,
) -> dict[str, Any]:
    """Store parts, a manifest with the writer's fields, and the pointer."""
    dataset, subject, session = names
    prefix = f"{dataset_summary.session_prefix(dataset, subject, session)}/{DIGEST}"
    size = 0
    for name, part in parts.items():
        blob = json.dumps(part, allow_nan=False, separators=(",", ":")).encode()
        store.objects[f"{prefix}/{name}"] = blob
        size += len(blob)
    manifest = {
        "schema": dataset_summary.SESSION_SCHEMA,
        "dataset": dataset,
        "subject": subject,
        "session": session,
        "digest": DIGEST,
        "asset_id": G_ASSET,
        "display": dataset_summary.DISPLAY,
        "inputs": {
            "recipe": "recipe-1",
            "defaults": "defaults-1",
            "code": "summary-2+000000000000",
            "assets": [{"asset_id": G_ASSET, "s3_version_id": "v1"}],
        },
        "units": {
            "total": total_units,
            "stored": min(total_units, compute_api.MAX_UNITS),
        },
        "units_table": (
            None
            if "units.json" not in parts
            else {
                "rows": len(parts["units.json"]["rows"]),
                "more": parts["units.json"]["more"],
            }
        ),
        "parts": list(parts),
        "skipped": [],
        "built_at": "2026-10-27T12:00:00+00:00",
        "build_ms": 1234,
        "athena_queries": 7,
        "bytes": size if stored_bytes is None else stored_bytes,
    }
    store.objects[f"{prefix}/manifest.json"] = json.dumps(manifest).encode()
    latest = {
        "digest": DIGEST,
        "manifest": f"{prefix}/manifest.json",
        "as_of": "2026-10-24T17:50:20",
    }
    store.objects[dataset_summary.latest_key(*names)] = json.dumps(latest).encode()
    return manifest


def test_a_hand_made_manifest_has_the_writers_fields(nwb_bytes: bytes) -> None:
    """The golden's manifest carries exactly the fields the writer writes."""
    world = make_world(nwb_bytes)
    built = dataset_summary.summarize_session(DATASET, SUBJECT, SESSION, world.deps)
    prefix = f"summary/{DATASET}/{SUBJECT}/{SESSION}/{built['digest']}"
    written = world.store.json(f"{prefix}/manifest.json")
    made = _store_session(DerivedStore(), (DATASET, SUBJECT, SESSION), {})
    assert set(made) == set(written)
    assert set(made["inputs"]) == set(written["inputs"])
    assert set(made["units"]) == set(written["units"])


@pytest.mark.parametrize(
    ("schema", "readable"),
    [
        ("asteroidea.session-summary/1", True),
        ("asteroidea.session-summary/1.1", True),
        ("asteroidea.session-summary/1.9", True),
        ("asteroidea.session-summary/2", False),
        ("asteroidea.session-summary/10", False),
        ("asteroidea.dataset-summary/1", False),
    ],
)
def test_a_minor_schema_version_reads_and_another_major_is_unreadable(
    schema: str, *, readable: bool
) -> None:
    """A summary stored before the events and units parts still opens the page."""
    store = DerivedStore()
    names = (DATASET, SUBJECT, SESSION)
    rates = json.loads(RATES.read_text())
    manifest = _store_session(store, names, {"rates-scene_cut.json": rates})
    prefix = f"{dataset_summary.session_prefix(*names)}/{DIGEST}"
    store.objects[f"{prefix}/manifest.json"] = json.dumps(
        {**manifest, "schema": schema}
    ).encode()
    status, body = _call(ComputeRoleView(store), _query())
    if readable:
        assert (status, list(body["parts"])) == (200, ["rates-scene_cut"])
    else:
        assert (status, body["error"]) == (502, "summary_unreadable")
        assert repr(schema) in body["detail"]


def test_parts_over_the_cap_are_presigned_without_reading_them() -> None:
    store = DerivedStore()
    names = (DATASET, SUBJECT, SESSION)
    rates = json.loads(RATES.read_text())
    _store_session(
        store,
        names,
        {"rates-scene_cut.json": rates},
        stored_bytes=summary_read.MAX_BODY_BYTES + 1,
    )
    view = ComputeRoleView(store)
    status, body = _call(view, _query())
    assert status == 200, body
    assert body["delivery"] == "presigned"
    assert "parts" not in body
    assert body["expires_in"] == summary_read.PART_URL_TTL_S
    url = urlsplit(body["part_urls"]["rates-scene_cut"])
    assert url.path.endswith(f"/{DIGEST}/rates-scene_cut.json")
    assert parse_qs(url.query)["X-Amz-Expires"] == [str(summary_read.PART_URL_TTL_S)]
    assert not any(k.endswith("rates-scene_cut.json") for k in view.gets)


def test_a_manifest_that_undercounts_still_falls_back_to_presigned() -> None:
    store = DerivedStore()
    big = {"pad": "x" * summary_read.MAX_BODY_BYTES}
    _store_session(
        store, (DATASET, SUBJECT, SESSION), {"rates-a.json": big}, stored_bytes=10
    )
    status, body = _call(ComputeRoleView(store), _query())
    assert status == 200
    assert body["delivery"] == "presigned"
    assert list(body["part_urls"]) == ["rates-a"]


def test_the_cap_is_the_compute_routes_cap() -> None:
    assert summary_read.MAX_BODY_BYTES == compute_api.MAX_BODY_BYTES


# --- the request ---


@pytest.mark.parametrize(
    ("query", "code"),
    [
        (None, "invalid_request"),
        (_query(dataset="DS1"), "invalid_request"),
        (_query(dataset="ds/1"), "invalid_request"),
        (_query(dataset="ds1\n"), "invalid_request"),
        (_query(subject=""), "invalid_request"),
        (_query(subject="../x"), "invalid_request"),
        (_query(subject="sub/1"), "invalid_request"),
        (_query(session=""), "invalid_session"),
        (_query(session="ses/1"), "invalid_session"),
        (_query(session="_x"), "invalid_session"),
    ],
)
def test_a_malformed_name_is_400_before_any_read(
    query: dict[str, str] | None, code: str
) -> None:
    view = ComputeRoleView(DerivedStore())
    status, body = _call(view, query)
    assert status == 400, body
    assert body["error"] == code
    assert view.gets == []


def test_the_no_session_spelling_reads_the_underscore_key() -> None:
    view = ComputeRoleView(DerivedStore())
    status, body = _call(view, _query(session="_"))
    assert (status, body) == (200, {"status": "missing"})
    assert view.gets == [f"summary/{DATASET}/{SUBJECT}/_/latest.json"]


def test_outside_the_researchers_group_is_403() -> None:
    view = ComputeRoleView(DerivedStore())
    status, _ = _call(view, _query(), groups="[]")
    assert status == 403
    assert view.gets == []


# --- the golden body the web parses ---


def test_the_golden_body_carries_the_events_units_and_s1_rates_parts() -> None:
    store = DerivedStore()
    names = (G_DATASET, G_SUBJECT, G_SESSION)
    rates = json.loads(RATES.read_text())
    events = {**json.loads(EVENTS.read_text()), "asset_id": G_ASSET}
    units = json.loads(UNITS.read_text())
    assert {r["asset_id"] for r in units["rows"]} == {G_ASSET}
    assert (len(units["rows"]), units["more"]) == (44, False)
    parts = {
        "events.json": events,
        "units.json": units,
        "rates-scene_cut.json": rates,
    }
    _store_session(store, names, parts)
    status, body = _call(ComputeRoleView(store), _query(*names))
    assert status == 200, body
    assert body["delivery"] == "inline"
    assert body["parts"] == {
        "events": events,
        "units": units,
        "rates-scene_cut": rates,
    }
    assert body["manifest"]["units_table"] == {"rows": 44, "more": False}
    check_golden(GOLDEN, body)
