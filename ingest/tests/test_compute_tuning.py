"""`GET /compute/tuning` and `GET /compute/events` through the handler.

The rows are the CS62 R2 session's (`sdk/tests/fixtures/dandi-000623`, as in
`test_compute_events.py`); the spikes are synthetic, seeded, with unit 0 firing
after every `change` cut so one unit is tuned. Every call goes through
`ComputeApi.handle` with the route's key, so the dispatch table is exercised. The
oracle is `analysis.tuning` on the same spikes and the resolver's events.
"""

import json
from dataclasses import dataclass, field
from typing import Any, Final, override

import numpy as np
import pytest

from asteroidea import analysis
from asteroidea_ingest import compute_api
from asteroidea_ingest.compute_api import ComputeApi

from .conftest import jwt_event
from .test_compute import Cols, Rows, StubLabels, StubS3
from .test_compute_events import (
    ASSET_ID,
    CSV_BYTES,
    CSV_KEY,
    DATASET,
    NWB_KEY,
    STAMPS,
    CsAthena,
    _oracle,
    _write,
)

TUNING: Final = "GET /compute/tuning"
EVENTS: Final = "GET /compute/events"
N_UNITS: Final = 12
SPAN_S: Final = 760.0
PER_UNIT: Final = 1734  # CS62 R2 density: 76,279 spikes over 44 units
LOCATIONS: Final = ("amygdala", "hippocampus")


def synthetic(n_units: int, seed: int = 7) -> dict[int, list[float]]:
    """Return seeded uniform spike trains; unit 0 also fires after each change cut."""
    rng = np.random.default_rng(seed)
    out = {u: np.sort(rng.uniform(0.0, SPAN_S, PER_UNIT)) for u in range(n_units)}
    changes = [
        e.onset_s
        for e in _oracle(len(STAMPS))
        if e.event_type == "scene_cut" and e.attributes.get("cut_kind") == "change"
    ]
    burst = np.concatenate([np.asarray(changes) + d for d in (0.1, 0.2, 0.3, 0.4)])
    out[0] = np.sort(np.concatenate([out[0], burst]))
    return {u: [round(float(t), 6) for t in times] for u, times in out.items()}


@dataclass
class TuningAthena(CsAthena):
    """CsAthena with `spikes` as the units and their `location` column."""

    spikes: dict[int, list[float]] = field(default_factory=dict)

    @override
    def _answer(self, sql: str, params: list[str]) -> tuple[Cols, Rows]:
        if "FROM unit_spikes" in sql:
            wanted = [int(u) for u in params[1].split(",")]
            cols: Cols = [("unit_index", "bigint"), ("spike_times", "array")]
            rows: Rows = [
                [str(u), "[" + ", ".join(map(str, self.spikes[u])) + "]"]
                for u in wanted
                if u in self.spikes
            ]
            return cols, rows
        if "FROM units" in sql:
            every = sorted(self.spikes)
            wanted = (
                every if len(params) == 1 else [int(u) for u in params[1].split(",")]
            )
            unit_cols: Cols = [
                ("unit_index", "bigint"),
                ("spike_count", "bigint"),
                ("first_spike_s", "double"),
                ("last_spike_s", "double"),
                ("location", "varchar"),
            ]
            return unit_cols, [
                [
                    str(u),
                    str(len(self.spikes[u])),
                    str(self.spikes[u][0]),
                    str(self.spikes[u][-1]),
                    LOCATIONS[u % 2],
                ]
                for u in wanted
                if u in self.spikes
            ]
        return super()._answer(sql, params)


@pytest.fixture(scope="module")
def nwb_bytes(tmp_path_factory: pytest.TempPathFactory) -> bytes:
    path = tmp_path_factory.mktemp("cs62-tuning") / "cs62.nwb"
    _write(path, STAMPS)
    return path.read_bytes()


