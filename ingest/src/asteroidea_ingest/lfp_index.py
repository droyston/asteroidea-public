"""Chunk-offset index of an NWB LFP series and exact-range window reads.

``build_index`` walks one chunked 2-D ``ElectricalSeries/data`` dataset with h5py's
``get_chunk_info`` and records, per chunk of the time x channel chunk grid, its byte
offset, stored size and filter mask. ``plan_window`` and ``decode_window`` then read
one channel's window from the file's bytes with no h5py: the plan names the byte
ranges to fetch (adjacent chunks merged into one range), and the decoder inflates each
chunk and copies the window out. Both run from the index alone.

Only the deflate (gzip) filter, or no filter, is supported: HDF5's shuffle and every
other filter are refused by name. A window is addressed in seconds from the series'
first sample (``starting_time`` is kept in the index, not added).

Serialised form (``dumps``/``loads``), version 1: the 8-byte magic ``ASTLFPIX``, a
little-endian u32 header length, a UTF-8 JSON header, then three little-endian arrays
over the chunk grid in C order: byte offset (u64), stored size (u32, 0 for a chunk the
file never allocated) and filter mask (u32). That is 16 bytes per chunk plus the
header: a 732,343 x 56 series in 11,443 x 1 chunks (64 x 56 = 3,584 chunks) takes
57,344 bytes of arrays.
"""

from __future__ import annotations

import json
import math
import struct
import zlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

import numpy as np

if TYPE_CHECKING:
    from collections.abc import Sequence

    import h5py
    from numpy.typing import NDArray

MAGIC: Final = b"ASTLFPIX"
FORMAT_VERSION: Final = 1
FILTER_DEFLATE: Final = 1
FILTER_SHUFFLE: Final = 2
_SUPPORTED_FILTERS: Final = frozenset({FILTER_DEFLATE})
_HEADER_LEN: Final = struct.Struct("<I")
_DTYPE_KINDS: Final = frozenset("fiu")
_MAX_CHUNK_BYTES: Final = 2**32 - 1
_NDIM: Final = 2
_MAX_ITEMSIZE: Final = 8


class LfpIndexError(ValueError):
    """Base of every refusal in this module."""


class UnsupportedFilterError(LfpIndexError):
    """The dataset's filter pipeline holds a filter other than deflate."""


class ShuffleFilterError(UnsupportedFilterError):
    """The dataset uses HDF5's shuffle filter, which the decoder does not undo."""


class UnsupportedLayoutError(LfpIndexError):
    """The series is not a chunked 2-D numeric dataset with a sampling rate."""


class IndexFormatError(LfpIndexError):
    """Serialised index bytes are not a version-1 index."""


class WindowError(LfpIndexError):
    """The channel or the time window lies outside the series."""


class ChunkDataError(LfpIndexError):
    """Fetched bytes do not match the plan or do not decode to one chunk."""


@dataclass(frozen=True, slots=True, eq=False)
class LfpIndex:
    """One series' layout and chunk table; the chunk arrays are (rows, columns).

    ``rows`` is the number of chunks along time, ``columns`` along channels. A chunk
    whose ``byte_size`` is 0 was never allocated and reads as ``fill_value``.
    """

    series_path: str
    shape: tuple[int, int]
    dtype: str
    rate_hz: float
    starting_time: float
    conversion: float
    offset: float
    chunk_shape: tuple[int, int]
    filters: tuple[int, ...]
    fill_value: float
    byte_offset: NDArray[np.uint64]
    byte_size: NDArray[np.uint32]
    filter_mask: NDArray[np.uint32]

    @property
    def grid(self) -> tuple[int, int]:
        """Return (chunk rows along time, chunk columns along channels)."""
        return (
            math.ceil(self.shape[0] / self.chunk_shape[0]),
            math.ceil(self.shape[1] / self.chunk_shape[1]),
        )

    def chunk_offset(self, row: int, column: int) -> tuple[int, int]:
        """Return the chunk's first (sample, channel)."""
        return (row * self.chunk_shape[0], column * self.chunk_shape[1])


