"""Compute Lambda logic: the `/compute/` routes and the stored session summary.

Routes: `GET /compute/rates`, `/compute/overview`, `/compute/spectrogram`,
`/compute/events` and `/compute/tuning`; and `GET /catalog/session-summary`, which
reads the stored per-session summary from the derived bucket (`summary_read`). It is
served here because this function already reads the derived bucket and presigns its
objects.

The JWT authorizer answers 401 first; this module adds the group check (403), the
request checks (400), the not-found checks (404), the entity-key and series checks
(422), the body cap (413) and the deadline (503). The deadline is the Lambda's
remaining time at entry minus two seconds. Spikes come from the catalog's
`unit_spikes` rows through `athena_query`, or, when the asset has units but no such
rows, from the file's Units table over block-cached ranged reads (the SDK reader's
`RemoteFile` and `BlockFile`; neither imports `asteroidea/__init__`). Every binning
step is `asteroidea.analysis`; nothing here bins.

The two LFP routes read the derived bucket and never open the NWB file with
h5py: `/compute/overview` returns one series' coarse whole-recording spectrogram (JSON
when it fits the body cap, otherwise a presigned GET of the binary object), and
`/compute/spectrogram` plans exact byte ranges from the chunk index, fetches them with
a pool of `WINDOW_WORKERS` ranged GETs on the version-pinned raw object, decodes them
and runs `analysis.spectrogram`. Window limit: see `MAX_SPECTROGRAM_WINDOW_S`.

This module is the compute image's entry point only. It imports numpy and h5py at
module level (extra `compute`), so the commit image never imports it.
"""

import csv
import io
import json
import math
import os
import re
import time
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Final, Protocol

import boto3
import h5py
import numpy as np
from boto3.dynamodb.types import TypeDeserializer
from botocore.config import Config
from botocore.exceptions import ClientError

from asteroidea import analysis
from asteroidea._blocks import BlockFile
from asteroidea._remote import RemoteFile, Source
from asteroidea_ingest import (
    api_common,
    bids_path,
    lfp_build,
    lfp_index,
    lfp_overview,
    summary_read,
)
from asteroidea_ingest.api_common import (
    ApiError,
    Invocation,
    Response,
    Timing,
    serve,
)
from asteroidea_ingest.athena_query import AthenaRunner, Params

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from numpy.typing import NDArray

    from asteroidea._remote import ObjectClient
    from asteroidea_ingest.aws_types import Athena, ItemsQueryArgs, LabelReader

__all__ = ["DISPATCH", "ComputeApi", "handler"]

# The 5 MiB body binds first at 34,113 bytes a unit (raster on, MAX_BINS),
# rounded down to a multiple of 8.
MAX_UNITS: Final = 152
# Bytes one raster spike adds to the body: a trial index and a 4-decimal time, each
# with its comma ("12," and "-0.1234,"). The body pre-check multiplies it by the
# spikes `units.spike_count` puts inside the windows.
RASTER_BYTES_PER_SPIKE: Final = 12
MAX_WINDOW_S: Final = 10.0
MIN_BIN_S: Final = 0.001
MAX_BINS: Final = 2000
MAX_BODY_BYTES: Final = 5 * 1024 * 1024  # under the 6 MiB buffered Lambda limit
MAX_CSV_BYTES: Final = 5 * 1024 * 1024
DEADLINE_MARGIN_S: Final = 2.0
SIG_DIGITS: Final = 6
FILTERED: Final = 2  # an align with its optional `<column>=<value>` part
SPIKE_DECIMALS: Final = 4  # relative spike times to 0.1 ms
MAX_FIELD_LEN: Final = 256
SLOW: Final = "the computation ran out of time; use fewer units or a shorter window"

# LFP routes. The window limit is the response-size math, not an estimate:
# the body holds power_db at SPEC_BINS values per time step, two steps per window
# second (1 s segments, 50 % overlap), each value at most BYTES_PER_VALUE bytes of
# JSON ("-123.4," is 7); plus the times (10 bytes each), the frequencies, the trace
# (2,000 points, two arrays, 10 bytes each) and 1,000 bytes of keys and scalars.
#   per window second: 2 * (SPEC_BINS * 8 + 10) = 4,036 bytes
#   fixed:             SPEC_BINS * 8 + 2 * TRACE_POINTS * 10 + 1,000 = 43,008 bytes
#   (5,242,880 - 43,008) / 4,036 = 1,288 s, rounded down to 1,200 s (20 minutes).
# At 1,200 s the worst case is 4.89 MB, and the work (2,399 one-second FFTs; about 105
# ranged GETs on 8 workers for CS62's 11,443-sample chunks) stays far inside the 26 s
# deadline; test_compute_lfp.py checks the body at the limit.
SPEC_SEGMENT_S: Final = 1.0
SPEC_OVERLAP: Final = 0.5
MIN_SEGMENT_SAMPLES: Final = 2
SPEC_FMAX_HZ: Final = lfp_overview.FMAX_HZ  # same top as the overview bands
SPEC_BINS: Final = int(SPEC_FMAX_HZ / (1.0 / SPEC_SEGMENT_S)) + 1  # 0..250 Hz
TRACE_POINTS: Final = 2000
BYTES_PER_VALUE: Final = 8
MAX_SPECTROGRAM_WINDOW_S: Final = 1200.0
WINDOW_WORKERS: Final = 8
POWER_DECIMALS: Final = 1
TIME_DECIMALS: Final = 4
# Above this many overview values the JSON would pass the body cap (7 bytes a value),
# so the route hands out a presigned GET of the binary object instead.
INLINE_OVERVIEW_VALUES: Final = 500_000
PRESIGN_TTL_S: Final = 900
OVERVIEW_ROUTE: Final = "GET /compute/overview"
SPECTROGRAM_ROUTE: Final = "GET /compute/spectrogram"
EVENTS_ROUTE: Final = "GET /compute/events"
TUNING_ROUTE: Final = "GET /compute/tuning"
SESSION_SUMMARY_ROUTE: Final = "GET /catalog/session-summary"
NO_OVERVIEW: Final = "overview not built for this file version"
NO_INDEX: Final = "index not built for this file version"
# The 404 codes a client branches on before the derived LFP objects exist.
NOT_BUILT: Final = {NO_OVERVIEW: "overview_not_built", NO_INDEX: "index_not_built"}
DISPATCH: Final = {
    "GET /compute/rates": "rates",
    OVERVIEW_ROUTE: "overview",
    SPECTROGRAM_ROUTE: "spectrogram",
    EVENTS_ROUTE: "events",
    TUNING_ROUTE: "tuning",
    SESSION_SUMMARY_ROUTE: "session_summary",
}

ASSET_SQL: Final = (
    "SELECT asset_id, dataset, subject, bids_session, s3_key, s3_version_id, "
    "size_bytes, format FROM assets WHERE asset_id = ?"
)
INTERVALS_SQL: Final = (
    "SELECT row_index, start_time, columns_json FROM intervals "
    "WHERE asset_id = ? AND table_name = ? ORDER BY row_index"
)
EVENTS_SQL: Final = (
    "SELECT row_index, onset_s, trial_type, columns_json FROM events "
    "WHERE asset_id = ? ORDER BY row_index"
)
SERIES_SQL: Final = (
    "SELECT path, has_timestamps, rate_hz, starting_time_s FROM series "
    "WHERE asset_id = ? AND path = ?"
)

ALL_INTERVALS_SQL: Final = (
    "SELECT table_name, row_index, start_time, stop_time, columns_json FROM intervals "
    "WHERE asset_id = ? ORDER BY table_name, row_index"
)
# A recipe's stimulus CSV: the dataset's newest asset at `raw/<dataset>/<path>`.
STIMULUS_SQL: Final = (
    "SELECT asset_id, dataset, subject, bids_session, s3_key, s3_version_id, "
    "size_bytes, format FROM assets WHERE dataset = ? AND s3_key = ? "
    "ORDER BY committed_at DESC LIMIT 1"
)

# One parameter holds the unit list ("0,3,5"), so each text is fixed.
UNITS_SQL: Final = (
    "SELECT unit_index, spike_count, first_spike_s, last_spike_s, location "
    "FROM units "
    "WHERE asset_id = ? AND unit_index IN "
    "(SELECT CAST(u AS bigint) FROM UNNEST(split(?, ',')) AS t(u))"
)
# `all` and `<a>-<b>` resolve against every unit row of the file.
UNITS_ALL_SQL: Final = (
    "SELECT unit_index, spike_count, first_spike_s, last_spike_s, location "
    "FROM units "
    "WHERE asset_id = ? ORDER BY unit_index"
)
UNIT_RANGE: Final = re.compile(r"(\d+)-(\d+)")
SPIKES_SQL: Final = (
    "SELECT unit_index, spike_times FROM unit_spikes WHERE asset_id = ? AND "
    "unit_index IN (SELECT CAST(u AS bigint) FROM UNNEST(split(?, ',')) AS t(u))"
)


