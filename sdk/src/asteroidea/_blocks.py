"""A read-only block cache over a `RemoteFile`, for readers that issue many small reads.

`open_nwb` and `.nii.gz` `open_image` use it; `open_file` and `.nii` `open_image` keep
the one-GET-per-read behaviour of `RemoteFile`.
"""

import io
from collections import OrderedDict
from typing import TYPE_CHECKING, Final, override

if TYPE_CHECKING:
    from collections.abc import Buffer

    from asteroidea._remote import RemoteFile

BLOCK_SIZE: Final = 4 * 1024 * 1024
MAX_BLOCKS: Final = 64


class BlockFile(io.RawIOBase):
    """A seekable read-only view of `inner` that fetches whole aligned blocks.

    At most `max_blocks` blocks of `block_size` bytes stay in memory; the least
    recently used block goes first. `requests` and `bytes_fetched` count what was
    asked of the object store.
    """

    def __init__(
        self,
        inner: RemoteFile,
        *,
        block_size: int = BLOCK_SIZE,
        max_blocks: int = MAX_BLOCKS,
    ) -> None:
        """Wrap `inner`; the first read makes the first request."""
        super().__init__()
        self.inner = inner
        self.block_size = block_size
        self.max_blocks = max_blocks
        self._size = inner.source.size
        self._blocks: OrderedDict[int, bytes] = OrderedDict()
        self._pos = 0

    @property
    def requests(self) -> int:
        """Return the number of ranged requests made."""
        return self.inner.requests

    @property
    def bytes_fetched(self) -> int:
        """Return the number of bytes fetched."""
        return self.inner.bytes_fetched

    @override
    def readable(self) -> bool:
        """Return true."""
        return True

    @override
    def seekable(self) -> bool:
        """Return true."""
        return True

    @override
    def tell(self) -> int:
        """Return the position."""
        return self._pos

    @override
    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        """Move the position; no request is made."""
        base = {io.SEEK_SET: 0, io.SEEK_CUR: self._pos, io.SEEK_END: self._size}
        if whence not in base:
            msg = f"invalid whence {whence}"
            raise ValueError(msg)
        self._pos = max(0, base[whence] + offset)
        return self._pos

    def _block(self, index: int) -> bytes:
        """Return the bytes of block `index`, reading it once and keeping it cached."""
        held = self._blocks.get(index)
        if held is not None:
            self._blocks.move_to_end(index)
            return held
        start = index * self.block_size
        self.inner.seek(start)
        data = bytes(self.inner.read(min(self.block_size, self._size - start)) or b"")
        self._blocks[index] = data
        while len(self._blocks) > self.max_blocks:
            self._blocks.popitem(last=False)
        return data

    @override
    def readinto(self, buffer: Buffer) -> int:
        """Fill `buffer` from the cached blocks; 0 at the end of the object."""
        view = memoryview(buffer).cast("B")
        want = min(len(view), self._size - self._pos)
        done = 0
        while done < want:
            index, offset = divmod(self._pos, self.block_size)
            chunk = self._block(index)[offset : offset + want - done]
            if not chunk:
                break
            view[done : done + len(chunk)] = chunk
            done += len(chunk)
            self._pos += len(chunk)
        return done
