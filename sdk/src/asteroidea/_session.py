"""Session loaders: a session's files, units, spikes, events, LFP and statistics.

A session is `<dataset>/<subject>/<session or _>`: the triple `assets` already
carries, with `_` standing for a null `bids_session` (so a BIDS-only dataset has
one implicit session per subject). Every read goes through `sql()`, `open_file`
and `open_nwb`, passed in as a `Backend`, so the tests substitute fakes.

This module imports numpy, polars and `asteroidea.analysis` only inside functions,
because `asteroidea/__init__` must import without the `analysis` extra.
"""

from __future__ import annotations

import csv
import dataclasses
import io
import json
import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Protocol, cast

from asteroidea import _image, _nwb, _pynapple, _run, _sample, _sql
from asteroidea._auth import default_auth

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from types import ModuleType

    import numpy as np
    import polars as pl
    import pyarrow as pa
    from numpy.typing import NDArray

    from asteroidea import analysis
    from asteroidea._remote import RemoteFile

    type FloatArray = NDArray[np.float64]

NO_SESSION: Final = "_"
"""The session part of a key whose assets carry no `bids_session`."""

_FILES: Final = (
    "SELECT * FROM files WHERE dataset = ? AND subject = ? "
    "AND coalesce(bids_session, '_') = CAST(? AS VARCHAR) ORDER BY s3_key"
)
_UNITS: Final = (
    "SELECT * FROM units WHERE dataset = ? AND subject = ? "
    "AND coalesce(bids_session, '_') = CAST(? AS VARCHAR) ORDER BY asset_id, unit_index"
)
_SPIKES: Final = (
    "SELECT asset_id, unit_index, spike_times FROM unit_spikes "
    "WHERE dataset = ? AND subject = ? "
    "AND coalesce(bids_session, '_') = CAST(? AS VARCHAR) ORDER BY asset_id, unit_index"
)
_INTERVALS: Final = (
    "SELECT * FROM intervals WHERE dataset = ? AND subject = ? "
    "AND coalesce(bids_session, '_') = CAST(? AS VARCHAR) "
    "ORDER BY asset_id, table_name, row_index"
)
_EVENTS: Final = (
    "SELECT * FROM events WHERE dataset = ? AND subject = ? "
    "AND coalesce(bids_session, '_') = CAST(? AS VARCHAR) "
    "ORDER BY asset_id, events_for, row_index"
)
_SERIES: Final = (
    "SELECT * FROM series WHERE dataset = ? AND subject = ? "
    "AND coalesce(bids_session, '_') = CAST(? AS VARCHAR) ORDER BY asset_id, path"
)
_RAW_ASSET: Final = "SELECT asset_id FROM assets WHERE dataset = ? AND s3_key = ?"
_INDEX: Final = (
    "SELECT a.dataset || '/' || a.subject || '/' || a.session AS session_key, "
    "a.dataset, a.subject, a.session, s.start_time, s.description "
    "FROM (SELECT DISTINCT dataset, subject, coalesce(bids_session, '_') AS session "
    "FROM assets WHERE subject IS NOT NULL) a "
    "LEFT JOIN sessions s ON s.dataset = a.dataset AND s.subject = a.subject "
    "AND coalesce(s.bids_session, '_') = a.session "
    "ORDER BY a.dataset, a.subject, a.session"
)
_STIMULUS_GROUPS: Final = {"presentation": "stimulus", "templates": "stimulus_template"}


class Backend(Protocol):
    """The three reads a session makes: `sql()`, `open_file` and `open_nwb`."""

    def sql(self, query: str, params: Sequence[str]) -> pa.Table:
        """Run `query` with `?` parameters and return an Arrow table."""
        ...

    def open_file(self, asset_id: str) -> RemoteFile:
        """Return a read-only seekable file of the asset."""
        ...

    def open_nwb(self, asset_id: str) -> object:
        """Return a pynwb NWBFile of the asset."""
        ...