def make_api(data: bytes, n_units: int = N_UNITS) -> tuple[ComputeApi, TuningAthena]:
    """Return the API over the CS62 R2 rows with `n_units` synthetic units."""
    athena = TuningAthena(size=len(data), spikes=synthetic(n_units))
    api = ComputeApi(
        athena=athena,
        workgroup="wg",
        database="db",
        s3=StubS3({NWB_KEY: data, CSV_KEY: CSV_BYTES}),
        raw_bucket="raw-bucket",
        clock=lambda: athena.clock,
        sleep=athena.sleep,
        labels=StubLabels([]),
        labels_table="labels",
    )
    return api, athena


def call(
    api: ComputeApi, route: str, query: dict[str, str], *, remaining_ms: int = 28_000
) -> tuple[int, dict[str, Any]]:
    """Answer one request on `route` through `handle` (the dispatch table)."""
    event = {**jwt_event(route), "queryStringParameters": query}
    resp = api.handle(event, remaining_ms)
    return resp["statusCode"], json.loads(resp["body"])


def oracle(
    spikes: dict[int, list[float]], event: str, attribute: str
) -> analysis.TuningResult:
    """Return `analysis.tuning` on every resolved event, all attributes kept."""
    trains = {u: np.asarray(t, dtype=np.float64) for u, t in spikes.items()}
    results = analysis.tuning(
        trains,
        _oracle(len(STAMPS)),
        analysis.load_recipe(DATASET),
        analysis.load_defaults(DATASET),
        {u: LOCATIONS[u % 2] for u in spikes},
    )
    (found,) = [r for r in results if (r.event_type, r.attribute) == (event, attribute)]
    return found


def query(event: str, attribute: str, units: str = "all") -> dict[str, str]:
    return {
        "asset_id": ASSET_ID,
        "event": event,
        "attribute": attribute,
        "units": units,
    }


@pytest.mark.parametrize(
    ("event", "attribute", "levels"),
    [
        ("scene_cut", "cut_kind", {"change": 13, "continuity": 80}),
        ("recognition_onset", "familiarity", {"familiar": 19, "novel": 15}),
        ("scene_cut", "scene_id", {"5": 31, "10": 19, "7": 13, "13": 12}),
    ],
)
def test_tuning_matches_the_analysis_screen(
    nwb_bytes: bytes, event: str, attribute: str, levels: dict[str, int]
) -> None:
    api, athena = make_api(nwb_bytes)
    status, body = call(api, TUNING, query(event, attribute))
    assert status == 200, body
    want = oracle(athena.spikes, event, attribute)
    assert body["label"] == "exploratory screen"
    assert (body["event_type"], body["attribute"]) == (event, attribute)
    assert body["source"] == "catalog"
    settings = body["settings"]
    assert settings["levels"] == sorted(levels)
    assert {k: settings["level_counts"][k] for k in levels} == levels
    assert settings["n_permutations"] == 2000
    assert settings["seed"] == 13
    assert settings["alpha"] == 0.05
    assert settings["min_events_per_level"] == 10  # the dandi-000623 paper block
    assert settings["count_events_where"] == {"response_correct": 1}
    assert settings["defaults_version"] == "defaults-1"
    assert settings["recipe_version"] == "recipe-1"
    assert [u["unit_index"] for u in body["units"]] == [r.unit for r in want.rows]
    for got, row in zip(body["units"], want.rows, strict=True):
        assert got["p"] == row.p
        assert got["q"] == row.q
        assert got["screened"] == row.screened
        assert got["location"] == row.location
        assert got["n_per_level"] == levels
        assert got["selectivity_index"] == pytest.approx(row.selectivity_index, 1e-5)
        for level, mean in row.level_means.items():
            assert got["level_means_hz"][level] == pytest.approx(mean, rel=1e-5)
    assert [a["location"] for a in body["areas"]] == list(LOCATIONS)
    for got, area in zip(body["areas"], want.areas, strict=True):
        assert (got["n_units"], got["n_below_alpha"], got["p"]) == (
            area.n_units,
            area.n_below_alpha,
            area.p,
        )


def test_the_tuned_unit_is_screened(nwb_bytes: bytes) -> None:
    api, _ = make_api(nwb_bytes)
    status, body = call(api, TUNING, query("scene_cut", "cut_kind"))
    assert status == 200, body
    first = body["units"][0]
    assert first["unit_index"] == 0
    assert first["screened"] is True
    assert first["level_means_hz"]["change"] > first["level_means_hz"]["continuity"]