_DESER = TypeDeserializer()


class LambdaContext(Protocol):
    """The one Lambda context method this module calls."""

    def get_remaining_time_in_millis(self) -> int:
        """Return the milliseconds left before the Lambda timeout."""
        ...


type FloatArray = NDArray[np.float64]


@dataclass(frozen=True, slots=True)
class Units:
    """A `units` selection: `all`, the range `low`-`high` (inclusive) or `listed`."""

    kind: str  # "all", "range" or "list"
    listed: tuple[int, ...] = ()
    low: int = 0
    high: int = 0


@dataclass(frozen=True, slots=True)
class Request:
    """A checked `GET /compute/rates` request.

    `pre_s`, `post_s` and `bin_s` are None only for an `event:` align that omits
    them; `rates_body` fills them from the dataset's analysis defaults.
    """

    asset_id: str
    units: Units
    align: str
    pre_s: float | None
    post_s: float | None
    bin_s: float | None
    zscore: bool
    raster: bool


@dataclass(frozen=True, slots=True)
class _Typed:
    """An `event:` align's resolution: recipe, defaults, events and missing frames.

    `kept` is the named type's events after the align's attribute filter;
    `resolved` is every event the (narrowed) recipe resolved.
    """

    recipe: analysis.Recipe
    defaults: analysis.Defaults
    kept: list[analysis.AlignmentEvent]
    resolved: list[analysis.AlignmentEvent]
    missing: list[int] | None


@dataclass(frozen=True, slots=True)
class TuningRequest:
    """A checked `GET /compute/tuning` request."""

    asset_id: str
    event: str
    attribute: str
    units: Units


@dataclass(frozen=True, slots=True)
class LfpRequest:
    """A checked `GET /compute/overview` or `/compute/spectrogram` request."""

    asset_id: str
    series: str
    channel: int = 0
    start_s: float = 0.0
    duration_s: float = 0.0


@dataclass(frozen=True, slots=True)
class Align:
    """A parsed `align` value: its mode and the parts after it."""

    mode: str
    parts: tuple[str, ...]


def _param(query: Mapping[str, str], name: str) -> str:
    value = query.get(name)
    if value is None or value == "":
        raise ApiError(400, f"{name} is required")
    if len(value) > MAX_FIELD_LEN:
        raise ApiError(400, f"{name} must be at most {MAX_FIELD_LEN} characters")
    return value


def _number(query: Mapping[str, str], name: str) -> float:
    raw = _param(query, name)
    try:
        value = float(raw)
    except ValueError:
        value = math.nan
    if not math.isfinite(value):
        raise ApiError(400, f"{name} must be a finite number")
    return value


def _flag(query: Mapping[str, str], name: str, *, default: bool) -> bool:
    raw = query.get(name)
    if raw is None or raw == "":
        return default
    if raw in {"1", "true"}:
        return True
    if raw in {"0", "false"}:
        return False
    raise ApiError(400, f"{name} must be 1, 0, true or false")


def too_many_units(count: int) -> ApiError:
    """Return the 400 for a selection that resolves past `MAX_UNITS`."""
    return ApiError(
        400,
        f"units resolve to {count} units, over the bound of {MAX_UNITS}; "
        f"request a range of at most {MAX_UNITS} units",
        code="too_many_units",
    )


def parse_units(raw: str) -> Units:
    """Parse `all`, a range `<a>-<b>` (0 <= a <= b) or a comma list; 400 otherwise."""
    if raw == "all":
        return Units("all")
    if "-" in raw and "," not in raw:
        found = UNIT_RANGE.fullmatch(raw)
        if found is None or int(found[1]) > int(found[2]):
            raise ApiError(
                400,
                f"a units range is <a>-<b> with 0 <= a <= b, not {raw}",
                code="malformed_range",
            )
        return Units("range", low=int(found[1]), high=int(found[2]))
    return Units("list", listed=_units(raw))


def _units(raw: str) -> tuple[int, ...]:
    try:
        units = [int(part) for part in raw.split(",")]
    except ValueError:
        units = []
    if not units or any(u < 0 for u in units) or len(set(units)) != len(units):
        raise ApiError(
            400, "units must be distinct non-negative integers, comma-separated"
        )
    if len(units) > MAX_UNITS:
        raise too_many_units(len(units))
    return tuple(sorted(int(u) for u in units))


def parse_align(raw: str) -> Align:
    """Split an `align` value into its mode and parts; 400 when malformed."""
    mode, _, rest = raw.partition(":")
    shapes = {
        "intervals": (1, 2),
        "events": (1, 2),
        "frames": (3, 3),
        "label": (1, 1),
        "label_name": (1, 1),
        "event": (1, 2),
    }
    if mode not in shapes or not rest:
        allowed = "intervals:<table>[:<column>=<value>], events:<asset_id>"
        raise ApiError(
            400,
            f"align must be one of: {allowed}[:trial_type=<value>], "
            "frames:<csv asset_id>:<frame column>:<series path>, "
            "label:<label_id>, label_name:<name>, event:<type>[:<attribute>=<value>]",
        )
    low, high = shapes[mode]
    parts = tuple(rest.split(":", high - 1))
    if not low <= len(parts) <= high or not all(parts):
        raise ApiError(400, f"malformed align {mode}: {raw}")
    if mode == "intervals" and len(parts) == FILTERED and "=" not in parts[1]:
        raise ApiError(400, "an intervals filter is <column>=<value>")
    if (
        mode == "events"
        and len(parts) == FILTERED
        and not parts[1].startswith("trial_type=")
    ):
        raise ApiError(400, "an events filter is trial_type=<value>")
    if mode == "event" and len(parts) == FILTERED and "=" not in parts[1]:
        raise ApiError(
            400, "an event filter is <attribute>=<value>", code="malformed_align"
        )
    return Align(mode, parts)


def parse_request(query: Mapping[str, str]) -> Request:
    """Check the query string against the documented bounds; 400 on any breach.

    With an `event:` align, `pre_s`, `post_s` and `bin_s` may be omitted (each
    given one is still checked); every other align requires all three.
    """
    optional = (query.get("align") or "").partition(":")[0] == "event"
    pre_s = _window_number(query, "pre_s", optional=optional)
    post_s = _window_number(query, "post_s", optional=optional)
    bin_s = _window_number(query, "bin_s", optional=optional)
    check_window(pre_s, post_s, bin_s)
    align = _param(query, "align")
    parse_align(align)
    return Request(
        asset_id=_param(query, "asset_id"),
        units=parse_units(_param(query, "units")),
        align=align,
        pre_s=pre_s,
        post_s=post_s,
        bin_s=bin_s,
        zscore=_flag(query, "zscore", default=False),
        raster=_flag(query, "raster", default=True),
    )


def _window_number(
    query: Mapping[str, str], name: str, *, optional: bool
) -> float | None:
    """Return a window number; None when `optional` and the query omits it."""
    if optional and query.get(name) in {None, ""}:
        return None
    return _number(query, name)


def check_window(
    pre_s: float | None, post_s: float | None, bin_s: float | None
) -> None:
    """Check each given window number, and the bin count once all three are given."""
    if any(v is not None and not 0 <= v <= MAX_WINDOW_S for v in (pre_s, post_s)):
        raise ApiError(400, f"pre_s and post_s must be from 0 to {MAX_WINDOW_S:g}")
    if bin_s is not None and bin_s < MIN_BIN_S:
        raise ApiError(400, f"bin_s must be at least {MIN_BIN_S:g}")
    if pre_s is None or post_s is None or bin_s is None:
        return
    bins = round((pre_s + post_s) / bin_s)
    if bins < 1 or bins > MAX_BINS:
        raise ApiError(400, f"the window must hold from 1 to {MAX_BINS} bins")


def parse_tuning_request(query: Mapping[str, str]) -> TuningRequest:
    """Check `asset_id`, `event`, `attribute` and `units`; 400 when one is wrong."""
    return TuningRequest(
        asset_id=_param(query, "asset_id"),
        event=_param(query, "event"),
        attribute=_param(query, "attribute"),
        units=parse_units(_param(query, "units")),
    )


