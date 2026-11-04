"""Shared analysis functions: PSTH, z-score, frame onsets, spectrogram, overview bands.

Pure functions: arrays in, frozen dataclasses out, no I/O. The module imports only the
standard library, numpy and polars (the SDK extra ``analysis``), because the compute
image copies this file into a stub package. ``asteroidea/__init__``
does not import it; use ``from asteroidea import analysis``.
"""

from __future__ import annotations

import dataclasses
import itertools
import json
import math
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Final, cast

import numpy as np
import polars as pl

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from numpy.typing import NDArray

    type FloatArray = NDArray[np.float64]
    type IntArray = NDArray[np.int64]

MIN_STD = 1e-9
"""A baseline whose standard deviation is at most this has no usable z-score."""

POWER_FLOOR = 1e-30
"""Linear power below this is clamped before the dB conversion, so dB is never -inf."""

_EDGE_TOL = 1e-9


@dataclass(frozen=True, slots=True, eq=False)
class Aligned:
    """Spikes of one unit relative to each onset, in window [-pre_s, post_s)."""

    trial: IntArray
    t: FloatArray


@dataclass(frozen=True, slots=True, eq=False)
class Rates:
    """Per-unit PSTH: ``rate_hz`` is (n_units, n_bins), ``edges`` has n_bins + 1."""

    edges: FloatArray
    units: tuple[int, ...]
    rate_hz: FloatArray
    n_trials: int


@dataclass(frozen=True, slots=True, eq=False)
class FrameOnsets:
    """Onset seconds for the frames inside the timestamps, and the frames outside."""

    seconds: FloatArray
    out_of_range: IntArray


@dataclass(frozen=True, slots=True, eq=False)
class Spectrogram:
    """Segment spectra: ``power`` linear (n_freqs, n_times), ``power_db`` in dB."""

    freqs: FloatArray
    times: FloatArray
    power: FloatArray
    power_db: FloatArray


@dataclass(frozen=True, slots=True, eq=False)
class Bands:
    """Log-spaced frequency bands: ``edges`` has n + 1 entries, ``centres`` n."""

    edges: FloatArray
    centres: FloatArray


@dataclass(frozen=True, slots=True, eq=False)
class Decimated:
    """Min/max envelope points in time order."""

    times: FloatArray
    samples: FloatArray


def align(
    spike_times: FloatArray, onsets: FloatArray, pre_s: float, post_s: float
) -> Aligned:
    """Return each spike in [onset - pre_s, onset + post_s) relative to that onset.

    ``spike_times`` must be sorted ascending (as stored in the Units table).
    """
    spikes = np.asarray(spike_times, dtype=np.float64)
    marks = np.asarray(onsets, dtype=np.float64)
    lo = np.searchsorted(spikes, marks - pre_s, side="left")
    hi = np.searchsorted(spikes, marks + post_s, side="left")
    counts = hi - lo
    trial = np.repeat(np.arange(marks.size, dtype=np.int64), counts)
    rel = np.concatenate(
        [spikes[a:b] - mark for a, b, mark in zip(lo, hi, marks, strict=True)]
        or [np.empty(0)]
    )
    return Aligned(trial=trial, t=rel)


def rates(
    spike_times: Mapping[int, FloatArray],
    onsets: FloatArray,
    pre_s: float,
    post_s: float,
    bin_s: float,
) -> Rates:
    """Return the PSTH of each unit: counts / (n_trials x bin_s), empty bins 0.

    ``edges`` is computed once and shared by every unit. Binning is a polars
    group-by over the aligned spikes of all units.
    """
    units = tuple(sorted(spike_times))
    n_trials = int(np.asarray(onsets).size)
    n_bins = round((pre_s + post_s) / bin_s)
    edges = -pre_s + np.arange(n_bins + 1, dtype=np.float64) * bin_s
    unit_parts: list[IntArray] = []
    t_parts: list[FloatArray] = []
    for unit in units:
        aligned = align(spike_times[unit], onsets, pre_s, post_s)
        unit_parts.append(np.full(aligned.t.size, unit, dtype=np.int64))
        t_parts.append(aligned.t)
    long = pl.DataFrame(
        {
            "unit": np.concatenate(unit_parts or [np.empty(0, dtype=np.int64)]),
            "t": np.concatenate(t_parts or [np.empty(0)]),
        }
    )
    counts = (
        long.with_columns(
            ((pl.col("t") + pre_s) / bin_s).floor().cast(pl.Int64).alias("bin")
        )
        .filter(pl.col("bin") < n_bins)
        .group_by("unit", "bin")
        .agg(pl.len().alias("n"))
    )
    grid = np.zeros((len(units), n_bins), dtype=np.float64)
    row_of = {unit: row for row, unit in enumerate(units)}
    for unit, bin_index, n in counts.iter_rows():
        grid[row_of[unit], bin_index] = n
    rate_hz = grid / (n_trials * bin_s) if n_trials else grid
    return Rates(edges=edges, units=units, rate_hz=rate_hz, n_trials=n_trials)


def zscore(
    rate_hz: FloatArray,
    edges: FloatArray,
    baseline: tuple[float, float] | None = None,
) -> tuple[FloatArray | None, ...]:
    """Return z per bin for each unit row, from that unit's baseline bins.

    Baseline bins are those lying wholly inside ``baseline`` (default: from the first
    edge to 0). The standard deviation is the sample one (ddof 1), as the notebook's.
    A unit is ``None`` when fewer than two baseline bins exist or the baseline
    standard deviation is at most ``MIN_STD``.
    """
    lo, hi = (float(edges[0]), 0.0) if baseline is None else baseline
    inside = (edges[:-1] >= lo - _EDGE_TOL) & (edges[1:] <= hi + _EDGE_TOL)
    out: list[FloatArray | None] = []
    for row in np.atleast_2d(rate_hz):
        base = row[inside]
        std = float(base.std(ddof=1)) if base.size > 1 else 0.0
        out.append(None if std <= MIN_STD else (row - base.mean()) / std)
    return tuple(out)


def frame_onsets(
    frames: IntArray, timestamps: FloatArray, base: int = 1
) -> FrameOnsets:
    """Return ``timestamps[frame - base]`` in seconds; frames outside are separate."""
    frame_ids = np.asarray(frames, dtype=np.int64)
    stamps = np.asarray(timestamps, dtype=np.float64)
    index = frame_ids - base
    valid = (index >= 0) & (index < stamps.size)
    return FrameOnsets(seconds=stamps[index[valid]], out_of_range=frame_ids[~valid])


def to_db(power: FloatArray) -> FloatArray:
    """Return ``10 * log10(max(power, POWER_FLOOR))``: finite for any input."""
    return 10.0 * np.log10(np.maximum(power, POWER_FLOOR))


