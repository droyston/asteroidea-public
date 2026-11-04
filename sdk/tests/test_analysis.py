"""Tests for asteroidea.analysis; each oracle is computed independently of the code."""

import ast
import csv
import dataclasses
import json
import math
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, cast

import numpy as np
import pandas as pd
import pytest

from asteroidea import analysis

MODULE = Path(analysis.__file__)
ALLOWED_TOP_LEVEL = {"numpy", "polars", *sys.stdlib_module_names}


def test_imports_only_stdlib_numpy_polars() -> None:
    tree = ast.parse(MODULE.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "relative import in analysis.py"
            found.add((node.module or "").split(".")[0])
    assert found - ALLOWED_TOP_LEVEL == set()
    assert {"numpy", "polars"} <= found


def test_package_init_does_not_import_analysis() -> None:
    init = (MODULE.parent / "__init__.py").read_text(encoding="utf-8")
    assert "analysis" not in init


def test_rates_three_unit_fixture_by_hand() -> None:
    # onsets 10 and 20, window [-1, 1), bins of 0.5 s: edges -1, -0.5, 0, 0.5, 1.
    # n_trials x bin_s = 2 x 0.5 = 1, so rate_hz equals the count.
    spikes = {
        # 9.2 -> t -0.8 bin 0; 10.1, 10.2 -> bin 2; 19.9 -> t -0.1 bin 1; 20.9 -> bin 3
        0: np.array([5.0, 9.2, 10.1, 10.2, 19.9, 20.9, 25.0]),
        1: np.array([1.0, 30.0]),  # no spike in any window
        # 10.7, 20.7, 20.75 -> bin 3
        2: np.array([10.7, 20.7, 20.75]),
    }
    result = analysis.rates(spikes, np.array([10.0, 20.0]), 1.0, 1.0, 0.5)
    assert result.units == (0, 1, 2)
    assert result.n_trials == 2
    np.testing.assert_allclose(result.edges, [-1.0, -0.5, 0.0, 0.5, 1.0])
    expected = np.array([[1, 1, 2, 1], [0, 0, 0, 0], [0, 0, 0, 3]], dtype=float)
    np.testing.assert_allclose(result.rate_hz, expected, rtol=1e-12)


def test_align_relative_times_and_trials() -> None:
    got = analysis.align(
        np.array([9.5, 10.5, 11.0, 19.0]), np.array([10.0, 20.0]), 1.0, 1.0
    )
    np.testing.assert_array_equal(got.trial, [0, 0, 1])
    np.testing.assert_allclose(got.t, [-0.5, 0.5, -1.0])


def test_zscore_by_hand_and_constant_baseline_is_none() -> None:
    edges = np.array([-2.0, -1.0, 0.0, 1.0, 2.0])
    rate = np.array([[1.0, 3.0, 4.0, 2.0], [2.0, 2.0, 5.0, 1.0]])
    got = analysis.zscore(rate, edges)
    # unit 0 baseline [1, 3]: mean 2, sample std sqrt(2)
    assert got[0] is not None
    np.testing.assert_allclose(got[0], (rate[0] - 2.0) / math.sqrt(2.0), rtol=1e-12)
    assert got[1] is None  # baseline [2, 2]: std 0 <= MIN_STD


def test_zscore_std_above_min_std_is_defined() -> None:
    edges = np.array([-2.0, -1.0, 0.0, 1.0])
    tiny = 1e-6  # baseline std ~7e-7, well above MIN_STD
    got = analysis.zscore(np.array([[0.0, tiny, 1.0]]), edges)
    assert got[0] is not None


def test_zscore_std_below_min_std_is_none() -> None:
    edges = np.array([-2.0, -1.0, 0.0, 1.0])
    got = analysis.zscore(np.array([[0.0, 1e-12, 1.0]]), edges)  # std ~7e-13
    assert got[0] is None


def test_frame_onsets_out_of_range_reported_separately() -> None:
    stamps = np.array([0.0, 0.1, 0.2, 0.3, 0.4])
    got = analysis.frame_onsets(np.array([1, 2, 5, 0, 7]), stamps)
    np.testing.assert_allclose(got.seconds, [0.0, 0.1, 0.4])  # frame f -> stamps[f - 1]
    np.testing.assert_array_equal(got.out_of_range, [0, 7])
    zero_based = analysis.frame_onsets(np.array([0, 5]), stamps, base=0)
    np.testing.assert_allclose(zero_based.seconds, [0.0])
    np.testing.assert_array_equal(zero_based.out_of_range, [5])


def _two_tones(rate_hz: int = 1000, seconds: int = 8) -> np.ndarray:
    t = np.arange(rate_hz * seconds) / rate_hz
    return 2.0 * np.sin(2 * np.pi * 10 * t) + 1.0 * np.sin(2 * np.pi * 40 * t)


def test_spectrogram_peaks_at_both_tones() -> None:
    spec = analysis.spectrogram(_two_tones(), 1000.0)
    mean_power = spec.power.mean(axis=1)
    df = spec.freqs[1] - spec.freqs[0]
    split = int(25 / df)
    stop = int(100 / df)
    low = spec.freqs[np.argmax(mean_power[:split])]
    high = spec.freqs[split + np.argmax(mean_power[split:stop])]
    assert abs(low - 10.0) <= df
    assert abs(high - 40.0) <= df
    assert spec.power.shape == (spec.freqs.size, spec.times.size)
    np.testing.assert_allclose(spec.times[:2], [0.5, 1.0])  # 1 s segments, hop 0.5 s
    # Parseval: power integrates to the variance, A^2/2 summed over tones = 2 + 0.5
    np.testing.assert_allclose(spec.power.sum(axis=0) * df, 2.5, rtol=1e-6)


def test_spectrogram_silence_is_floor_not_minus_inf() -> None:
    spec = analysis.spectrogram(np.zeros(4000), 1000.0)
    assert np.isfinite(spec.power_db).all()
    np.testing.assert_allclose(spec.power_db, 10 * math.log10(analysis.POWER_FLOOR))


def test_spectrogram_too_short_raises() -> None:
    with pytest.raises(ValueError, match="at least one segment"):
        analysis.spectrogram(np.zeros(10), 1000.0)


def test_log_bands_geometric() -> None:
    bands = analysis.log_bands(1.0, 1000.0, 3)
    np.testing.assert_allclose(bands.edges, [1.0, 10.0, 100.0, 1000.0])
    assert bands.centres.size == 3
    assert analysis.log_bands(1.0, 500.0).centres.size == 64


def test_band_power_tone_lands_in_its_band() -> None:
    spec = analysis.spectrogram(_two_tones(), 1000.0)
    bands = analysis.log_bands(1.0, 200.0, 64)
    out = analysis.band_power(spec.freqs, spec.power, bands)
    assert out.shape == (64, spec.times.size)
    assert np.isfinite(out).all()
    mean_db = out.mean(axis=1)
    band_10 = int(np.searchsorted(bands.edges, 10.0, side="right")) - 1
    band_40 = int(np.searchsorted(bands.edges, 40.0, side="right")) - 1
    assert mean_db.argmax() == band_10  # the stronger tone (amplitude 2)
    assert mean_db[band_40] > mean_db[band_40 - 3] + 20
    assert mean_db[band_40] > mean_db[band_40 + 3] + 20
    assert out.astype(np.float16).dtype == np.float16


def test_band_power_averages_linear_power_before_db() -> None:
    bands = analysis.Bands(edges=np.array([0.0, 5.0]), centres=np.array([2.5]))
    freqs = np.array([1.0, 2.0, 7.0])
    power = np.array([[1.0], [3.0], [100.0]])  # 1 and 2 Hz in the band, 7 Hz outside
    got = analysis.band_power(freqs, power, bands)
    np.testing.assert_allclose(got, [[10 * math.log10(2.0)]])  # not log of sqrt(3)


def test_band_power_band_without_bin_uses_nearest_bin() -> None:
    bands = analysis.Bands(edges=np.array([1.1, 1.4]), centres=np.array([1.25]))
    got = analysis.band_power(np.array([1.0, 2.0]), np.array([[10.0], [1000.0]]), bands)
    np.testing.assert_allclose(got, [[10.0 * math.log10(10.0)]])  # nearest is 1.0 Hz


def test_decimate_minmax_keeps_extremes_in_time_order() -> None:
    x = np.zeros(10_000)
    x[1234] = 7.0
    x[8765] = -5.0
    got = analysis.decimate_minmax(x, 1000.0, 100)
    assert got.samples.size <= 100
    assert got.samples.max() == 7.0
    assert got.samples.min() == -5.0
    assert (np.diff(got.times) > 0).all()
    np.testing.assert_allclose(got.times[got.samples == 7.0], [1.234])


def test_decimate_minmax_short_input_unchanged() -> None:
    x = np.array([1.0, 2.0, 3.0])
    got = analysis.decimate_minmax(x, 2.0, 10)
    np.testing.assert_array_equal(got.samples, x)
    np.testing.assert_allclose(got.times, [0.0, 0.5, 1.0])


# --- cross-check against the exemplar notebook's pandas PSTH (psth_pandas cell) ---
WIN = 2.0
BIN = 0.05
NBINS = int(2 * WIN / BIN)


def _notebook_long_table(
    spikes_by_unit: dict[int, np.ndarray], cut_times: np.ndarray
) -> pd.DataFrame:
    unit_parts, cut_parts, t_parts = [], [], []
    for unit, times in spikes_by_unit.items():
        lo = np.searchsorted(times, cut_times - WIN, side="left")
        hi = np.searchsorted(times, cut_times + WIN, side="left")
        for cut, (start, stop) in enumerate(zip(lo, hi, strict=True)):
            unit_parts.append(np.full(stop - start, unit))
            cut_parts.append(np.full(stop - start, cut))
            t_parts.append(times[start:stop] - cut_times[cut])
    return pd.DataFrame(
        {
            "unit_index": np.concatenate(unit_parts),
            "cut": np.concatenate(cut_parts),
            "t": np.concatenate(t_parts),
        }
    )


def _notebook_psth_pandas(
    long: pd.DataFrame, unit_list: list[int], n_cuts: int
) -> pd.DataFrame:
    grid = pd.DataFrame(
        {
            "unit_index": np.repeat(unit_list, NBINS),
            "bin": np.tile(range(NBINS), len(unit_list)),
        }
    )
    binned = long.assign(bin=np.floor((long["t"] + WIN) / BIN).astype(int))
    counts = binned.groupby(["unit_index", "bin"], as_index=False).agg(n=("t", "size"))
    out = grid.merge(counts, on=["unit_index", "bin"], how="left")
    out["n"] = out["n"].fillna(0).astype(int)
    out["rate_hz"] = out["n"] / (n_cuts * BIN)
    return out.sort_values(["unit_index", "bin"]).reset_index(drop=True)


def test_rates_match_exemplar_pandas_psth() -> None:
    rng = np.random.default_rng(20261025)
    spikes = {u: np.sort(rng.uniform(0.0, 600.0, 3000)) for u in (3, 7, 11)}
    spikes[12] = np.array([1.0])  # a unit with no spike in any window
    cuts = np.sort(rng.uniform(5.0, 595.0, 40))
    oracle = _notebook_psth_pandas(
        _notebook_long_table(spikes, cuts), sorted(spikes), cuts.size
    )
    got = analysis.rates(spikes, cuts, WIN, WIN, BIN)
    assert got.rate_hz.shape == (4, NBINS)
    np.testing.assert_allclose(
        got.rate_hz.reshape(-1), oracle["rate_hz"].to_numpy(), rtol=1e-12
    )


# --- alignment events and analysis defaults ---
FIXTURES = Path(__file__).parent / "fixtures"
CSV_PATH = "stimuli/scenecut_info.csv"
MOVIE = "stimulus/presentation/movieframe_time"


def _rows(dataset: str) -> list[dict[str, object]]:
    text = (FIXTURES / dataset / "intervals.json").read_text("utf-8")
    return cast("list[dict[str, object]]", json.loads(text))


def _sources_623() -> analysis.EventSources:
    rows = _rows("dandi-000623")
    with (FIXTURES / "dandi-000623" / "scenecut_info.csv").open(encoding="utf-8") as f:
        cuts = list(csv.DictReader(f))
    frames = json.loads(
        (FIXTURES / "dandi-000623" / "movieframe_timestamps.json").read_text("utf-8")
    )
    assert frames["series"] == MOVIE
    return analysis.EventSources(
        asset_id=str(rows[0]["asset_id"]),
        intervals=rows,
        csv={CSV_PATH: cuts},
        frames={MOVIE: frames["timestamps"]},
    )


def _by_type(
    events: list[analysis.AlignmentEvent],
) -> dict[str, list[analysis.AlignmentEvent]]:
    out: dict[str, list[analysis.AlignmentEvent]] = {}
    for event in events:
        out.setdefault(event.event_type, []).append(event)
    return out


def _levels(events: list[analysis.AlignmentEvent], name: str) -> dict[object, int]:
    return dict(Counter(e.attributes.get(name) for e in events))


def test_000623_scene_cuts_from_csv_and_string_frame_series() -> None:
    recipe = analysis.load_recipe("dandi-000623")
    sources = _sources_623()
    got = _by_type(analysis.resolve_events(recipe, sources))["scene_cut"]
    assert len(got) == 93
    assert _levels(got, "cut_kind") == {"change": 13, "continuity": 80}
    assert got[0].attributes["cut_kind"] == "change"  # row 0 forced (addendum)
    assert {e.averaging for e in got} == {"average"}
    assert {e.source.kind for e in got} == {"stimulus_file"}
    # onset = timestamps[shot_start_fr - 1] (the string series' data is 0-based)
    stamps = sources.frames[MOVIE]
    second = sources.csv[CSV_PATH][1]
    assert got[1].onset_s == stamps[int(float(second["shot_start_fr"])) - 1]
    assert got[0].onset_s == 0.0
    assert max(e.onset_s for e in got) == pytest.approx(475.4468, abs=1e-3)


def test_000623_recognition_onsets_and_familiarity() -> None:
    recipe = analysis.load_recipe("dandi-000623")
    rec = _by_type(analysis.resolve_events(recipe, _sources_623()))["recognition_onset"]
    assert len(rec) == 40
    assert _levels(rec, "familiarity") == {"novel": 16, "familiar": 24}
    assert _levels(rec, "actual_response") == {1: 8, 2: 4, 3: 4, 4: 5, 5: 6, 6: 13}
    assert rec[0].onset_s == pytest.approx(490.456834)
    assert {e.source.kind for e in rec} == {"interval"}
    assert {e.clock_asset_id for e in rec} == {"7887e844-7100-4384-975e-5ee38dbf153d"}


def test_000623_movie_onset_is_single() -> None:
    recipe = analysis.load_recipe("dandi-000623")
    movie = _by_type(analysis.resolve_events(recipe, _sources_623()))["movie_onset"]
    assert len(movie) == 1
    assert movie[0].averaging == "single"
    assert movie[0].onset_s == 0.0
    assert movie[0].stop_s == pytest.approx(478.83512)


def test_000623_recipe_carries_addendum_corrections() -> None:
    recipe = analysis.load_recipe("dandi-000623")
    assert recipe.lfp_clock_offset_s == 10
    specs = {t.name: t for t in recipe.event_types}
    assert specs["scene_cut"].source["series"] == MOVIE
    assert specs["scene_cut"].source["frame_base"] == 1
    assert specs["recognition_onset"].window_s == {
        "pre_s": 1.0,
        "post_s": 2.0,
        "response_window_s": [0.2, 1.7],
    }
    events = analysis.resolve_events(recipe, _sources_623())[:2]
    np.testing.assert_allclose(
        analysis.lfp_onsets(recipe, events), [e.onset_s + 10 for e in events]
    )


def test_001775_dnmp_events_measured_from_fixture() -> None:
    # The counts were measured from the fixture.
    rows = _rows("dandi-001775")
    recipe = analysis.load_recipe("dandi-001775")
    sources = analysis.EventSources(asset_id=str(rows[0]["asset_id"]), intervals=rows)
    got = _by_type(analysis.resolve_events(recipe, sources))
    assert set(got) == {"trial_start", "reward_grasp"}
    for name in ("trial_start", "reward_grasp"):
        assert len(got[name]) == 20
        assert _levels(got[name], "trial_phase") == {"sample": 10, "test": 10}
        assert _levels(got[name], "choice") == {"L": 10, "R": 10}
        assert {e.averaging for e in got[name]} == {"average"}
    grasps = sorted(
        json.loads(str(r["columns_json"]))["grasp_time"]
        for r in rows
        if r["table_name"] == "trials" and "grasp_time" in str(r["columns_json"])
    )
    assert [e.onset_s for e in got["reward_grasp"]] == grasps


def test_default_recipe_when_no_file_and_two_to_twelve_levels() -> None:
    recipe = analysis.load_recipe("dandi-999999")
    assert recipe.default
    rows = _rows("dandi-000623")
    events = analysis.resolve_events(
        recipe, analysis.EventSources(asset_id="a", intervals=rows)
    )
    assert {e.event_type for e in events} == {"trials"}
    assert len(events) == 41
    names: set[str] = set()
    for event in events:
        names.update(event.attributes)
    # stim_phase 2 levels, actual_response 6, response_correct 2 qualify;
    # stimulus_file (41 values) and response_time (41) do not.
    assert {"stim_phase", "actual_response", "response_correct"} <= names
    assert not names & {"stimulus_file", "response_time"}


def test_bids_event_source_and_default_events_type() -> None:
    rows = [
        {
            "asset_id": "b",
            "dataset": "openneuro-ds1",
            "subject": "sub-1",
            "bids_session": None,
            "events_for": "sub-1_task-x_bold.nii.gz",
            "row_index": i,
            "onset_s": float(10 * i),
            "duration_s": 2.0,
            "trial_type": kind,
            "columns_json": json.dumps({"stim": f"s{i}"}),
        }
        for i, kind in enumerate(["face", "house", "face"])
    ]
    recipe = analysis.parse_recipe(
        {
            "schema": "asteroidea.event-recipe/1",
            "version": "v",
            "dataset": "openneuro-ds1",
            "event_types": [
                {
                    "name": "face",
                    "source": {"kind": "bids_event", "trial_type": "face"},
                    "attributes": [{"name": "stim", "column": "stim"}],
                }
            ],
        }
    )
    got = analysis.resolve_events(recipe, analysis.EventSources("b", events=rows))
    assert [(e.onset_s, e.stop_s) for e in got] == [(0.0, 2.0), (20.0, 22.0)]
    assert [dict(e.attributes) for e in got] == [
        {"trial_type": "face", "stim": "s0"},
        {"trial_type": "face", "stim": "s2"},
    ]
    assert {e.source.kind for e in got} == {"bids_event"}
    default = analysis.resolve_events(
        analysis.default_recipe("openneuro-ds1"),
        analysis.EventSources("b", events=rows),
    )
    assert [e.event_type for e in default] == ["events"] * 3


def test_label_source_by_name_and_single() -> None:
    labels = [
        {
            "label_id": "L1",
            "name": "baseline",
            "note": None,
            "start_s": 5.0,
            "stop_s": 6.0,
            "series": None,
            "mine": True,
        },
        {
            "label_id": "L2",
            "name": "other",
            "note": None,
            "start_s": 1.0,
            "stop_s": None,
            "series": None,
            "mine": False,
        },
    ]
    recipe = analysis.parse_recipe(
        {
            "schema": "asteroidea.event-recipe/1",
            "version": "v",
            "dataset": "d",
            "event_types": [
                {"name": "mark", "source": {"kind": "label", "name": "baseline"}}
            ],
        }
    )
    got = analysis.resolve_events(recipe, analysis.EventSources("c", labels=labels))
    assert len(got) == 1
    assert got[0].averaging == "single"  # one event, whatever the recipe says
    assert (got[0].onset_s, got[0].stop_s) == (5.0, 6.0)
    assert dict(got[0].attributes) == {"name": "baseline", "mine": True}
    assert got[0].source == analysis.EventSource("label", "L1", 0)


def test_recipe_refuses_unknown_kind_and_derive() -> None:
    base = {"schema": "asteroidea.event-recipe/1", "dataset": "d", "version": "v"}
    with pytest.raises(ValueError, match=r"source\.kind"):
        analysis.parse_recipe(
            {**base, "event_types": [{"name": "x", "source": {"kind": "video"}}]}
        )
    bad_derive = {
        "name": "x",
        "source": {"kind": "interval", "table": "t"},
        "attributes": [{"name": "a", "column": "c", "derive": "zscore"}],
    }
    with pytest.raises(ValueError, match="derive"):
        analysis.parse_recipe({**base, "event_types": [bad_derive]})


def test_every_recipe_file_parses_and_names_its_dataset() -> None:
    files = sorted((MODULE.parent / "recipes").glob("*.json"))
    assert [f.stem for f in files] == ["dandi-000623", "dandi-001775"]
    for f in files:
        recipe = analysis.load_recipe(f.stem)
        assert not recipe.default
        assert recipe.description


def test_defaults_convention_and_000623_paper_block() -> None:
    conv = analysis.load_defaults()
    paper = analysis.load_defaults("dandi-000623")
    assert conv.version == paper.version == "defaults-1"
    assert conv.label == "exploratory screen"
    assert conv.rates["baseline"] == "pre_window_mean"
    assert paper.rates["baseline"] == "none"  # raw rates for 000623
    assert paper.rates["bin_s"] == 0.25
    assert conv.tests["min_events_per_level"] == 5
    assert paper.tests["min_events_per_level"] == 10
    assert paper.tests["count_events_where"] == {"response_correct": 1}
    assert paper.tests["n_permutations"] == 2000  # kept from the convention
    assert paper.bands["hfb"] == {"hz": [70, 170], "cycles": 5, "freq_step_hz": 10}
    assert "delta" in paper.bands
    assert paper.bandpower["baseline"] == "zscore_over_time"
    assert conv.not_reproduced == ()
    assert "60 Hz notch" in paper.not_reproduced
    assert any("AUC" in x for x in paper.not_reproduced)
    assert analysis.load_defaults("dandi-001775").rates == conv.rates


def test_defaults_morlet_cycles_at_least_three() -> None:
    for dataset in (None, "dandi-000623"):
        bands = analysis.load_defaults(dataset).bands
        assert min(float(str(b["cycles"])) for b in bands.values()) >= 3


def test_defaults_refuse_cycles_below_three(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    body = json.loads((MODULE.parent / "analysis_defaults.json").read_text("utf-8"))
    body["default"]["bands"]["delta"]["cycles"] = 2
    path = tmp_path / "analysis_defaults.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    monkeypatch.setattr(analysis, "_DEFAULTS", path)
    with pytest.raises(ValueError, match="below 3"):
        analysis.load_defaults()


def test_the_word_priors_appears_nowhere() -> None:
    paths = (
        MODULE,
        MODULE.parent / "analysis_defaults.json",
        *(MODULE.parent / "recipes").glob("*.json"),
    )
    for path in paths:
        assert "prior" not in path.read_text("utf-8").lower(), path


# --- session statistics ---


def _event(
    onset: float, kind: str = "cue", **attributes: str | float
) -> analysis.AlignmentEvent:
    return analysis.AlignmentEvent(
        event_type=kind,
        onset_s=onset,
        stop_s=None,
        clock_asset_id="a",
        source=analysis.EventSource(kind="interval", ref="trials", row=0),
        attributes=attributes,
        averaging="average",
    )


def _defaults(**tests: object) -> analysis.Defaults:
    base = analysis.load_defaults()
    return dataclasses.replace(base, tests={**base.tests, **tests})


RECIPE_D = analysis.default_recipe("d")


def test_responsive_one_unit_three_events_by_hand() -> None:
    # window [0, 1) s, reference [-1, 0) s; d = response - reference per event
    spikes = np.array(
        [9.5, 10.1, 10.2, 10.3, 11.0, 12.5, 20.5, 29.5, 30.1, 30.2, 30.3, 30.4, 30.5]
    )
    events = [_event(10.0), _event(20.0), _event(30.0)]
    (got,) = analysis.responsive({7: spikes}, events, RECIPE_D, _defaults())
    # d = [3 - 1, 1 - 0, 5 - 1] = [2, 1, 4]; |sum| over the 8 sign patterns is
    # 7 1 5 3 3 5 1 7, so 2 of 8 reach the observed 7: p = 0.25 (exact).
    assert got.exact
    assert got.n_permutations == 8
    assert got.response_window_s == (0.0, 1.0)
    assert got.reference_window_s == (-1.0, 0.0)
    (row,) = got.rows
    assert row.response_hz == pytest.approx(3.0)
    assert row.reference_hz == pytest.approx(2 / 3)
    assert row.statistic == pytest.approx(7 / 3)
    assert row.p == pytest.approx(0.25)
    assert row.q == pytest.approx(0.25)
    assert not row.screened
    assert got.label == "exploratory screen"
    assert got.defaults_version == "defaults-1"
    assert got.recipe_version == "default"


def test_tuning_two_levels_selectivity_index_by_hand() -> None:
    # unit 1: level a rates 2 and 4 (mean 3), level b 1 and 1 (mean 1): SI 0.5;
    # between-level variance (2 * 1**2 + 2 * 1**2) / 4 = 1. Unit 2 is silent: SI 0.
    onsets = [10.0, 20.0, 30.0, 40.0]
    per_event = [2, 4, 1, 1]
    spikes = np.concatenate(
        [t + 0.1 + 0.1 * np.arange(n) for t, n in zip(onsets, per_event, strict=True)]
    )
    events = [_event(t, side=s) for t, s in zip(onsets, "aabb", strict=True)]
    silent = np.array([100.0])
    (got,) = analysis.tuning(
        {1: spikes, 2: silent}, events, RECIPE_D, _defaults(min_events_per_level=2)
    )
    assert got.attribute == "side"
    assert got.levels == ("a", "b")
    assert got.level_counts == {"a": 2, "b": 2}
    assert got.skipped is None
    one, two = got.rows
    assert one.level_means == {"a": pytest.approx(3.0), "b": pytest.approx(1.0)}
    assert one.selectivity_index == pytest.approx(0.5)
    assert one.statistic == pytest.approx(1.0)
    assert two.level_means == {"a": 0.0, "b": 0.0}
    assert two.selectivity_index == 0.0
    assert 0.0 < one.p <= 1.0
    assert got.label == "exploratory screen"


def test_selectivity_index_zero_when_both_zero() -> None:
    got = analysis.selectivity_index(np.array([[3.0, 0.0, 2.0], [1.0, 0.0, 2.0]]))
    np.testing.assert_allclose(got, [0.5, 0.0, 0.0])


def test_benjamini_hochberg_against_textbook_by_hand() -> None:
    p = [0.04, 0.011, 0.03, 0.2]
    # textbook step-up, by hand: sorted 0.011 0.03 0.04 0.2, m = 4;
    # p * m / rank = 0.044 0.06 0.05333 0.2; running minimum from the right
    # gives q = 0.044 0.05333 0.05333 0.2 in sorted order.
    expected = [0.04 * 4 / 3, 0.011 * 4, 0.04 * 4 / 3, 0.2]
    m = len(p)
    ranked = sorted(range(m), key=lambda i: p[i])
    hand = [0.0] * m
    for pos, i in enumerate(ranked):
        hand[i] = min(p[ranked[j]] * m / (j + 1) for j in range(pos, m))
    np.testing.assert_allclose(hand, expected)
    got = analysis.benjamini_hochberg(np.array(p))
    np.testing.assert_allclose(got, expected)
    # the rejection rule: the largest k with p_(k) <= k / m * alpha; here k = 1
    alpha = 0.05
    k = max((j + 1 for j in range(m) if p[ranked[j]] <= (j + 1) / m * alpha), default=0)
    assert k == int(np.sum(got <= alpha)) == 1


def test_area_proportion_by_hand() -> None:
    # unit 1's shuffled statistics 4 3 2 1 give shuffle p 1/4 2/4 3/4 1, so only
    # shuffle 0 is at most 0.25; unit 2's are all 1. Observed fraction 1/2 (unit 1
    # p 0.01); null fractions 1/2 0 0 0: one reaches it, p = (1 + 1) / (4 + 1).
    null = np.array([[4.0, 1.0], [3.0, 1.0], [2.0, 1.0], [1.0, 1.0]])
    (row,) = analysis.area_proportion(np.array([0.01, 0.5]), null, ["X", "X"], 0.25)
    assert row == analysis.AreaRow(
        location="X", n_units=2, n_below_alpha=1, fraction=0.5, p=0.4
    )
    assert analysis.area_proportion(np.array([0.01]), null[:, :1], [None], 0.25) == ()


def _poisson(
    rng: np.random.Generator, rate_hz: float, t0: float, t1: float
) -> np.ndarray:
    n = rng.poisson(rate_hz * (t1 - t0))
    return np.sort(rng.uniform(t0, t1, n))


def test_cs62_r2_screen_shape_and_counted_levels() -> None:
    recipe = analysis.load_recipe("dandi-000623")
    defaults = analysis.load_defaults("dandi-000623")
    events = analysis.resolve_events(recipe, _sources_623())
    rng = np.random.default_rng(5)
    spikes = {u: _poisson(rng, 4.0, 0.0, 3000.0) for u in range(5)}
    spikes[99] = _poisson(rng, 4.0, 600.0, 3000.0)  # silent during movie_onset
    locations = {0: "AMY", 1: "AMY", 2: "AMY", 3: "HIP", 4: "HIP", 99: "HIP"}
    resp = analysis.responsive(spikes, events, recipe, defaults, locations)
    assert [(r.event_type, r.n_events) for r in resp] == [
        ("scene_cut", 93),
        ("recognition_onset", 34),  # correct trials only (count_events_where)
    ]
    rec = resp[1]
    assert rec.response_window_s == (0.2, 1.7)
    assert rec.reference_window_s == pytest.approx((-1.5, 0.0))
    for result in resp:
        assert result.dropped_units == (99,)
        assert [r.unit for r in result.rows] == [0, 1, 2, 3, 4]
        assert [a.location for a in result.areas] == ["AMY", "HIP"]
        assert result.n_permutations == 2000
        assert result.recipe_version == "recipe-1"
    tuned = analysis.tuning(spikes, events, recipe, defaults, locations)
    tun = {(t.event_type, t.attribute): t for t in tuned}
    assert sorted(tun) == [
        ("recognition_onset", "actual_response"),
        ("recognition_onset", "familiarity"),
        ("recognition_onset", "response_correct"),
        ("scene_cut", "cut_kind"),
        ("scene_cut", "scene_id"),
    ]
    fam = tun["recognition_onset", "familiarity"]
    assert fam.level_counts == {"familiar": 19, "novel": 15}
    assert fam.levels == ("familiar", "novel")
    assert fam.n_events == 34
    assert len(fam.rows) == 5
    assert set(fam.rows[0].level_means) == {"familiar", "novel"}
    actual = tun["recognition_onset", "actual_response"]
    assert actual.level_counts == {"1": 8, "2": 4, "3": 3, "4": 2, "5": 4, "6": 13}
    assert actual.skipped is not None
    assert actual.rows == ()
    assert tun["recognition_onset", "response_correct"].skipped is not None
    cut = tun["scene_cut", "cut_kind"]
    assert cut.level_counts == {"change": 13, "continuity": 80}
    assert cut.skipped is None
    scene = tun["scene_cut", "scene_id"]
    counts = sorted(scene.level_counts.values())
    assert counts == [1, 1, 1, 1, 1, 1, 2, 3, 7, 12, 13, 19, 31]
    assert len(scene.levels) == 4
    assert scene.n_events == 31 + 19 + 13 + 12
    for result in (fam, cut, scene):
        assert result.label == "exploratory screen"
        assert result.defaults_version == "defaults-1"
        assert all(0.0 < r.p <= 1.0 and r.q >= r.p for r in result.rows)
        assert all(r.screened == (r.q <= 0.05) for r in result.rows)


def test_drop_units_needs_the_named_phase() -> None:
    defaults = _defaults(drop_units_silent_during="movie_onset")
    with pytest.raises(ValueError, match="movie_onset"):
        analysis.responsive({1: np.array([1.0])}, [_event(5.0)], RECIPE_D, defaults)


def _perf_case(
    n_units: int = 44, n_events: int = 93, n_levels: int = 6
) -> tuple[dict[int, np.ndarray], list[analysis.AlignmentEvent]]:
    rng = np.random.default_rng(11)
    spikes = {u: _poisson(rng, 5.0, 0.0, 1000.0) for u in range(n_units)}
    events = [_event(5.0 + 10.0 * i, level=f"L{i % n_levels}") for i in range(n_events)]
    return spikes, events


def test_statistics_deterministic_under_the_seed() -> None:
    spikes, events = _perf_case(n_units=6, n_events=40, n_levels=2)
    defaults = _defaults(n_permutations=200)
    reseeded = _defaults(n_permutations=200, seed=14)
    first = analysis.tuning(spikes, events, RECIPE_D, defaults)
    again = analysis.tuning(spikes, events, RECIPE_D, defaults)
    other = analysis.tuning(spikes, events, RECIPE_D, reseeded)
    p = [r.p for r in first[0].rows]
    assert p == [r.p for r in again[0].rows]
    assert p != [r.p for r in other[0].rows]
    r1 = analysis.responsive(spikes, events, RECIPE_D, defaults)
    r2 = analysis.responsive(spikes, events, RECIPE_D, defaults)
    r3 = analysis.responsive(spikes, events, RECIPE_D, reseeded)
    assert not r1[0].exact
    assert [r.p for r in r1[0].rows] == [r.p for r in r2[0].rows]
    assert [r.p for r in r1[0].rows] != [r.p for r in r3[0].rows]


def test_tuning_44_units_93_events_6_levels_2000_shuffles_under_1_s() -> None:
    spikes, events = _perf_case()
    defaults = _defaults()
    start = time.perf_counter()
    (got,) = analysis.tuning(spikes, events, RECIPE_D, defaults)
    wall = time.perf_counter() - start
    assert len(got.levels) == 6
    assert got.n_events == 93
    assert len(got.rows) == 44
    assert got.n_permutations == 2000
    assert wall < 1.0, f"tuning took {wall:.3f} s"


# --- event_band_power ---

RATE = 1000.0
SQRT_PI = math.sqrt(math.pi)


def _bp_settings(**changes: Any) -> analysis.BandPowerSettings:
    base = analysis.band_power_settings(
        analysis.load_defaults(), analysis.default_recipe("x"), "x", averaging="average"
    )
    return dataclasses.replace(base, **changes)


def _morlet_sine_power(amplitude: float, f0: float, f: float, cycles: float) -> float:
    # A Morlet at f with c cycles is a Gaussian of sigma f / c in frequency, gain 1 on
    # the analytic signal, so a sine of amplitude A at f0 has power
    # A**2 exp(-(f - f0)**2 / sigma**2).
    sigma = f / cycles
    return amplitude**2 * math.exp(-((f - f0) ** 2) / sigma**2)


def _morlet_noise_power(noise_sd: float, f: float, cycles: float) -> float:
    # White noise of variance s**2 has two-sided density s**2 / fs; through a gain of
    # 2 exp(-(v - f)**2 / (2 sigma**2)) the power is s**2 / fs * 4 sigma sqrt(pi).
    return noise_sd**2 / RATE * 4.0 * (f / cycles) * SQRT_PI


def _burst_recording(
    amplitude: float, n_events: int, rng: np.random.Generator
) -> tuple[np.ndarray, np.ndarray]:
    # one channel of unit white noise, a 10 Hz sine for 1 s from each onset
    spacing = 10.0
    onsets = 5.0004 + spacing * np.arange(n_events)
    t = np.arange(0.0, onsets[-1] + 8.0, 1.0 / RATE)
    x = rng.standard_normal(t.size)
    for onset in onsets:
        inside = (t >= onset) & (t < onset + 1.0)
        x[inside] += amplitude * np.sin(2 * np.pi * 10.0 * (t[inside] - onset))
    return x, onsets


def _expected_db(band: analysis.BandSpec, amplitude: float) -> float:
    freqs = band.freqs
    noise = np.array([_morlet_noise_power(1.0, f, band.cycles) for f in freqs])
    sine = np.array(
        [_morlet_sine_power(amplitude, 10.0, f, band.cycles) for f in freqs]
    )
    return 10.0 * math.log10((sine + noise).mean() / noise.mean())


def test_event_band_power_alpha_burst_rises_and_beta_stays() -> None:
    amplitude = 0.3
    x, onsets = _burst_recording(amplitude, 100, np.random.default_rng(3))
    settings = _bp_settings(pre_s=2.0)
    by_name = {b.name: b for b in settings.bands}
    result = analysis.event_band_power(
        x, analysis.SampleClock(rate_hz=RATE), onsets, settings
    )
    alpha = result.mean[0, result.bands.index("alpha")]
    beta = result.mean[0, result.bands.index("beta")]
    inner = (result.times_s > 0.25) & (result.times_s < 0.75)
    pre = result.times_s < 0
    want_alpha = _expected_db(by_name["alpha"], amplitude)
    want_beta = _expected_db(by_name["beta"], amplitude)
    assert want_alpha > 5.0
    assert abs(want_beta) < 0.5
    assert alpha[inner].mean() == pytest.approx(want_alpha, abs=1.0)
    assert abs(alpha[pre].mean()) < 0.5
    assert beta[inner].mean() == pytest.approx(want_beta, abs=0.5)
    assert np.all(np.abs(beta) < 1.0)
    assert result.label == "exploratory screen"
    assert result.defaults_version == "defaults-1"
    assert result.recipe_version == "default"
    assert result.n.tolist() == [[100] * len(result.bands)]
    assert result.n_dropped == 0


def test_event_band_power_pre_window_mean_is_zero_db_on_average() -> None:
    x, onsets = _burst_recording(0.3, 30, np.random.default_rng(4))
    result = analysis.event_band_power(
        x, analysis.SampleClock(rate_hz=RATE), onsets, _bp_settings()
    )
    pre = result.times_s < 0
    # dB re the pre-window mean: the linear ratio averages to 1 over the pre steps
    ratio = 10.0 ** (result.mean[:, :, pre] / 10.0)
    np.testing.assert_allclose(ratio.mean(axis=2), 1.0, rtol=1e-9)
    assert np.all(np.isfinite(result.sem))


def test_event_band_power_morlet_scale_of_a_pure_sine() -> None:
    t = np.arange(0.0, 60.0, 1.0 / RATE)
    x = 2.0 * np.sin(2 * np.pi * 10.0 * t + 0.3) + 0.5 * np.sin(2 * np.pi * 77.0 * t)
    bands = (
        analysis.BandSpec("ten", 10.0, 11.0, 4.0, 1.0),
        analysis.BandSpec("seventy_seven", 77.0, 78.0, 7.0, 1.0),
    )
    settings = _bp_settings(baseline="none", bands=bands)
    result = analysis.event_band_power(
        x, analysis.SampleClock(rate_hz=RATE), np.array([10.0003, 31.7, 50.2]), settings
    )
    # power of a sine of amplitude A is A**2: 4 at 10 Hz, 0.25 at 77 Hz
    np.testing.assert_allclose(result.mean[0, 0], 4.0, rtol=0.05)
    np.testing.assert_allclose(result.mean[0, 1], 0.25, rtol=0.05)


def test_event_band_power_timestamps_equal_uniform() -> None:
    x, onsets = _burst_recording(0.3, 12, np.random.default_rng(5))
    data = np.stack([x, 0.5 * x[::-1]], axis=1)
    start = 3349699.35
    uniform = analysis.event_band_power(
        data,
        analysis.SampleClock(rate_hz=RATE, start_s=start),
        onsets + start,
        _bp_settings(),
    )
    stamps = start + np.arange(data.shape[0]) / RATE
    stamped = analysis.event_band_power(
        data, analysis.SampleClock(timestamps=stamps), onsets + start, _bp_settings()
    )
    assert stamped.bands == uniform.bands
    np.testing.assert_allclose(stamped.times_s, uniform.times_s, rtol=1e-6)
    np.testing.assert_allclose(stamped.mean, uniform.mean, rtol=1e-6, atol=1e-6)
    assert stamped.n.tolist() == uniform.n.tolist()


def test_event_band_power_zscore_over_time() -> None:
    x, onsets = _burst_recording(0.3, 40, np.random.default_rng(6))
    settings = _bp_settings(baseline="zscore_over_time", units="z")
    result = analysis.event_band_power(
        x, analysis.SampleClock(rate_hz=RATE), onsets, settings
    )
    # each frequency is z-scored over every step of every event, so the event mean
    # averages to 0 over time; the burst lifts alpha above it after onset
    np.testing.assert_allclose(result.mean.mean(axis=2), 0.0, atol=1e-9)
    alpha = result.mean[0, result.bands.index("alpha")]
    inner = (result.times_s > 0.25) & (result.times_s < 0.75)
    assert alpha[inner].min() > 0.5
    assert alpha[result.times_s < 0].max() < 0.0


def test_event_band_power_drops_and_counts_edge_events() -> None:
    x, onsets = _burst_recording(0.3, 5, np.random.default_rng(7))
    end = x.size / RATE
    marks = np.concatenate([[0.5], onsets, [end - 1.0]])
    result = analysis.event_band_power(
        x, analysis.SampleClock(rate_hz=RATE), marks, _bp_settings()
    )
    assert result.n_dropped == 2
    assert result.dropped_onsets_s == (0.5, end - 1.0)
    assert set(result.n.reshape(-1).tolist()) == {5}


def test_event_band_power_window_rule_and_single() -> None:
    x, onsets = _burst_recording(0.3, 3, np.random.default_rng(8))
    clock = analysis.SampleClock(rate_hz=RATE)
    narrow = analysis.event_band_power(x, clock, onsets, _bp_settings(pad_s=1.0))
    assert "delta" not in narrow.bands
    assert "not computable at this window" in narrow.not_computable["delta"]
    single = analysis.event_band_power(
        x, clock, onsets[:1], _bp_settings(averaging="single")
    )
    assert single.n.reshape(-1).tolist() == [1] * len(single.bands)
    assert np.all(np.isnan(single.sem))
    with pytest.raises(ValueError, match="single event type takes one onset"):
        analysis.event_band_power(x, clock, onsets, _bp_settings(averaging="single"))


def test_band_power_settings_000623_hfb_and_zscore() -> None:
    defaults = analysis.load_defaults("dandi-000623")
    settings = analysis.band_power_settings(
        defaults,
        analysis.default_recipe("dandi-000623"),
        "x",
        averaging="average",
        bands=["hfb"],
    )
    (hfb,) = settings.bands
    assert (hfb.lo_hz, hfb.hi_hz, hfb.cycles, hfb.freq_step_hz) == (70, 170, 5, 10)
    assert hfb.freqs.tolist() == [70, 80, 90, 100, 110, 120, 130, 140, 150, 160]
    assert settings.baseline == "zscore_over_time"
    with pytest.raises(ValueError, match="unknown bands"):
        analysis.band_power_settings(
            defaults,
            analysis.default_recipe("x"),
            "x",
            averaging="average",
            bands=["x"],
        )


def test_event_band_power_8_channels_93_events_under_2_5_s() -> None:
    """8 channels x 93 events x one 4 s read at 1 kHz, all six default bands.

    The full size, 96 channels, measured 9.96 s on a laptop.
    Cost is linear in channels: 9.96 s x 8 / 96 = 0.83 s, and 3x margin gives the
    2.5 s bound. The full-size test stays out of the suite (a warm make check
    stays at most 60 s).
    """
    settings = _bp_settings(pre_s=0.5, post_s=0.5, pad_s=1.5)
    onsets = 3.0 + 4.5 * np.arange(93, dtype=np.float64)
    data = np.random.default_rng(9).standard_normal((int((onsets[-1] + 3) * RATE), 8))
    started = time.perf_counter()
    result = analysis.event_band_power(
        data, analysis.SampleClock(rate_hz=RATE), onsets, settings
    )
    elapsed = time.perf_counter() - started
    assert result.mean.shape == (8, 6, 4)
    assert result.n_dropped == 0
    assert elapsed < 2.5
