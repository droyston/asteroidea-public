"""Coarse whole-recording spectrogram of an LFP series, per channel.

``build_overview`` reads one chunk column of channels at a time through a caller's
reader, so at most one column's samples are in memory beside the output. Per channel
it runs ``asteroidea.analysis.spectrogram`` with 1 s Hann segments and 1 s steps
(no overlap), averages the LINEAR power into ``log_bands(1, 250, 64)`` with
``analysis.band_power`` and stores the dB values as float16, laid out
[channel, time, band] so one channel is one contiguous byte range.

Values are ``data * conversion`` from the index (the NWB ``conversion`` attribute);
``offset`` drops out with each segment's mean. The upper band edge is clamped to the
Nyquist frequency when the rate is under 500 Hz.

Serialised form (``dumps``/``loads``), version 1: the 8-byte magic ``ASTLFPOV``, a
little-endian u32 header length, a UTF-8 JSON header (shape, band edges, rate,
segment and starting time), the segment-centre times as little-endian f64 and the dB
array as little-endian f16 in C order. A 96-channel series of 764 s at 1000 Hz with 64
bands takes 96 x 764 x 64 x 2 = 9,388,032 bytes of dB values plus 6,112 bytes of times.
"""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

import numpy as np

from asteroidea import analysis

if TYPE_CHECKING:
    from collections.abc import Callable

    from numpy.typing import NDArray

    from asteroidea_ingest.lfp_index import LfpIndex

MAGIC: Final = b"ASTLFPOV"
FORMAT_VERSION: Final = 1
SEGMENT_S: Final = 1.0
FMIN_HZ: Final = 1.0
FMAX_HZ: Final = 250.0
N_BANDS: Final = 64
_HEADER_LEN: Final = struct.Struct("<I")
_NDIM: Final = 3


class OverviewError(ValueError):
    """The series cannot be summarised, or overview bytes are not version 1."""


type ChannelBlockReader = Callable[[int, int], NDArray[Any]]
"""``read(first, stop)`` returns ``data[:, first:stop]``: (samples, stop - first)."""


@dataclass(frozen=True, slots=True, eq=False)
class Overview:
    """``db`` is float16 (n_channels, n_times, n_bands); ``edges`` has n_bands + 1.

    ``times`` are segment centres in seconds from the series' first sample; add
    ``starting_time`` for the NWB session clock.
    """

    db: NDArray[np.float16]
    times: NDArray[np.float64]
    edges: NDArray[np.float64]
    rate_hz: float
    segment_s: float
    starting_time: float


def build_overview(
    read_channel_block: ChannelBlockReader,
    index: LfpIndex,
    *,
    segment_s: float = SEGMENT_S,
    bands: analysis.Bands | None = None,
) -> Overview:
    """Return the overview, reading one chunk column of channels per call.

    ``bands`` defaults to ``log_bands(1, 250, 64)``, its top edge clamped to Nyquist.
    """
    n_samples, n_channels = index.shape
    width = index.chunk_shape[1]
    rate = index.rate_hz
    if not rate > 0:
        msg = f"{index.series_path} has no positive sampling rate ({rate})"
        raise OverviewError(msg)
    if n_samples < round(segment_s * rate):
        msg = f"{index.series_path} holds {n_samples} samples, under one segment"
        raise OverviewError(msg)
    if bands is None:
        bands = analysis.log_bands(FMIN_HZ, min(FMAX_HZ, rate / 2.0), N_BANDS)
    n_bands = bands.centres.size
    db: NDArray[np.float16] | None = None
    times: NDArray[np.float64] | None = None
    for first in range(0, n_channels, width):
        stop = min(first + width, n_channels)
        block = np.asarray(read_channel_block(first, stop))
        if block.shape != (n_samples, stop - first):
            msg = f"reader returned {block.shape} for channels {first}..{stop - 1}"
            raise OverviewError(msg)
        for column in range(stop - first):
            samples = block[:, column].astype(np.float64) * index.conversion
            spec = analysis.spectrogram(samples, rate, segment_s, overlap=0.0)
            power = analysis.band_power(spec.freqs, spec.power, bands)
            if db is None or times is None:
                times = spec.times
                db = np.empty((n_channels, times.size, n_bands), dtype=np.float16)
            db[first + column] = power.T.astype(np.float16)
        del block
    if db is None or times is None:
        msg = f"{index.series_path} has no channels"
        raise OverviewError(msg)
    return Overview(
        db=db,
        times=times,
        edges=bands.edges,
        rate_hz=rate,
        segment_s=segment_s,
        starting_time=index.starting_time,
    )


def dumps(overview: Overview) -> bytes:
    """Return the version-1 serialised overview."""
    head = json.dumps(
        {
            "version": FORMAT_VERSION,
            "shape": list(overview.db.shape),
            "dtype": "<f2",
            "edges": overview.edges.tolist(),
            "rate_hz": overview.rate_hz,
            "segment_s": overview.segment_s,
            "starting_time": overview.starting_time,
        },
        separators=(",", ":"),
    ).encode()
    return b"".join(
        (
            MAGIC,
            _HEADER_LEN.pack(len(head)),
            head,
            overview.times.astype("<f8").tobytes(),
            overview.db.astype("<f2").tobytes(),
        )
    )


_NOT_OVERVIEW: Final = f"not a version-{FORMAT_VERSION} LFP overview:"


def loads(blob: bytes) -> Overview:
    """Return the overview ``dumps`` wrote; refuse any other bytes."""
    lead = len(MAGIC) + _HEADER_LEN.size
    if len(blob) < lead or blob[: len(MAGIC)] != MAGIC:
        msg = f"{_NOT_OVERVIEW} bad magic"
        raise OverviewError(msg)
    (head_len,) = _HEADER_LEN.unpack_from(blob, len(MAGIC))
    try:
        head = json.loads(blob[lead : lead + head_len])
    except ValueError as exc:
        msg = f"{_NOT_OVERVIEW} header is not JSON ({exc})"
        raise OverviewError(msg) from exc
    if head.get("version") != FORMAT_VERSION or head.get("dtype") != "<f2":
        msg = f"{_NOT_OVERVIEW} version {head.get('version')!r} {head.get('dtype')!r}"
        raise OverviewError(msg)
    shape = tuple(int(n) for n in head["shape"])
    edges = np.asarray(head["edges"], dtype=np.float64)
    if len(shape) != _NDIM or edges.size != shape[2] + 1:
        msg = f"{_NOT_OVERVIEW} shape {shape} with {edges.size} edges"
        raise OverviewError(msg)
    n_channels, n_times, n_bands = shape
    body = blob[lead + head_len :]
    times_bytes = n_times * 8
    if len(body) != times_bytes + n_channels * n_times * n_bands * 2:
        msg = f"{_NOT_OVERVIEW} {len(body)} body bytes for shape {shape}"
        raise OverviewError(msg)
    times = np.frombuffer(body[:times_bytes], "<f8").astype(np.float64)
    db = np.frombuffer(body[times_bytes:], "<f2").astype(np.float16)
    return Overview(
        db=db.reshape(n_channels, n_times, n_bands),
        times=times,
        edges=edges,
        rate_hz=float(head["rate_hz"]),
        segment_s=float(head["segment_s"]),
        starting_time=float(head["starting_time"]),
    )
