"""Whole-recording LFP overview over an NWB-shaped HDF5 fixture.

Channel 0 carries a 10 Hz sine and channel 1 a 100 Hz sine, both over faint noise;
the rest is noise. The 10 Hz band of log_bands(1, 250, 64) holds one 1 Hz bin, so the
peak test checks where the tone lands; the 100 Hz band holds nine bins, so the level
test checks the band is the LINEAR mean of its bins: a sine of amplitude a has total
power a^2 / 2 (Parseval), spread over the band's bins by the Hann window, so the
band's mean PSD is a^2 / 2 / (bins x 1 Hz). A dB (geometric) mean reads far lower.
"""

import dataclasses
import math
import tracemalloc
from typing import TYPE_CHECKING, Final

import h5py
import numpy as np
import pytest

from asteroidea import analysis
from asteroidea_ingest import lfp_index, lfp_overview
from asteroidea_ingest.lfp_overview import Overview, OverviewError

from .test_lfp_index import SERIES, write_series

if TYPE_CHECKING:
    from pathlib import Path

    from asteroidea_ingest.lfp_index import LfpIndex

RATE: Final = 1000.0
SECONDS: Final = 60
CHANNELS: Final = 5
LOW_HZ: Final = 10.0
HIGH_HZ: Final = 100.0
AMPLITUDE: Final = 1e-4
NOISE: Final = 1e-8


def _signal() -> np.ndarray:
    t = np.arange(int(SECONDS * RATE)) / RATE
    rng = np.random.default_rng(3)
    data = rng.normal(scale=NOISE, size=(t.size, CHANNELS))
    data[:, 0] += AMPLITUDE * np.sin(2 * np.pi * LOW_HZ * t)
    data[:, 1] += AMPLITUDE * np.sin(2 * np.pi * HIGH_HZ * t)
    return data