def spectrogram(
    samples: FloatArray,
    rate_hz: float,
    segment_s: float = 1.0,
    overlap: float = 0.5,
) -> Spectrogram:
    """Return a Hann-windowed, mean-removed, one-sided PSD per segment.

    ``power`` is linear (units^2/Hz), ``power_db`` is ``to_db(power)``. ``times`` are
    segment centres in seconds from the first sample.
    """
    x = np.asarray(samples, dtype=np.float64)
    n = round(segment_s * rate_hz)
    hop = max(1, n - round(n * overlap))
    min_samples = 2
    if n < min_samples or x.size < n:
        msg = f"need at least one segment of {n} samples, got {x.size}"
        raise ValueError(msg)
    window = 0.5 - 0.5 * np.cos(2.0 * np.pi * np.arange(n) / n)
    segments = np.lib.stride_tricks.sliding_window_view(x, n)[::hop]
    segments = segments - segments.mean(axis=1, keepdims=True)
    spectrum = np.fft.rfft(segments * window, axis=1)
    power = (np.abs(spectrum) ** 2 / (rate_hz * np.sum(window**2))).T
    power[1 : (n + 1) // 2] *= 2.0  # one-sided; DC and Nyquist (even n) stay single
    freqs = np.fft.rfftfreq(n, 1.0 / rate_hz)
    times = (np.arange(segments.shape[0]) * hop + n / 2) / rate_hz
    return Spectrogram(freqs=freqs, times=times, power=power, power_db=to_db(power))


def log_bands(fmin: float, fmax: float, n: int = 64) -> Bands:
    """Return ``n`` geometrically spaced bands from ``fmin`` to ``fmax`` (both > 0)."""
    if not 0 < fmin < fmax:
        msg = "need 0 < fmin < fmax"
        raise ValueError(msg)
    edges = np.geomspace(fmin, fmax, n + 1)
    return Bands(edges=edges, centres=np.sqrt(edges[:-1] * edges[1:]))


def band_power(freqs: FloatArray, power: FloatArray, bands: Bands) -> FloatArray:
    """Return dB per band per time step, (n_bands, n_times); float16-castable.

    ``power`` is LINEAR power (``Spectrogram.power``), shape (n_freqs, n_times). Bins
    are averaged in linear power and only then converted with ``to_db``: averaging dB
    values would give the geometric mean, which underweights a tone. A band takes the
    bins with ``lo <= f < hi`` (the last band includes ``hi``); a band holding no bin
    (narrow bands at low frequency) takes the bin nearest its centre.
    """
    n_bands = bands.centres.size
    out = np.empty((n_bands, power.shape[1]), dtype=np.float64)
    for band in range(n_bands):
        lo, hi = bands.edges[band], bands.edges[band + 1]
        upper = (freqs <= hi) if band == n_bands - 1 else (freqs < hi)
        members = np.flatnonzero((freqs >= lo) & upper)
        if members.size == 0:
            members = np.array([np.argmin(np.abs(freqs - bands.centres[band]))])
        out[band] = power[members].mean(axis=0)
    return to_db(out)


def decimate_minmax(samples: FloatArray, rate_hz: float, points: int) -> Decimated:
    """Return at most ``points`` samples: each bucket's min and max, in time order.

    ``points // 2`` equal buckets each yield their min and max (one point if equal),
    so spikes survive. At or under ``points`` samples are returned unchanged.
    """
    x = np.asarray(samples, dtype=np.float64)
    if x.size <= points:
        return Decimated(times=np.arange(x.size) / rate_hz, samples=x)
    n_buckets = max(1, points // 2)
    bounds = np.linspace(0, x.size, n_buckets + 1).astype(np.int64)
    picks: list[int] = []
    for start, stop in itertools.pairwise(bounds.tolist()):
        chunk = x[start:stop]
        picks.extend(sorted({start + int(chunk.argmin()), start + int(chunk.argmax())}))
    index = np.array(picks, dtype=np.int64)
    return Decimated(times=index / rate_hz, samples=x[index])


# --- alignment events ---

RECIPE_SCHEMA: Final = "asteroidea.event-recipe/1"
DEFAULTS_SCHEMA: Final = "asteroidea.analysis-defaults/1"
SOURCE_KINDS: Final = frozenset({"interval", "bids_event", "stimulus_file", "label"})
AVERAGING: Final = frozenset({"average", "single"})
DEFAULT_MIN_LEVELS: Final = 2
DEFAULT_MAX_LEVELS: Final = 12
MIN_CYCLES: Final = 3
_PAIR: Final = 2
_RECIPES: Final = Path(__file__).parent / "recipes"
_DEFAULTS: Final = Path(__file__).parent / "analysis_defaults.json"

type Scalar = str | int | float | bool
type Row = Mapping[str, object]


@dataclass(frozen=True, slots=True)
class EventSource:
    """Where an event came from: kind, ``ref`` (table, recording, CSV or label), row."""

    kind: str
    ref: str
    row: int


@dataclass(frozen=True, slots=True)
class AlignmentEvent:
    """One event to align to, on the clock of ``clock_asset_id``."""

    event_type: str
    onset_s: float
    stop_s: float | None
    clock_asset_id: str
    source: EventSource
    attributes: Mapping[str, Scalar]
    averaging: str


@dataclass(frozen=True, slots=True)
class AttributeSpec:
    """A named attribute: a source column, or ``derive`` over that column."""

    name: str
    column: str
    derive: str | None = None
    changed: str = "change"
    unchanged: str = "continuity"
    first: str | None = None
    ranges: tuple[tuple[str, float, float], ...] = ()


@dataclass(frozen=True, slots=True)
class EventTypeSpec:
    """One event type of a recipe: its source, attributes and averaging."""

    name: str
    averaging: str
    source: Mapping[str, object]
    attributes: tuple[AttributeSpec, ...]
    window_s: Mapping[str, object] | None = None


@dataclass(frozen=True, slots=True)
class Recipe:
    """An ``asteroidea.event-recipe/1`` file; ``default`` derives types from rows."""

    dataset: str
    description: str
    event_types: tuple[EventTypeSpec, ...]
    lfp_clock_offset_s: float = 0.0
    default: bool = False
    version: str = "default"


@dataclass(frozen=True, slots=True)
class EventSources:
    """The rows one file's events are resolved from, as ``sql()`` returns them.

    ``intervals`` and ``events`` are catalog rows (``columns_json`` a JSON string),
    ``labels`` are label rows, ``csv`` maps a raw path to its ``csv.DictReader``
    rows and ``frames`` maps a series path to its frame timestamps in seconds.
    """

    asset_id: str
    intervals: Sequence[Row] = ()
    events: Sequence[Row] = ()
    labels: Sequence[Row] = ()
    csv: Mapping[str, Sequence[Mapping[str, str]]] = field(default_factory=dict)
    frames: Mapping[str, Sequence[float]] = field(default_factory=dict)


def _obj(value: object, where: str) -> dict[str, object]:
    """Return `value` as a `str`-keyed dict, else raise `TypeError`."""
    if not isinstance(value, dict):
        msg = f"{where} must be an object"
        raise TypeError(msg)
    return {str(k): v for k, v in cast("dict[object, object]", value).items()}


def _list(value: object, where: str) -> list[object]:
    """Return `value` as a list, else raise `TypeError`."""
    if not isinstance(value, list):
        msg = f"{where} must be a list"
        raise TypeError(msg)
    return list(cast("list[object]", value))


def _str(value: object, where: str) -> str:
    """Return `value` when it is a non-empty string, else raise `ValueError`."""
    if not isinstance(value, str) or not value:
        msg = f"{where} must be a non-empty string"
        raise ValueError(msg)
    return value


def _num(value: object, where: str) -> float:
    """Return `value` as a float, else raise `TypeError`; a bool is not a number."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        msg = f"{where} must be a number"
        raise TypeError(msg)
    return float(value)


def _ranges(raw: object, where: str) -> tuple[tuple[str, float, float], ...]:
    """Parse `{level: [low, high]}` into `(level, low, high)` tuples."""
    parsed: list[tuple[str, float, float]] = []
    for level, bounds in _obj(raw, where).items():
        pair = _list(bounds, f"{where}.{level}")
        if len(pair) != _PAIR:
            msg = f"{where}.{level} must be [low, high]"
            raise ValueError(msg)
        parsed.append((level, _num(pair[0], level), _num(pair[1], level)))
    return tuple(parsed)


def _attribute_spec(raw: object, where: str) -> AttributeSpec:
    """Parse one attribute spec of a recipe; raise on a bad shape."""
    spec = _obj(raw, where)
    name = _str(spec.get("name"), f"{where}.name")
    column = _str(spec.get("column"), f"{where}.column")
    derive = spec.get("derive")
    if derive is None:
        return AttributeSpec(name=name, column=column)
    if derive == "changed_from_previous":
        first = spec.get("first")
        return AttributeSpec(
            name=name,
            column=column,
            derive="changed_from_previous",
            changed=_str(spec.get("changed", "change"), f"{where}.changed"),
            unchanged=_str(spec.get("unchanged", "continuity"), f"{where}.unchanged"),
            first=None if first is None else _str(first, f"{where}.first"),
        )
    if derive == "bucket":
        return AttributeSpec(
            name=name,
            column=column,
            derive="bucket",
            ranges=_ranges(spec.get("ranges"), f"{where}.ranges"),
        )
    msg = f"{where}.derive {derive!r} is not changed_from_previous or bucket"
    raise ValueError(msg)


def _event_type_spec(raw: object, where: str) -> EventTypeSpec:
    """Parse one event type of a recipe; raise on a bad source or averaging."""
    spec = _obj(raw, where)
    source = _obj(spec.get("source"), f"{where}.source")
    kind = source.get("kind")
    if kind not in SOURCE_KINDS:
        msg = f"{where}.source.kind {kind!r} is not one of {sorted(SOURCE_KINDS)}"
        raise ValueError(msg)
    averaging = spec.get("averaging", "average")
    if averaging not in AVERAGING:
        msg = f"{where}.averaging must be average or single"
        raise ValueError(msg)
    window = spec.get("window_s")
    return EventTypeSpec(
        name=_str(spec.get("name"), f"{where}.name"),
        averaging=str(averaging),
        source=source,
        attributes=tuple(
            _attribute_spec(a, f"{where}.attributes[{i}]")
            for i, a in enumerate(_list(spec.get("attributes", []), where))
        ),
        window_s=None if window is None else _obj(window, f"{where}.window_s"),
    )


def parse_recipe(raw: object) -> Recipe:
    """Return the ``Recipe`` of a parsed ``asteroidea.event-recipe/1`` JSON value.

    Raises ``ValueError`` or ``TypeError`` naming the first field that breaks the
    schema. ``version`` is required: every statistics result carries it.
    """
    body = _obj(raw, "recipe")
    if body.get("schema") != RECIPE_SCHEMA:
        msg = f"recipe schema must be {RECIPE_SCHEMA}"
        raise ValueError(msg)
    types = _list(body.get("event_types"), "recipe.event_types")
    return Recipe(
        dataset=_str(body.get("dataset"), "recipe.dataset"),
        description=str(body.get("description", "")),
        event_types=tuple(
            _event_type_spec(t, f"event_types[{i}]") for i, t in enumerate(types)
        ),
        lfp_clock_offset_s=_num(
            body.get("lfp_clock_offset_s", 0), "recipe.lfp_clock_offset_s"
        ),
        version=_str(body.get("version"), "recipe.version"),
    )


def default_recipe(dataset: str) -> Recipe:
    """Return the default recipe, used for a dataset with no recipe file.

    Each ``intervals`` table is an event type named after it, its attributes the
    ``columns_json`` columns with 2 to 12 distinct non-empty values; each BIDS
    events recording is ``events`` with attribute ``trial_type``. Labels never
    enter a default.
    """
    return Recipe(
        dataset=dataset,
        description="default recipe: no recipe file for this dataset",
        event_types=(),
        default=True,
    )


def load_recipe(dataset: str) -> Recipe:
    """Return ``recipes/<dataset>.json`` beside this module, else the default recipe."""
    path = _RECIPES / f"{dataset}.json"
    if not path.is_file():
        return default_recipe(dataset)
    recipe = parse_recipe(json.loads(path.read_text(encoding="utf-8")))
    if recipe.dataset != dataset:
        msg = f"{path.name} names dataset {recipe.dataset}"
        raise ValueError(msg)
    return recipe


def _columns(row: Row) -> dict[str, object]:
    """Return the parsed `columns_json` of an intervals row, `{}` when absent."""
    text = row.get("columns_json")
    if not isinstance(text, str) or not text:
        return {}
    return _obj(json.loads(text), "columns_json")


def _scalar(value: object) -> Scalar | None:
    """Return a typed attribute value; None, NaN and the empty string are absent."""
    if isinstance(value, bool | int | str):
        return None if value == "" else value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    return None


def _csv_value(text: str) -> Scalar | None:
    """Return a CSV cell as int, float or text (``csv`` reads every cell as text)."""
    stripped = text.strip()
    for convert in (int, float):
        try:
            return _scalar(convert(stripped))
        except ValueError:
            continue
    return _scalar(stripped)


def _float_or_none(value: object) -> float | None:
    """Return `value` as a float, or `None` when it is not a number."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


@dataclass(frozen=True, slots=True)
class _Raw:
    """One source row before derived attributes: onset, stop, columns."""

    onset_s: float
    stop_s: float | None
    source: EventSource
    columns: Mapping[str, Scalar | None]
    fixed: Mapping[str, Scalar]


def _from_intervals(spec: Mapping[str, object], sources: EventSources) -> list[_Raw]:
    """Return the raw events from the intervals rows that match the spec."""
    table = _str(spec.get("table"), "source.table")
    where = _obj(spec.get("where", {}), "source.where")
    out: list[_Raw] = []
    for row in sources.intervals:
        if row.get("table_name") != table:
            continue
        columns = _columns(row)
        if any(columns.get(k) != v for k, v in where.items()):
            continue
        start = _float_or_none(row.get("start_time"))
        if start is None:
            continue
        out.append(
            _Raw(
                onset_s=start,
                stop_s=_float_or_none(row.get("stop_time")),
                source=EventSource(
                    "interval", table, int(_num(row.get("row_index"), "row_index"))
                ),
                columns={k: _scalar(v) for k, v in columns.items()},
                fixed={},
            )
        )
    return out


def _from_events(spec: Mapping[str, object], sources: EventSources) -> list[_Raw]:
    """Return the raw events from the events-file rows that match the spec."""
    recording = spec.get("events_for")
    trial_type = spec.get("trial_type")
    out: list[_Raw] = []
    for row in sources.events:
        if recording is not None and row.get("events_for") != recording:
            continue
        if trial_type is not None and row.get("trial_type") != trial_type:
            continue
        onset = _float_or_none(row.get("onset_s"))
        if onset is None:
            continue
        duration = _float_or_none(row.get("duration_s"))
        kind = _scalar(row.get("trial_type"))
        out.append(
            _Raw(
                onset_s=onset,
                stop_s=None if duration is None else onset + duration,
                source=EventSource(
                    "bids_event",
                    str(row.get("events_for")),
                    int(_num(row.get("row_index"), "row_index")),
                ),
                columns={k: _scalar(v) for k, v in _columns(row).items()},
                fixed={} if kind is None else {"trial_type": kind},
            )
        )
    return out


def _from_stimulus_file(
    spec: Mapping[str, object], sources: EventSources
) -> list[_Raw]:
    """Return the raw events from a stimulus CSV mapped through frame times.

    Raises `ValueError` when the CSV rows are missing.
    """
    path = _str(spec.get("csv"), "source.csv")
    series = _str(spec.get("series"), "source.series")
    column = _str(spec.get("frame_column"), "source.frame_column")
    base = int(_num(spec.get("frame_base", 1), "source.frame_base"))
    if path not in sources.csv:
        msg = f"no CSV rows for {path}"
        raise ValueError(msg)
    if series not in sources.frames:
        msg = f"no frame timestamps for {series}"
        raise ValueError(msg)
    rows = sources.csv[path]
    stamps = np.asarray(sources.frames[series], dtype=np.float64)
    numbers = np.array([float(r[column]) for r in rows], dtype=np.float64)
    if not np.array_equal(numbers, np.round(numbers)):
        msg = f"{path} column {column} holds a frame number that is not whole"
        raise ValueError(msg)
    frames = numbers.astype(np.int64)  # the CSV writes frames as "1.0"
    index = frames - base
    inside = np.flatnonzero((index >= 0) & (index < stamps.size))
    seconds = frame_onsets(frames, stamps, base).seconds
    return [
        _Raw(
            onset_s=float(onset),
            stop_s=None,
            source=EventSource("stimulus_file", path, int(position)),
            columns={k: _csv_value(v) for k, v in rows[position].items()},
            fixed={},
        )
        for position, onset in zip(inside.tolist(), seconds.tolist(), strict=True)
    ]


def _from_labels(spec: Mapping[str, object], sources: EventSources) -> list[_Raw]:
    """Return the raw events from the shared labels that match the spec.

    Raises `ValueError` when the spec names neither `label_id` nor `name`.
    """
    label_id = spec.get("label_id")
    name = spec.get("name")
    if label_id is None and name is None:
        msg = "a label source names label_id or name"
        raise ValueError(msg)
    out: list[_Raw] = []
    for position, row in enumerate(sources.labels):
        if label_id is not None and row.get("label_id") != label_id:
            continue
        if name is not None and row.get("name") != name:
            continue
        start = _float_or_none(row.get("start_s"))
        if start is None:
            continue
        fixed: dict[str, Scalar] = {"mine": bool(row.get("mine"))}
        label_name = _scalar(row.get("name"))
        if label_name is not None:
            fixed["name"] = label_name
        out.append(
            _Raw(
                onset_s=start,
                stop_s=_float_or_none(row.get("stop_s")),
                source=EventSource("label", str(row.get("label_id")), position),
                columns={},
                fixed=fixed,
            )
        )
    return out


_READERS: Final = {
    "interval": _from_intervals,
    "bids_event": _from_events,
    "stimulus_file": _from_stimulus_file,
    "label": _from_labels,
}


def _bucket(value: Scalar | None, spec: AttributeSpec) -> str | None:
    """Return the level whose range holds `value`, or `None`."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return next((lvl for lvl, lo, hi in spec.ranges if lo <= value <= hi), None)


def _attributes(
    raws: Sequence[_Raw], specs: Sequence[AttributeSpec]
) -> list[dict[str, Scalar]]:
    """Return the attribute values of each raw event, with derived ones added."""
    out: list[dict[str, Scalar]] = [dict(raw.fixed) for raw in raws]
    for spec in specs:
        previous: Scalar | None = None
        for position, raw in enumerate(raws):
            value = raw.columns.get(spec.column)
            got: Scalar | None
            if spec.derive == "changed_from_previous":
                if position == 0:
                    got = spec.first
                else:
                    got = spec.unchanged if value == previous else spec.changed
                previous = value
            elif spec.derive == "bucket":
                got = _bucket(value, spec)
            else:
                got = value
            if got is not None:
                out[position][spec.name] = got
    return out


def _default_types(sources: EventSources) -> tuple[EventTypeSpec, ...]:
    """Return the default event types, one per interval table."""
    levels: dict[str, dict[str, set[str]]] = {}
    for row in sources.intervals:
        table = row.get("table_name")
        if not isinstance(table, str) or not table:
            continue
        per_column = levels.setdefault(table, {})
        for column, value in _columns(row).items():
            scalar = _scalar(value)
            if scalar is not None:
                per_column.setdefault(column, set()).add(str(scalar))
    types = [
        EventTypeSpec(
            name=table,
            averaging="average",
            source={"kind": "interval", "table": table},
            attributes=tuple(
                AttributeSpec(name=column, column=column)
                for column, values in sorted(per_column.items())
                if DEFAULT_MIN_LEVELS <= len(values) <= DEFAULT_MAX_LEVELS
            ),
        )
        for table, per_column in sorted(levels.items())
    ]
    if sources.events:
        types.append(
            EventTypeSpec(
                name="events",
                averaging="average",
                source={"kind": "bids_event"},
                attributes=(),
            )
        )
    return tuple(types)


def resolve_events(recipe: Recipe, sources: EventSources) -> list[AlignmentEvent]:
    """Return the alignment events of one file: rows in, events out, no I/O.

    Each event type of ``recipe`` (or, for the default recipe, the types its rule
    derives from the rows) reads its source kind, takes the named columns and the
    derived attributes (``changed_from_previous`` over source row order,
    ``bucket`` inclusive at both ends) and is ordered by onset. A type with one
    event is ``single`` whatever the recipe says.
    """
    types = _default_types(sources) if recipe.default else recipe.event_types
    out: list[AlignmentEvent] = []
    for spec in types:
        raws = _READERS[str(spec.source["kind"])](spec.source, sources)
        attributes = _attributes(raws, spec.attributes)
        averaging = "single" if len(raws) == 1 else spec.averaging
        events = [
            AlignmentEvent(
                event_type=spec.name,
                onset_s=raw.onset_s,
                stop_s=raw.stop_s,
                clock_asset_id=sources.asset_id,
                source=raw.source,
                attributes=attrs,
                averaging=averaging,
            )
            for raw, attrs in zip(raws, attributes, strict=True)
        ]
        out.extend(sorted(events, key=lambda e: e.onset_s))
    return out


def lfp_onsets(recipe: Recipe, events: Sequence[AlignmentEvent]) -> FloatArray:
    """Return the onsets on the LFP clock: ``onset_s + recipe.lfp_clock_offset_s``."""
    onsets = np.array([e.onset_s for e in events], dtype=np.float64)
    return onsets + float(recipe.lfp_clock_offset_s)


# --- analysis defaults ---


@dataclass(frozen=True, slots=True)
class Defaults:
    """One dataset's analysis defaults: the convention with its paper block over it."""

    version: str
    label: str
    dataset: str
    source: str | None
    rates: Mapping[str, object]
    bands: Mapping[str, Mapping[str, object]]
    bandpower: Mapping[str, object]
    tests: Mapping[str, object]
    reproduced: tuple[str, ...]
    not_reproduced: tuple[str, ...]


def load_defaults(dataset: str | None = None) -> Defaults:
    """Return the analysis defaults: the ``default`` block, then ``datasets[dataset]``.

    Each section (``rates``, ``bands``, ``bandpower``, ``tests``) of the dataset
    block replaces the convention's keys it names; its bands join the convention's.
    ``reproduced`` and ``not_reproduced`` come from the dataset block alone. Every
    band's Morlet cycles must be at least 3 (``ValueError`` otherwise).
    """
    body = _obj(json.loads(_DEFAULTS.read_text(encoding="utf-8")), "defaults")
    if body.get("schema") != DEFAULTS_SCHEMA:
        msg = f"defaults schema must be {DEFAULTS_SCHEMA}"
        raise ValueError(msg)
    base = _obj(body.get("default"), "default")
    datasets = _obj(body.get("datasets", {}), "datasets")
    paper = (
        _obj(datasets.get(dataset, {}), "datasets entry") if dataset is not None else {}
    )
    sections: dict[str, dict[str, object]] = {}
    for key in ("rates", "bands", "bandpower", "tests"):
        merged = _obj(base.get(key, {}), f"default.{key}")
        merged.update(_obj(paper.get(key, {}), f"datasets entry {key}"))
        sections[key] = merged
    bands = {k: _obj(v, f"bands.{k}") for k, v in sections["bands"].items()}
    for name, band in bands.items():
        cycles = _num(band.get("cycles"), f"bands.{name}.cycles")
        if cycles < MIN_CYCLES:
            msg = f"bands.{name}.cycles {cycles:g} is below {MIN_CYCLES}"
            raise ValueError(msg)
    source = paper.get("source")
    return Defaults(
        version=_str(body.get("version"), "version"),
        label=_str(body.get("label"), "label"),
        dataset="" if dataset is None else dataset,
        source=source if isinstance(source, str) else None,
        rates=sections["rates"],
        bands=bands,
        bandpower=sections["bandpower"],
        tests=sections["tests"],
        reproduced=tuple(str(x) for x in _list(paper.get("reproduced", []), "list")),
        not_reproduced=tuple(
            str(x) for x in _list(paper.get("not_reproduced", []), "list")
        ),
    )


# --- session statistics: an exploratory screen ---

SCREEN_LABEL: Final = "exploratory screen"
"""The label every statistics result carries."""

_EXACT_FLIP_LIMIT: Final = 20
"""Sign flips are enumerated exactly only up to this many events (2**20 rows)."""


@dataclass(frozen=True, slots=True)
class AreaRow:
    """One ``location``: the fraction of its units with p at most alpha, and its p."""

    location: str
    n_units: int
    n_below_alpha: int
    fraction: float
    p: float


@dataclass(frozen=True, slots=True)
class ResponsiveRow:
    """One unit: response minus reference rate over the events, with p and BH q."""

    unit: int
    location: str | None
    response_hz: float
    reference_hz: float
    statistic: float
    p: float
    q: float
    screened: bool


@dataclass(frozen=True, slots=True)
class ResponsiveResult:
    """The responsive screen of one ``average`` event type over a session's units."""

    label: str
    defaults_version: str
    recipe_version: str
    event_type: str
    response_window_s: tuple[float, float]
    reference_window_s: tuple[float, float]
    n_events: int
    n_permutations: int
    exact: bool
    seed: int
    alpha: float
    dropped_units: tuple[int, ...]
    rows: tuple[ResponsiveRow, ...]
    areas: tuple[AreaRow, ...]


@dataclass(frozen=True, slots=True)
class TuningRow:
    """One unit: mean rate per level, selectivity index, between-level variance."""

    unit: int
    location: str | None
    level_means: Mapping[str, float]
    selectivity_index: float
    statistic: float
    p: float
    q: float
    screened: bool


@dataclass(frozen=True, slots=True)
class TuningResult:
    """The tuning screen of one (event type, attribute); ``skipped`` says why not."""

    label: str
    defaults_version: str
    recipe_version: str
    event_type: str
    attribute: str
    response_window_s: tuple[float, float]
    level_counts: Mapping[str, int]
    levels: tuple[str, ...]
    n_events: int
    n_permutations: int
    seed: int
    alpha: float
    dropped_units: tuple[int, ...]
    rows: tuple[TuningRow, ...]
    areas: tuple[AreaRow, ...]
    skipped: str | None = None


@dataclass(frozen=True, slots=True)
class _Window:
    """The response window and the padding around it, in seconds."""

    pre_s: float
    post_s: float
    response: tuple[float, float]

    @property
    def length(self) -> float:
        """Return the length of the response window in seconds."""
        return self.response[1] - self.response[0]

    @property
    def read(self) -> tuple[float, float]:
        """Return ``(R_pre, R_post)``: the response and reference windows."""
        return max(self.pre_s, self.length), max(self.post_s, self.response[1])


@dataclass(frozen=True, slots=True)
class _Screen:
    """What every result of one call shares: tests settings, units, versions."""

    n_perm: int
    seed: int
    alpha: float
    minimum: int
    units: tuple[int, ...]
    dropped: tuple[int, ...]
    where: tuple[str | None, ...]
    defaults_version: str
    recipe_version: str


def _window(recipe: Recipe, defaults: Defaults, event_type: str) -> _Window:
    """Return the window of an event type: the defaults, then its own.

    Raises `ValueError` when the response window is not `[start, end]`.
    """
    merged: dict[str, object] = dict(defaults.rates)
    for spec in recipe.event_types:
        if spec.name == event_type and spec.window_s is not None:
            merged.update(spec.window_s)
    pair = _list(merged.get("response_window_s"), "response_window_s")
    if len(pair) != _PAIR:
        msg = "response_window_s must be [start, end]"
        raise ValueError(msg)
    start = _num(pair[0], "response_window_s")
    end = _num(pair[1], "response_window_s")
    if end <= start:
        msg = f"{event_type} response window [{start:g}, {end:g}] is empty"
        raise ValueError(msg)
    return _Window(
        pre_s=_num(merged.get("pre_s"), "pre_s"),
        post_s=_num(merged.get("post_s"), "post_s"),
        response=(start, end),
    )


def _screen(
    spike_times: Mapping[int, FloatArray],
    events: Sequence[AlignmentEvent],
    recipe: Recipe,
    defaults: Defaults,
    locations: Mapping[int, str] | None,
) -> _Screen:
    """Run the unit screen and return the units that pass it.

    Raises `ValueError` when the test settings are not supported.
    """
    tests = defaults.tests
    if tests.get("fdr") != "benjamini-hochberg":
        msg = f"tests.fdr {tests.get('fdr')!r} is not benjamini-hochberg"
        raise ValueError(msg)
    n_perm = int(_num(tests.get("n_permutations"), "tests.n_permutations"))
    if n_perm < 1:
        msg = "tests.n_permutations must be at least 1"
        raise ValueError(msg)
    units, dropped = _kept_units(spike_times, events, defaults)
    return _Screen(
        n_perm=n_perm,
        seed=int(_num(tests.get("seed"), "tests.seed")),
        alpha=_num(tests.get("alpha"), "tests.alpha"),
        minimum=int(_num(tests.get("min_events_per_level"), "min_events_per_level")),
        units=units,
        dropped=dropped,
        where=tuple(None if locations is None else locations.get(u) for u in units),
        defaults_version=defaults.version,
        recipe_version=recipe.version,
    )


def _rng(seed: int, *key: str) -> np.random.Generator:
    """One seeded stream per key, independent of the order keys are visited."""
    return np.random.default_rng([seed, *(zlib.crc32(k.encode()) for k in key)])


def window_rates(
    spike_times: Mapping[int, FloatArray],
    units: Sequence[int],
    onsets: FloatArray,
    window_s: tuple[float, float],
    read_s: tuple[float, float],
) -> FloatArray:
    """Return the (events, units) rate in ``[onset + start, onset + end)`` in Hz.

    Spikes are read with ``align(spikes, onsets, *read_s)``; ``window_s`` must lie
    inside ``[-read_s[0], read_s[1]]``.
    """
    marks = np.asarray(onsets, dtype=np.float64)
    start, end = window_s
    out = np.zeros((marks.size, len(units)), dtype=np.float64)
    for col, unit in enumerate(units):
        aligned = align(spike_times[unit], marks, read_s[0], read_s[1])
        inside = (aligned.t >= start) & (aligned.t < end)
        out[:, col] = np.bincount(aligned.trial[inside], minlength=marks.size)
    return out / (end - start)


def benjamini_hochberg(p: FloatArray) -> FloatArray:
    """Return Benjamini-Hochberg q values in ``p`` order (step-up, capped at 1)."""
    values = np.asarray(p, dtype=np.float64)
    m = values.size
    if m == 0:
        return values.copy()
    order = np.argsort(values, kind="stable")
    scaled = values[order] * m / np.arange(1, m + 1)
    stepped = np.minimum.accumulate(scaled[::-1])[::-1]
    q = np.empty(m, dtype=np.float64)
    q[order] = np.minimum(stepped, 1.0)
    return q


def _null_p(null: FloatArray) -> FloatArray:
    """Each shuffle's p per unit: the share of shuffles at least as large, itself in."""
    ranked = np.sort(null, axis=0)
    out = np.empty_like(null)
    rows = null.shape[0]
    for col in range(null.shape[1]):
        below = np.searchsorted(ranked[:, col], null[:, col] - _EDGE_TOL, side="left")
        out[:, col] = (rows - below) / rows
    return out


def area_proportion(
    p: FloatArray,
    null: FloatArray,
    locations: Sequence[str | None],
    alpha: float,
) -> tuple[AreaRow, ...]:
    """Return, per location, the fraction of units with p at most alpha and its p.

    ``null`` is the (shuffles, units) matrix of shuffled statistics the unit p
    values came from. For shuffle k each unit's p is the share of all shuffles
    whose statistic is at least its shuffle-k statistic (its rank among the other
    shuffles, plus one); the location's null fraction for k is the share of its
    units at most alpha under shuffle k, and the location's p is ``(1 + #{k: null
    fraction >= observed}) / (shuffles + 1)``. Units with no location are left out.
    """
    below_null = _null_p(np.asarray(null, dtype=np.float64)) <= alpha
    below = np.asarray(p, dtype=np.float64) <= alpha
    out: list[AreaRow] = []
    for location in sorted({loc for loc in locations if loc is not None}):
        cols = np.array([loc == location for loc in locations])
        observed = float(below[cols].mean())
        null_fraction = below_null[:, cols].mean(axis=1)
        out.append(
            AreaRow(
                location=location,
                n_units=int(cols.sum()),
                n_below_alpha=int(below[cols].sum()),
                fraction=observed,
                p=float(
                    (1 + np.sum(null_fraction >= observed)) / (null_fraction.size + 1)
                ),
            )
        )
    return tuple(out)


def _counted(
    events: Sequence[AlignmentEvent], defaults: Defaults
) -> list[AlignmentEvent]:
    """Apply ``count_events_where`` to a type whose events all carry its keys."""
    where = _obj(defaults.tests.get("count_events_where", {}), "count_events_where")
    if not where or not all(k in e.attributes for e in events for k in where):
        return list(events)
    return [
        e for e in events if all(e.attributes.get(k) == v for k, v in where.items())
    ]


def _kept_units(
    spike_times: Mapping[int, FloatArray],
    events: Sequence[AlignmentEvent],
    defaults: Defaults,
) -> tuple[tuple[int, ...], tuple[int, ...]]:
    """Return (kept, dropped) units under ``tests.drop_units_silent_during``."""
    units = tuple(sorted(spike_times))
    phase = defaults.tests.get("drop_units_silent_during")
    if phase is None:
        return units, ()
    name = _str(phase, "tests.drop_units_silent_during")
    spans = [(e.onset_s, e.stop_s) for e in events if e.event_type == name]
    if not spans or any(stop is None for _, stop in spans):
        msg = f"drop_units_silent_during needs {name} events with a stop time"
        raise ValueError(msg)
    dropped: list[int] = []
    for unit in units:
        spikes = np.asarray(spike_times[unit], dtype=np.float64)
        hits = sum(
            int(np.searchsorted(spikes, float(stop or 0.0), side="left"))
            - int(np.searchsorted(spikes, start, side="left"))
            for start, stop in spans
        )
        if hits == 0:
            dropped.append(unit)
    return tuple(u for u in units if u not in dropped), tuple(dropped)


def _average_types(events: Sequence[AlignmentEvent]) -> list[str]:
    """Return the names of the event types that are averaged, in first-seen order."""
    names: list[str] = []
    for event in events:
        if event.averaging == "average" and event.event_type not in names:
            names.append(event.event_type)
    return names


def _sign_flips(
    n_events: int, n_perm: int, rng: np.random.Generator
) -> tuple[FloatArray, bool]:
    """Return (flips, exact): every pattern when ``2**n_events <= n_perm``."""
    if n_events <= _EXACT_FLIP_LIMIT and 2**n_events <= n_perm:
        flips = np.array(
            list(itertools.product((1.0, -1.0), repeat=n_events)), dtype=np.float64
        ).reshape(2**n_events, n_events)
        return flips, True
    flips = rng.integers(0, 2, size=(n_perm, n_events)).astype(np.float64)
    return flips * 2.0 - 1.0, False


def _responsive_one(
    spike_times: Mapping[int, FloatArray],
    counted: Sequence[AlignmentEvent],
    name: str,
    window: _Window,
    screen: _Screen,
) -> ResponsiveResult:
    """Return the responsive-unit result of one event type."""
    onsets = np.array([e.onset_s for e in counted], dtype=np.float64)
    reference = (-window.length, 0.0)
    units = screen.units
    resp = window_rates(spike_times, units, onsets, window.response, window.read)
    ref = window_rates(spike_times, units, onsets, reference, window.read)
    diff = resp - ref
    n_events = max(onsets.size, 1)
    flips, exact = _sign_flips(
        onsets.size, screen.n_perm, _rng(screen.seed, name, "responsive")
    )
    observed = np.abs(diff.sum(axis=0)) / n_events
    null = np.abs(flips @ diff) / n_events
    ge = np.sum(null >= observed - _EDGE_TOL, axis=0)
    p = ge / flips.shape[0] if exact else (1 + ge) / (flips.shape[0] + 1)
    q = benjamini_hochberg(p)
    rows = tuple(
        ResponsiveRow(
            unit=unit,
            location=screen.where[i],
            response_hz=float(resp[:, i].sum() / n_events),
            reference_hz=float(ref[:, i].sum() / n_events),
            statistic=float(diff[:, i].sum() / n_events),
            p=float(p[i]),
            q=float(q[i]),
            screened=bool(q[i] <= screen.alpha),
        )
        for i, unit in enumerate(units)
    )
    return ResponsiveResult(
        label=SCREEN_LABEL,
        defaults_version=screen.defaults_version,
        recipe_version=screen.recipe_version,
        event_type=name,
        response_window_s=window.response,
        reference_window_s=reference,
        n_events=onsets.size,
        n_permutations=int(flips.shape[0]),
        exact=exact,
        seed=screen.seed,
        alpha=screen.alpha,
        dropped_units=screen.dropped,
        rows=rows,
        areas=area_proportion(p, null, screen.where, screen.alpha),
    )


def responsive(
    spike_times: Mapping[int, FloatArray],
    events: Sequence[AlignmentEvent],
    recipe: Recipe,
    defaults: Defaults,
    locations: Mapping[int, str] | None = None,
) -> tuple[ResponsiveResult, ...]:
    """Return the responsive screen of each ``average`` event type, per unit.

    Per event the statistic reads the response-window rate minus the rate of the
    equal-length reference window just before onset (``[-length, 0)``), whatever
    the display baseline is; the unit statistic is the mean difference over the
    events ``count_events_where`` keeps. The p value is a two-sided sign-flip
    test: one seeded set of ``n_permutations`` flips per event type, shared by
    every unit (one flips x events by events x units product), enumerated exactly
    when ``2**n_events <= n_permutations``. Random flips give ``(1 + #{|null| >=
    |obs|}) / (n + 1)``; exact flips (the identity among them) give ``#{|null| >=
    |obs|} / 2**n_events``. q is Benjamini-Hochberg across the session's kept
    units within the type; a row is screened when q is at most alpha. Units
    silent during ``tests.drop_units_silent_during`` are dropped first.
    """
    screen = _screen(spike_times, events, recipe, defaults, locations)
    return tuple(
        _responsive_one(
            spike_times,
            _counted([e for e in events if e.event_type == name], defaults),
            name,
            _window(recipe, defaults, name),
            screen,
        )
        for name in _average_types(events)
    )


def level_name(value: Scalar) -> str:
    """Return a level's name: a whole float prints as an int (``6.0`` is ``6``)."""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def selectivity_index(level_means: FloatArray) -> FloatArray:
    """Return ``(max - min) / (max + min)`` over levels (axis 0); 0 where both are 0."""
    means = np.asarray(level_means, dtype=np.float64)
    top, bottom = means.max(axis=0), means.min(axis=0)
    total = top + bottom
    safe = np.where(total == 0.0, 1.0, total)
    return np.where(total == 0.0, 0.0, (top - bottom) / safe)


def between_level_variance(
    rates_by_event: FloatArray, codes: IntArray, n_levels: int
) -> FloatArray:
    """Return ``sum_k n_k (mean_k - mean)^2 / n`` per unit for each labelling.

    ``rates_by_event`` is (events, units); ``codes`` is (shuffles, events) of level
    codes; the result is (shuffles, units). Each labelling is one matrix product.
    """
    rate = np.asarray(rates_by_event, dtype=np.float64)
    labels = np.asarray(codes, dtype=np.int64)
    one_hot = (labels[:, :, None] == np.arange(n_levels)).astype(np.float64)
    counts = one_hot.sum(axis=1)
    means = (
        np.einsum("snk,nu->sku", one_hot, rate) / np.maximum(counts, 1.0)[:, :, None]
    )
    spread = counts[:, :, None] * (means - rate.mean(axis=0)) ** 2
    return spread.sum(axis=1) / rate.shape[0]


def _level_counts(
    counted: Sequence[AlignmentEvent], attribute: str, minimum: int
) -> tuple[dict[str, int], tuple[str, ...]]:
    """Count events per attribute level; return the counts and kept levels."""
    counts: dict[str, int] = {}
    for event in counted:
        value = event.attributes.get(attribute)
        if value is not None:
            key = level_name(value)
            counts[key] = counts.get(key, 0) + 1
    kept = tuple(sorted(k for k, n in counts.items() if n >= minimum))
    return dict(sorted(counts.items())), kept


def _tuning_one(
    spike_times: Mapping[int, FloatArray],
    counted: Sequence[AlignmentEvent],
    key: tuple[str, str],
    window: _Window,
    screen: _Screen,
) -> TuningResult:
    """Return the tuning result of one `(event type, attribute)` pair."""
    name, attribute = key
    counts, levels = _level_counts(counted, attribute, screen.minimum)
    index = {level: k for k, level in enumerate(levels)}
    chosen: list[tuple[float, int]] = []
    for event in counted:
        value = event.attributes.get(attribute)
        if value is not None and level_name(value) in index:
            chosen.append((event.onset_s, index[level_name(value)]))
    result = TuningResult(
        label=SCREEN_LABEL,
        defaults_version=screen.defaults_version,
        recipe_version=screen.recipe_version,
        event_type=name,
        attribute=attribute,
        response_window_s=window.response,
        level_counts=counts,
        levels=levels,
        n_events=len(chosen),
        n_permutations=screen.n_perm,
        seed=screen.seed,
        alpha=screen.alpha,
        dropped_units=screen.dropped,
        rows=(),
        areas=(),
    )
    if len(levels) < _PAIR:
        why = f"fewer than 2 levels hold {screen.minimum} counted events"
        return dataclasses.replace(result, skipped=why)
    onsets = np.array([t for t, _ in chosen], dtype=np.float64)
    codes = np.array([k for _, k in chosen], dtype=np.int64)
    rate = window_rates(spike_times, screen.units, onsets, window.response, window.read)
    observed = between_level_variance(rate, codes[None, :], len(levels))[0]
    shuffles = _rng(screen.seed, name, attribute).permuted(
        np.tile(codes, (screen.n_perm, 1)), axis=1
    )
    null = between_level_variance(rate, shuffles, len(levels))
    p = (1 + np.sum(null >= observed - _EDGE_TOL, axis=0)) / (screen.n_perm + 1)
    q = benjamini_hochberg(p)
    means = np.stack([rate[codes == k].mean(axis=0) for k in range(len(levels))])
    si = selectivity_index(means)
    rows = tuple(
        TuningRow(
            unit=unit,
            location=screen.where[i],
            level_means={lv: float(means[k, i]) for k, lv in enumerate(levels)},
            selectivity_index=float(si[i]),
            statistic=float(observed[i]),
            p=float(p[i]),
            q=float(q[i]),
            screened=bool(q[i] <= screen.alpha),
        )
        for i, unit in enumerate(screen.units)
    )
    areas = area_proportion(p, null, screen.where, screen.alpha)
    return dataclasses.replace(result, rows=rows, areas=areas)


def tuning(
    spike_times: Mapping[int, FloatArray],
    events: Sequence[AlignmentEvent],
    recipe: Recipe,
    defaults: Defaults,
    locations: Mapping[int, str] | None = None,
) -> tuple[TuningResult, ...]:
    """Return the tuning screen of each (``average`` event type, attribute).

    Events are those ``count_events_where`` keeps; a level is kept when it holds
    at least ``min_events_per_level`` of them, and an attribute is tested when at
    least 2 levels are kept (otherwise its result is ``skipped``, with the
    counts). Per unit: the response-window rate per event, the mean per level,
    the selectivity index and, as the statistic, the between-level variance of
    the event rates. One seeded set of ``n_permutations`` label shuffles per
    (event type, attribute) is shared by every unit, with no nested permutation;
    p is ``(1 + #{null >= obs}) / (n + 1)`` and q is Benjamini-Hochberg across
    the kept units. The area test reuses the same shuffle matrix.
    """
    screen = _screen(spike_times, events, recipe, defaults, locations)
    out: list[TuningResult] = []
    for name in _average_types(events):
        counted = _counted([e for e in events if e.event_type == name], defaults)
        window = _window(recipe, defaults, name)
        attributes: list[str] = []
        for event in counted:
            attributes.extend(a for a in event.attributes if a not in attributes)
        out.extend(
            _tuning_one(spike_times, counted, (name, a), window, screen)
            for a in attributes
        )
    return tuple(out)


# --- event-aligned band power ---

BASELINES: Final = ("pre_window_mean", "zscore_over_time", "none")
"""The band-power baselines: dB re the pre-window mean, z over time, or raw power."""

_GAUSS_WIDTH: Final = 4.0
"""A wavelet's spectrum is kept to this many standard deviations around its centre."""

_GAP_FACTOR: Final = 1.5
"""A timestamps window with a step over this many sample periods has a gap."""

_CELLS_PER_CHUNK: Final = 4_000_000
"""Channels are batched so one inverse FFT holds at most this many complex cells."""


@dataclass(frozen=True, slots=True)
class BandSpec:
    """One band: ``[lo_hz, hi_hz)`` every ``freq_step_hz``, Morlet ``cycles``."""

    name: str
    lo_hz: float
    hi_hz: float
    cycles: float
    freq_step_hz: float

    @property
    def freqs(self) -> FloatArray:
        """Return the band's wavelet centre frequencies, from ``lo_hz``, below hi."""
        count = math.ceil((self.hi_hz - self.lo_hz) / self.freq_step_hz - _EDGE_TOL)
        return self.lo_hz + self.freq_step_hz * np.arange(max(count, 0), dtype=float)


@dataclass(frozen=True, slots=True)
class BandPowerSettings:
    """Window and band settings of one event type's band power.

    ``pre_s`` and ``post_s`` are ``R_pre`` and ``R_post`` of the window rule; each
    read is ``[onset - pre_s - pad_s, onset + post_s + pad_s]``.
    """

    bands: tuple[BandSpec, ...]
    pre_s: float
    post_s: float
    pad_s: float
    step_s: float
    baseline: str
    units: str
    averaging: str
    defaults_version: str
    recipe_version: str


@dataclass(frozen=True, slots=True, eq=False)
class SampleClock:
    """Sample times: a uniform ``rate_hz`` from ``start_s``, or explicit timestamps."""

    rate_hz: float | None = None
    start_s: float = 0.0
    timestamps: FloatArray | None = None


@dataclass(frozen=True, slots=True, eq=False)
class EventBandPower:
    """Band power per channel x band x step: ``mean``, ``sem`` and ``n`` events.

    ``mean`` and ``sem`` are (n_channels, n_bands, n_steps), ``n`` is
    (n_channels, n_bands); ``times_s`` are step centres relative to onset.
    ``not_computable`` maps a skipped band to its reason; ``dropped_onsets_s`` are
    the onsets whose read falls outside the recording (or over a gap).
    """

    label: str
    defaults_version: str
    recipe_version: str
    settings: BandPowerSettings
    rate_hz: float
    bands: tuple[str, ...]
    times_s: FloatArray
    mean: FloatArray
    sem: FloatArray
    n: IntArray
    n_dropped: int
    dropped_onsets_s: tuple[float, ...]
    not_computable: Mapping[str, str]


def _band_spec(name: str, raw: Mapping[str, object], step_hz: float) -> BandSpec:
    """Parse one band of the band-power settings.

    Raises `ValueError` when `hz` is not `[lo, hi]` or the cycles are too few.
    """
    edges = _list(raw.get("hz"), f"bands.{name}.hz")
    if len(edges) != _PAIR:
        msg = f"bands.{name}.hz must be [lo, hi]"
        raise ValueError(msg)
    cycles = _num(raw.get("cycles"), f"bands.{name}.cycles")
    if cycles < MIN_CYCLES:
        msg = f"bands.{name}.cycles {cycles:g} is below {MIN_CYCLES}"
        raise ValueError(msg)
    return BandSpec(
        name=name,
        lo_hz=_num(edges[0], f"bands.{name}.hz"),
        hi_hz=_num(edges[1], f"bands.{name}.hz"),
        cycles=cycles,
        freq_step_hz=_num(raw.get("freq_step_hz", step_hz), f"bands.{name}.step"),
    )


def band_power_settings(
    defaults: Defaults,
    recipe: Recipe,
    event_type: str,
    *,
    averaging: str,
    bands: Sequence[str] | None = None,
) -> BandPowerSettings:
    """Return the band-power settings of ``event_type`` from the analysis defaults.

    Bands, edges, cycles and a band's own ``freq_step_hz`` come from
    ``defaults.bands`` (``bands`` picks some, in that order; an unknown name is a
    ``ValueError``); ``step_s``, ``pad_s``, the baseline and units from
    ``defaults.bandpower``. ``R_pre = max(pre_s, response length)`` and ``R_post =
    max(post_s, response end)``, with the response window of the type.
    """
    power = defaults.bandpower
    if power.get("method") != "morlet":
        msg = f"bandpower.method {power.get('method')!r} is not morlet"
        raise ValueError(msg)
    baseline = _str(power.get("baseline"), "bandpower.baseline")
    if baseline not in BASELINES:
        msg = f"bandpower.baseline {baseline!r} is not one of {BASELINES}"
        raise ValueError(msg)
    names = list(defaults.bands) if bands is None else list(bands)
    unknown = [n for n in names if n not in defaults.bands]
    if unknown:
        msg = f"unknown bands {unknown}; bands: {sorted(defaults.bands)}"
        raise ValueError(msg)
    step_hz = _num(power.get("freq_step_hz"), "bandpower.freq_step_hz")
    window = _window(recipe, defaults, event_type)
    return BandPowerSettings(
        bands=tuple(_band_spec(n, defaults.bands[n], step_hz) for n in names),
        pre_s=max(_num(power.get("pre_s"), "bandpower.pre_s"), window.length),
        post_s=max(_num(power.get("post_s"), "bandpower.post_s"), window.response[1]),
        pad_s=_num(power.get("pad_s"), "bandpower.pad_s"),
        step_s=_num(power.get("step_s"), "bandpower.step_s"),
        baseline=baseline,
        units=_str(power.get("units"), "bandpower.units"),
        averaging=averaging,
        defaults_version=defaults.version,
        recipe_version=recipe.version,
    )


@dataclass(frozen=True, slots=True)
class _Grid:
    """Sample geometry of one read: rate, pad, step and step counts, in samples."""

    rate: float
    pad: int
    step: int
    n_pre: int
    n_post: int

    @property
    def n_steps(self) -> int:
        """Return the number of time steps, before plus after the event."""
        return self.n_pre + self.n_post

    @property
    def length(self) -> int:
        """Return the padded length in samples."""
        return 2 * self.pad + self.n_steps * self.step

    @property
    def before(self) -> int:
        """Samples read before the onset sample."""
        return self.pad + self.n_pre * self.step


def _grid(settings: BandPowerSettings, rate: float) -> _Grid:
    """Return the sample grid of a band-power window at `rate`.

    Raises `ValueError` when one step is under one sample.
    """
    step = round(settings.step_s * rate)
    if step < 1:
        msg = f"step_s {settings.step_s:g} is under one sample at {rate:g} Hz"
        raise ValueError(msg)
    return _Grid(
        rate=rate,
        pad=round(settings.pad_s * rate),
        step=step,
        n_pre=math.floor(settings.pre_s / settings.step_s + _EDGE_TOL),
        n_post=math.floor(settings.post_s / settings.step_s + _EDGE_TOL),
    )


def sample_rate(timestamps: FloatArray) -> float:
    """Return the sample rate of timestamps: steps over their summed gap-free span.

    Steps over 1.5 median periods are gaps and left out; summing the rest
    telescopes, so the rate is exact to the stamps' rounding over the whole span.
    """
    steps = np.diff(np.asarray(timestamps, dtype=np.float64))
    regular = steps[steps <= _GAP_FACTOR * float(np.median(steps))]
    return regular.size / float(regular.sum())


def _onset_index(
    clock: SampleClock, n_samples: int, onsets: FloatArray
) -> tuple[float, IntArray]:
    """Return the rate and, per onset, the first sample at or after it."""
    if clock.timestamps is not None:
        stamps = np.asarray(clock.timestamps, dtype=np.float64)
        if stamps.size != n_samples or stamps.size < _PAIR:
            msg = f"{stamps.size} timestamps for {n_samples} samples"
            raise ValueError(msg)
        rate = sample_rate(stamps)
        index = np.searchsorted(stamps, onsets - _EDGE_TOL / rate, side="left")
        return rate, index.astype(np.int64)
    if clock.rate_hz is None or clock.rate_hz <= 0:
        msg = "a clock needs rate_hz > 0 or timestamps"
        raise ValueError(msg)
    rate = float(clock.rate_hz)
    index = np.ceil((onsets - clock.start_s) * rate - _EDGE_TOL)
    return rate, index.astype(np.int64)


def _decimation(grid: _Grid, sigma_hz: float) -> int:
    """Return the largest divisor d of gcd(step, pad) with ``rate / d >= 4 K sigma``.

    ``|z|**2`` spans ``2 K sigma`` either side of 0, so the decimated rate keeps it
    above Nyquist; d divides the pad and the step, so steps stay whole.
    """
    common = math.gcd(grid.step, grid.pad) or grid.step
    limit = max(grid.rate / (4.0 * _GAUSS_WIDTH * sigma_hz), 1.0)
    return max(d for d in range(1, common + 1) if common % d == 0 and d <= limit)


@dataclass(frozen=True, slots=True, eq=False)
class _Group:
    """Wavelets sharing one decimation: spectrum bins, Gaussian weights, positions."""

    decim: int
    freq_index: IntArray
    bins: IntArray
    weights: FloatArray
    positions: IntArray


def _groups(grid: _Grid, freqs: FloatArray, cycles: FloatArray) -> list[_Group]:
    """Group the wavelets by decimation, each with its band of spectrum bins.

    A Morlet of centre ``f`` and ``c`` cycles is a Gaussian in frequency with
    sigma ``f / c``, gain 2 at ``f`` and positive frequencies only, so a sine of
    amplitude A at ``f`` gives ``|z| = A``, power A**2. Bins beyond ``K`` sigma are
    zero; the kept bins are shifted to 0 (a phase, which ``|z|`` ignores) so the
    inverse FFT runs at the decimated length and returns every d-th sample of z.
    """
    n = grid.length
    duration = n / grid.rate
    sigma = freqs / cycles
    decims = np.array([_decimation(grid, s) for s in sigma], dtype=np.int64)
    out = []
    for decim in sorted(set(decims.tolist())):
        members = np.flatnonzero(decims == decim)
        half = np.ceil(_GAUSS_WIDTH * sigma[members] * duration).astype(np.int64)
        width = int(half.max())
        offsets = np.arange(-width, width + 1, dtype=np.int64)
        centre = np.round(freqs[members] * duration).astype(np.int64)
        bins = centre[:, None] + offsets[None, :]
        valid = (np.abs(offsets)[None, :] <= half[:, None]) & (bins >= 0)
        valid &= bins < (n + 1) // 2
        hz = bins / duration
        gauss = np.exp(
            -((hz - freqs[members, None]) ** 2) / (2.0 * sigma[members, None] ** 2)
        )
        out.append(
            _Group(
                decim=int(decim),
                freq_index=members,
                bins=np.clip(bins, 0, n - 1),
                weights=np.where(valid, 2.0 * gauss, 0.0),
                positions=offsets % (n // int(decim)),
            )
        )
    return out


def _step_power(
    window: FloatArray, grid: _Grid, groups: Sequence[_Group], n_freqs: int
) -> FloatArray:
    """Return the (channels, freqs, steps) mean Morlet power of one event's read."""
    n = grid.length
    spectrum = np.fft.fft(window, axis=1)
    n_ch = window.shape[0]
    out = np.empty((n_ch, n_freqs, grid.n_steps), dtype=np.float64)
    for group in groups:
        size = n // group.decim
        n_f = group.freq_index.size
        chunk = max(1, _CELLS_PER_CHUNK // (n_f * size))
        start, stop = grid.pad // group.decim, (n - grid.pad) // group.decim
        per_step = grid.step // group.decim
        rows = np.arange(n_f)[:, None]
        for a in range(0, n_ch, chunk):
            part = spectrum[a : a + chunk]
            buf = np.zeros((part.shape[0], n_f, size), dtype=np.complex128)
            buf[:, rows, group.positions] = part[:, group.bins] * group.weights
            z = np.fft.ifft(buf, axis=2)[:, :, start:stop] * (size / n)
            power = z.real**2 + z.imag**2
            out[a : a + chunk, group.freq_index] = power.reshape(
                part.shape[0], n_f, grid.n_steps, per_step
            ).mean(axis=3)
    return out


def _computable(
    settings: BandPowerSettings, rate: float
) -> tuple[list[BandSpec], dict[str, str]]:
    """Split the bands into those the window can compute and those it cannot."""
    kept: list[BandSpec] = []
    skipped: dict[str, str] = {}
    for band in settings.bands:
        wavelet = band.cycles / band.lo_hz
        if wavelet > 2.0 * settings.pad_s + _EDGE_TOL:
            skipped[band.name] = (
                f"not computable at this window: wavelet {wavelet:g} s exceeds "
                f"2 x pad_s {2.0 * settings.pad_s:g} s"
            )
        elif band.freqs.size == 0 or band.freqs[0] >= rate / 2.0:
            skipped[band.name] = f"not computable: no frequency below {rate / 2.0:g} Hz"
        else:
            kept.append(band)
    return kept, skipped


def _sem(values: FloatArray) -> FloatArray:
    """Return the standard error over axis 0; NaN for fewer than 2 rows."""
    if values.shape[0] < _PAIR:
        return np.full(values.shape[1:], np.nan)
    return values.std(axis=0, ddof=1) / math.sqrt(values.shape[0])


def _summarise(
    steps: FloatArray, owner: IntArray, settings: BandPowerSettings, n_pre: int
) -> tuple[FloatArray, FloatArray]:
    """Return (mean, sem) per channel x band x step from (events, ch, freqs, steps).

    ``pre_window_mean``: dB of the event-mean power re its mean over the pre-onset
    steps; its sem is the linear sem through the dB derivative. ``zscore_over_time``:
    each frequency z-scored over every step of every event, then the band mean.
    """
    n_bands = int(owner.max()) + 1 if owner.size else 0
    _, n_ch, _, n_steps = steps.shape
    mean = np.empty((n_ch, n_bands, n_steps))
    sem = np.empty((n_ch, n_bands, n_steps))
    for b in range(n_bands):
        mine = steps[:, :, owner == b, :]
        if settings.baseline == "zscore_over_time":
            mu = mine.mean(axis=(0, 3), keepdims=True)
            sd = mine.std(axis=(0, 3), keepdims=True)
            z = np.where(sd > MIN_STD, (mine - mu) / np.maximum(sd, MIN_STD), np.nan)
            trace = z.mean(axis=2)
            mean[:, b], sem[:, b] = trace.mean(axis=0), _sem(trace)
            continue
        trace = mine.mean(axis=2)
        linear, spread = trace.mean(axis=0), _sem(trace)
        if settings.baseline == "none":
            mean[:, b], sem[:, b] = linear, spread
            continue
        base = linear[:, :n_pre].mean(axis=1, keepdims=True)
        mean[:, b] = to_db(linear / base)
        sem[:, b] = 10.0 / math.log(10.0) * spread / np.maximum(linear, POWER_FLOOR)
    return mean, sem


def _band_freqs(
    kept: Sequence[BandSpec], rate: float
) -> tuple[FloatArray, FloatArray, IntArray]:
    """Return every wavelet's frequency, cycles and band index, below Nyquist."""
    per = [b.freqs[b.freqs < rate / 2.0] for b in kept]
    freqs = np.concatenate([*per, np.empty(0)])
    cycles = np.concatenate(
        [
            *(np.full(f.size, b.cycles) for b, f in zip(kept, per, strict=True)),
            np.empty(0),
        ]
    )
    owner = np.concatenate(
        [
            *(np.full(f.size, i, dtype=np.int64) for i, f in enumerate(per)),
            np.empty(0, dtype=np.int64),
        ]
    )
    return freqs, cycles, owner


def event_band_power(
    samples: FloatArray,
    clock: SampleClock,
    onsets: FloatArray,
    settings: BandPowerSettings,
) -> EventBandPower:
    """Return event-aligned Morlet band power per channel and band.

    ``samples`` is (n_samples,) or (n_samples, n_channels) on ``clock``; ``onsets``
    are on the same clock (``lfp_onsets`` adds the recipe's offset). Per event the
    read is ``[onset - R_pre - pad, onset + R_post + pad]`` around the first sample
    at or after the onset; an event whose read falls outside the samples (or over
    a timestamps gap) is dropped and counted. Per channel, event and band
    frequency, the Morlet power (a sine of amplitude A gives A**2) is averaged into
    ``step_s`` steps from the onset, then over the band's frequencies, then across
    the events. Baselines: ``pre_window_mean`` is dB relative to the mean over
    every pre-onset step; ``zscore_over_time`` z-scores each frequency over all
    steps of all events, then averages; ``none`` is raw power. A ``single`` type
    takes exactly one onset and returns its trace (``sem`` NaN). A band whose
    wavelet (``cycles / lo_hz``) exceeds ``2 * pad_s`` is not computed.
    """
    data = np.asarray(samples, dtype=np.float64)
    if data.ndim == 1:
        data = data[:, None]
    marks = np.asarray(onsets, dtype=np.float64)
    if settings.averaging == "single" and marks.size != 1:
        msg = f"a single event type takes one onset, got {marks.size}"
        raise ValueError(msg)
    rate, index = _onset_index(clock, data.shape[0], marks)
    grid = _grid(settings, rate)
    if settings.baseline == "pre_window_mean" and grid.n_pre < 1:
        msg = "pre_window_mean needs at least one pre-onset step"
        raise ValueError(msg)
    kept, skipped = _computable(settings, rate)
    freqs, cycles, owner = _band_freqs(kept, rate)
    groups = _groups(grid, freqs, cycles)
    stamps = None if clock.timestamps is None else np.asarray(clock.timestamps)
    used: list[FloatArray] = []
    dropped: list[float] = []
    for mark, i_on in zip(marks, index, strict=True):
        lo = int(i_on) - grid.before
        hi = lo + grid.length
        inside = lo >= 0 and hi <= data.shape[0]
        if inside and stamps is not None:
            inside = not bool(np.any(np.diff(stamps[lo:hi]) > _GAP_FACTOR / rate))
        if not inside:
            dropped.append(float(mark))
            continue
        used.append(_step_power(data[lo:hi].T, grid, groups, freqs.size))
    n_ch = data.shape[1]
    steps = np.arange(-grid.n_pre, grid.n_post, dtype=np.float64)
    times = (steps + 0.5) * (grid.step / rate)
    if used:
        mean, sem = _summarise(np.stack(used), owner, settings, grid.n_pre)
    else:
        mean = np.full((n_ch, len(kept), grid.n_steps), np.nan)
        sem = mean.copy()
    return EventBandPower(
        label=SCREEN_LABEL,
        defaults_version=settings.defaults_version,
        recipe_version=settings.recipe_version,
        settings=settings,
        rate_hz=rate,
        bands=tuple(b.name for b in kept),
        times_s=times,
        mean=mean,
        sem=sem,
        n=np.full((n_ch, len(kept)), len(used), dtype=np.int64),
        n_dropped=len(dropped),
        dropped_onsets_s=tuple(dropped),
        not_computable=skipped,
    )
