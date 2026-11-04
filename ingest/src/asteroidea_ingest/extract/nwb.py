"""NWB extraction through pynwb's own API.

`extract_nwb` takes an open `NWBFile` and returns the catalog rows for `asset_headers`,
`series`, `electrodes`, `units`, `unit_spikes`, `intervals`, `subjects` and
`sessions`. Column names
come from `asteroidea_infra.schema`. It reads no TimeSeries data (shape and dtype come
from the lazy dataset); tables are read through `DynamicTable.to_dataframe()`, which
resolves ragged columns and electrode regions. Nothing is written here.
"""

import json
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

import numpy as np
import pandas as pd
from hdmf.common import DynamicTable
from pynwb.base import ProcessingModule, TimeSeries
from pynwb.epoch import TimeIntervals

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from pynwb import NWBFile

EXTRACTOR_VERSION: Final = "nwb-2"
SOURCE: Final = "nwb:/general/subject"
TOP_GROUPS: Final = ("acquisition", "stimulus", "stimulus_template", "analysis")
# pynwb attribute -> HDF5 group holding its series (stimulus series sit one level down)
HDF5_GROUPS: Final = {
    "acquisition": "acquisition",
    "stimulus": "stimulus/presentation",
    "stimulus_template": "stimulus/templates",
    "analysis": "analysis",
}
SPIKE_COLUMNS: Final = frozenset({"spike_times", "electrodes", "id"})
ELECTRODE_COLUMNS: Final = frozenset({"id", "location", "group_name", "x", "y", "z"})
Row = dict[str, Any]


@dataclass(frozen=True)
class AssetScope:
    """Who the file belongs to; ``subject`` and ``bids_session`` come from the path."""

    asset_id: str
    dataset: str
    subject: str | None
    bids_session: str | None

    def row(self) -> Row:
        """Return the four scope columns every per-asset table carries."""
        return {
            "asset_id": self.asset_id,
            "dataset": self.dataset,
            "subject": self.subject,
            "bids_session": self.bids_session,
        }


@dataclass
class NwbRows:
    """The rows one NWB file yields, by table name."""

    asset_headers: list[Row] = field(default_factory=list)
    series: list[Row] = field(default_factory=list)
    electrodes: list[Row] = field(default_factory=list)
    units: list[Row] = field(default_factory=list)
    intervals: list[Row] = field(default_factory=list)
    unit_spikes: list[Row] = field(default_factory=list)
    subjects: list[Row] = field(default_factory=list)
    sessions: list[Row] = field(default_factory=list)


def _naive_utc(value: object) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def _scalar(value: object) -> object | None:
    """Return a JSON-able scalar, or None for NaN, arrays, tables and objects."""
    result: object | None = None
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, bytes):
        value = value.decode(errors="replace")
    if isinstance(value, bool | int | str) or (
        isinstance(value, float) and math.isfinite(value)
    ):
        result = value
    return result


def _get(obj: object, name: str) -> object | None:
    """Return a pynwb attribute (pynwb declares its fields dynamically, so untyped)."""
    return getattr(obj, name, None)


def _float(value: object) -> float | None:
    scalar = _scalar(value)
    if isinstance(scalar, bool) or not isinstance(scalar, int | float):
        return None
    return float(scalar)


def _text(value: object) -> str | None:
    scalar = _scalar(value)
    return None if scalar is None else str(scalar)


def _int(value: object) -> int | None:
    scalar = _scalar(value)
    if isinstance(scalar, bool) or not isinstance(scalar, int):
        return None
    return scalar


def _extras(frame_row: Mapping[str, object], known: frozenset[str]) -> str | None:
    """Return the scalar columns beyond the schema as sorted-key JSON, or None."""
    extras: dict[str, object] = {}
    for name, value in frame_row.items():
        if name in known:
            continue
        scalar = _scalar(value)
        if scalar is not None:
            extras[str(name)] = scalar
    return json.dumps(extras, sort_keys=True) if extras else None


