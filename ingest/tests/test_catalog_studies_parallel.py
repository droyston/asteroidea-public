"""GET /catalog/studies reads the stored summaries alongside the Athena round.

No real sleeps: each fake records its calls on an injected, thread-safe tick clock,
and blocks only on an `Event` that the other side sets. If the summary GETs ran
after the Athena round (or at a lower concurrency than the pool size), the Athena
fake would wait for a GET that never starts, or the GETs for a pool that never fills,
and the wait would time out and fail the test.
"""

import json
import threading
from typing import TYPE_CHECKING, Any, Final, cast, override

from botocore.exceptions import ClientError

from asteroidea_ingest import catalog_api
from asteroidea_ingest.catalog_api import SUMMARY_READERS

from .conftest import jwt_event
from .test_catalog_studies_stored import (
    ALPHA,
    BETA,
    CALLER,
    DERIVED,
    GAMMA,
    STUDIES,
    SqliteAthena,
    SummaryStore,
    _api,
    _database,
    _live_body,
    _stored_by_the_builder,
    _studies,
    _summary,
)

if TYPE_CHECKING:
    import pytest

WAIT_SECONDS: Final = 5.0


class Ticks:
    """An injected clock: every reading is the next integer, safe across threads."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.now = 0
        self.log: list[tuple[str, int]] = []

    def mark(self, what: str) -> int:
        with self.lock:
            self.now += 1
            self.log.append((what, self.now))
            return self.now

    def first(self, what: str) -> int:
        return next(t for w, t in self.log if w == what)

    def last(self, what: str) -> int:
        return max(t for w, t in self.log if w == what)


class TimedAthena(SqliteAthena):
    """Every poll waits for the first summary GET: the Athena round cannot end first."""

    def __init__(self, ticks: Ticks, got_one: threading.Event) -> None:
        super().__init__(_database())
        self.ticks, self.got_one = ticks, got_one

    @override
    def start_query_execution(self, **kwargs: Any) -> dict[str, Any]:
        self.ticks.mark("athena-start")
        return super().start_query_execution(**kwargs)

    @override
    def get_query_execution(self, **kwargs: Any) -> dict[str, Any]:
        assert self.got_one.wait(WAIT_SECONDS), "no summary GET began during Athena"
        self.ticks.mark("athena-end")
        return super().get_query_execution(**kwargs)


class TimedStore(SummaryStore):
    """A store that records each GET and holds all of them until `release` is set."""

    def __init__(self, ticks: Ticks, got_one: threading.Event) -> None:
        super().__init__()
        self.ticks, self.got_one = ticks, got_one
        self.release = threading.Event()
        self.lock = threading.Lock()
        self.in_flight = 0
        self.peak = 0

    @override
    def list_objects_v2(self, **kwargs: Any) -> dict[str, Any]:
        self.ticks.mark("list-start")
        return super().list_objects_v2(**kwargs)

    @override
    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        self.ticks.mark("get-start")
        self.got_one.set()
        with self.lock:
            self.in_flight += 1
            self.peak = max(self.peak, self.in_flight)
            if self.in_flight >= SUMMARY_READERS:
                self.release.set()
        try:
            self.release.wait(WAIT_SECONDS)
            return super().get_object(**kwargs)
        finally:
            with self.lock:
                self.in_flight -= 1
            self.ticks.mark("get-end")


def _timed(extra: int) -> tuple[Ticks, TimedAthena, TimedStore]:
    ticks, got_one = Ticks(), threading.Event()
    store = TimedStore(ticks, got_one)
    api = _api(SqliteAthena(_database()), None)
    for name in (ALPHA, BETA, GAMMA):
        store.put(name, _summary(api.dataset_entry(name, deadline=1e9)))
    for i in range(extra):  # summaries of datasets the catalog does not list
        store.put(f"extra{i:03d}", _summary(catalog_api.empty_entry()))
    return ticks, TimedAthena(ticks, got_one), store


def test_summary_gets_begin_before_the_athena_round_ends() -> None:
    ticks, athena, store = _timed(extra=0)
    store.release.set()
    body = _studies(athena, store)
    assert body == _live_body()
    assert ticks.first("list-start") < ticks.last("athena-end")
    assert ticks.first("get-start") < ticks.first("athena-end")
    assert ticks.first("athena-start") < ticks.first("get-end")  # both ran, joined


def test_the_get_pool_fills_to_its_size_and_no_further() -> None:
    assert SUMMARY_READERS == 32
    ticks, athena, store = _timed(extra=70)  # 73 summaries, 3 of them listed
    body = _studies(athena, store)
    assert body == _live_body()  # the 70 others are read and discarded
    assert len(store.gets) == 73
    assert store.peak == SUMMARY_READERS
    assert ticks.first("get-start") < ticks.first("athena-end")


def test_the_handler_gives_the_s3_client_a_pool_as_large_as_the_readers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configs: dict[str, Any] = {}
    store = _stored_by_the_builder(ALPHA, BETA, GAMMA)
    athena = SqliteAthena(_database())

    def client(name: str, **kwargs: Any) -> object:
        configs[name] = kwargs.get("config")
        return {"athena": athena, "s3": store}[name]

    monkeypatch.setattr(catalog_api.boto3, "client", client)
    monkeypatch.setenv("ATHENA_WORKGROUP", "asteroidea")
    monkeypatch.setenv("GLUE_DATABASE", "asteroidea")
    monkeypatch.setenv("RAW_BUCKET", "raw-bucket")
    monkeypatch.setenv("DERIVED_BUCKET", DERIVED)
    event = {**jwt_event(STUDIES, sub=CALLER), "queryStringParameters": None}
    assert catalog_api.handler(event, object())["statusCode"] == 200
    assert configs["s3"].max_pool_connections == SUMMARY_READERS >= 32


def test_the_timing_line_splits_athena_summaries_and_overlap(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _studies(SqliteAthena(_database()), _stored_by_the_builder(ALPHA, BETA, GAMMA))
    lines = [json.loads(x) for x in capsys.readouterr().out.splitlines() if x]
    phases = next(x for x in lines if x["timing"] == STUDIES)["phases"]
    assert {"athena_ms", "summaries_ms", "overlap_ms", "total_ms"} <= set(phases)


def test_overlap_is_the_shared_span_and_never_negative() -> None:
    assert catalog_api.overlap_ms(0.0, 3.0, 1.0, 5.0) == 2000
    assert catalog_api.overlap_ms(0.0, 1.0, 2.0, 3.0) == 0


def test_a_refused_listing_reads_the_needed_summaries_by_name() -> None:
    class NoList(SummaryStore):
        @override
        def list_objects_v2(self, **kwargs: Any) -> dict[str, Any]:
            raise ClientError(
                {"Error": {"Code": "AccessDenied", "Message": "no"}}, "ListObjectsV2"
            )

    store = NoList()
    api = _api(SqliteAthena(_database()), None)
    for name in (ALPHA, BETA, GAMMA):
        store.put(name, _summary(api.dataset_entry(name, deadline=1e9)))
    athena = SqliteAthena(_database())
    assert _studies(athena, cast("Any", store)) == _live_body()
    assert sorted(store.gets) == [
        catalog_api.summary_key(n) for n in (ALPHA, BETA, GAMMA)
    ]
    assert len(athena.started) == 3  # no live fallback: every entry was read


def test_a_dataset_the_listing_lacks_reads_live() -> None:
    athena = SqliteAthena(_database())
    store = _stored_by_the_builder(ALPHA, BETA)  # gamma has no summary key at all
    assert _studies(athena, store) == _live_body()
    assert [k for k in store.gets if "gamma" in k] == []  # never requested
    assert len(athena.started) == 3 + len(catalog_api.DATASET_ENTRY_SQL)