@dataclass(frozen=True, slots=True)
class ByteRange:
    """Half-open byte range ``[start, stop)`` of the file."""

    start: int
    stop: int

    @property
    def length(self) -> int:
        """Return the number of bytes in the range."""
        return self.stop - self.start

    def header(self) -> str:
        """Return the HTTP ``Range`` header value (inclusive end)."""
        return f"bytes={self.start}-{self.stop - 1}"


@dataclass(frozen=True, slots=True)
class _Piece:
    row: int
    span: int  # index into the merged ranges
    at: int  # chunk's first byte relative to its range's start
    size: int


def check_filters(filters: Sequence[int]) -> None:
    """Refuse a filter pipeline the decoder cannot undo, naming the filter."""
    for code in filters:
        if code == FILTER_SHUFFLE:
            msg = "shuffle filter (HDF5 id 2) is not supported; only gzip"
            raise ShuffleFilterError(msg)
        if code not in _SUPPORTED_FILTERS:
            msg = f"HDF5 filter id {code} is not supported; only gzip (id 1)"
            raise UnsupportedFilterError(msg)


def _check_dtype(dtype: np.dtype[Any]) -> None:
    if dtype.kind not in _DTYPE_KINDS or dtype.itemsize > _MAX_ITEMSIZE:
        msg = f"dtype {dtype.str} is not a numeric scalar type"
        raise UnsupportedLayoutError(msg)


def _scalar(value: object, default: float) -> float:
    if value is None:
        return default
    return float(np.asarray(value).reshape(()))


def _filter_ids(dataset: h5py.Dataset) -> tuple[int, ...]:
    plist = dataset.id.get_create_plist()
    return tuple(int(plist.get_filter(i)[0]) for i in range(plist.get_nfilters()))


def build_index(h5file: h5py.File, series_path: str) -> LfpIndex:
    """Return the index of ``<series_path>/data`` in an open h5py file.

    ``series_path`` names the ElectricalSeries group; its ``starting_time`` dataset
    carries the ``rate`` attribute. A series with ``timestamps`` and no rate, an
    unchunked or non-2-D dataset, a non-numeric dtype or an unsupported filter is
    refused.
    """
    group = h5file[series_path]
    if "starting_time" not in group:
        msg = f"{series_path} has no starting_time and rate (timestamps series)"
        raise UnsupportedLayoutError(msg)
    start = group["starting_time"]
    data = group["data"]
    if data.ndim != _NDIM or data.chunks is None:
        msg = f"{series_path}/data must be chunked 2-D, got {data.shape} {data.chunks}"
        raise UnsupportedLayoutError(msg)
    dtype = np.dtype(data.dtype)
    _check_dtype(dtype)
    filters = _filter_ids(data)
    check_filters(filters)
    shape = (int(data.shape[0]), int(data.shape[1]))
    chunk_shape = (int(data.chunks[0]), int(data.chunks[1]))
    rows = math.ceil(shape[0] / chunk_shape[0])
    columns = math.ceil(shape[1] / chunk_shape[1])
    byte_offset = np.zeros((rows, columns), dtype=np.uint64)
    byte_size = np.zeros((rows, columns), dtype=np.uint32)
    filter_mask = np.zeros((rows, columns), dtype=np.uint32)
    for i in range(data.id.get_num_chunks()):
        info = data.id.get_chunk_info(i)
        row = int(info.chunk_offset[0]) // chunk_shape[0]
        column = int(info.chunk_offset[1]) // chunk_shape[1]
        if int(info.size) > _MAX_CHUNK_BYTES:
            msg = f"chunk {i} of {series_path} holds {info.size} bytes (over u32)"
            raise UnsupportedLayoutError(msg)
        byte_offset[row, column] = int(info.byte_offset)
        byte_size[row, column] = int(info.size)
        filter_mask[row, column] = int(info.filter_mask)
    return LfpIndex(
        series_path=series_path,
        shape=shape,
        dtype=dtype.str,
        rate_hz=_scalar(start.attrs.get("rate"), math.nan),
        starting_time=_scalar(start[()], 0.0),
        conversion=_scalar(data.attrs.get("conversion"), 1.0),
        offset=_scalar(data.attrs.get("offset"), 0.0),
        chunk_shape=chunk_shape,
        filters=filters,
        fill_value=_scalar(data.fillvalue, 0.0),
        byte_offset=byte_offset,
        byte_size=byte_size,
        filter_mask=filter_mask,
    )