def _records(frame: pd.DataFrame) -> Iterable[tuple[object, dict[str, object]]]:
    for index, record in zip(frame.index, frame.to_dict("records"), strict=True):
        yield index, {str(k): v for k, v in record.items()}


def series_path(nwbfile: NWBFile, obj: TimeSeries) -> str:
    """Return the object's true HDF5 path, from its parent chain.

    ``compute`` and the LFP builder open this path with h5py as it stands.
    """
    names: list[str] = []
    node: Any = obj
    while node.parent is not nwbfile and node.parent is not None:
        names.append(node.name)
        node = node.parent
    names.append(node.name)  # the direct child of the file
    if isinstance(node, ProcessingModule):
        group = "processing"
    else:
        group = next((g for g in TOP_GROUPS if node.name in getattr(nwbfile, g)), "?")
    names.append(HDF5_GROUPS.get(group, group))
    return "/" + "/".join(reversed(names))


def _series_row(nwbfile: NWBFile, obj: TimeSeries, scope: Row) -> Row:
    data: Any = obj.data
    shape = getattr(data, "shape", None)
    if shape is None:
        shape = np.shape(data)
    dtype = getattr(data, "dtype", None)
    return {
        **scope,
        "path": series_path(nwbfile, obj),
        "neurodata_type": type(obj).__name__,
        "shape": [int(n) for n in shape],
        "dtype": None if dtype is None else str(dtype),
        "rate_hz": _float(_get(obj, "rate")),
        "starting_time_s": _float(_get(obj, "starting_time")),
        "has_timestamps": _get(obj, "timestamps") is not None,
        "unit": _text(_get(obj, "unit")),
    }


def _electrode_rows(table: object, scope: Row) -> list[Row]:
    if not isinstance(table, DynamicTable):
        return []
    rows: list[Row] = []
    for position, (ident, record) in enumerate(_records(table.to_dataframe())):
        rows.append(
            {
                **scope,
                "electrode_index": position,
                "electrode_id": _int(ident),
                "location": _text(record.get("location")),
                "group_name": _text(record.get("group_name")),
                "x": _float(record.get("x")),
                "y": _float(record.get("y")),
                "z": _float(record.get("z")),
                "columns_json": _extras(record, ELECTRODE_COLUMNS),
            }
        )
    return rows


def _first_electrode(
    cell: object, positions: Mapping[int, int]
) -> tuple[int | None, str | None, str | None]:
    """Return (position, location, group_name) of the first electrode of a region."""
    if not isinstance(cell, pd.DataFrame) or cell.empty:
        return None, None, None
    ident = _int(cell.index[0])
    position = None if ident is None else positions.get(ident)
    first = {str(k): v for k, v in cell.iloc[0].to_dict().items()}
    return position, _text(first.get("location")), _text(first.get("group_name"))


def _unit_rows(
    nwbfile: NWBFile, positions: Mapping[int, int], scope: Row
) -> tuple[list[Row], list[Row]]:
    """Return the `units` rows and, one per unit, the `unit_spikes` rows."""
    units = _get(nwbfile, "units")
    if not isinstance(units, DynamicTable):
        return [], []
    rows: list[Row] = []
    spikes: list[Row] = []
    for position, (ident, record) in enumerate(_records(units.to_dataframe())):
        times = np.asarray(record.get("spike_times", []), dtype=float)
        count = int(times.size)
        first = float(times.min()) if count else None
        last = float(times.max()) if count else None
        span = None if first is None or last is None else last - first
        spikes.append(
            {
                **scope,
                "unit_index": position,
                "spike_count": count,
                "spike_times": [float(t) for t in times],
            }
        )
        electrode, location, group = _first_electrode(
            record.get("electrodes"), positions
        )
        rows.append(
            {
                **scope,
                "unit_index": position,
                "unit_id": _text(ident),
                "electrode_index": electrode,
                "location": location,
                "group_name": group,
                "spike_count": count,
                "first_spike_s": first,
                "last_spike_s": last,
                "mean_rate_hz": count / span if span is not None and span > 0 else None,
                "columns_json": _extras(record, SPIKE_COLUMNS),
            }
        )
    return rows, spikes


