"""Chunk-offset index and exact-range window reads against h5py.

Fixtures are NWB-shaped HDF5 files written with h5py: an ElectricalSeries group with
``data`` (gzip chunks of N x 1 or N x 2 samples x channels, 1000 Hz) and
``starting_time`` carrying ``rate``. ``rows`` files are written in one call, so h5py
allocates the chunks of one time row next to each other; ``columns`` files are
written channel by channel, so one channel's chunks are adjacent in the file. The
expected values always come from h5py: its slice for the samples, its
``get_chunk_info`` for the bytes.
"""

import itertools
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

import h5py
import numpy as np
import pytest

from asteroidea_infra.runner import execute
from asteroidea_ingest import lfp_index
from asteroidea_ingest.lfp_index import (
    ByteRange,
    ChunkDataError,
    IndexFormatError,
    LfpIndex,
    ShuffleFilterError,
    UnsupportedFilterError,
    UnsupportedLayoutError,
    WindowError,
)

if TYPE_CHECKING:
    from pathlib import Path

SERIES: Final = "/processing/ecephys/LFP/ElectricalSeries"
RATE: Final = 1000.0
SAMPLES: Final = 10_250  # not a chunk multiple: the last chunk row is partial
CHANNELS: Final = 5  # odd: with 2-channel chunks the last chunk column is partial
CHUNK_ROWS: Final = 1000


@dataclass(frozen=True)
class Fixture:
    path: Path
    width: int
    order: str


def _signal() -> np.ndarray:
    rng = np.random.default_rng(7)
    return rng.normal(scale=1e-4, size=(SAMPLES, CHANNELS))


def write_series(
    path: Path,
    *,
    width: int,
    order: str = "rows",
    data: np.ndarray | None = None,
    **create: Any,
) -> None:
    values = _signal() if data is None else data
    with h5py.File(path, "w") as f:
        group = f.create_group(SERIES)
        start = group.create_dataset("starting_time", data=0.25)
        start.attrs["rate"] = RATE
        options = {"compression": "gzip", **create}
        if order == "rows":
            dset = group.create_dataset(
                "data", data=values, chunks=(CHUNK_ROWS, width), **options
            )
        else:
            dset = group.create_dataset(
                "data",
                shape=values.shape,
                dtype=values.dtype,
                chunks=(CHUNK_ROWS, width),
                **options,
            )
            for c0 in range(0, values.shape[1], width):
                dset[:, c0 : c0 + width] = values[:, c0 : c0 + width]
        dset.attrs["conversion"] = 1.0


@pytest.fixture(
    scope="module",
    params=[(1, "rows"), (2, "rows"), (1, "columns"), (2, "columns")],
    ids=["n1-rows", "n2-rows", "n1-columns", "n2-columns"],
)
def fixture(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory):
    width, order = request.param
    path = tmp_path_factory.mktemp("lfp") / f"lfp-{width}-{order}.nwb"
    write_series(path, width=width, order=order)
    return Fixture(path=path, width=width, order=order)


def _index(path: Path) -> LfpIndex:
    with h5py.File(path, "r") as f:
        return lfp_index.build_index(f, SERIES)


def _fetch(path: Path, ranges: list[ByteRange]) -> list[bytes]:
    blob = path.read_bytes()
    return [blob[r.start : r.stop] for r in ranges]


def _h5py_slice(path: Path, first: int, stop: int, channel: int) -> np.ndarray:
    with h5py.File(path, "r") as f:
        return f[SERIES]["data"][first:stop, channel]


# (start_s, duration_s): start, middle, end (clipped), one-sample, across boundaries.
WINDOWS: Final = (
    (0.0, 0.5),
    (0.0, 1.0),
    (4.2, 1.7),
    (0.999, 0.002),
    (2.5, 3.0),
    (9.9, 5.0),
    (10.249, 1.0),
    (0.0, 20.0),
)


@pytest.mark.parametrize(("start_s", "duration_s"), WINDOWS)
@pytest.mark.parametrize("channel", [0, 1, 4])
def test_decode_equals_h5py(
    fixture: Fixture, channel: int, start_s: float, duration_s: float
) -> None:
    index = _index(fixture.path)
    ranges = lfp_index.plan_window(index, channel, start_s, duration_s)
    got = lfp_index.decode_window(
        index, _fetch(fixture.path, ranges), channel, start_s, duration_s
    )
    first = round(start_s * RATE)
    stop = min(SAMPLES, first + round(duration_s * RATE))
    want = _h5py_slice(fixture.path, first, stop, channel)
    assert got.dtype == want.dtype
    assert got.shape == want.shape
    assert np.array_equal(got, want)


