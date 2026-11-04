"""Live test: the catalog routes behind the API.

Run with `make live T=tests/live/test_catalog_api.py` after the deploy
(page budget: query p50 at most 3.5 s and max at most 8 s;
whole page p50 at most 5 s and worst 10 s).

Stand-in: sign-in is the headless `sandbox-test` client (the conftest
`auth` fixture); the non-member is a user in no group, signed in the same way.
Nothing is written to the sandbox except that groupless user (kept in the scratch
directory, as `test_raw_bucket_deny.py` does).

The latency test makes 10 calls per route, one round of three calls about every 12 s
(at least 2 minutes), records the wall time from this machine and the body's
`query_ms`, prints a table, and logs whether the first call was a cold start (the
CatalogFunction REPORT line's `Init Duration`; not asserted).
"""

import json
import statistics
import time
from typing import TYPE_CHECKING, Any, Final, NamedTuple

import pytest

from asteroidea._http import send
from asteroidea_infra.admin import stack_outputs

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    import boto3

    from asteroidea._auth import Auth

EXEMPLARS: Final = ("dandi-000623", "openneuro-ds004798")
DATASET: Final = "openneuro-ds004798"
SUBJECT: Final = "sub-p41cs"
CALLS: Final = 10
ROUND_SECONDS: Final = 12.0
P50_MAX: Final = 5.0
WORST_MAX: Final = 10.0
LOGS_WITHIN: Final = 60.0
ROUTES: Final = ("datasets", "assets", "asset")


class Reply(NamedTuple):
    """One response: status, parsed body and wall seconds from this machine."""

    status: int
    body: dict[str, Any]
    wall: float


class Api(NamedTuple):
    """The base URL and a function that sends one authenticated GET."""

    base: str
    get: Callable[[str, str | None], Reply]


def _getter(
    base: str, token_of: Callable[[], str]
) -> Callable[[str, str | None], Reply]:
    def get(path: str, token: str | None = None) -> Reply:
        headers = {"Authorization": f"Bearer {token or token_of()}"}
        started = time.monotonic()
        reply = send("GET", f"{base.rstrip('/')}{path}", headers)
        wall = time.monotonic() - started
        body = json.loads(reply.body) if reply.body else {}
        return Reply(reply.status, body, wall)

    return get


@pytest.fixture(scope="module")
def api(auth: Auth) -> Api:
    """Return an authenticated GET over the researcher's access token."""
    return Api(
        auth.config.api_url,
        _getter(auth.config.api_url, lambda: auth.tokens().access_token),
    )


@pytest.fixture(scope="module")
def asset_id(api: Api) -> str:
    """Return one asset id of the OpenNeuro exemplar."""
    found = api.get(f"/catalog/datasets/{DATASET}/assets", None)
    assert found.status == 200, found
    return str(found.body["assets"][0]["asset_id"])


def test_datasets_lists_the_exemplars(api: Api) -> None:
    found = api.get("/catalog/datasets", None)
    assert found.status == 200, found
    names = {row["dataset"] for row in found.body["datasets"]}
    assert set(EXEMPLARS) <= names, names
    assert isinstance(found.body["query_ms"], int)


def test_the_subject_filter_narrows_the_assets(api: Api) -> None:
    whole = api.get(f"/catalog/datasets/{DATASET}/assets?page=0", None)
    narrow = api.get(f"/catalog/datasets/{DATASET}/assets?subject={SUBJECT}", None)
    assert whole.status == 200, whole
    assert narrow.status == 200, narrow
    assert narrow.body["assets"], "the filter found no asset of the subject"
    assert {a["subject"] for a in narrow.body["assets"]} == {SUBJECT}
    assert len(narrow.body["assets"]) < len(whole.body["assets"]) or (
        {a["subject"] for a in whole.body["assets"]} != {SUBJECT}
    )


def test_one_asset_returns_its_row_and_headers(api: Api, asset_id: str) -> None:
    found = api.get(f"/catalog/assets/{asset_id}", None)
    assert found.status == 200, found
    assert found.body["asset"]["asset_id"] == asset_id
    assert isinstance(found.body["headers"], list)