def _interval_rows(nwbfile: NWBFile, scope: Row) -> list[Row]:
    tables: dict[str, TimeIntervals] = {}
    for obj in nwbfile.objects.values():
        if isinstance(obj, TimeIntervals):
            tables.setdefault(obj.name, obj)
    rows: list[Row] = []
    for name in sorted(tables):
        for position, (_, record) in enumerate(_records(tables[name].to_dataframe())):
            start = _float(record.get("start_time"))
            if start is None:
                continue
            rows.append(
                {
                    **scope,
                    "table_name": name,
                    "row_index": position,
                    "start_time": start,
                    "stop_time": _float(record.get("stop_time")),
                    "columns_json": _extras(
                        record, frozenset({"id", "start_time", "stop_time"})
                    ),
                }
            )
    return rows


def extract_nwb(nwbfile: NWBFile, who: AssetScope, extracted_at: datetime) -> NwbRows:
    """Return the catalog rows of ``nwbfile``.

    ``who.subject`` and ``who.bids_session`` come from the object's path
    (``assets.subject``, with ``sub-``), never from the file. The file's own
    ``subject_id`` is kept as ``source_subject_id``.
    """
    asset_id, dataset = who.asset_id, who.dataset
    subject, bids_session = who.subject, who.bids_session
    scope = who.row()
    out = NwbRows()
    for obj in nwbfile.objects.values():
        if isinstance(obj, TimeSeries):
            out.series.append(_series_row(nwbfile, obj, scope))
    out.series.sort(key=lambda r: r["path"])
    out.electrodes = _electrode_rows(_get(nwbfile, "electrodes"), scope)
    positions = {
        row["electrode_id"]: row["electrode_index"]
        for row in out.electrodes
        if row["electrode_id"] is not None
    }
    out.units, out.unit_spikes = _unit_rows(nwbfile, positions, scope)
    out.intervals = _interval_rows(nwbfile, scope)
    n_trials = sum(1 for r in out.intervals if r["table_name"] == "trials")
    start = _naive_utc(_get(nwbfile, "session_start_time"))
    out.asset_headers = [
        {
            "asset_id": asset_id,
            "dataset": dataset,
            "format": "nwb",
            "extractor_version": EXTRACTOR_VERSION,
            "extracted_at": extracted_at,
            "nwb_version": _text(_get(nwbfile, "nwb_version")),
            "nwb_identifier": _text(_get(nwbfile, "identifier")),
            "session_description": _text(_get(nwbfile, "session_description")),
            "session_start_time": start,
            "institution": _text(_get(nwbfile, "institution")),
            "lab": _text(_get(nwbfile, "lab")),
            "n_units": len(out.units),
            "n_electrodes": len(out.electrodes),
            "n_trials": n_trials,
        }
    ]
    if subject is not None:
        file_subject = _get(nwbfile, "subject")
        out.subjects = [
            {
                "dataset": dataset,
                "subject": subject,
                "species": _text(_get(file_subject, "species")),
                "sex": _text(_get(file_subject, "sex")),
                "age": _text(_get(file_subject, "age")),
                "source": SOURCE,
                "source_subject_id": _text(_get(file_subject, "subject_id")),
                "source_asset_id": asset_id,
                "extracted_at": extracted_at,
            }
        ]
        if bids_session is not None:
            out.sessions = [
                {
                    "dataset": dataset,
                    "subject": subject,
                    "bids_session": bids_session,
                    "start_time": start,
                    "description": _text(_get(nwbfile, "session_description")),
                    "source_asset_id": asset_id,
                    "extracted_at": extracted_at,
                }
            ]
    return out