def test_a_range_and_a_list_select_the_units(nwb_bytes: bytes) -> None:
    api, _ = make_api(nwb_bytes)
    status, body = call(api, TUNING, query("scene_cut", "cut_kind", "2-4"))
    assert status == 200, body
    assert [u["unit_index"] for u in body["units"]] == [2, 3, 4]
    status, body = call(api, TUNING, query("scene_cut", "cut_kind", "0,5"))
    assert status == 200, body
    assert [u["unit_index"] for u in body["units"]] == [0, 5]


@pytest.mark.parametrize(
    ("override", "status", "code"),
    [
        ({"attribute": "colour"}, 400, "no_such_attribute"),
        ({"event": "movie_onset", "attribute": "x"}, 400, "not_average"),
        ({"event": "nope"}, 404, "no_such_event_type"),
        ({"attribute": ""}, 400, "invalid_request"),
        ({"units": "3-1"}, 400, "malformed_range"),
        ({"asset_id": "nope"}, 404, "not_found"),
        ({"asset_id": "csv-623"}, 422, "not_nwb"),
    ],
)
def test_tuning_refusals_carry_their_code(
    nwb_bytes: bytes, override: dict[str, str], status: int, code: str
) -> None:
    api, _ = make_api(nwb_bytes)
    got, body = call(api, TUNING, {**query("scene_cut", "cut_kind"), **override})
    assert (got, body["error"]) == (status, code), body


def test_a_level_under_the_minimum_is_400_naming_the_counts(nwb_bytes: bytes) -> None:
    # Correct recognition trials by actual_response: no two levels hold 10.
    api, _ = make_api(nwb_bytes)
    status, body = call(api, TUNING, query("recognition_onset", "actual_response"))
    assert status == 400
    assert body["error"] == "too_few_events_per_level"
    want = oracle(make_api(nwb_bytes)[1].spikes, "recognition_onset", "actual_response")
    assert want.skipped is not None
    for level, count in want.level_counts.items():
        assert f"{level} {count}" in body["detail"]
    assert "10" in body["detail"]


def test_tuning_over_the_unit_bound_is_400(nwb_bytes: bytes) -> None:
    api, athena = make_api(nwb_bytes, n_units=compute_api.MAX_UNITS + 1)
    status, body = call(api, TUNING, query("scene_cut", "cut_kind"))
    assert (status, body["error"]) == (400, "too_many_units")
    assert str(compute_api.MAX_UNITS + 1) in body["detail"]
    texts = [s["QueryString"] for s in athena.started]
    assert compute_api.SPIKES_SQL not in texts  # refused before the spikes


def test_a_tuning_body_over_the_cap_is_413(
    nwb_bytes: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(compute_api, "MAX_BODY_BYTES", 1000)
    api, _ = make_api(nwb_bytes)
    status, body = call(api, TUNING, query("scene_cut", "cut_kind"))
    assert (status, body["error"]) == (413, "too_large")


def test_tuning_past_the_deadline_is_503(
    nwb_bytes: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    api, athena = make_api(nwb_bytes)
    status, body = call(api, TUNING, query("scene_cut", "cut_kind"), remaining_ms=1000)
    assert (status, body["error"]) == (503, "slow")
    real = analysis.tuning

    def slow(*args: Any, **kwargs: Any) -> Any:
        athena.clock += 60.0  # the screen outlasts the deadline
        return real(*args, **kwargs)

    monkeypatch.setattr(analysis, "tuning", slow)
    status, body = call(api, TUNING, query("scene_cut", "cut_kind"))
    assert (status, body) == (503, {"error": "slow", "detail": compute_api.SLOW})


def test_the_events_route_answers_through_the_dispatch_table(nwb_bytes: bytes) -> None:
    api, _ = make_api(nwb_bytes)
    status, body = call(api, EVENTS, {"asset_id": ASSET_ID})
    assert status == 200, body
    names = [t["name"] for t in body["event_types"]]
    assert names == ["scene_cut", "recognition_onset", "movie_onset"]
    assert call(api, EVENTS, {"asset_id": "nope"})[0] == 404