def parse_overview_request(query: Mapping[str, str]) -> LfpRequest:
    """Check `asset_id` and `series`; 400 when either is missing."""
    return LfpRequest(
        asset_id=_param(query, "asset_id"), series=_param(query, "series")
    )


def parse_spectrogram_request(query: Mapping[str, str]) -> LfpRequest:
    """Check the window against the limits; 400 above `MAX_SPECTROGRAM_WINDOW_S`."""
    base = parse_overview_request(query)
    try:
        channel = int(_param(query, "channel"))
    except ValueError:
        channel = -1
    if channel < 0:
        raise ApiError(400, "channel must be a non-negative integer")
    start_s = _number(query, "start_s")
    duration_s = _number(query, "duration_s")
    if start_s < 0:
        raise ApiError(400, "start_s must be at least 0")
    if not SPEC_SEGMENT_S <= duration_s <= MAX_SPECTROGRAM_WINDOW_S:
        raise ApiError(
            400,
            f"duration_s must be from {SPEC_SEGMENT_S:g} to "
            f"{MAX_SPECTROGRAM_WINDOW_S:g} seconds",
        )
    return LfpRequest(base.asset_id, base.series, channel, start_s, duration_s)


def _spikes(cell: object) -> FloatArray:
    """Sort the runner's parsed `array<double>` cell (a list) into seconds."""
    if cell is None:
        return np.empty(0, dtype=np.float64)
    if not isinstance(cell, list):
        message = f"spike_times is not a parsed array: {cell!r:.60}"
        raise TypeError(message)
    values = cell
    return np.sort(np.asarray(values, dtype=np.float64))


parse_spikes: Final = _spikes  # the live precision test uses the route's step


def _extras(cell: object) -> dict[str, object]:
    if not cell:
        return {}
    loaded: object = json.loads(str(cell))
    if not isinstance(loaded, dict):
        return {}
    return {str(k): v for k, v in loaded.items()}


def _text(value: object) -> str:
    return str(value).lower() if isinstance(value, bool) else str(value)


def _strings(query: Mapping[str, object]) -> dict[str, str]:
    return {k: str(v) for k, v in query.items()}


def _sig(values: FloatArray) -> list[float]:
    return [float(f"{v:.{SIG_DIGITS}g}") for v in values.tolist()]


def _not_built(missing: str) -> ApiError:
    """Return the 404 for an LFP object not built yet (NO_OVERVIEW or NO_INDEX)."""
    return ApiError(404, missing, code=NOT_BUILT[missing])


def _body(status: int, text: str) -> Response:
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json"},
        "body": text,
    }