def _header(index: LfpIndex) -> dict[str, Any]:
    return {
        "version": FORMAT_VERSION,
        "series_path": index.series_path,
        "shape": list(index.shape),
        "dtype": index.dtype,
        "rate_hz": index.rate_hz,
        "starting_time": index.starting_time,
        "conversion": index.conversion,
        "offset": index.offset,
        "chunk_shape": list(index.chunk_shape),
        "filters": list(index.filters),
        "fill_value": index.fill_value,
    }


def dumps(index: LfpIndex) -> bytes:
    """Return the version-1 serialised index (magic, header, three arrays)."""
    head = json.dumps(_header(index), separators=(",", ":")).encode()
    return b"".join(
        (
            MAGIC,
            _HEADER_LEN.pack(len(head)),
            head,
            index.byte_offset.astype("<u8").tobytes(),
            index.byte_size.astype("<u4").tobytes(),
            index.filter_mask.astype("<u4").tobytes(),
        )
    )


_NOT_INDEX: Final = f"not a version-{FORMAT_VERSION} LFP index:"


def loads(blob: bytes) -> LfpIndex:
    """Return the index ``dumps`` wrote; refuse any other bytes."""
    lead = len(MAGIC) + _HEADER_LEN.size
    if len(blob) < lead or blob[: len(MAGIC)] != MAGIC:
        msg = f"{_NOT_INDEX} bad magic"
        raise IndexFormatError(msg)
    (head_len,) = _HEADER_LEN.unpack_from(blob, len(MAGIC))
    try:
        head = json.loads(blob[lead : lead + head_len])
    except ValueError as exc:
        msg = f"{_NOT_INDEX} header is not JSON ({exc})"
        raise IndexFormatError(msg) from exc
    if head.get("version") != FORMAT_VERSION:
        msg = f"{_NOT_INDEX} version {head.get('version')!r}"
        raise IndexFormatError(msg)
    dtype = np.dtype(head["dtype"])
    _check_dtype(dtype)
    filters = tuple(int(code) for code in head["filters"])
    check_filters(filters)
    shape = (int(head["shape"][0]), int(head["shape"][1]))
    chunk_shape = (int(head["chunk_shape"][0]), int(head["chunk_shape"][1]))
    grid = (math.ceil(shape[0] / chunk_shape[0]), math.ceil(shape[1] / chunk_shape[1]))
    cells = grid[0] * grid[1]
    body = blob[lead + head_len :]
    if len(body) != cells * 16:
        msg = f"{_NOT_INDEX} {len(body)} array bytes for {cells} chunks"
        raise IndexFormatError(msg)
    split = (cells * 8, cells * 12)
    return LfpIndex(
        series_path=str(head["series_path"]),
        shape=shape,
        dtype=dtype.str,
        rate_hz=float(head["rate_hz"]),
        starting_time=float(head["starting_time"]),
        conversion=float(head["conversion"]),
        offset=float(head["offset"]),
        chunk_shape=chunk_shape,
        filters=filters,
        fill_value=float(head["fill_value"]),
        byte_offset=np.frombuffer(body[: split[0]], "<u8").reshape(grid).copy(),
        byte_size=np.frombuffer(body[split[0] : split[1]], "<u4").reshape(grid).copy(),
        filter_mask=np.frombuffer(body[split[1] :], "<u4").reshape(grid).copy(),
    )