@dataclass(frozen=True, slots=True)
class PlatformBackend:
    """The production `Backend`: the researcher's sign-in and the catalog."""

    sql_run: Callable[[str, Sequence[str]], pa.Table]
    file_open: Callable[[str], RemoteFile]
    nwb_open: Callable[[str], object]

    def sql(self, query: str, params: Sequence[str]) -> pa.Table:
        """Run `query` over the catalog tables."""
        return self.sql_run(query, params)

    def open_file(self, asset_id: str) -> RemoteFile:
        """Open the asset as a ranged remote file."""
        return self.file_open(asset_id)

    def open_nwb(self, asset_id: str) -> object:
        """Open the asset as a pynwb NWBFile."""
        return self.nwb_open(asset_id)


def default_backend() -> Backend:
    """Return the production backend for the saved sign-in.

    Every file it opens, for `load_session` or a `Session` method, is recorded
    in the open run with its version and the role `session`.
    """
    auth = default_auth()

    def opened(asset_id: str) -> RemoteFile:
        remote = _image.open_asset(auth, asset_id)
        _run.record_asset(asset_id, remote.source.version_id, "session")
        return remote

    return PlatformBackend(
        sql_run=lambda query, params: _sql.sql(auth, query, list(params)),
        file_open=opened,
        nwb_open=lambda asset_id: _nwb.read_nwb(opened(asset_id)),
    )


def sessions_index(backend: Backend | None = None) -> pa.Table:
    """Return every session: key, dataset, subject, session, start time, description.

    One row per distinct (dataset, subject, `coalesce(bids_session, '_')`) of
    `assets`, LEFT JOINed to `sessions`; `session_key` is
    `<dataset>/<subject>/<session or _>`. Files with no subject (ancillary files)
    are not sessions.
    """
    return (backend or default_backend()).sql(_INDEX, [])


def _modules() -> tuple[Any, Any, Any]:
    """Return (numpy, polars, the shared module); a missing extra is named."""
    numpy = _sample.optional("numpy", "analysis")
    polars = _sample.optional("polars", "analysis")
    analysis_module = _sample.optional("asteroidea.analysis", "analysis")
    return numpy, polars, analysis_module


def _norm(path: str) -> str:
    """Return `path` without leading and trailing slashes."""
    return path.strip("/")


def _node(nwbfile: object, path: str) -> object:
    """Return the object at an HDF5 series path (`/processing/ecephys/LFP/...`)."""
    parts = _norm(path).split("/")
    head, rest = parts[0], parts[1:]
    node: Any
    if head == "stimulus" and rest and rest[0] in _STIMULUS_GROUPS:
        node = getattr(nwbfile, _STIMULUS_GROUPS[rest[0]])
        rest = rest[1:]
    elif head in {"acquisition", "analysis", "processing"}:
        node = getattr(nwbfile, head)
    else:
        msg = f"series path {path} is not under a known NWB group"
        raise ValueError(msg)
    for name in rest:
        node = node[name]
    return node


@dataclass(frozen=True, slots=True, eq=False)
class Lfp:
    """An LFP window: `times_s` on the trials clock, `samples` (n, channels).

    `lfp_clock_offset_s` is what the recipe added to the requested start to find
    the samples (the LFP clock minus the trials clock).
    """

    times_s: FloatArray
    samples: FloatArray
    lfp_clock_offset_s: float


