"""`GET /compute/rates` with `align=event:<type>`: the defaults and responsive keys.

An `event:` align fills an omitted `pre_s`, `post_s` or `bin_s` from
`analysis.load_defaults(dataset)`, and the body gains `label`, `defaults` (the values
used) and `responsive` (`analysis.responsive` over the same events and units). Other
aligns keep the body they had before. The CS62 R2 rows and synthetic spikes are
`test_compute_tuning.py`'s; the dandi-001775 rows are a hand-made trials table that
the shipped 001775 recipe resolves.
"""

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any, Final, override

import numpy as np
import pytest

from asteroidea import analysis
from asteroidea_ingest import compute_api
from asteroidea_ingest.compute_api import ComputeApi

from .conftest import jwt_event
from .test_compute import ASSET_COLS, Cols, Rows, StubLabels, StubS3, _get
from .test_compute import BASE as SMALL_BASE
from .test_compute import _api as small_api
from .test_compute import _write as write_small
from .test_compute_events import ASSET_ID, STAMPS, _oracle, _write
from .test_compute_tuning import LOCATIONS, TuningAthena, call, make_api, synthetic

RATES: Final = "GET /compute/rates"
OLD_KEYS: Final = {"edges", "units", "n_trials", "align", "source"}
TIMING_KEYS: Final = {"query_ms", "compute_ms"}
NEW_KEYS: Final = {"label", "defaults", "responsive"}
# The worst unit at raster on and MAX_BINS (compute_api's MAX_UNITS comment).
WORST_BYTES_PER_UNIT: Final = 34_113
WARM_BOUND_S: Final = 13.0
PAPER_SOURCE: Final = (
    "Keles et al. 2024, Sci Data 11:214; release code at commit 8c35290b"
)


@pytest.fixture(scope="module")
def nwb_bytes(tmp_path_factory: pytest.TempPathFactory) -> bytes:
    path = tmp_path_factory.mktemp("cs62-defaults") / "cs62.nwb"
    _write(path, STAMPS)
    return path.read_bytes()


def rates(api: ComputeApi, align: str, **extra: str) -> tuple[int, dict[str, Any]]:
    return call(
        api, RATES, {"asset_id": ASSET_ID, "units": "all", "align": align, **extra}
    )


def test_an_omitted_window_is_filled_from_the_000623_block(nwb_bytes: bytes) -> None:
    api, athena = make_api(nwb_bytes)
    status, body = rates(api, "event:recognition_onset")
    assert status == 200, body
    assert body["label"] == "exploratory screen"
    assert body["defaults"] == {
        "version": "defaults-1",
        "block": "dandi-000623",
        "source": PAPER_SOURCE,
        "baseline": "none",  # the paper block's display baseline
        "bin_s": 0.25,
        "pre_s": 0.5,
        "post_s": 1.0,
        "response_window_s": [0.2, 1.7],  # the recipe's window for this type
    }
    assert body["edges"] == [-0.5, -0.25, 0.0, 0.25, 0.5, 0.75, 1.0]
    onsets = np.asarray(
        [e.onset_s for e in _oracle(len(STAMPS)) if e.event_type == "recognition_onset"]
    )
    trains = {u: np.asarray(t) for u, t in athena.spikes.items()}
    want = analysis.rates(trains, onsets, 0.5, 1.0, 0.25)
    for row, unit in enumerate(body["units"]):
        np.testing.assert_allclose(unit["rate_hz"], want.rate_hz[row], rtol=1e-5)
    explicit = rates(
        api, "event:recognition_onset", pre_s="0.5", post_s="1", bin_s="0.25"
    )
    assert explicit == (status, body)


def test_an_explicit_value_overrides_its_default(nwb_bytes: bytes) -> None:
    api, _ = make_api(nwb_bytes)
    status, body = rates(api, "event:recognition_onset", pre_s="2")
    assert status == 200, body
    assert (body["defaults"]["pre_s"], body["defaults"]["post_s"]) == (2.0, 1.0)
    assert body["defaults"]["bin_s"] == 0.25
    assert body["edges"][0] == -2.0
    assert body["edges"][-1] == 1.0


def test_a_filled_window_past_the_bin_bound_names_the_defaults(
    nwb_bytes: bytes,
) -> None:
    api, _ = make_api(nwb_bytes)
    status, body = rates(api, "event:recognition_onset", pre_s="10", bin_s="0.001")
    assert (status, body["error"]) == (400, "defaults_window"), body
    assert "dandi-000623 defaults: post_s 1" in body["detail"]


def test_other_aligns_still_require_the_window(nwb_bytes: bytes) -> None:
    api, _ = make_api(nwb_bytes)
    status, body = rates(api, "intervals:trials")
    assert (status, body) == (
        400,
        {"error": "invalid_request", "detail": "pre_s is required"},
    )