def _window(
    index: LfpIndex, channel: int, start_s: float, duration_s: float
) -> tuple[int, int]:
    """Return the sample range [first, stop) of the window, clipped at the end."""
    if not 0 <= channel < index.shape[1]:
        msg = f"channel {channel} outside 0..{index.shape[1] - 1}"
        raise WindowError(msg)
    if not (start_s >= 0 and duration_s > 0):
        msg = f"need start_s >= 0 and duration_s > 0, got {start_s}, {duration_s}"
        raise WindowError(msg)
    first = round(start_s * index.rate_hz)
    stop = min(index.shape[0], first + max(1, round(duration_s * index.rate_hz)))
    if first >= index.shape[0]:
        msg = f"start {start_s} s is past the last sample ({index.shape[0]} samples)"
        raise WindowError(msg)
    return first, stop


def _pieces(
    index: LfpIndex, channel: int, first: int, stop: int
) -> tuple[list[ByteRange], list[_Piece]]:
    """Return the merged ranges and where each allocated touched chunk lies in them."""
    column = channel // index.chunk_shape[1]
    rows = range(first // index.chunk_shape[0], (stop - 1) // index.chunk_shape[0] + 1)
    touched = sorted(
        (
            (
                int(index.byte_offset[row, column]),
                int(index.byte_size[row, column]),
                row,
            )
            for row in rows
            if index.byte_size[row, column] > 0
        ),
    )
    ranges: list[ByteRange] = []
    pieces: list[_Piece] = []
    for offset, size, row in touched:
        if ranges and ranges[-1].stop == offset:
            ranges[-1] = ByteRange(ranges[-1].start, offset + size)
        else:
            ranges.append(ByteRange(offset, offset + size))
        pieces.append(
            _Piece(
                row=row, span=len(ranges) - 1, at=offset - ranges[-1].start, size=size
            )
        )
    return ranges, pieces


def plan_window(
    index: LfpIndex, channel: int, start_s: float, duration_s: float
) -> list[ByteRange]:
    """Return the byte ranges holding ``channel`` over the window, in file order.

    The window starts at sample ``round(start_s * rate)`` from the series' first
    sample and is clipped at the last sample. Chunks that are adjacent in the file
    merge into one range; unallocated chunks need no bytes.
    """
    first, stop = _window(index, channel, start_s, duration_s)
    return _pieces(index, channel, first, stop)[0]


def _inflate(index: LfpIndex, raw: bytes, mask: int) -> bytes:
    out = raw
    for position, code in reversed(list(enumerate(index.filters))):
        if mask & (1 << position):
            continue  # HDF5 skipped this filter for this chunk
        if code == FILTER_DEFLATE:
            try:
                out = zlib.decompress(out)
            except zlib.error as exc:
                msg = f"chunk does not inflate: {exc}"
                raise ChunkDataError(msg) from exc
    return out


def decode_window(
    index: LfpIndex,
    ranges_bytes: Sequence[bytes],
    channel: int,
    start_s: float,
    duration_s: float,
) -> NDArray[Any]:
    """Return the window's samples from the bytes of ``plan_window``'s ranges.

    ``ranges_bytes`` holds one bytes object per planned range, in plan order. The
    result has the stored dtype in native byte order and equals h5py's
    ``data[first:stop, channel]``: raw stored values, no ``conversion`` applied.
    """
    check_filters(index.filters)
    first, stop = _window(index, channel, start_s, duration_s)
    ranges, pieces = _pieces(index, channel, first, stop)
    if len(ranges_bytes) != len(ranges):
        msg = f"plan has {len(ranges)} ranges, got {len(ranges_bytes)} buffers"
        raise ChunkDataError(msg)
    for span, (want, got) in enumerate(zip(ranges, ranges_bytes, strict=True)):
        if len(got) != want.length:
            msg = f"range {span} ({want.header()}) needs {want.length} bytes"
            msg += f", got {len(got)}"
            raise ChunkDataError(msg)
    stored = np.dtype(index.dtype)
    rows_per_chunk, width = index.chunk_shape
    column = channel // width
    out = np.full(stop - first, index.fill_value, dtype=stored.newbyteorder("="))
    for piece in pieces:
        raw = ranges_bytes[piece.span][piece.at : piece.at + piece.size]
        flat = _inflate(index, raw, int(index.filter_mask[piece.row, column]))
        if len(flat) != rows_per_chunk * width * stored.itemsize:
            msg = f"chunk row {piece.row} inflates to {len(flat)} bytes"
            raise ChunkDataError(msg)
        block = np.frombuffer(flat, dtype=stored).reshape(rows_per_chunk, width)
        base = piece.row * rows_per_chunk
        lo, hi = max(first, base), min(stop, base + rows_per_chunk)
        out[lo - first : hi - first] = block[lo - base : hi - base, channel % width]
    return out


def _check_column(index: LfpIndex, column: int) -> None:
    if not 0 <= column < index.grid[1]:
        msg = f"chunk column {column} outside 0..{index.grid[1] - 1}"
        raise WindowError(msg)


def plan_column(index: LfpIndex, column: int) -> list[ByteRange]:
    """Return the byte ranges of every chunk in one chunk column, in file order.

    A chunk column holds ``chunk_shape[1]`` channels over the whole series; each of
    its chunks is named once, so a block reader fetches each chunk once per column,
    not once per channel. Adjacent chunks merge as in ``plan_window``.
    """
    _check_column(index, column)
    return _pieces(index, column * index.chunk_shape[1], 0, index.shape[0])[0]


def decode_column(
    index: LfpIndex, ranges_bytes: Sequence[bytes], column: int
) -> NDArray[Any]:
    """Return ``data[:, a:b]`` of one chunk column from ``plan_column``'s bytes.

    ``a`` is the column's first channel and ``b`` its last plus one, clipped at the
    channel count. Stored dtype in native byte order, raw values, equal to h5py's.
    """
    check_filters(index.filters)
    _check_column(index, column)
    rows_per_chunk, width = index.chunk_shape
    first_channel = column * width
    ranges, pieces = _pieces(index, first_channel, 0, index.shape[0])
    if len(ranges_bytes) != len(ranges):
        msg = f"plan has {len(ranges)} ranges, got {len(ranges_bytes)} buffers"
        raise ChunkDataError(msg)
    for span, (want, got) in enumerate(zip(ranges, ranges_bytes, strict=True)):
        if len(got) != want.length:
            msg = f"range {span} ({want.header()}) needs {want.length} bytes"
            msg += f", got {len(got)}"
            raise ChunkDataError(msg)
    stored = np.dtype(index.dtype)
    n_samples = index.shape[0]
    channels = min(width, index.shape[1] - first_channel)
    out = np.full(
        (n_samples, channels), index.fill_value, dtype=stored.newbyteorder("=")
    )
    for piece in pieces:
        raw = ranges_bytes[piece.span][piece.at : piece.at + piece.size]
        flat = _inflate(index, raw, int(index.filter_mask[piece.row, column]))
        if len(flat) != rows_per_chunk * width * stored.itemsize:
            msg = f"chunk row {piece.row} inflates to {len(flat)} bytes"
            raise ChunkDataError(msg)
        block = np.frombuffer(flat, dtype=stored).reshape(rows_per_chunk, width)
        base = piece.row * rows_per_chunk
        hi = min(n_samples, base + rows_per_chunk)
        out[base:hi] = block[: hi - base, :channels]
    return out