@dataclass(frozen=True, slots=True)
class ComputeApi:
    """The compute routes over injected Athena and S3 clients, clock and sleep."""

    athena: Athena
    workgroup: str
    database: str
    s3: ObjectClient
    raw_bucket: str
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    derived_bucket: str = ""
    presign: Callable[[str, int], str] | None = None  # (derived key, seconds) -> URL
    labels: LabelReader | None = None  # the `label:` and `label_name:` aligns
    labels_table: str = ""

    def handle(
        self,
        event: dict[str, Any],
        remaining_ms: int,
        *,
        invocation: Invocation | None = None,
    ) -> Response:
        """Answer one HTTP API (payload v2) event; `remaining_ms` is the Lambda's."""
        at = replace(invocation or Invocation(), remaining_ms=remaining_ms)
        return serve(event, api=self, dispatch=DISPATCH, invocation=at)

    def _window(self, request: api_common.Request) -> tuple[float, float]:
        """Return the start and the deadline: remaining time less the margin."""
        started = self.clock()
        budget = max(0.0, (request.remaining_ms or 0) / 1000 - DEADLINE_MARGIN_S)
        return started, started + budget

    def rates(self, request: api_common.Request, _timing: Timing) -> Response:
        """GET /compute/rates."""
        started, deadline = self._window(request)
        return self._encode(
            self.rates_body(parse_request(_strings(request.query)), started, deadline)
        )

    def overview(self, request: api_common.Request, _timing: Timing) -> Response:
        """GET /compute/overview."""
        _, deadline = self._window(request)
        query = _strings(request.query)
        return self._encode(self._overview(parse_overview_request(query), deadline))

    def spectrogram(self, request: api_common.Request, _timing: Timing) -> Response:
        """GET /compute/spectrogram."""
        started, deadline = self._window(request)
        req = parse_spectrogram_request(_strings(request.query))
        return self._encode(self._spectrogram(req, started, deadline))

    def session_summary(self, request: api_common.Request, _timing: Timing) -> Response:
        """GET /catalog/session-summary: the stored per-session summary, or missing."""
        _, deadline = self._window(request)
        names = summary_read.parse_query(_strings(request.query))
        return self._encode(
            summary_read.read_summary(
                self.s3,
                self.derived_bucket,
                names,
                presign=self.presign,
                check=lambda: self.check(deadline),
            )
        )

    def _encode(self, payload: dict[str, Any]) -> Response:
        try:
            text = json.dumps(payload, allow_nan=False, separators=(",", ":"))
        except ValueError as err:
            raise ApiError(500, f"{type(err).__name__}: {err}") from err
        size = len(text.encode())
        if size > MAX_BODY_BYTES:
            raise ApiError(
                413,
                f"the response is {size} bytes, over the {MAX_BODY_BYTES} limit; "
                "use fewer units, a shorter window (pre_s, post_s) or raster=0",
                code="too_large",
            )
        return _body(200, text)

    def check(self, deadline: float) -> None:
        """Raise the 503 once the deadline has passed."""
        if self.clock() >= deadline:
            raise ApiError(503, SLOW, code="slow")

    def _runner(self) -> AthenaRunner:
        return AthenaRunner(
            athena=self.athena,
            workgroup=self.workgroup,
            database=self.database,
            clock=self.clock,
            sleep=self.sleep,
        )

    def events(self, request: api_common.Request, _timing: Timing) -> Response:
        """GET /compute/events: the file's resolved alignment events.

        Every event type of the dataset's recipe (`analysis.load_recipe`, else the
        default recipe) with its averaging, its events (onset, stop, attributes,
        source) and, for a stimulus-file type, the frames outside the series.
        """
        _, deadline = self._window(request)
        asset_id = _param(_strings(request.query), "asset_id")
        return self._encode(self.events_body(asset_id, deadline))

    def events_body(self, asset_id: str, deadline: float) -> dict[str, Any]:
        """Return the `/compute/events` body of one asset: its resolved event types.

        The route and the dataset summary builder both call this, so the summary's
        event counts are the route's.
        """
        found, intervals = self._runner().run_all(
            [(ASSET_SQL, [asset_id]), (ALL_INTERVALS_SQL, [asset_id])], deadline
        )
        if not found:
            raise ApiError(404, "no such asset")
        asset = found[0]
        if str(asset["format"]) != "nwb":
            raise ApiError(422, "the asset is not an NWB file", code="not_nwb")
        recipe = analysis.load_recipe(str(asset["dataset"]))
        reader = _FileReader(self, asset)
        resolved, missing = self._recipe_events(
            recipe, asset, intervals, reader, deadline
        )
        types: dict[str, dict[str, Any]] = {}
        for event in resolved:
            entry = types.setdefault(
                event.event_type,
                {"name": event.event_type, "averaging": event.averaging, "events": []},
            )
            entry["events"].append(_event_json(event))
        for name, frames in missing.items():
            entry = types.setdefault(
                name, {"name": name, "averaging": "average", "events": []}
            )
            entry["missing_frames"] = frames
        for entry in types.values():
            entry["n_events"] = len(entry["events"])
        return {
            "asset_id": asset_id,
            "dataset": asset["dataset"],
            "recipe": {
                "version": recipe.version,
                "default": recipe.default,
                "lfp_clock_offset_s": recipe.lfp_clock_offset_s,
            },
            "event_types": list(types.values()),
        }

    def tuning(self, request: api_common.Request, _timing: Timing) -> Response:
        """GET /compute/tuning: the tuning screen of one (event type, attribute)."""
        started, deadline = self._window(request)
        req = parse_tuning_request(_strings(request.query))
        return self._encode(self.tuning_body(req, started, deadline))

    def tuning_body(
        self, req: TuningRequest, started: float, deadline: float
    ) -> dict[str, Any]:
        """Resolve the units and the type's events, then run `analysis.tuning`.

        The events handed to `analysis.tuning` are the named type's, each with only
        the tested attribute and the `count_events_where` keys (so no other attribute
        of the type is shuffled), plus the `drop_units_silent_during` type's with no
        attributes (so it only drops units). A result `analysis.tuning` skips is a
        400 naming the level counts.
        """
        selection = req.units
        head = [(ASSET_SQL, [req.asset_id]), (ALL_INTERVALS_SQL, [req.asset_id])]
        if selection.kind == "list":
            listed = ",".join(map(str, selection.listed))
            found, intervals, unit_rows, spike_rows = self._runner().run_all(
                [
                    *head,
                    (UNITS_SQL, [req.asset_id, listed]),
                    (SPIKES_SQL, [req.asset_id, listed]),
                ],
                deadline,
            )
        else:
            found, intervals, unit_rows = self._runner().run_all(
                [*head, (UNITS_ALL_SQL, [req.asset_id])], deadline
            )
            spike_rows = None
        if not found:
            raise ApiError(404, "no such asset")
        asset = found[0]
        if str(asset["format"]) != "nwb":
            raise ApiError(422, "the asset is not an NWB file", code="not_nwb")
        wanted, rows = _resolve_units(selection, unit_rows)
        defaults = analysis.load_defaults(str(asset["dataset"]))
        phase = defaults.tests.get("drop_units_silent_during")
        also = () if phase is None else (str(phase),)
        reader = _FileReader(self, asset)
        recipe, kept, resolved, missing = self._typed_events(
            (req.event, *also), asset, intervals, reader, deadline
        )
        events = _tuning_events(req, kept, resolved, defaults, also)
        if spike_rows is None:
            spike_rows = self._runner().run(
                SPIKES_SQL, [req.asset_id, ",".join(map(str, wanted))], deadline
            )
        by_unit, source = self._unit_spikes(spike_rows, wanted, reader, deadline)
        queried = self.clock()
        self.check(deadline)
        locations = {
            int(str(r["unit_index"])): str(r["location"])
            for r in rows
            if r.get("location") is not None
        }
        try:
            results = analysis.tuning(by_unit, events, recipe, defaults, locations)
        except ValueError as err:
            raise ApiError(
                422, f"the screen does not resolve: {err}", code="screen_unresolved"
            ) from err
        self.check(deadline)
        (result,) = [
            r
            for r in results
            if r.event_type == req.event and r.attribute == req.attribute
        ]
        if result.skipped is not None:
            raise _too_few_events(req, result, defaults)
        payload = _tuning_json(req, result, defaults, source)
        if (frames := missing.get(req.event)) is not None:
            payload["missing_frames"] = frames
        payload["query_ms"] = int((queried - started) * 1000)
        payload["compute_ms"] = int((self.clock() - queried) * 1000)
        return payload

    def _unit_spikes(
        self,
        spike_rows: list[dict[str, object]],
        wanted: tuple[int, ...],
        reader: _FileReader,
        deadline: float,
    ) -> tuple[dict[int, FloatArray], str]:
        """Return the wanted units' spikes: the catalog's rows, else the file's."""
        self.check(deadline)
        by_unit = {
            int(str(r["unit_index"])): _spikes(r["spike_times"]) for r in spike_rows
        }
        if all(u in by_unit for u in wanted):
            return {u: by_unit[u] for u in wanted}, "catalog"
        return reader.units(wanted, deadline), "file"

    def rates_body(
        self, req: Request, started: float, deadline: float
    ) -> dict[str, Any]:
        """Bin the units' spikes around the align's onsets (`analysis.rates`).

        With an `event:` align the body also carries `label`, `defaults` (the
        values used, `pre_s`, `post_s` and `bin_s` filled from
        `analysis.load_defaults` where the request omits them) and `responsive`
        (`analysis.responsive` over the same events and units); see
        `_event_screen`. Other aligns return the keys they always did.
        """
        align = parse_align(req.align)
        selection = req.units
        head = [(ASSET_SQL, [req.asset_id])]
        if selection.kind == "list":
            listed = ",".join(map(str, selection.listed))
            queries = [
                *head,
                (UNITS_SQL, [req.asset_id, listed]),
                (SPIKES_SQL, [req.asset_id, listed]),
                *self._align_queries(req, align),
            ]
            found, unit_rows, spike_rows, *extra = self._runner().run_all(
                queries, deadline
            )
        else:
            queries = [
                *head,
                (UNITS_ALL_SQL, [req.asset_id]),
                *self._align_queries(req, align),
            ]
            found, unit_rows, *extra = self._runner().run_all(queries, deadline)
            spike_rows = None
        if not found:
            raise ApiError(404, "no such asset")
        asset = found[0]
        wanted, counts = _resolve_units(selection, unit_rows)
        reader = _FileReader(self, asset)
        typed: _Typed | None = None
        if align.mode == "event":
            typed = self._event_type_events(align, asset, extra[0], reader, deadline)
            req = _with_defaults(req, typed.defaults)
            onsets = _onsets_from(typed.kept)
            missing_frames = typed.missing
        else:
            onsets, missing_frames = self._onsets(align, asset, extra, reader, deadline)
        pre_s, post_s, bin_s = _window_of(req)
        if req.raster:
            _precheck_body(counts, onsets, pre_s, post_s)
        if spike_rows is None:
            listed = ",".join(map(str, wanted))
            spike_rows = self._runner().run(
                SPIKES_SQL, [req.asset_id, listed], deadline
            )
        queried = self.clock()
        by_unit, source = self._unit_spikes(spike_rows, wanted, reader, deadline)
        self.check(deadline)
        result = analysis.rates(by_unit, onsets, pre_s, post_s, bin_s)
        payload: dict[str, Any] = {
            "edges": _sig(result.edges),
            "units": _unit_entries(req, result, by_unit, onsets),
            "n_trials": result.n_trials,
            "align": req.align,
            "source": source,
        }
        if missing_frames is not None:
            payload["missing_frames"] = missing_frames
        if typed is not None:
            payload.update(_event_screen(align, req, typed, by_unit, counts))
            self.check(deadline)
        payload["query_ms"] = int((queried - started) * 1000)
        payload["compute_ms"] = int((self.clock() - queried) * 1000)
        return payload

    def _derived(self, key: str) -> bytes | None:
        """Return a derived object's bytes, or None when the key does not exist."""
        try:
            reply = self.s3.get_object(Bucket=self.derived_bucket, Key=key)
        except ClientError as err:
            if err.response.get("Error", {}).get("Code") in {"NoSuchKey", "404"}:
                return None
            raise
        return reply["Body"].read()

    def _lfp_entry(
        self, req: LfpRequest, deadline: float, missing: str
    ) -> tuple[dict[str, object], dict[str, Any], dict[str, Any]]:
        """Return the asset row, the manifest and the series' manifest entry."""
        (found,) = self._runner().run_all([(ASSET_SQL, [req.asset_id])], deadline)
        if not found:
            raise ApiError(404, "no such asset")
        asset = found[0]
        blob = self._derived(
            lfp_build.manifest_key(req.asset_id, str(asset["s3_version_id"]))
        )
        if blob is None:
            raise _not_built(missing)
        manifest: dict[str, Any] = json.loads(blob)
        path = "/" + req.series.strip("/")
        for entry in manifest["series"]:
            if entry["path"] == path:
                return asset, manifest, entry
        for skipped in manifest["skipped"]:
            if skipped["path"] == path:
                reason = str(skipped["reason"])
                if "timestamps series" in reason:
                    raise ApiError(422, f"the series has no uniform rate: {reason}")
                raise ApiError(404, f"series not indexed: {reason}")
        raise ApiError(404, f"no such series in the file's manifest: {path}")

    def _overview(self, req: LfpRequest, deadline: float) -> dict[str, Any]:
        """Serve the overview as JSON when it fits the cap, else a presigned GET."""
        _, manifest, entry = self._lfp_entry(req, deadline, NO_OVERVIEW)
        shape = [int(n) for n in entry["overview_shape"]]
        meta = {
            "built_at": manifest.get("built_at"),
            "builder_version": manifest["builder_version"],
        }
        if shape[0] * shape[1] * shape[2] <= INLINE_OVERVIEW_VALUES:
            blob = self._derived(str(entry["overview_key"]))
            if blob is None:
                raise _not_built(NO_OVERVIEW)
            view = lfp_overview.loads(blob)
            payload: dict[str, Any] = {
                "times_s": np.round(view.times, TIME_DECIMALS).tolist(),
                "band_edges_hz": _sig(view.edges),
                "channels": list(range(view.db.shape[0])),
                "power_db": np.round(
                    view.db.astype(np.float64), POWER_DECIMALS
                ).tolist(),
                "rate_hz": view.rate_hz,
                "starting_time_s": view.starting_time,
                **meta,
            }
            size = len(json.dumps(payload, allow_nan=False, separators=(",", ":")))
            if size <= MAX_BODY_BYTES:
                return payload
        if self.presign is None:
            raise ApiError(500, "no presigner is configured")
        key = str(entry["overview_key"])
        return {
            "url": self.presign(key, PRESIGN_TTL_S),
            "expires_in": PRESIGN_TTL_S,
            "format": f"ASTLFPOV v{lfp_overview.FORMAT_VERSION}",
            "bytes": entry["overview_bytes"],
            "shape": shape,
            "rate_hz": entry["rate_hz"],
            "starting_time_s": entry["starting_time"],
            **meta,
        }

    def _spectrogram(
        self, req: LfpRequest, started: float, deadline: float
    ) -> dict[str, Any]:
        """Compute one channel's window from exact byte ranges; no h5py open."""
        asset, _, entry = self._lfp_entry(req, deadline, NO_INDEX)
        blob = self._derived(str(entry["index_key"]))
        if blob is None:
            raise _not_built(NO_INDEX)
        looked_up = self.clock()
        try:
            index = lfp_index.loads(blob)
            if not (math.isfinite(index.rate_hz) and index.rate_hz > 0):
                raise ApiError(422, "the series has no uniform sampling rate")
            if round(SPEC_SEGMENT_S * index.rate_hz) < MIN_SEGMENT_SAMPLES:
                raise ApiError(422, "the sampling rate is too low to analyse")
            ranges = lfp_index.plan_window(
                index, req.channel, req.start_s, req.duration_s
            )
            source = Source(
                bucket=self.raw_bucket,
                key=str(asset["s3_key"]),
                version_id=str(asset["s3_version_id"]),
                size=int(str(asset["size_bytes"])),
            )
            fetcher = lfp_build.RangeFetcher(source, self.s3, WINDOW_WORKERS)
            fetched = fetcher.fetch(ranges)
            self.check(deadline)
            raw = lfp_index.decode_window(
                index, fetched, req.channel, req.start_s, req.duration_s
            )
        except lfp_index.WindowError as err:
            raise ApiError(400, str(err)) from err
        except lfp_index.LfpIndexError as err:
            raise ApiError(500, f"{type(err).__name__}: {err}") from err
        fetch_done = self.clock()
        samples = raw.astype(np.float64) * index.conversion
        if samples.size < round(SPEC_SEGMENT_S * index.rate_hz):
            raise ApiError(400, "the window holds fewer samples than one segment")
        spec = analysis.spectrogram(
            samples, index.rate_hz, SPEC_SEGMENT_S, SPEC_OVERLAP
        )
        keep = spec.freqs <= SPEC_FMAX_HZ
        t0 = round(req.start_s * index.rate_hz) / index.rate_hz
        trace = analysis.decimate_minmax(
            samples + index.offset, index.rate_hz, TRACE_POINTS
        )
        self.check(deadline)
        return {
            "freqs_hz": np.round(spec.freqs[keep], TIME_DECIMALS).tolist(),
            "times_s": np.round(t0 + spec.times, TIME_DECIMALS).tolist(),
            "power_db": np.round(spec.power_db[keep], POWER_DECIMALS).tolist(),
            "trace": {
                "times_s": np.round(t0 + trace.times, TIME_DECIMALS).tolist(),
                "values": _sig(trace.samples),
            },
            "rate_hz": index.rate_hz,
            "starting_time_s": index.starting_time,
            "bytes_read": fetcher.bytes_fetched,
            "requests": fetcher.requests,
            "lookup_ms": int((looked_up - started) * 1000),
            "fetch_ms": int((fetch_done - looked_up) * 1000),
            "compute_ms": int((self.clock() - fetch_done) * 1000),
        }

    def _align_queries(self, req: Request, align: Align) -> list[tuple[str, Params]]:
        if align.mode == "intervals":
            return [(INTERVALS_SQL, [req.asset_id, align.parts[0]])]
        if align.mode == "events":
            return [(ASSET_SQL, [align.parts[0]]), (EVENTS_SQL, [align.parts[0]])]
        if align.mode == "event":
            return [(ALL_INTERVALS_SQL, [req.asset_id])]
        if align.mode in {"label", "label_name"}:
            return []  # read from DynamoDB in `_label_rows`, not Athena
        csv_id, _, path = align.parts
        return [(ASSET_SQL, [csv_id]), (SERIES_SQL, [req.asset_id, path])]

    def _onsets(
        self,
        align: Align,
        asset: dict[str, object],
        extra: list[list[dict[str, object]]],
        reader: _FileReader,
        deadline: float,
    ) -> tuple[FloatArray, list[int] | None]:
        """Return the align's onsets through `analysis.resolve_events`.

        The old forms (`intervals:`, `events:`, `frames:`, `label:`, `label_name:`)
        keep their checks and filters here and hand the kept rows to the resolver as
        one ad hoc event type; `event:` resolves the dataset's recipe in
        `_event_type_events` instead.
        """
        asset_id = str(asset["asset_id"])
        if align.mode == "frames":
            return self._frame_onsets(align, asset_id, extra, reader, deadline)
        if align.mode == "intervals":
            table = align.parts[0]
            rows = extra[0]
            if not rows:
                raise ApiError(404, f"no intervals table {table} for the asset")
            wanted = align.parts[1] if len(align.parts) == FILTERED else None
            kept = _filter(rows, wanted, "columns_json", "intervals")
            spec: dict[str, object] = {"kind": "interval", "table": table}
            sources = analysis.EventSources(
                asset_id, intervals=[{**r, "table_name": table} for r in kept]
            )
        elif align.mode == "events":
            spec = {"kind": "bids_event"}
            sources = analysis.EventSources(
                asset_id, events=self._event_rows(align, asset, extra)
            )
        else:
            field = "label_id" if align.mode == "label" else "name"
            spec = {"kind": "label", field: align.parts[0]}
            sources = analysis.EventSources(
                asset_id, labels=self._label_rows(align, asset_id)
            )
        return _onsets_of(_ad_hoc(align, spec), sources), None

    def _event_type_events(
        self,
        align: Align,
        asset: dict[str, object],
        intervals: list[dict[str, object]],
        reader: _FileReader,
        deadline: float,
    ) -> _Typed:
        """Resolve `event:<type>[:<attribute>=<value>]` against the shipped recipe.

        The dataset's analysis defaults are loaded here; the recipe also resolves
        their `drop_units_silent_during` type, which `analysis.responsive` needs to
        drop silent units.
        """
        name = align.parts[0]
        defaults = analysis.load_defaults(str(asset["dataset"]))
        recipe, kept, resolved, missing = self._typed_events(
            (name, *_drop_phase(defaults)), asset, intervals, reader, deadline
        )
        if len(align.parts) == FILTERED:
            attribute, _, value = align.parts[1].partition("=")
            if not any(attribute in e.attributes for e in kept):
                raise ApiError(
                    404,
                    f"event type {name} has no attribute {attribute}",
                    code="no_such_attribute",
                )
            kept = [
                e
                for e in kept
                if attribute in e.attributes and _text(e.attributes[attribute]) == value
            ]
        return _Typed(recipe, defaults, kept, resolved, missing.get(name))

    def _typed_events(
        self,
        names: tuple[str, ...],
        asset: dict[str, object],
        intervals: list[dict[str, object]],
        reader: _FileReader,
        deadline: float,
    ) -> tuple[
        analysis.Recipe,
        list[analysis.AlignmentEvent],
        list[analysis.AlignmentEvent],
        dict[str, list[int]],
    ]:
        """Resolve the event type `names[0]` of the dataset's recipe; 404 when none.

        A shipped recipe is narrowed to `names` (the type and any type it also
        needs) before it resolves, so only their sources are read. Returns the
        (narrowed) recipe, the type's events, every resolved event and the frames
        outside each series.
        """
        name = names[0]
        recipe = analysis.load_recipe(str(asset["dataset"]))
        if not recipe.default:
            known = [t.name for t in recipe.event_types]
            if name not in known:
                raise _no_event_type(name, known)
            wanted = set(names)
            recipe = replace(
                recipe,
                event_types=tuple(t for t in recipe.event_types if t.name in wanted),
            )
        resolved, missing = self._recipe_events(
            recipe, asset, intervals, reader, deadline
        )
        kept = [e for e in resolved if e.event_type == name]
        if recipe.default and not kept:
            raise _no_event_type(name, sorted({e.event_type for e in resolved}))
        return recipe, kept, resolved, missing

    def _recipe_events(
        self,
        recipe: analysis.Recipe,
        asset: dict[str, object],
        intervals: list[dict[str, object]],
        reader: _FileReader,
        deadline: float,
    ) -> tuple[list[analysis.AlignmentEvent], dict[str, list[int]]]:
        """Gather a recipe's rows (CSV, frame series, labels) and resolve its events.

        Returns the events and, per stimulus-file type, the frames outside the
        series. A recipe the rows cannot satisfy is a 422 naming the resolver's
        sentence.
        """
        asset_id = str(asset["asset_id"])
        dataset = str(asset["dataset"])
        csv_rows: dict[str, list[dict[str, str]]] = {}
        frames: dict[str, list[float]] = {}
        missing: dict[str, list[int]] = {}
        labels: list[dict[str, object]] = []
        kinds = {str(spec.source["kind"]) for spec in recipe.event_types}
        if "label" in kinds:
            labels = self._label_rows(None, asset_id)
        for spec in recipe.event_types:
            if str(spec.source["kind"]) != "stimulus_file":
                continue
            path = str(spec.source["csv"])
            series = str(spec.source["series"])
            column = str(spec.source["frame_column"])
            base = int(str(spec.source.get("frame_base", 1)))
            found, series_rows = self._runner().run_all(
                [
                    (STIMULUS_SQL, [dataset, f"raw/{dataset}/{path}"]),
                    (SERIES_SQL, [asset_id, "/" + series.strip("/")]),
                ],
                deadline,
            )
            if not found:
                raise ApiError(
                    404,
                    f"the recipe's stimulus file {path} is not in the catalog",
                    code="no_stimulus_file",
                )
            if not series_rows:
                raise ApiError(404, f"no such series: {series}")
            raw = self._csv_bytes(found[0])
            numbers = _csv_frames(raw, column)
            self.check(deadline)
            stamps = reader.timestamps(series_rows[0])
            csv_rows[path] = _csv_rows(raw, column)
            frames[series] = stamps.tolist()
            out = analysis.frame_onsets(numbers, stamps, base=base)
            missing[spec.name] = [int(f) for f in out.out_of_range.tolist()]
        sources = analysis.EventSources(
            asset_id, intervals=intervals, labels=labels, csv=csv_rows, frames=frames
        )
        try:
            resolved = analysis.resolve_events(recipe, sources)
        except (ValueError, TypeError) as err:
            raise ApiError(
                422, f"the recipe does not resolve: {err}", code="recipe_unresolved"
            ) from err
        return resolved, missing

    def _event_rows(
        self,
        align: Align,
        asset: dict[str, object],
        extra: list[list[dict[str, object]]],
    ) -> list[dict[str, object]]:
        """Return the `events:` rows after the entity and `trial_type` checks."""
        events_asset, rows = extra
        if not events_asset:
            raise ApiError(404, "no such events asset")
        if not _same_recording(asset, events_asset[0]):
            raise ApiError(
                422,
                "the events asset must have the same dataset, subject, session "
                "and file name entities as the target",
            )
        if not rows:
            raise ApiError(404, "the events asset has no event rows")
        wanted = align.parts[1] if len(align.parts) == FILTERED else None
        kept = _filter(rows, wanted, "trial_type", "events")
        return [{**r, "events_for": align.parts[0]} for r in kept]

    def _label_rows(
        self, align: Align | None, asset_id: str
    ) -> list[dict[str, object]]:
        """Return the asset's label rows, `start_s` and `stop_s` as floats.

        `label:<id>` is one trial; `label_name:<name>` is every label of that name on
        the asset, whoever wrote it. A label's `stop_s` is ignored by the
        align: alignment uses the start only. The asset filter is the label table's
        partition key and is checked again on each item, so a label of another asset
        never aligns this one. With an align, no matching label is a 404.
        """
        if self.labels is None:
            raise ApiError(500, "the labels table is not configured")
        rows: list[dict[str, object]] = [
            {
                "label_id": str(item["label_id"]),
                "name": str(item["name"]),
                "start_s": _seconds(item["start_s"]),
                "stop_s": _seconds(item.get("stop_s")),
            }
            for item in _label_items(self.labels, self.labels_table, asset_id)
            if str(item["asset_id"]) == asset_id
        ]
        if align is None:
            return rows
        wanted = align.parts[0]
        field = "label_id" if align.mode == "label" else "name"
        if not any(r[field] == wanted for r in rows):
            raise ApiError(404, f"no label with {field} {wanted} on the asset")
        return rows

    def _frame_onsets(
        self,
        align: Align,
        asset_id: str,
        extra: list[list[dict[str, object]]],
        reader: _FileReader,
        deadline: float,
    ) -> tuple[FloatArray, list[int]]:
        csv_asset, series = extra
        _, column, path = align.parts
        if not csv_asset:
            raise ApiError(404, "no such CSV asset")
        if not series:
            raise ApiError(404, f"no such series: {path}")
        raw = self._csv_bytes(csv_asset[0])
        numbers = _csv_frames(raw, column)
        self.check(deadline)
        stamps = reader.timestamps(series[0])
        out = analysis.frame_onsets(numbers, stamps, base=1)
        spec: dict[str, object] = {
            "kind": "stimulus_file",
            "csv": "align",
            "series": path,
            "frame_column": column,
            "frame_base": 1,
        }
        sources = analysis.EventSources(
            asset_id,
            csv={"align": _csv_rows(raw, column)},
            frames={path: stamps.tolist()},
        )
        return (
            _onsets_of(_ad_hoc(align, spec), sources),
            [int(f) for f in out.out_of_range.tolist()],
        )

    def _csv_bytes(self, asset: dict[str, object]) -> bytes:
        size = int(str(asset["size_bytes"]))
        if size > MAX_CSV_BYTES:
            raise ApiError(413, f"the CSV is {size} bytes, over {MAX_CSV_BYTES}")
        return self.remote(asset).readall()

    def remote(self, asset: dict[str, object]) -> RemoteFile:
        """Return the version-pinned ranged reader of one catalogued object."""
        source = Source(
            bucket=self.raw_bucket,
            key=str(asset["s3_key"]),
            version_id=str(asset["s3_version_id"]),
            size=int(str(asset["size_bytes"])),
        )
        return RemoteFile(source, lambda: self.s3, lambda: self.s3)