def test_responsive_is_the_analysis_screen_over_the_same_units(
    nwb_bytes: bytes,
) -> None:
    api, athena = make_api(nwb_bytes)
    status, body = rates(api, "event:scene_cut")
    assert status == 200, body
    trains = {u: np.asarray(t, dtype=np.float64) for u, t in athena.spikes.items()}
    results = analysis.responsive(
        trains,
        _oracle(len(STAMPS)),
        analysis.load_recipe("dandi-000623"),
        analysis.load_defaults("dandi-000623"),
        {u: LOCATIONS[u % 2] for u in athena.spikes},
    )
    (want,) = [r for r in results if r.event_type == "scene_cut"]
    assert body["defaults"]["response_window_s"] == list(want.response_window_s)
    got = body["responsive"]
    assert [r["unit_index"] for r in got] == [r.unit for r in want.rows]
    for row, ref in zip(got, want.rows, strict=True):
        assert set(row) == {"unit_index", "location", "p", "q", "screened"}
        assert (row["p"], row["q"], row["screened"]) == (ref.p, ref.q, ref.screened)
        assert row["location"] == ref.location
        assert 0.0 < row["p"] <= 1.0
        assert row["p"] <= row["q"] <= 1.0
    # unit 0 fires after every change cut: the smallest q of the session
    assert min(got, key=lambda r: (r["q"], r["p"]))["unit_index"] == 0


def test_responsive_reads_the_events_the_align_filter_keeps(nwb_bytes: bytes) -> None:
    api, athena = make_api(nwb_bytes)
    status, body = rates(api, "event:scene_cut:cut_kind=change")
    assert status == 200, body
    assert body["n_trials"] == 13
    trains = {u: np.asarray(t, dtype=np.float64) for u, t in athena.spikes.items()}
    events = [
        e
        for e in _oracle(len(STAMPS))
        if e.event_type == "movie_onset" or e.attributes.get("cut_kind") == "change"
    ]
    results = analysis.responsive(
        trains,
        events,
        analysis.load_recipe("dandi-000623"),
        analysis.load_defaults("dandi-000623"),
        {u: LOCATIONS[u % 2] for u in athena.spikes},
    )
    (want,) = [r for r in results if r.event_type == "scene_cut"]
    assert want.n_events == 13
    got = [(r["unit_index"], r["p"], r["q"], r["screened"]) for r in body["responsive"]]
    assert got == [(r.unit, r.p, r.q, r.screened) for r in want.rows]
    assert body["responsive"][0]["screened"] is True


def test_a_single_type_has_an_empty_screen(nwb_bytes: bytes) -> None:
    api, _ = make_api(nwb_bytes)
    status, body = rates(api, "event:movie_onset")
    assert status == 200, body
    assert body["responsive"] == []
    assert body["defaults"]["response_window_s"] == [0.0, 1.0]


# --- dandi-001775: a recipe with no defaults block ---

DNMP_ID: Final = "asset-1775"
DNMP: Final = "dandi-001775"
N_TRIALS: Final = 20