@dataclass(frozen=True, slots=True, eq=False)
class Session:
    """One session: files, units, spikes, events, LFP windows and statistics.

    `files` and `units` are polars DataFrames; `spikes` maps `unit_index` to its
    sorted spike times (seconds, trials clock); `events` has one row per resolved
    event (`attributes` is JSON text). `rates` and `tuning` label every row with the
    settings used, the defaults and recipe versions and "exploratory screen".
    """

    dataset: str
    subject: str
    session: str
    files: pl.DataFrame
    units: pl.DataFrame
    spikes: Mapping[int, FloatArray]
    events: pl.DataFrame
    recipe: analysis.Recipe
    alignment_events: tuple[analysis.AlignmentEvent, ...]
    series: pl.DataFrame
    backend: Backend

    @property
    def key(self) -> str:
        """Return `<dataset>/<subject>/<session or _>`."""
        return f"{self.dataset}/{self.subject}/{self.session}"

    def lfp(
        self,
        series: str,
        channels: Sequence[int] | None = None,
        *,
        start_s: float,
        duration_s: float,
    ) -> Lfp:
        """Return `duration_s` seconds of an ElectricalSeries from `start_s`.

        `start_s` is on the trials and spike clock. The recipe's
        `lfp_clock_offset_s` is added before the samples are indexed (10 s for
        dandi-000623, 0 for dandi-001775), and `times_s` come back on the trials
        clock. `channels` are column positions; the default is every column.
        A series with a rate is indexed by `starting_time` and `rate`, one with
        timestamps by searching them.
        """
        np, _, _ = _modules()
        asset_id = self._series_asset(series)
        obj: Any = _node(self.backend.open_nwb(asset_id), series)
        offset = float(self.recipe.lfp_clock_offset_s)
        t0 = float(start_s) + offset
        stamps = getattr(obj, "timestamps", None)
        if stamps is not None:
            clock = np.asarray(stamps[:], dtype=np.float64)
            lo = int(np.searchsorted(clock, t0, side="left"))
            hi = int(np.searchsorted(clock, t0 + duration_s, side="left"))
            times = clock[lo:hi]
        else:
            rate = float(obj.rate)
            first = float(obj.starting_time or 0.0)
            total = len(obj.data)
            lo = min(max(0, math.ceil((t0 - first) * rate - 1e-9)), total)
            hi = min(total, lo + round(duration_s * rate))
            times = first + np.arange(lo, hi, dtype=np.float64) / rate
        block = np.asarray(obj.data[lo:hi], dtype=np.float64)
        if block.ndim == 1:
            block = block[:, None]
        if channels is not None:
            block = block[:, list(channels)]
        return Lfp(times_s=times - offset, samples=block, lfp_clock_offset_s=offset)

    def _series_asset(self, series: str) -> str:
        """Return the asset id of the one file that holds the series at `series`.

        Raises `ValueError` when no file or more than one file holds it.
        """
        _, pl_, _ = _modules()
        found = self.series.filter(
            pl_.col("path").str.strip_chars("/") == _norm(series)
        )["asset_id"].unique()
        if found.len() != 1:
            known = sorted(set(self.series["path"].to_list()))
            msg = f"{found.len()} files of {self.key} hold {series}; series: {known}"
            raise ValueError(msg)
        return str(found[0])

    def _locations(self) -> dict[int, str]:
        """Return the brain location of each unit, by unit index."""
        if "location" not in self.units.columns:
            return {}
        return {
            int(i): str(loc)
            for i, loc in zip(
                self.units["unit_index"], self.units["location"], strict=True
            )
            if loc is not None
        }

    def _settings(self, defaults: analysis.Defaults) -> dict[str, object]:
        """Return the settings a run records: label and the two versions."""
        return {
            "label": defaults.label,
            "defaults_version": defaults.version,
            "recipe_version": self.recipe.version,
        }

    def rates(
        self,
        event_type: str,
        *,
        pre_s: float | None = None,
        post_s: float | None = None,
        bin_s: float | None = None,
    ) -> pl.DataFrame:
        """Return the PSTH of every unit aligned to `event_type`, with its screen.

        One row per (unit, bin): `rate_hz` from `analysis.rates` over the event
        onsets, plus the responsive screen of the type (`response_hz`,
        `reference_hz`, `p`, `q`, `screened`; null for a `single` type or a
        dropped unit). `pre_s`, `post_s` and `bin_s` default to the dataset's
        `analysis.load_defaults` rates, overridden by the recipe's `window_s` for
        the type. Every row carries the settings, `defaults_version`,
        `recipe_version` and the label "exploratory screen". Inside a `run()`, both
        versions are recorded as inputs of the run.
        """
        np, pl_, an = _modules()
        defaults = an.load_defaults(self.dataset)
        chosen = self._of_type(event_type)
        _run.record_settings(
            self.dataset, defaults.version, self.recipe.version, "rates"
        )
        merged: dict[str, object] = dict(defaults.rates)
        for spec in self.recipe.event_types:
            if spec.name == event_type and spec.window_s is not None:
                merged.update(spec.window_s)
        pre = float(pre_s if pre_s is not None else cast("float", merged["pre_s"]))
        post = float(post_s if post_s is not None else cast("float", merged["post_s"]))
        step = float(bin_s if bin_s is not None else cast("float", merged["bin_s"]))
        onsets = np.array([e.onset_s for e in chosen], dtype=np.float64)
        psth = an.rates(self.spikes, onsets, pre, post, step)
        n_bins = psth.rate_hz.shape[1]
        units = list(psth.units)
        where = self._locations()
        stat: dict[int, Any] = {}
        tests = defaults.tests
        results = an.responsive(
            self.spikes, self.alignment_events, self.recipe, defaults, where
        )
        for result in results:
            if result.event_type == event_type:
                stat = {row.unit: row for row in result.rows}
        frame = pl_.DataFrame(
            {
                "unit_index": np.repeat(np.array(units, dtype=np.int64), n_bins),
                "location": [where.get(u) for u in units for _ in range(n_bins)],
                "bin_start_s": np.tile(psth.edges[:-1], len(units)),
                "bin_end_s": np.tile(psth.edges[1:], len(units)),
                "rate_hz": psth.rate_hz.reshape(-1),
            },
            schema_overrides={"location": pl_.String},
        )
        per_unit = {
            name: [
                getattr(stat.get(u), name, None) for u in units for _ in range(n_bins)
            ]
            for name in ("response_hz", "reference_hz", "p", "q", "screened")
        }
        frame = frame.with_columns(
            pl_.Series("response_hz", per_unit["response_hz"], dtype=pl_.Float64),
            pl_.Series("reference_hz", per_unit["reference_hz"], dtype=pl_.Float64),
            pl_.Series("p", per_unit["p"], dtype=pl_.Float64),
            pl_.Series("q", per_unit["q"], dtype=pl_.Float64),
            pl_.Series("screened", per_unit["screened"], dtype=pl_.Boolean),
        )
        return cast(
            "pl.DataFrame",
            frame.with_columns(
                pl_.lit(event_type).alias("event_type"),
                pl_.lit(len(chosen)).alias("n_events"),
                pl_.lit(pre).alias("pre_s"),
                pl_.lit(post).alias("post_s"),
                pl_.lit(step).alias("bin_s"),
                pl_.lit(json.dumps(tests, sort_keys=True)).alias("tests_json"),
                *(pl_.lit(v).alias(k) for k, v in self._settings(defaults).items()),
            ),
        )

    def tuning(self, event_type: str, attribute: str) -> pl.DataFrame:
        """Return the tuning screen of `attribute` over `event_type`, one row per unit.

        Columns: `unit_index`, `location`, `level_means` (JSON text),
        `selectivity_index`, `statistic`, `p`, `q`, `screened`, then the settings
        (levels, level counts, response window, permutations, seed, alpha),
        `defaults_version`, `recipe_version` and the label "exploratory screen".
        A screen with fewer than 2 kept levels is skipped, and this raises
        `ValueError` naming why. Inside a `run()`, the defaults and recipe versions
        are recorded as inputs of the run.
        """
        _, pl_, an = _modules()
        defaults = an.load_defaults(self.dataset)
        self._of_type(event_type)
        _run.record_settings(
            self.dataset, defaults.version, self.recipe.version, "tuning"
        )
        results = an.tuning(
            self.spikes, self.alignment_events, self.recipe, defaults, self._locations()
        )
        found = [
            r
            for r in results
            if r.event_type == event_type and r.attribute == attribute
        ]
        if not found:
            msg = (
                f"no tuning screen for {event_type}.{attribute}: it is not an "
                "average event type with that attribute"
            )
            raise ValueError(msg)
        result = found[0]
        if result.skipped is not None:
            msg = f"tuning of {event_type}.{attribute} skipped: {result.skipped}"
            raise ValueError(msg)
        frame = pl_.DataFrame(
            {
                "unit_index": [r.unit for r in result.rows],
                "location": [r.location for r in result.rows],
                "level_means": [json.dumps(dict(r.level_means)) for r in result.rows],
                "selectivity_index": [r.selectivity_index for r in result.rows],
                "statistic": [r.statistic for r in result.rows],
                "p": [r.p for r in result.rows],
                "q": [r.q for r in result.rows],
                "screened": [r.screened for r in result.rows],
            },
            schema={
                "unit_index": pl_.Int64,
                "location": pl_.String,
                "level_means": pl_.String,
                "selectivity_index": pl_.Float64,
                "statistic": pl_.Float64,
                "p": pl_.Float64,
                "q": pl_.Float64,
                "screened": pl_.Boolean,
            },
        )
        constants: dict[str, object] = {
            "event_type": event_type,
            "attribute": attribute,
            "levels": json.dumps(list(result.levels)),
            "level_counts": json.dumps(dict(result.level_counts)),
            "response_start_s": result.response_window_s[0],
            "response_end_s": result.response_window_s[1],
            "n_events": result.n_events,
            "n_permutations": result.n_permutations,
            "seed": result.seed,
            "alpha": result.alpha,
            "dropped_units": json.dumps(list(result.dropped_units)),
            "defaults_version": result.defaults_version,
            "recipe_version": result.recipe_version,
            "label": result.label,
        }
        return cast(
            "pl.DataFrame",
            frame.with_columns(*(pl_.lit(v).alias(k) for k, v in constants.items())),
        )

    def band_power(
        self,
        event_type: str,
        series: str | None = None,
        channels: Sequence[int] | None = None,
        bands: Sequence[str] | None = None,
    ) -> pl.DataFrame:
        """Return event-aligned band power of `event_type`, one row per step.

        Rows are (series, channel, band, step): `time_s` (the step centre from
        onset), `mean`, `sem` and `n` events from `analysis.event_band_power`.
        `series` defaults to every series of the session whose path holds `LFP`;
        `channels` are column positions (default every column); `bands` default to
        every band of the dataset's analysis defaults. Only each event's read
        `[onset - R_pre - pad, onset + R_post + pad]` (plus one sample) is read,
        once per merged window, with the recipe's `lfp_clock_offset_s` applied
        through `analysis.lfp_onsets`. Every row carries the settings, the dropped
        and not-computable counts, `defaults_version`, `recipe_version` and the
        label "exploratory screen". Inside a `run()`, the defaults and recipe
        versions are recorded as inputs of the run.
        """
        _, pl_, an = _modules()
        defaults = an.load_defaults(self.dataset)
        chosen = self._of_type(event_type)
        _run.record_settings(
            self.dataset, defaults.version, self.recipe.version, "band_power"
        )
        settings = an.band_power_settings(
            defaults,
            self.recipe,
            event_type,
            averaging=chosen[0].averaging,
            bands=bands,
        )
        onsets = an.lfp_onsets(self.recipe, chosen)
        if series is None:
            paths = sorted({p for p in self.series["path"].to_list() if "LFP" in p})
        else:
            paths = [series]
        frames = []
        for path in paths:
            samples, clock = self._windows(path, channels, onsets, settings)
            result = an.event_band_power(samples, clock, onsets, settings)
            frames.append(_band_frame(path, channels, result))
        if not frames:
            msg = f"{self.key} has no LFP series; series: {self.series['path']}"
            raise ValueError(msg)
        constants: dict[str, object] = {
            "event_type": event_type,
            "averaging": settings.averaging,
            "baseline": settings.baseline,
            "units": settings.units,
            "pre_s": settings.pre_s,
            "post_s": settings.post_s,
            "pad_s": settings.pad_s,
            "step_s": settings.step_s,
            "lfp_clock_offset_s": float(self.recipe.lfp_clock_offset_s),
            **self._settings(defaults),
        }
        return cast(
            "pl.DataFrame",
            pl_.concat(frames).with_columns(
                *(pl_.lit(v).alias(k) for k, v in constants.items())
            ),
        )

    def _windows(
        self,
        series: str,
        channels: Sequence[int] | None,
        onsets: FloatArray,
        settings: analysis.BandPowerSettings,
    ) -> tuple[FloatArray, analysis.SampleClock]:
        """Read each onset's band-power window once, merged, with its timestamps."""
        np, _, an = _modules()
        obj: Any = _node(self.backend.open_nwb(self._series_asset(series)), series)
        total = len(obj.data)
        stamps = getattr(obj, "timestamps", None)
        clock = None if stamps is None else np.asarray(stamps[:], dtype=np.float64)
        rate = float(obj.rate) if clock is None else float(an.sample_rate(clock))
        first = float(obj.starting_time or 0.0) if clock is None else 0.0
        reach = (settings.pre_s + settings.pad_s, settings.post_s + settings.pad_s)
        spans: list[list[int]] = []
        for onset in sorted(float(x) for x in onsets):
            if clock is None:
                lo = math.floor((onset - reach[0] - first) * rate) - 1
                hi = math.ceil((onset + reach[1] - first) * rate) + 2
            else:
                lo = int(np.searchsorted(clock, onset - reach[0])) - 2
                hi = int(np.searchsorted(clock, onset + reach[1])) + 2
            lo, hi = max(lo, 0), min(hi, total)
            if hi <= lo:
                continue
            if spans and lo <= spans[-1][1]:
                spans[-1][1] = max(spans[-1][1], hi)
            else:
                spans.append([lo, hi])
        width = obj.data.shape[1] if len(obj.data.shape) > 1 else 1
        picked = list(range(width)) if channels is None else list(channels)
        blocks = [np.empty((0, len(picked)))]
        times = [np.empty(0)]
        for lo, hi in spans:
            block = np.asarray(obj.data[lo:hi], dtype=np.float64)
            block = block[:, None] if block.ndim == 1 else block
            blocks.append(block[:, picked])
            index = np.arange(lo, hi, dtype=np.float64)
            times.append(first + index / rate if clock is None else clock[lo:hi])
        return np.concatenate(blocks), an.SampleClock(timestamps=np.concatenate(times))

    def to_pynapple(self, lfp: Lfp | None = None) -> _pynapple.PynappleSession:
        """Return the session as pynapple objects (extra `pynapple`).

        `units` is a `TsGroup` of the spike trains with `location` metadata ("" when
        null), `events` maps each event type to an `IntervalSet` (a point event
        lasts one microsecond; pynapple joins overlapping epochs) and `lfp` is a
        `TsdFrame` of the `Lfp` window passed in, on the trials clock, or None.
        Without the extra it raises `ExtraMissingError`.
        """
        return _pynapple.PynappleSession(
            units=_pynapple.units_to_tsgroup(self.spikes, self._locations()),
            events=_pynapple.events_to_intervalsets(self.alignment_events),
            lfp=None if lfp is None else _pynapple.lfp_to_tsdframe(lfp),
        )

    def _of_type(self, event_type: str) -> list[analysis.AlignmentEvent]:
        """Return the events of one type.

        Raises `ValueError` naming the known types when there are none.
        """
        chosen = [e for e in self.alignment_events if e.event_type == event_type]
        if not chosen:
            known = sorted({e.event_type for e in self.alignment_events})
            msg = f"{self.key} has no {event_type} events; event types: {known}"
            raise ValueError(msg)
        return chosen