def _touched_chunks(path: Path, channel: int, first: int, stop: int) -> list[Any]:
    """Return h5py's chunk infos for every chunk holding the window, by offset."""
    with h5py.File(path, "r") as f:
        dset = f[SERIES]["data"]
        rows, width = dset.chunks
        infos = [dset.id.get_chunk_info(i) for i in range(dset.id.get_num_chunks())]
    return sorted(
        (
            info
            for info in infos
            if info.chunk_offset[1] <= channel < info.chunk_offset[1] + width
            and info.chunk_offset[0] < stop
            and info.chunk_offset[0] + rows > first
        ),
        key=lambda info: info.byte_offset,
    )


@pytest.mark.parametrize(("start_s", "duration_s"), [(2.5, 3.0), (0.0, 20.0)])
def test_plan_bytes_equal_touched_chunks(
    fixture: Fixture, start_s: float, duration_s: float
) -> None:
    index = _index(fixture.path)
    first = round(start_s * RATE)
    stop = min(SAMPLES, first + round(duration_s * RATE))
    touched = _touched_chunks(fixture.path, 3, first, stop)
    ranges = lfp_index.plan_window(index, 3, start_s, duration_s)
    assert sum(r.length for r in ranges) == sum(info.size for info in touched)
    covered = {(r.start, r.stop) for r in ranges}
    assert all(r.start < r.stop for r in ranges)
    assert len(covered) == len(ranges)
    # Expected range count: runs of file-adjacent chunks, counted from h5py's infos.
    runs = 1 + sum(
        1
        for a, b in itertools.pairwise(touched)
        if a.byte_offset + a.size != b.byte_offset
    )
    assert len(ranges) == runs


def test_column_written_channel_is_one_range(tmp_path: Path) -> None:
    """Channel-by-channel writes make a channel's chunks adjacent: one range."""
    path = tmp_path / "columns.nwb"
    write_series(path, width=1, order="columns")
    index = _index(path)
    ranges = lfp_index.plan_window(index, 2, 2.5, 3.0)  # chunk rows 2..5
    assert len(ranges) == 1
    with h5py.File(path, "r") as f:
        dset = f[SERIES]["data"]
        sizes = [
            dset.id.get_chunk_info_by_coord((row * CHUNK_ROWS, 2)).size
            for row in range(2, 6)
        ]
    assert ranges[0].length == sum(sizes)


def test_row_written_channel_is_one_range_per_chunk(tmp_path: Path) -> None:
    path = tmp_path / "rows.nwb"
    write_series(path, width=1, order="rows")
    ranges = lfp_index.plan_window(_index(path), 2, 2.5, 3.0)
    assert len(ranges) == 4