def test_an_unknown_asset_is_404(api: Api) -> None:
    missing = api.get("/catalog/assets/no-such-asset-id", None)
    assert missing.status == 404, missing


def test_a_non_member_is_refused_403(
    api: Api, groupless_tokens: Mapping[str, Any]
) -> None:
    token = str(groupless_tokens["AccessToken"])
    for path in (
        "/catalog/datasets",
        f"/catalog/datasets/{DATASET}/assets",
        "/catalog/assets/anything",
    ):
        refused = api.get(path, token)
        assert refused.status == 403, (path, refused)


def _quantile(values: list[float], share: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(share * (len(ordered) - 1)))]


def test_latency_is_inside_the_page_budget(
    api: Api,
    asset_id: str,
    admin_session: boto3.Session,
    load_sibling: Callable[[str], Any],
) -> None:
    ingest = load_sibling("test_ingest_core")
    paths = {
        "datasets": "/catalog/datasets",
        "assets": f"/catalog/datasets/{DATASET}/assets?page=0",
        "asset": f"/catalog/assets/{asset_id}",
    }
    started_ms = int(time.time() * 1000)
    walls: dict[str, list[float]] = {route: [] for route in ROUTES}
    queries: dict[str, list[float]] = {route: [] for route in ROUTES}
    for _ in range(CALLS):
        round_started = time.monotonic()
        for route in ROUTES:
            found = api.get(paths[route], None)
            assert found.status == 200, (route, found)
            walls[route].append(found.wall)
            queries[route].append(float(found.body["query_ms"]) / 1000)
        time.sleep(max(0.0, ROUND_SECONDS - (time.monotonic() - round_started)))
    elapsed = (time.time() * 1000 - started_ms) / 1000
    lines = [f"catalog latency: {CALLS} calls per route over {elapsed:.0f} s"]
    lines.append("route     wall p50  wall max  query p50  query max (seconds)")
    for route in ROUTES:
        w, q = walls[route], queries[route]
        lines.append(
            f"{route:<9} {statistics.median(w):8.2f}  {max(w):8.2f}  "
            f"{statistics.median(q):9.2f}  {max(q):9.2f}"
        )
    for line in lines:
        ingest.report(line)
    ingest.report(_cold_start(admin_session, ingest, started_ms))
    assert elapsed >= 120, elapsed
    for route in ROUTES:
        assert statistics.median(walls[route]) <= P50_MAX, (route, walls[route])
        assert max(walls[route]) <= WORST_MAX, (route, walls[route])


def _cold_start(admin: boto3.Session, ingest: Any, since_ms: int) -> str:
    """Return a line saying whether the first call was a cold start (not asserted)."""
    names: list[str] = []
    pages = admin.client("lambda").get_paginator("list_functions")
    for page in pages.paginate():
        names.extend(f["FunctionName"] for f in page["Functions"])
    catalog = [n for n in names if "CatalogFunction" in n]
    if len(catalog) != 1:
        return f"catalog cold start: skipped, {len(catalog)} CatalogFunction names"
    outputs = stack_outputs(admin)
    shell = ingest.Live(admin, outputs, None, catalog[0], catalog[0])
    deadline = time.monotonic() + LOGS_WITHIN
    found = ingest.invocations(shell, catalog[0], since_ms - 1000)
    while not found and time.monotonic() < deadline:
        time.sleep(5)
        found = ingest.invocations(shell, catalog[0], since_ms - 1000)
    if not found:
        return f"catalog cold start: no REPORT line within {LOGS_WITHIN:.0f} s"
    colds = [i.init_ms for i in found if i.init_ms is not None]
    first = found[0]
    kind = f"cold, Init Duration {first.init_ms} ms" if first.init_ms else "warm"
    return (
        f"catalog cold start: first call {kind}; {len(colds)} cold start(s) in "
        f"{len(found)} invocations (not asserted)"
    )