@dataclass
class DnmpAthena(TuningAthena):
    """A dandi-001775 asset whose `trials` the shipped 001775 recipe resolves."""

    @override
    def _answer(self, sql: str, params: list[str]) -> tuple[Cols, Rows]:
        if "FROM assets" in sql:
            row = [DNMP_ID, DNMP, "sub-UT06", "ses-Day5", "raw/x.nwb", "v1", "1", "nwb"]
            return ASSET_COLS, [row] if params == [DNMP_ID] else []
        if "FROM intervals" in sql:
            cols: Cols = [
                ("table_name", "varchar"),
                ("row_index", "bigint"),
                ("start_time", "double"),
                ("stop_time", "double"),
                ("columns_json", "varchar"),
            ]
            rows: Rows = []
            for i in range(N_TRIALS):
                start = 10.0 + 30.0 * i
                attrs = {
                    "epoch_type": "dnmp",
                    "trial_phase": ("sample", "test")[i % 2],
                    "choice": ("L", "R")[(i // 2) % 2],
                }
                rows.append(
                    ["trials", str(i), str(start), str(start + 20.0), json.dumps(attrs)]
                )
            return cols, rows
        return super()._answer(sql, params)


def test_001775_uses_the_default_block() -> None:
    athena = DnmpAthena(size=1, spikes=synthetic(4))
    api = ComputeApi(
        athena=athena,
        workgroup="wg",
        database="db",
        s3=StubS3({}),
        raw_bucket="raw-bucket",
        clock=lambda: athena.clock,
        sleep=athena.sleep,
        labels=StubLabels([]),
        labels_table="labels",
    )
    query = {"asset_id": DNMP_ID, "units": "all", "align": "event:trial_start"}
    status, body = call(api, RATES, query)
    assert status == 200, body
    assert body["n_trials"] == N_TRIALS
    assert body["defaults"] == {
        "version": "defaults-1",
        "block": "default",
        "source": None,
        "baseline": "pre_window_mean",
        "bin_s": 0.05,
        "pre_s": 1.0,
        "post_s": 2.0,
        "response_window_s": [0.0, 1.0],
    }
    assert [r["unit_index"] for r in body["responsive"]] == [0, 1, 2, 3]


# --- the old keys ---

# The bodies dff01ec returned (captured there; this test passes on dff01ec too).
INTERVALS_BODIES: Final = [
    (
        {"units": "0,1", "align": "intervals:trials", "zscore": "1"},
        {
            "edges": [-1.0, -0.5, 0.0, 0.5, 1.0],
            "units": [
                {
                    "unit_index": 0,
                    "raster": {
                        "trial": [0, 0, 0, 1, 1, 1, 2],
                        "t": [-0.5, 0.0, 0.1, -1.0, -0.9, 0.5, 0.2],
                    },
                    "rate_hz": [1.33333, 0.666667, 2.0, 0.666667],
                    "z": [0.707107, -0.707107, 2.12132, -0.707107],
                },
                {
                    "unit_index": 1,
                    "raster": {"trial": [0, 1, 1], "t": [0.05, -0.95, 0.05]},
                    "rate_hz": [0.666667, 0.0, 1.33333, 0.0],
                    "z": [0.707107, -0.707107, 2.12132, -0.707107],
                },
            ],
            "n_trials": 3,
            "align": "intervals:trials",
            "source": "catalog",
            "query_ms": 0,
            "compute_ms": 0,
        },
    ),
    (
        {"units": "all", "align": "intervals:trials:condition=a", "raster": "0"},
        {
            "edges": [-1.0, -0.5, 0.0, 0.5, 1.0],
            "units": [
                {"unit_index": 0, "rate_hz": [0.0, 1.0, 3.0, 0.0]},
                {"unit_index": 1, "rate_hz": [0.0, 0.0, 1.0, 0.0]},
                {"unit_index": 2, "rate_hz": [0.0, 0.0, 0.0, 0.0]},
            ],
            "n_trials": 2,
            "align": "intervals:trials:condition=a",
            "source": "catalog",
            "query_ms": 0,
            "compute_ms": 0,
        },
    ),
]
# sha256 of the old keys of `event:recognition_onset` at pre 1, post 2, bin 0.5 over
# the 12 synthetic CS62 R2 units, as dff01ec returned them.
EVENT_OLD_SHA: Final = (
    "64f4d5fc92bc6e8450072b04728e384880d1dfd87de06ecc951145b444b3fe81"
)


@pytest.fixture(scope="module")
def small_nwb(tmp_path_factory: pytest.TempPathFactory) -> bytes:
    path = tmp_path_factory.mktemp("small") / "small.nwb"
    write_small(path)
    return path.read_bytes()


@pytest.mark.parametrize(("query", "want"), INTERVALS_BODIES)
def test_an_intervals_body_is_the_one_dff01ec_returned(
    small_nwb: bytes, query: dict[str, str], want: dict[str, Any]
) -> None:
    api, _ = small_api(small_nwb, n_units=3)
    status, body = _get(api, {**SMALL_BASE, **query})
    assert status == 200, body
    assert body == want
    assert not NEW_KEYS & set(body)


def test_an_event_body_keeps_its_old_keys_and_values(nwb_bytes: bytes) -> None:
    api, _ = make_api(nwb_bytes)
    window = {"pre_s": "1", "post_s": "2", "bin_s": "0.5"}
    status, body = rates(api, "event:recognition_onset", **window)
    assert status == 200, body
    assert set(body) == OLD_KEYS | TIMING_KEYS | NEW_KEYS
    old = {k: body[k] for k in sorted(OLD_KEYS)}
    digest = hashlib.sha256(json.dumps(old, sort_keys=True).encode()).hexdigest()
    assert digest == EVENT_OLD_SHA


# --- the bounds at MAX_UNITS ---


def test_responsive_at_max_units_keeps_the_s5_body_bound(nwb_bytes: bytes) -> None:
    api, _ = make_api(nwb_bytes, n_units=compute_api.MAX_UNITS)
    query = {"asset_id": ASSET_ID, "units": "all", "align": "event:scene_cut"}
    event = {**jwt_event(RATES), "queryStringParameters": query}
    api.handle(event, 28_000)  # the warm-up call
    began = time.perf_counter()
    resp = api.handle(event, 28_000)
    wall = time.perf_counter() - began
    assert resp["statusCode"] == 200, resp["body"]
    body = json.loads(resp["body"])
    size = len(resp["body"].encode())
    screen = len(json.dumps(body["responsive"], separators=(",", ":")).encode())
    per_unit = screen / compute_api.MAX_UNITS
    assert len(body["responsive"]) == compute_api.MAX_UNITS
    assert size <= compute_api.MAX_BODY_BYTES
    worst = compute_api.MAX_UNITS * (WORST_BYTES_PER_UNIT + per_unit)
    assert worst <= compute_api.MAX_BODY_BYTES
    assert wall < WARM_BOUND_S