def _label_items(
    store: LabelReader, table: str, asset_id: str
) -> list[dict[str, object]]:
    """Return every label item of the asset (partition key), following 1 MB pages."""
    out: list[dict[str, object]] = []
    start: dict[str, Any] | None = None
    while True:
        args: ItemsQueryArgs = {
            "TableName": table,
            "KeyConditionExpression": "asset_id = :a",
            "ExpressionAttributeValues": {":a": {"S": asset_id}},
            "ConsistentRead": True,
        }
        if start is not None:
            args["ExclusiveStartKey"] = start
        page = store.query(**args)
        out.extend(
            {k: _DESER.deserialize(v) for k, v in item.items()}
            for item in page.get("Items", [])
        )
        start = page.get("LastEvaluatedKey")
        if start is None:
            return out


def _ad_hoc(align: Align, source: Mapping[str, object]) -> analysis.Recipe:
    """Return a one-type recipe for an old align form over pre-filtered rows."""
    return analysis.Recipe(
        dataset="",
        description=f"align {align.mode}",
        event_types=(
            analysis.EventTypeSpec(
                name=align.mode, averaging="average", source=source, attributes=()
            ),
        ),
        version="align",
    )


def _onsets_of(recipe: analysis.Recipe, sources: analysis.EventSources) -> FloatArray:
    """Return the resolved onsets; a resolver refusal is a 422 naming it."""
    try:
        events = analysis.resolve_events(recipe, sources)
    except (ValueError, TypeError) as err:
        raise ApiError(
            422, f"the align does not resolve: {err}", code="recipe_unresolved"
        ) from err
    return np.asarray([e.onset_s for e in events], dtype=np.float64)