def _band_frame(
    series: str,
    channels: Sequence[int] | None,
    result: analysis.EventBandPower,
) -> pl.DataFrame:
    """Flatten one series' result to rows of (channel, band, step)."""
    np, pl_, _ = _modules()
    n_ch, n_bands, n_steps = result.mean.shape
    picked = list(range(n_ch)) if channels is None else list(channels)
    return cast(
        "pl.DataFrame",
        pl_.DataFrame(
            {
                "series": [series] * (n_ch * n_bands * n_steps),
                "channel": np.repeat(
                    np.array(picked, dtype=np.int64), n_bands * n_steps
                ),
                "band": np.tile(
                    np.repeat(np.array(result.bands, dtype=object), n_steps), n_ch
                ).tolist(),
                "time_s": np.tile(result.times_s, n_ch * n_bands),
                "mean": result.mean.reshape(-1),
                "sem": result.sem.reshape(-1),
                "n": np.repeat(result.n.reshape(-1), n_steps),
                "n_dropped": [result.n_dropped] * (n_ch * n_bands * n_steps),
                "not_computable": [json.dumps(dict(result.not_computable))]
                * (n_ch * n_bands * n_steps),
            },
            schema_overrides={"band": pl_.String},
        ),
    )


def _rows(table: pa.Table) -> list[dict[str, Any]]:
    """Return the table rows as dictionaries."""
    return cast("list[dict[str, Any]]", table.to_pylist())