@pytest.fixture(scope="module", params=[1, 2], ids=["n1", "n2"])
def series(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> Path:
    path = tmp_path_factory.mktemp("overview") / f"lfp-{request.param}.nwb"
    write_series(path, width=request.param, data=_signal())
    return path


def _h5py_overview(path: Path) -> tuple[LfpIndex, Overview, list[tuple[int, int]]]:
    calls: list[tuple[int, int]] = []
    with h5py.File(path, "r") as f:
        index = lfp_index.build_index(f, SERIES)
        dset = f[SERIES]["data"]

        def read(first: int, stop: int) -> np.ndarray:
            calls.append((first, stop))
            return dset[:, first:stop]

        return index, lfp_overview.build_overview(read, index), calls


def _band_of(freq: float, edges: np.ndarray) -> int:
    return int(np.searchsorted(edges, freq, side="right")) - 1


def test_shape_times_and_edges(series: Path) -> None:
    index, overview, _ = _h5py_overview(series)
    assert overview.db.dtype == np.float16
    assert overview.db.shape == (CHANNELS, SECONDS, 64)  # 1 s segments, 1 s steps
    assert np.allclose(overview.times, np.arange(SECONDS) + 0.5)
    assert np.array_equal(overview.edges, analysis.log_bands(1.0, 250.0, 64).edges)
    assert overview.starting_time == index.starting_time == 0.25
    assert np.isfinite(overview.db).all()


def test_tone_peaks_in_its_band(series: Path) -> None:
    _, overview, _ = _h5py_overview(series)
    band = _band_of(LOW_HZ, overview.edges)
    assert overview.edges[band] <= LOW_HZ < overview.edges[band + 1]
    channel0 = overview.db[0].astype(np.float64)  # (times, bands)
    assert (channel0[:, band] == channel0.max(axis=1)).all()
    # The tone band stands far above the noise bands of the same channel.
    assert np.median(channel0[:, band]) > np.median(channel0) + 60
    # A noise-only channel shows no peak there.
    assert overview.db[3, :, band].mean() < channel0[:, band].mean() - 60


def test_band_is_linear_mean_of_its_bins(series: Path) -> None:
    _, overview, _ = _h5py_overview(series)
    band = _band_of(HIGH_HZ, overview.edges)
    lo, hi = overview.edges[band], overview.edges[band + 1]
    bins = np.arange(math.ceil(lo), math.ceil(hi))  # 1 Hz bins with lo <= f < hi
    assert bins.size == 9
    assert {HIGH_HZ - 1, HIGH_HZ, HIGH_HZ + 1} <= set(bins.tolist())
    expect_db = 10 * np.log10(AMPLITUDE**2 / 2 / bins.size)
    level = overview.db[1, :, band].astype(np.float64)
    assert np.abs(level - expect_db).max() < 0.5


def test_streams_one_chunk_column_per_read(series: Path) -> None:
    index, _, calls = _h5py_overview(series)
    width = index.chunk_shape[1]
    assert calls == [
        (first, min(first + width, CHANNELS)) for first in range(0, CHANNELS, width)
    ]


def test_index_reader_matches_h5py_reader(series: Path) -> None:
    """The overview built from exact ranges via the index equals the h5py one."""
    index, want, _ = _h5py_overview(series)
    blob = series.read_bytes()
    seconds = index.shape[0] / index.rate_hz

    def read(first: int, stop: int) -> np.ndarray:
        cols = []
        for channel in range(first, stop):
            ranges = lfp_index.plan_window(index, channel, 0.0, seconds)
            buffers = [blob[r.start : r.stop] for r in ranges]
            cols.append(lfp_index.decode_window(index, buffers, channel, 0.0, seconds))
        return np.stack(cols, axis=1)

    got = lfp_overview.build_overview(read, index)
    assert np.array_equal(got.db, want.db)


def test_conversion_scales_power(series: Path) -> None:
    index, base, _ = _h5py_overview(series)
    scaled_index = dataclasses.replace(index, conversion=1e-3)
    with h5py.File(series, "r") as f:
        dset = f[SERIES]["data"]
        scaled = lfp_overview.build_overview(
            lambda first, stop: dset[:, first:stop], scaled_index
        )
    diff = base.db.astype(np.float64) - scaled.db.astype(np.float64)
    assert np.abs(diff[:2] - 60.0).max() < 0.2  # f16 step near 100 dB is 0.0625


def test_round_trip(series: Path) -> None:
    _, overview, _ = _h5py_overview(series)
    blob = lfp_overview.dumps(overview)
    assert blob.startswith(lfp_overview.MAGIC)
    head_len = int.from_bytes(blob[8:12], "little")
    assert len(blob) == 12 + head_len + SECONDS * 8 + CHANNELS * SECONDS * 64 * 2
    back = lfp_overview.loads(blob)
    assert np.array_equal(back.db, overview.db)
    assert back.db.dtype == np.float16
    assert np.array_equal(back.times, overview.times)
    assert np.array_equal(back.edges, overview.edges)
    assert (back.rate_hz, back.segment_s, back.starting_time) == (
        overview.rate_hz,
        overview.segment_s,
        overview.starting_time,
    )
    assert lfp_overview.dumps(back) == blob


def test_loads_refuses_foreign_bytes(series: Path) -> None:
    blob = lfp_overview.dumps(_h5py_overview(series)[1])
    with pytest.raises(OverviewError, match="magic"):
        lfp_overview.loads(b"ASTLFPIX" + blob[8:])
    with pytest.raises(OverviewError, match="body bytes"):
        lfp_overview.loads(blob[:-2])
    head_len = int.from_bytes(blob[8:12], "little")
    head = blob[12 : 12 + head_len].replace(b'"version":1', b'"version":9')
    with pytest.raises(OverviewError, match="version 9"):
        lfp_overview.loads(blob[:12] + head + blob[12 + head_len :])


def test_short_series_refused(tmp_path: Path) -> None:
    path = tmp_path / "short.nwb"
    with h5py.File(path, "w") as f:
        group = f.create_group(SERIES)
        group.create_dataset("starting_time", data=0.0).attrs["rate"] = RATE
        group.create_dataset("data", data=np.zeros((999, 2)), chunks=(500, 1))
    with h5py.File(path, "r") as f:
        index = lfp_index.build_index(f, SERIES)
        dset = f[SERIES]["data"]
        with pytest.raises(OverviewError, match="under one segment"):
            lfp_overview.build_overview(lambda a, b: dset[:, a:b], index)


def test_reader_shape_checked(series: Path) -> None:
    index, _, _ = _h5py_overview(series)
    with pytest.raises(OverviewError, match="reader returned"):
        lfp_overview.build_overview(lambda a, b: np.zeros((10, b - a)), index)


def _synthetic_index(n_samples: int, n_channels: int) -> LfpIndex:
    grid = (-(-n_samples // 10_000), n_channels)
    return lfp_index.LfpIndex(
        series_path="/synthetic",
        shape=(n_samples, n_channels),
        dtype="<f8",
        rate_hz=RATE,
        starting_time=0.0,
        conversion=1.0,
        offset=0.0,
        chunk_shape=(10_000, 1),
        filters=(1,),
        fill_value=0.0,
        byte_offset=np.zeros(grid, dtype=np.uint64),
        byte_size=np.zeros(grid, dtype=np.uint32),
        filter_mask=np.zeros(grid, dtype=np.uint32),
    )


def _peak_over_output(n_channels: int, n_samples: int) -> int:
    """Return tracemalloc's peak minus the output array, for a noise reader."""
    rng = np.random.default_rng(0)
    index = _synthetic_index(n_samples, n_channels)
    tracemalloc.start()
    try:
        overview = lfp_overview.build_overview(
            lambda a, b: rng.normal(size=(n_samples, b - a)), index
        )
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    return peak - overview.db.nbytes


def test_peak_memory_does_not_grow_with_channels() -> None:
    """Streaming bound: 32 channels peak like 4 (the whole series is 25.6 MB)."""
    n_samples = 100_000
    few = _peak_over_output(4, n_samples)
    many = _peak_over_output(32, n_samples)
    assert many < few * 1.2
    assert many < 32 * n_samples * 8 / 2  # far under the whole series in memory