def _unit_entries(
    req: Request,
    result: analysis.Rates,
    by_unit: Mapping[int, FloatArray],
    onsets: FloatArray,
) -> list[dict[str, Any]]:
    """Return the body's `units`: per unit its raster (unless off), rates and z."""
    pre_s, post_s, _ = _window_of(req)
    z = analysis.zscore(result.rate_hz, result.edges) if req.zscore else None
    units: list[dict[str, Any]] = []
    for row, unit in enumerate(result.units):
        entry: dict[str, Any] = {"unit_index": unit}
        if req.raster:
            hits = analysis.align(by_unit[unit], onsets, pre_s, post_s)
            entry["raster"] = {
                "trial": hits.trial.tolist(),
                "t": np.round(hits.t, SPIKE_DECIMALS).tolist(),
            }
        entry["rate_hz"] = _sig(result.rate_hz[row])
        if z is not None:
            zrow = z[row]
            entry["z"] = None if zrow is None else _sig(zrow)
        units.append(entry)
    return units


def _onsets_from(events: list[analysis.AlignmentEvent]) -> FloatArray:
    return np.asarray([e.onset_s for e in events], dtype=np.float64)


def _drop_phase(defaults: analysis.Defaults) -> tuple[str, ...]:
    """Return the `drop_units_silent_during` type of `defaults`, if it names one."""
    phase = defaults.tests.get("drop_units_silent_during")
    return () if phase is None else (str(phase),)


