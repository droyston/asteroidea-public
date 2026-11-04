"""Converters from a session's units, events and LFP window to pynapple objects.

pynapple is the optional `pynapple` extra; it is imported only inside the
functions, through `_sample.optional`, so `asteroidea/__init__` imports without
it and a missing extra raises `ExtraMissingError` naming it.

pynapple keeps an `IntervalSet` sorted, drops an epoch of zero duration and joins
overlapping epochs. A point event (`stop_s` is None) therefore gets a stop of
`POINT_S` after its onset (pynapple's own one-microsecond step), so it survives.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, cast

from asteroidea import _sample

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from types import ModuleType

    import pynapple as nap

    from asteroidea import analysis
    from asteroidea._session import FloatArray, Lfp

POINT_S: Final = 1e-6
"""Duration given to an event with no `stop_s`."""
UNKNOWN_LOCATION: Final = ""
"""Location metadata of a unit whose location is null."""


@dataclass(frozen=True, slots=True)
class PynappleSession:
    """A session as pynapple objects.

    `units` is a `TsGroup` keyed by `unit_index` with `location` metadata;
    `events` maps each event type to an `IntervalSet`; `lfp` is a `TsdFrame`
    of the window on the trials clock, columns the channel positions, or None.
    """

    units: nap.TsGroup
    events: Mapping[str, nap.IntervalSet]
    lfp: nap.TsdFrame | None


def _nap() -> ModuleType:
    """Return the pynapple module, or raise the optional-install error."""
    return _sample.optional("pynapple", "pynapple")


def units_to_tsgroup(
    spikes: Mapping[int, FloatArray], locations: Mapping[int, str]
) -> nap.TsGroup:
    """Return a `TsGroup` of the spike trains with `location` metadata."""
    mod = _nap()
    keys = sorted(spikes)
    trains = {k: mod.Ts(t=spikes[k]) for k in keys}
    where = [locations.get(k, UNKNOWN_LOCATION) for k in keys]
    return cast("nap.TsGroup", mod.TsGroup(trains, metadata={"location": where}))


def events_to_intervalsets(
    events: Sequence[analysis.AlignmentEvent],
) -> dict[str, nap.IntervalSet]:
    """Return one `IntervalSet` per event type; stop is onset + `POINT_S` if None."""
    mod = _nap()
    by_type: dict[str, list[tuple[float, float]]] = {}
    for e in events:
        stop = e.onset_s if e.stop_s is None else e.stop_s
        stop = max(stop, e.onset_s + POINT_S)
        by_type.setdefault(e.event_type, []).append((e.onset_s, stop))
    out: dict[str, nap.IntervalSet] = {}
    for name, spans in by_type.items():
        spans.sort()
        out[name] = mod.IntervalSet(
            start=[s for s, _ in spans], end=[t for _, t in spans]
        )
    return out


def lfp_to_tsdframe(window: Lfp) -> nap.TsdFrame:
    """Return the window as a `TsdFrame` on the trials clock, one column per channel."""
    mod = _nap()
    columns = list(range(window.samples.shape[1]))
    frame = mod.TsdFrame(t=window.times_s, d=window.samples, columns=columns)
    return cast("nap.TsdFrame", frame)