def _by_asset(
    rows: Sequence[Mapping[str, object]],
) -> dict[str, list[Mapping[str, object]]]:
    """Return the rows grouped by their `asset_id`."""
    out: dict[str, list[Mapping[str, object]]] = {}
    for row in rows:
        out.setdefault(str(row["asset_id"]), []).append(row)
    return out


def _csv_rows(backend: Backend, dataset: str, path: str) -> list[dict[str, str]]:
    """Return the rows of the dataset's raw CSV at `raw/<dataset>/<path>`."""
    key = f"raw/{dataset}/{path}"
    found = _rows(backend.sql(_RAW_ASSET, [dataset, key]))
    if not found:
        msg = (
            f"the catalog has no asset with s3_key {key}; import the ancillary "
            f"file {path} (curation/imports) first"
        )
        raise ValueError(msg)
    remote = backend.open_file(str(found[0]["asset_id"]))
    text = remote.readall().decode("utf-8")
    return list(csv.DictReader(io.StringIO(text)))


def _frame_times(backend: Backend, asset_id: str, series: str) -> list[float]:
    """Return the frame timestamps of `series` in one file."""
    obj: Any = _node(backend.open_nwb(asset_id), series)
    return [float(t) for t in obj.timestamps[:]]


@dataclass(frozen=True, slots=True)
class _Rows:
    """The catalog rows of one session, grouped by asset."""

    dataset: str
    intervals: Mapping[str, list[Mapping[str, object]]]
    events: Mapping[str, list[Mapping[str, object]]]
    series_paths: Mapping[str, set[str]]


