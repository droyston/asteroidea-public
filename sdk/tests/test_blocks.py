import io
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest

from asteroidea import _image, _nwb, _sample
from asteroidea._blocks import BlockFile
from asteroidea._remote import GetReply, RemoteFile, Source
from asteroidea.errors import ExtraMissingError, NotNwbError

if TYPE_CHECKING:
    from pathlib import Path

BLOCK = 1024
BODY = bytes(range(256)) * 40  # 10240 bytes = 10 blocks of 1024


class Store:
    """A stub object store that serves ranges and records each request."""

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.ranges: list[str] = []

    def get_object(self, **kwargs: str | int) -> GetReply:
        self.ranges.append(str(kwargs["Range"]))
        first, last = str(kwargs["Range"]).removeprefix("bytes=").split("-")
        return {"Body": io.BytesIO(self.data[int(first) : int(last) + 1])}


def remote(store: Store, key: str = "raw/ds/f.bin") -> RemoteFile:
    source = Source("raw", key, "v1", len(store.data))
    return RemoteFile(source, client=lambda: store, renewed=lambda: store)


def blocks(store: Store, max_blocks: int = 64) -> BlockFile:
    return BlockFile(remote(store), block_size=BLOCK, max_blocks=max_blocks)


def test_many_small_reads_in_one_block_make_one_request() -> None:
    store = Store(BODY)
    handle = blocks(store)
    for start in range(0, 500, 10):
        handle.seek(start)
        assert handle.read(10) == BODY[start : start + 10]
    assert store.ranges == ["bytes=0-1023"]
    assert handle.requests == 1
    assert handle.bytes_fetched == BLOCK


def test_a_read_across_blocks_fetches_each_block_once() -> None:
    store = Store(BODY)
    handle = blocks(store)
    handle.seek(1000)
    assert handle.read(2100) == BODY[1000:3100]
    assert store.ranges == [
        "bytes=0-1023",
        "bytes=1024-2047",
        "bytes=2048-3071",
        "bytes=3072-4095",
    ]
    handle.seek(1500)
    handle.read(10)
    assert handle.requests == 4


def test_last_block_is_short_and_end_reads_nothing() -> None:
    store = Store(BODY[:2500])
    handle = blocks(store)
    handle.seek(-10, io.SEEK_END)
    assert handle.read() == BODY[2490:2500]
    assert store.ranges == ["bytes=2048-2499"]
    assert handle.read(5) == b""


def test_least_recently_used_block_is_evicted() -> None:
    store = Store(BODY)
    handle = blocks(store, max_blocks=2)
    for index in (0, 1, 0, 2):  # 0 is refreshed, so 1 is evicted when 2 arrives
        handle.seek(index * BLOCK)
        handle.read(1)
    assert handle.requests == 3
    handle.seek(0)
    handle.read(1)
    assert handle.requests == 3
    handle.seek(BLOCK)
    handle.read(1)
    assert handle.requests == 4


def test_file_is_read_only() -> None:
    handle = blocks(Store(BODY))
    assert handle.readable()
    assert not handle.writable()
    with pytest.raises(NotImplementedError):
        handle.write(b"x")


def test_nwb_written_by_pynwb_reads_through_the_block_cache(tmp_path: Path) -> None:
    pynwb: Any = _sample.optional("pynwb", "nwb")
    path = tmp_path / "s.nwb"
    nwbfile = pynwb.NWBFile(
        session_description="d",
        identifier="id-1",
        session_start_time=datetime(2026, 1, 1, tzinfo=UTC),
    )
    nwbfile.add_unit(spike_times=[0.1, 0.2, 0.3])
    nwbfile.add_unit(spike_times=[1.0])
    with pynwb.NWBHDF5IO(str(path), "w") as writer:
        writer.write(nwbfile)
    data = path.read_bytes()
    store = Store(data)
    opened: Any = _nwb.read_nwb(remote(store, "raw/ds/s.nwb"))
    assert opened.identifier == "id-1"
    assert list(opened.units["spike_times"][0]) == [0.1, 0.2, 0.3]
    assert list(opened.units["spike_times"][1]) == [1.0]
    blocks_needed = -(-len(data) // BlockFile(remote(store)).block_size)
    assert opened.block_file.requests <= blocks_needed
    assert opened.block_file.bytes_fetched <= len(data)
    assert opened.remote_file.requests == opened.block_file.requests


def test_a_non_nwb_file_is_not_nwb() -> None:
    with pytest.raises(NotNwbError):
        _nwb.read_nwb(remote(Store(BODY), "raw/ds/f.bin"))


def test_missing_extra_names_the_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(__import__("sys").modules, "h5py", None)
    with pytest.raises(ExtraMissingError, match=r"asteroidea\[nwb\]"):
        _nwb.read_nwb(remote(Store(BODY)))


def _nifti_bytes(tmp_path: Path, name: str) -> bytes:
    nibabel: Any = _sample.optional("nibabel")
    numpy: Any = _sample.optional("numpy")
    array: Any = _sample.sample_array()
    path = tmp_path / name
    nibabel.save(nibabel.Nifti1Image(array, numpy.eye(4)), path)
    return path.read_bytes()


def test_gzipped_nifti_slice_reads_through_blocks(tmp_path: Path) -> None:
    numpy: Any = _sample.optional("numpy")
    data = _nifti_bytes(tmp_path, "s.nii.gz")
    store = Store(data)
    handle = remote(store, "raw/ds/s.nii.gz")
    image: Any = _image.image_over(handle)
    expected: Any = _sample.sample_array()
    assert numpy.array_equal(image.dataobj[:, :, 48], expected[:, :, 48])
    assert 0 < handle.requests <= -(-len(data) // (4 * 1024 * 1024))
    assert image.extra["remote_file"] is handle


def test_plain_nifti_still_one_request_per_read(tmp_path: Path) -> None:
    data = _nifti_bytes(tmp_path, "s.nii")
    store = Store(data)
    handle = remote(store, "raw/ds/s.nii")
    image: Any = _image.image_over(handle)
    assert image.shape == _sample.SHAPE
    assert handle.bytes_fetched <= 64 * 1024
    assert all(
        int(r.split("-")[1]) - int(r.split("-")[0].removeprefix("bytes=")) + 1
        <= 64 * 1024
        for r in store.ranges
    )