def test_index_records_layout(fixture: Fixture) -> None:
    index = _index(fixture.path)
    assert index.shape == (SAMPLES, CHANNELS)
    assert index.chunk_shape == (CHUNK_ROWS, fixture.width)
    assert index.grid == (11, -(-CHANNELS // fixture.width))
    assert index.rate_hz == RATE
    assert index.starting_time == 0.25
    assert index.dtype == "<f8"
    assert index.filters == (lfp_index.FILTER_DEFLATE,)
    assert index.chunk_offset(3, 1) == (3 * CHUNK_ROWS, fixture.width)
    with h5py.File(fixture.path, "r") as f:
        dset = f[SERIES]["data"]
        for i in range(dset.id.get_num_chunks()):
            info = dset.id.get_chunk_info(i)
            row = info.chunk_offset[0] // CHUNK_ROWS
            col = info.chunk_offset[1] // fixture.width
            assert index.byte_offset[row, col] == info.byte_offset
            assert index.byte_size[row, col] == info.size
            assert index.filter_mask[row, col] == info.filter_mask


def test_round_trip(fixture: Fixture) -> None:
    index = _index(fixture.path)
    blob = lfp_index.dumps(index)
    back = lfp_index.loads(blob)
    for name in LfpIndex.__slots__:
        a, b = getattr(index, name), getattr(back, name)
        if isinstance(a, np.ndarray):
            assert np.array_equal(a, b)
            assert a.dtype == b.dtype
        else:
            assert a == b, name
    assert lfp_index.dumps(back) == blob
    cells = index.grid[0] * index.grid[1]
    assert blob.startswith(lfp_index.MAGIC)
    head_len = int.from_bytes(blob[8:12], "little")
    assert len(blob) == 12 + head_len + 16 * cells
    # A decode from the loaded index alone equals h5py.
    ranges = lfp_index.plan_window(back, 1, 3.3, 2.0)
    got = lfp_index.decode_window(back, _fetch(fixture.path, ranges), 1, 3.3, 2.0)
    assert np.array_equal(got, _h5py_slice(fixture.path, 3300, 5300, 1))


def _synthetic(shape: tuple[int, int], chunks: tuple[int, int]) -> LfpIndex:
    grid = (-(-shape[0] // chunks[0]), -(-shape[1] // chunks[1]))
    return LfpIndex(
        series_path="/processing/ecephys/LFP_micro/ElectricalSeries",
        shape=shape,
        dtype="<f8",
        rate_hz=1000.0,
        starting_time=0.000366,
        conversion=1.0,
        offset=0.0,
        chunk_shape=chunks,
        filters=(1,),
        fill_value=0.0,
        byte_offset=np.arange(grid[0] * grid[1], dtype=np.uint64).reshape(grid)
        * 80_000,
        byte_size=np.full(grid, 80_000, dtype=np.uint32),
        filter_mask=np.zeros(grid, dtype=np.uint32),
    )


def test_size_of_cs62_micro_layout() -> None:
    """732,343 x 56 in 11,443 x 1 chunks: 3,584 chunks, 57,344 array bytes."""
    index = _synthetic((732_343, 56), (11_443, 1))
    assert index.grid == (64, 56)
    blob = lfp_index.dumps(index)
    head_len = int.from_bytes(blob[8:12], "little")
    assert len(blob) - 12 - head_len == 57_344
    assert len(blob) < 58_000


def test_shuffle_refused_at_build(tmp_path: Path) -> None:
    path = tmp_path / "shuffle.nwb"
    write_series(path, width=1, shuffle=True)
    with h5py.File(path, "r") as f, pytest.raises(ShuffleFilterError, match="shuffle"):
        lfp_index.build_index(f, SERIES)


def test_other_filter_refused_at_build(tmp_path: Path) -> None:
    path = tmp_path / "fletcher.nwb"
    write_series(path, width=1, fletcher32=True)
    with h5py.File(path, "r") as f, pytest.raises(UnsupportedFilterError, match="id 3"):
        lfp_index.build_index(f, SERIES)


def test_unfiltered_series_decodes(tmp_path: Path) -> None:
    path = tmp_path / "plain.nwb"
    write_series(path, width=2, compression=None)
    index = _index(path)
    assert index.filters == ()
    ranges = lfp_index.plan_window(index, 3, 1.5, 2.0)
    got = lfp_index.decode_window(index, _fetch(path, ranges), 3, 1.5, 2.0)
    assert np.array_equal(got, _h5py_slice(path, 1500, 3500, 3))


def test_integer_big_endian_series(tmp_path: Path) -> None:
    path = tmp_path / "int.nwb"
    data = (_signal() * 1e8).astype(">i2")
    write_series(path, width=2, data=data)
    index = _index(path)
    assert index.dtype == ">i2"
    ranges = lfp_index.plan_window(index, 4, 7.0, 3.0)
    got = lfp_index.decode_window(index, _fetch(path, ranges), 4, 7.0, 3.0)
    assert np.array_equal(got, _h5py_slice(path, 7000, 10_000, 4))


def test_unallocated_chunks_read_fill_value(tmp_path: Path) -> None:
    path = tmp_path / "sparse.nwb"
    with h5py.File(path, "w") as f:
        group = f.create_group(SERIES)
        group.create_dataset("starting_time", data=0.0).attrs["rate"] = RATE
        dset = group.create_dataset(
            "data",
            shape=(SAMPLES, 2),
            dtype="<f4",
            chunks=(CHUNK_ROWS, 1),
            compression="gzip",
            fillvalue=-3.5,
        )
        dset[2000:3000, 0] = np.arange(1000, dtype="<f4")
    index = _index(path)
    assert int((index.byte_size > 0).sum()) == 1
    ranges = lfp_index.plan_window(index, 0, 1.5, 2.0)
    assert len(ranges) == 1
    got = lfp_index.decode_window(index, _fetch(path, ranges), 0, 1.5, 2.0)
    assert np.array_equal(got, _h5py_slice(path, 1500, 3500, 0))


def test_timestamps_series_refused(tmp_path: Path) -> None:
    path = tmp_path / "stamps.nwb"
    with h5py.File(path, "w") as f:
        group = f.create_group(SERIES)
        group.create_dataset("data", data=np.zeros((10, 2)), chunks=(5, 1))
        group.create_dataset("timestamps", data=np.arange(10.0))
    with h5py.File(path, "r") as f, pytest.raises(UnsupportedLayoutError):
        lfp_index.build_index(f, SERIES)


def test_unchunked_series_refused(tmp_path: Path) -> None:
    path = tmp_path / "contig.nwb"
    with h5py.File(path, "w") as f:
        group = f.create_group(SERIES)
        group.create_dataset("starting_time", data=0.0).attrs["rate"] = RATE
        group.create_dataset("data", data=np.zeros((10, 2)))
    with (
        h5py.File(path, "r") as f,
        pytest.raises(UnsupportedLayoutError, match="chunked"),
    ):
        lfp_index.build_index(f, SERIES)


def test_loads_refuses_foreign_bytes(fixture: Fixture) -> None:
    blob = lfp_index.dumps(_index(fixture.path))
    with pytest.raises(IndexFormatError, match="magic"):
        lfp_index.loads(b"XXXXXXXX" + blob[8:])
    with pytest.raises(IndexFormatError, match="array bytes"):
        lfp_index.loads(blob[:-1])
    head_len = int.from_bytes(blob[8:12], "little")
    head = blob[12 : 12 + head_len]
    bumped = head.replace(b'"version":1', b'"version":2')
    with pytest.raises(IndexFormatError, match="version 2"):
        lfp_index.loads(blob[:12] + bumped + blob[12 + head_len :])
    shuffled = head.replace(b'"filters":[1]', b'"filters":[2,1]')
    blob2 = blob[:8] + len(shuffled).to_bytes(4, "little") + shuffled
    with pytest.raises(ShuffleFilterError):
        lfp_index.loads(blob2 + blob[12 + head_len :])


def test_decode_refuses_shuffle_in_index(fixture: Fixture) -> None:
    index = _index(fixture.path)
    bad = LfpIndex(
        **{
            name: getattr(index, name)
            for name in LfpIndex.__slots__
            if name != "filters"
        },
        filters=(2, 1),
    )
    with pytest.raises(ShuffleFilterError):
        lfp_index.decode_window(bad, [], 0, 0.0, 1.0)


@pytest.mark.parametrize(
    ("channel", "start_s", "duration_s"),
    [
        (-1, 0.0, 1.0),
        (CHANNELS, 0.0, 1.0),
        (0, -0.1, 1.0),
        (0, 0.0, 0.0),
        (0, 10.25, 1.0),
    ],
)
def test_window_refused(
    fixture: Fixture, channel: int, start_s: float, duration_s: float
) -> None:
    with pytest.raises(WindowError):
        lfp_index.plan_window(_index(fixture.path), channel, start_s, duration_s)


def test_decode_refuses_mismatched_buffers(fixture: Fixture) -> None:
    index = _index(fixture.path)
    ranges = lfp_index.plan_window(index, 0, 2.5, 3.0)
    buffers = _fetch(fixture.path, ranges)
    with pytest.raises(ChunkDataError, match="buffers"):
        lfp_index.decode_window(index, [*buffers, b""], 0, 2.5, 3.0)
    with pytest.raises(ChunkDataError, match="needs"):
        lfp_index.decode_window(index, [buffers[0][:-1], *buffers[1:]], 0, 2.5, 3.0)
    garbage = [bytes(len(b)) for b in buffers]
    with pytest.raises(ChunkDataError, match="inflate"):
        lfp_index.decode_window(index, garbage, 0, 2.5, 3.0)


def test_plan_and_decode_run_without_h5py(fixture: Fixture, tmp_path: Path) -> None:
    """With h5py made unimportable, a loaded index still plans and decodes."""
    index_path = tmp_path / "index.bin"
    index_path.write_bytes(lfp_index.dumps(_index(fixture.path)))
    out_path = tmp_path / "window.npy"
    script = (
        "import sys; sys.modules['h5py'] = None\n"
        "import numpy as np, pathlib\n"
        "from asteroidea_ingest import lfp_index as m\n"
        f"ix = m.loads(pathlib.Path({str(index_path)!r}).read_bytes())\n"
        f"blob = pathlib.Path({str(fixture.path)!r}).read_bytes()\n"
        "rs = m.plan_window(ix, 1, 2.5, 3.0)\n"
        "w = m.decode_window(ix, [blob[r.start:r.stop] for r in rs], 1, 2.5, 3.0)\n"
        f"np.save({str(out_path)!r}, w)\n"
        "assert 'h5py' not in sys.modules or sys.modules['h5py'] is None\n"
    )
    done = execute([sys.executable, "-c", script], timeout=60)
    assert done.code == 0, done.err
    assert np.array_equal(np.load(out_path), _h5py_slice(fixture.path, 2500, 5500, 1))