def _gather_events(
    backend: Backend, recipe: analysis.Recipe, an: ModuleType, rows: _Rows
) -> list[analysis.AlignmentEvent]:
    """Return every alignment event the recipe finds in the session's rows."""
    intervals, events, series_paths = rows.intervals, rows.events, rows.series_paths
    out: list[analysis.AlignmentEvent] = []
    csv_cache: dict[str, list[dict[str, str]]] = {}
    for asset_id in sorted({*intervals, *events, *series_paths}):
        kept = tuple(
            spec
            for spec in recipe.event_types
            if spec.source.get("kind") != "stimulus_file"
            or _norm(str(spec.source["series"])) in series_paths.get(asset_id, set())
        )
        local = dataclasses.replace(recipe, event_types=kept)
        tables: dict[str, list[dict[str, str]]] = {}
        frames: dict[str, list[float]] = {}
        for spec in kept:
            if spec.source.get("kind") != "stimulus_file":
                continue
            path, series = str(spec.source["csv"]), str(spec.source["series"])
            if path not in csv_cache:
                csv_cache[path] = _csv_rows(backend, rows.dataset, path)
            tables[path] = csv_cache[path]
            frames[series] = _frame_times(backend, asset_id, series)
        sources = an.EventSources(
            asset_id=asset_id,
            intervals=intervals.get(asset_id, []),
            events=events.get(asset_id, []),
            csv=tables,
            frames=frames,
        )
        out.extend(an.resolve_events(local, sources))
    return out