def _window_of(req: Request) -> tuple[float, float, float]:
    """Return `(pre_s, post_s, bin_s)` of a request whose window is filled."""
    if req.pre_s is None or req.post_s is None or req.bin_s is None:
        raise ApiError(500, "the rates window is not filled")
    return req.pre_s, req.post_s, req.bin_s


def defaults_block(defaults: analysis.Defaults) -> str:
    """Return the dataset block name `defaults` used, or "default" when none applied.

    A dataset with no block loads equal to the convention alone.
    """
    if replace(defaults, dataset="") == analysis.load_defaults():
        return "default"
    return defaults.dataset


def _with_defaults(req: Request, defaults: analysis.Defaults) -> Request:
    """Fill the omitted `pre_s`, `post_s` and `bin_s` from `defaults.rates`.

    An explicit value wins. A filled window the bounds refuse is a 400
    `defaults_window` naming the filled values.
    """
    filled: dict[str, float] = {}
    for name in ("pre_s", "post_s", "bin_s"):
        if getattr(req, name) is None:
            filled[name] = float(str(defaults.rates[name]))
    if not filled:
        return req
    out = replace(req, **filled)
    try:
        check_window(out.pre_s, out.post_s, out.bin_s)
    except ApiError as err:
        named = ", ".join(f"{k} {v:g}" for k, v in filled.items())
        raise ApiError(
            400,
            f"{err.detail}; filled from the {defaults_block(defaults)} defaults: "
            f"{named}",
            code="defaults_window",
        ) from err
    return out


def _event_screen(
    align: Align,
    req: Request,
    typed: _Typed,
    by_unit: Mapping[int, FloatArray],
    rows: list[dict[str, object]],
) -> dict[str, Any]:
    """Return the `event:` align's `label`, `defaults` and `responsive` keys.

    `responsive` is `analysis.responsive` over the align's events (after its
    attribute filter) and the request's units, with the drop-phase type's events
    attribute-free beside them; its windows are the defaults' and the recipe's,
    not the request's `pre_s` and `post_s`. Units the drop phase removes have no
    row; a `single` type (or no event) gives an empty list. A screen the
    defaults cannot run is a 422 `screen_unresolved`.
    """
    name = align.parts[0]
    defaults = typed.defaults
    also = set(_drop_phase(defaults)) - {name}
    events = [
        *typed.kept,
        *(replace(e, attributes={}) for e in typed.resolved if e.event_type in also),
    ]
    locations = {
        int(str(r["unit_index"])): str(r["location"])
        for r in rows
        if r.get("location") is not None
    }
    try:
        results = analysis.responsive(
            by_unit, events, typed.recipe, defaults, locations
        )
    except ValueError as err:
        raise ApiError(
            422, f"the screen does not resolve: {err}", code="screen_unresolved"
        ) from err
    found = [r for r in results if r.event_type == name]
    window = (
        list(found[0].response_window_s)
        if found
        else _seq(defaults.rates.get("response_window_s"))
    )
    pre_s, post_s, bin_s = _window_of(req)
    return {
        "label": analysis.SCREEN_LABEL,
        "defaults": {
            "version": defaults.version,
            "block": defaults_block(defaults),
            "source": defaults.source,
            "baseline": defaults.rates.get("baseline"),
            "bin_s": bin_s,
            "pre_s": pre_s,
            "post_s": post_s,
            "response_window_s": window,
        },
        "responsive": [
            {
                "unit_index": row.unit,
                "location": row.location,
                "p": row.p,
                "q": row.q,
                "screened": row.screened,
            }
            for result in found
            for row in result.rows
        ],
    }


def _seq(value: object) -> list[object]:
    return list(value) if isinstance(value, list | tuple) else []


def _tuning_events(
    req: TuningRequest,
    kept: list[analysis.AlignmentEvent],
    resolved: list[analysis.AlignmentEvent],
    defaults: analysis.Defaults,
    also: tuple[str, ...],
) -> list[analysis.AlignmentEvent]:
    """Return the events `analysis.tuning` reads for one (type, attribute).

    The type's events keep only the attribute and the `count_events_where` keys;
    the `also` types' events (the silent-unit phase) keep none. 400 when the type
    is not an `average` type or no event of it carries the attribute.
    """
    if kept and kept[0].averaging != "average":
        raise ApiError(
            400,
            f"event type {req.event} is {kept[0].averaging}; tuning needs an "
            "average type",
            code="not_average",
        )
    if not any(req.attribute in e.attributes for e in kept):
        known = sorted({a for e in kept for a in e.attributes})
        raise ApiError(
            400,
            f"event type {req.event} has no attribute {req.attribute}; "
            f"known: {', '.join(known) or 'none'}",
            code="no_such_attribute",
        )
    where = defaults.tests.get("count_events_where")
    keys = {req.attribute, *(where if isinstance(where, dict) else {})}
    events = [
        replace(e, attributes={k: v for k, v in e.attributes.items() if k in keys})
        for e in kept
    ]
    events.extend(
        replace(e, attributes={})
        for e in resolved
        if e.event_type in also and e.event_type != req.event
    )
    return events


def _too_few_events(
    req: TuningRequest, result: analysis.TuningResult, defaults: analysis.Defaults
) -> ApiError:
    """Return the 400 for an attribute with fewer than 2 levels at the minimum."""
    minimum = defaults.tests.get("min_events_per_level")
    counts = ", ".join(f"{k} {n}" for k, n in result.level_counts.items())
    return ApiError(
        400,
        f"attribute {req.attribute} of {req.event} needs at least 2 levels of "
        f"{minimum} counted events; counts: {counts or 'none'}",
        code="too_few_events_per_level",
    )


def _round(value: float) -> float:
    return float(f"{value:.{SIG_DIGITS}g}")


def _tuning_json(
    req: TuningRequest,
    result: analysis.TuningResult,
    defaults: analysis.Defaults,
    source: str,
) -> dict[str, Any]:
    """Return the route body: per unit, per location, the settings and the label."""
    n_per_level = {lv: result.level_counts[lv] for lv in result.levels}
    tests = defaults.tests
    return {
        "label": result.label,
        "asset_id": req.asset_id,
        "event_type": result.event_type,
        "attribute": result.attribute,
        "units": [
            {
                "unit_index": row.unit,
                "location": row.location,
                "level_means_hz": {k: _round(v) for k, v in row.level_means.items()},
                "n_per_level": n_per_level,
                "selectivity_index": _round(row.selectivity_index),
                "statistic": _round(row.statistic),
                "p": row.p,
                "q": row.q,
                "screened": row.screened,
            }
            for row in result.rows
        ],
        "areas": [
            {
                "location": area.location,
                "n_units": area.n_units,
                "n_below_alpha": area.n_below_alpha,
                "fraction": area.fraction,
                "p": area.p,
            }
            for area in result.areas
        ],
        "settings": {
            "defaults_version": result.defaults_version,
            "recipe_version": result.recipe_version,
            "response_window_s": list(result.response_window_s),
            "levels": list(result.levels),
            "level_counts": dict(result.level_counts),
            "n_events": result.n_events,
            "n_permutations": result.n_permutations,
            "seed": result.seed,
            "alpha": result.alpha,
            "fdr": tests.get("fdr"),
            "min_events_per_level": tests.get("min_events_per_level"),
            "count_events_where": tests.get("count_events_where"),
            "drop_units_silent_during": tests.get("drop_units_silent_during"),
            "dropped_units": list(result.dropped_units),
        },
        "source": source,
    }


