import io
from typing import TYPE_CHECKING, Any

import pytest
from botocore.exceptions import ClientError

from asteroidea import _sample
from asteroidea._remote import GetReply, RemoteFile, Source
from asteroidea.errors import ReadInterruptedError

if TYPE_CHECKING:
    from pathlib import Path

BODY = bytes(range(256)) * 400  # 102400 bytes
SOURCE = Source("raw", "raw/ds/f.bin", "v1", len(BODY))
SLICE = 48
SLICE_LIMIT = 64 * 1024


class Store:
    """A stub object store that serves ranges and records each request."""

    def __init__(self, data: bytes = BODY, *, fail: list[str] | None = None) -> None:
        self.data = data
        self.fail = fail if fail is not None else []
        self.requests: list[dict[str, str | int]] = []

    def get_object(self, **kwargs: str | int) -> GetReply:
        self.requests.append(kwargs)
        if self.fail:
            code = self.fail.pop(0)
            raise ClientError({"Error": {"Code": code, "Message": "m"}}, "GetObject")
        first, last = str(kwargs["Range"]).removeprefix("bytes=").split("-")
        return {"Body": io.BytesIO(self.data[int(first) : int(last) + 1])}


def remote(store: Store, renewed: Store | None = None) -> RemoteFile:
    return RemoteFile(SOURCE, client=lambda: store, renewed=lambda: renewed or store)


def test_each_read_is_one_ranged_request_for_exactly_the_bytes_asked() -> None:
    store = Store()
    handle = remote(store)
    assert handle.read(10) == BODY[:10]
    handle.seek(5000)
    assert handle.read(100) == BODY[5000:5100]
    assert [r["Range"] for r in store.requests] == ["bytes=0-9", "bytes=5000-5099"]
    assert handle.bytes_fetched == 110
    assert handle.requests == 2
    assert all(
        r["VersionId"] == "v1" and r["Key"] == "raw/ds/f.bin" for r in store.requests
    )


def test_no_read_ahead_and_no_cache() -> None:
    store = Store()
    handle = remote(store)
    handle.read(10)
    handle.seek(0)
    handle.read(10)  # the same bytes again: asked again, not served from memory
    assert len(store.requests) == 2
    assert handle.bytes_fetched == 20


def test_seek_makes_no_request_and_reads_stop_at_the_end() -> None:
    store = Store()
    handle = remote(store)
    assert handle.seek(-10, io.SEEK_END) == len(BODY) - 10
    assert handle.read(100) == BODY[-10:]
    assert handle.read(100) == b""
    assert len(store.requests) == 1


def test_expired_credentials_renew_once_and_retry() -> None:
    old, new = Store(fail=["ExpiredToken"]), Store()
    handle = remote(old, new)
    assert handle.read(4) == BODY[:4]
    assert len(old.requests) == 1
    assert len(new.requests) == 1
    assert handle.read(4) == BODY[4:8]  # the renewed client stays in use
    assert len(old.requests) == 1


def test_a_second_expiry_raises_read_interrupted() -> None:
    old, new = Store(fail=["ExpiredToken"]), Store(fail=["ExpiredToken"])
    with pytest.raises(ReadInterruptedError, match="interrupted") as caught:
        remote(old, new).read(4)
    assert isinstance(caught.value.__cause__, ClientError)


def test_any_other_failure_raises_read_interrupted_without_renewing() -> None:
    old, new = Store(fail=["InternalError"]), Store()
    with pytest.raises(ReadInterruptedError):
        remote(old, new).read(4)
    assert new.requests == []


def test_one_nifti_slice_fetches_at_most_64_kib(tmp_path: Path) -> None:
    nibabel: Any = _sample.optional("nibabel")
    numpy: Any = _sample.optional("numpy")
    path = tmp_path / "sample.nii"
    nibabel.save(nibabel.Nifti1Image(_sample.sample_array(), numpy.eye(4)), path)
    data = path.read_bytes()
    store = Store(data)
    handle = RemoteFile(
        Source("raw", "raw/ds/s.nii", "v1", len(data)),
        client=lambda: store,
        renewed=lambda: store,
    )
    holder = nibabel.FileHolder(fileobj=handle)
    image = nibabel.Nifti1Image.from_file_map({"header": holder, "image": holder})
    got = image.dataobj[:, :, SLICE]
    expected: Any = _sample.sample_array()
    assert numpy.array_equal(got, expected[:, :, SLICE])
    assert handle.bytes_fetched <= SLICE_LIMIT
    assert handle.bytes_fetched < len(data) // 40