def _events_frame(
    pl_: ModuleType, found: Sequence[analysis.AlignmentEvent]
) -> pl.DataFrame:
    """Return the alignment events as a polars table."""
    frame = pl_.DataFrame(
        {
            "event_type": [e.event_type for e in found],
            "onset_s": [e.onset_s for e in found],
            "stop_s": [e.stop_s for e in found],
            "clock_asset_id": [e.clock_asset_id for e in found],
            "source_kind": [e.source.kind for e in found],
            "source_ref": [e.source.ref for e in found],
            "source_row": [e.source.row for e in found],
            "averaging": [e.averaging for e in found],
            "attributes": [
                json.dumps(dict(e.attributes), sort_keys=True) for e in found
            ],
        },
        schema={
            "event_type": pl_.String,
            "onset_s": pl_.Float64,
            "stop_s": pl_.Float64,
            "clock_asset_id": pl_.String,
            "source_kind": pl_.String,
            "source_ref": pl_.String,
            "source_row": pl_.Int64,
            "averaging": pl_.String,
            "attributes": pl_.String,
        },
    )
    return cast("pl.DataFrame", frame)


def load_session(
    dataset: str, subject: str, session: str, backend: Backend | None = None
) -> Session:
    """Load one session: its files, units, spikes and resolved events.

    `session` is the `bids_session` label, or `_` for a session with none.
    `events` come from `analysis.resolve_events(analysis.load_recipe(dataset),
    sources)`, the sources gathered from the catalog: the session's `intervals`
    and `events` rows, and, for a `stimulus_file` event type, the dataset's raw
    CSV asset (`raw/<dataset>/<csv path>`) through `open_file` and the frame
    series through `open_nwb`. Labels are not loaded. A session whose units sit in
    more than one file raises `ValueError` (unit indexes would collide).
    """
    be = backend or default_backend()
    np, pl_, an = _modules()
    scope = [dataset, subject, session]
    files = pl_.from_arrow(be.sql(_FILES, scope))
    units = pl_.from_arrow(be.sql(_UNITS, scope))
    series = pl_.from_arrow(be.sql(_SERIES, scope))
    if units["asset_id"].n_unique() > 1:
        msg = f"{dataset}/{subject}/{session} has units in several files"
        raise ValueError(msg)
    spikes: dict[int, FloatArray] = {
        int(row["unit_index"]): np.asarray(row["spike_times"] or [], dtype=np.float64)
        for row in _rows(be.sql(_SPIKES, scope))
    }
    paths: dict[str, set[str]] = {}
    for asset_id, path in zip(series["asset_id"], series["path"], strict=True):
        paths.setdefault(str(asset_id), set()).add(_norm(str(path)))
    rows = _Rows(
        dataset,
        _by_asset(_rows(be.sql(_INTERVALS, scope))),
        _by_asset(_rows(be.sql(_EVENTS, scope))),
        paths,
    )
    found = _gather_events(be, an.load_recipe(dataset), an, rows)
    return Session(
        dataset=dataset,
        subject=subject,
        session=session,
        files=files,
        units=units,
        spikes=spikes,
        events=_events_frame(pl_, found),
        recipe=an.load_recipe(dataset),
        alignment_events=tuple(found),
        series=series,
        backend=be,
    )