def _no_event_type(name: str, known: list[str]) -> ApiError:
    return ApiError(
        404,
        f"no event type {name} for the asset; known: {', '.join(known) or 'none'}",
        code="no_such_event_type",
    )


def _seconds(value: object) -> float | None:
    """Return a DynamoDB number (a Decimal) or a float as a float; None stays None."""
    if value is None:
        return None
    if isinstance(value, Decimal | int | float):
        return float(value)
    return float(str(value))


def _event_json(event: analysis.AlignmentEvent) -> dict[str, Any]:
    return {
        "onset_s": event.onset_s,
        "stop_s": event.stop_s,
        "attributes": dict(event.attributes),
        "source": {
            "kind": event.source.kind,
            "ref": event.source.ref,
            "row": event.source.row,
        },
    }


def _resolve_units(
    selection: Units, rows: list[dict[str, object]]
) -> tuple[tuple[int, ...], list[dict[str, object]]]:
    """Resolve a selection against the file's unit rows; 404 missing, 400 too many.

    Returns the unit indices and their rows (for the body pre-check).
    """
    by_index = {int(str(r["unit_index"])): r for r in rows}
    if selection.kind == "list":
        if missing := [u for u in selection.listed if u not in by_index]:
            raise ApiError(404, f"no such unit: {missing}")
        wanted = selection.listed
    elif selection.kind == "range":
        wanted = tuple(
            u for u in sorted(by_index) if selection.low <= u <= selection.high
        )
        if not wanted:
            raise ApiError(
                404, f"no unit in the range {selection.low}-{selection.high}"
            )
    else:
        wanted = tuple(sorted(by_index))
        if not wanted:
            raise ApiError(404, "the file has no units")
    if len(wanted) > MAX_UNITS:
        raise too_many_units(len(wanted))
    return wanted, [by_index[u] for u in wanted]


def _precheck_body(
    rows: list[dict[str, object]], onsets: FloatArray, pre_s: float, post_s: float
) -> None:
    """Refuse (413) a raster whose spikes alone would pass `MAX_BODY_BYTES`.

    Each unit's raster spikes are estimated from `units.spike_count` at a uniform
    rate over `[first_spike_s, last_spike_s]`: the rate times each trial window's
    overlap with that span, summed over trials. A unit without those columns counts
    0. The estimate is not a bound; `_encode` stays the exact cap.
    """
    marks = np.asarray(onsets, dtype=np.float64)
    estimate = 0.0
    for row in rows:
        count = row.get("spike_count")
        first = row.get("first_spike_s")
        last = row.get("last_spike_s")
        if count is None or first is None or last is None:
            continue
        lo, hi = float(str(first)), float(str(last))
        if hi <= lo:
            continue
        overlap = np.clip(
            np.minimum(marks + post_s, hi) - np.maximum(marks - pre_s, lo),
            0.0,
            None,
        )
        estimate += int(str(count)) * float(overlap.sum()) / (hi - lo)
    size = int(estimate * RASTER_BYTES_PER_SPIKE)
    if size > MAX_BODY_BYTES:
        raise ApiError(
            413,
            f"the raster would be about {size} bytes, over the {MAX_BODY_BYTES} "
            "limit; use fewer units, a shorter window (pre_s, post_s) or raster=0",
            code="body_estimate_too_large",
        )


def _csv_rows(raw: bytes, column: str) -> list[dict[str, str]]:
    """Return the CSV's rows with a frame number in ``column`` (blank cells dropped)."""
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
    return [row for row in reader if row.get(column, "") != ""]


def _same_recording(target: dict[str, object], events: dict[str, object]) -> bool:
    """Return true when both assets share dataset, subject, session and entity key."""
    same = all(target[k] == events[k] for k in ("dataset", "subject", "bids_session"))
    prefix = f"raw/{target['dataset']}/"
    keys = [str(a["s3_key"]).removeprefix(prefix) for a in (target, events)]
    return same and bids_path.entity_key(keys[0]) == bids_path.entity_key(keys[1])


def _filter(
    rows: list[dict[str, object]], wanted: str | None, column: str, what: str
) -> list[dict[str, object]]:
    """Keep the rows matching a `name=value` filter; 404 when no row has the name."""
    if wanted is None:
        return rows
    name, _, value = wanted.partition("=")
    if column == "trial_type":
        pairs = [(r, r["trial_type"]) for r in rows]
        known = any(v is not None for _, v in pairs)
    else:
        pairs = [(r, _extras(r["columns_json"]).get(name)) for r in rows]
        known = any(name in _extras(r["columns_json"]) for r in rows)
    if not known:
        raise ApiError(404, f"no column {name} in the {what} rows")
    return [r for r, v in pairs if v is not None and _text(v) == value]


def _csv_frames(raw: bytes, column: str) -> NDArray[np.int64]:
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
    if column not in (reader.fieldnames or []):
        raise ApiError(404, f"no column {column} in the CSV")
    try:
        values = [int(float(row[column])) for row in reader if row[column] != ""]
    except ValueError as err:
        raise ApiError(422, f"column {column} must hold frame numbers") from err
    return np.asarray(values, dtype=np.int64)


@dataclass(slots=True)
class _FileReader:
    """Opens the target asset's NWB file once, only when something needs it."""

    api: ComputeApi
    asset: dict[str, object]
    _file: h5py.File | None = None

    def _open(self) -> h5py.File:
        if self._file is None:
            blocks = BlockFile(self.api.remote(self.asset))
            try:
                self._file = h5py.File(blocks, "r")
            except (OSError, ValueError) as err:
                raise ApiError(422, "the asset is not a readable NWB file") from err
        return self._file

    def units(self, wanted: tuple[int, ...], deadline: float) -> dict[int, FloatArray]:
        """Read the spike times of ``wanted`` units from the Units table."""
        handle = self._open()
        if "units/spike_times_index" not in handle:
            raise ApiError(404, "the file has no spike times")
        ends = np.asarray(handle["units/spike_times_index"][:], dtype=np.int64)
        starts = np.concatenate([[0], ends[:-1]]) if ends.size else ends
        out: dict[int, FloatArray] = {}
        for unit in wanted:
            self.api.check(deadline)
            if unit >= ends.size:
                raise ApiError(404, f"no such unit: {unit}")
            data = handle["units/spike_times"][int(starts[unit]) : int(ends[unit])]
            out[unit] = np.sort(np.asarray(data, dtype=np.float64))
        return out

    def timestamps(self, series: dict[str, object]) -> FloatArray:
        """Return the series' timestamps, or ``starting_time + n / rate``."""
        handle = self._open()
        path = str(series["path"])
        if path not in handle:
            raise ApiError(404, f"no such series: {path}")
        group = handle[path]
        if series["has_timestamps"] is True and "timestamps" in group:
            return np.asarray(group["timestamps"][:], dtype=np.float64)
        rate = series["rate_hz"]
        if rate is None or float(str(rate)) <= 0:
            raise ApiError(422, "the series has neither timestamps nor a rate")
        start = float(str(series["starting_time_s"] or 0.0))
        count = int(group["data"].shape[0])
        return start + np.arange(count, dtype=np.float64) / float(str(rate))


def handler(event: dict[str, Any], context: LambdaContext) -> Response:
    """Lambda entry point."""
    remaining = int(context.get_remaining_time_in_millis())
    invocation = Invocation.of(context)
    s3 = boto3.client("s3", config=Config(signature_version="s3v4"))
    derived = os.environ["DERIVED_BUCKET"]

    def presign(key: str, seconds: int) -> str:
        """Return a presigned GET URL for `key` in the derived bucket."""
        return str(
            s3.generate_presigned_url(
                ClientMethod="get_object",
                Params={"Bucket": derived, "Key": key},
                ExpiresIn=seconds,
            )
        )

    return ComputeApi(
        athena=boto3.client("athena"),
        workgroup=os.environ["ATHENA_WORKGROUP"],
        database=os.environ["GLUE_DATABASE"],
        s3=s3,
        raw_bucket=os.environ["RAW_BUCKET"],
        derived_bucket=derived,
        presign=presign,
        labels=boto3.client("dynamodb"),
        labels_table=os.environ["LABELS_TABLE"],
    ).handle(event, remaining, invocation=invocation)
