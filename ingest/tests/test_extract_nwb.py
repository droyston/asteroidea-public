"""NWB extractor tests on a small file pynwb writes here."""

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

import h5py
import numpy as np
import pytest
from pynwb import NWBHDF5IO, NWBFile, TimeSeries
from pynwb.ecephys import LFP, ElectricalSeries
from pynwb.file import Subject

from asteroidea_infra import schema
from asteroidea_ingest.extract.nwb import AssetScope, NwbRows, extract_nwb

if TYPE_CHECKING:
    from pathlib import Path

# pynwb declares its fields dynamically and its NWBFile reads as abstract to ty.
new_nwbfile: Any = NWBFile

START_UTC: Final = datetime(2019, 4, 1, 9, 30, tzinfo=UTC)
START_NAIVE: Final = START_UTC.replace(tzinfo=None)  # the `timestamp` column type
NOW: Final = datetime(2026, 10, 24, 12, 0, tzinfo=UTC).replace(tzinfo=None)
SCOPE: Final = AssetScope("asset-1", "dandi-000999", "sub-X1", "ses-A")
SPIKES: Final = ([0.5, 1.0, 3.5], [2.0], [])
# unit -> electrode ids of its region (the first one gives the unit's location)
REGIONS: Final = ([2, 0], [1], [3])
LOCATIONS: Final = ("Left amygdala", "Left amygdala", "Right hippocampus", "Right ACC")
TABLES: Final = (
    "asset_headers",
    "series",
    "electrodes",
    "units",
    "intervals",
    "unit_spikes",
    "subjects",
    "sessions",
)


def _write(path: Path) -> None:
    nwb = new_nwbfile(
        session_description="a test session",
        identifier="id-1",
        session_start_time=START_UTC,
        institution="Inst",
        lab="Lab",
        subject=Subject(subject_id="X1", species="Homo sapiens", sex="F", age="P40Y"),
    )
    device = nwb.create_device(name="probe")
    group = nwb.create_electrode_group(
        name="grp", description="g", location="brain", device=device
    )
    nwb.add_electrode_column(name="quality", description="q")
    for index, location in enumerate(LOCATIONS):
        nwb.add_electrode(
            x=float(index),
            y=1.0,
            z=2.0,
            imp=np.nan,
            location=location,
            filtering="none",
            group=group,
            quality=f"q{index}",
        )
    nwb.add_unit_column(name="snr", description="snr")
    for times, region in zip(SPIKES, REGIONS, strict=True):
        nwb.add_unit(spike_times=times, electrodes=region, snr=1.5)
    nwb.add_trial_column(name="label", description="label")
    nwb.add_trial(start_time=0.0, stop_time=1.0, label="a")
    nwb.add_trial(start_time=2.0, stop_time=2.5, label="b")
    region = nwb.create_electrode_table_region(region=[0, 1, 2, 3], description="all")
    lfp: Any = LFP()
    nwb.create_processing_module(name="ecephys", description="e").add(lfp)
    lfp.add_electrical_series(
        ElectricalSeries(
            name="lfp_series",
            data=np.zeros((50, 4), dtype="float32"),
            electrodes=region,
            rate=1000.0,
            starting_time=0.25,
        )
    )
    with NWBHDF5IO(str(path), "w") as io:
        io.write(nwb)


@pytest.fixture(scope="module")
def rows(tmp_path_factory: pytest.TempPathFactory) -> NwbRows:
    path = tmp_path_factory.mktemp("nwb") / "small.nwb"
    _write(path)
    with NWBHDF5IO(str(path), "r", load_namespaces=True) as io:
        return extract_nwb(io.read(), SCOPE, NOW)


def test_units_get_count_span_rate_and_first_region_electrode_location(
    rows: NwbRows,
) -> None:
    got = [
        (
            r["unit_index"],
            r["spike_count"],
            r["first_spike_s"],
            r["last_spike_s"],
            r["mean_rate_hz"],
            r["electrode_index"],
            r["location"],
            r["group_name"],
        )
        for r in rows.units
    ]
    assert got == [
        (0, 3, 0.5, 3.5, 1.0, 2, "Right hippocampus", "grp"),
        (1, 1, 2.0, 2.0, None, 1, "Left amygdala", "grp"),
        (2, 0, None, None, None, 3, "Right ACC", "grp"),
    ]
    assert [json.loads(r["columns_json"]) for r in rows.units] == [{"snr": 1.5}] * 3
    assert {r["subject"] for r in rows.units} == {"sub-X1"}


def test_electrodes_keep_schema_columns_and_extras(rows: NwbRows) -> None:
    assert len(rows.electrodes) == 4
    third = rows.electrodes[2]
    assert (third["electrode_index"], third["electrode_id"]) == (2, 2)
    assert (third["location"], third["group_name"]) == ("Right hippocampus", "grp")
    assert (third["x"], third["y"], third["z"]) == (2.0, 1.0, 2.0)
    # imp is NaN (dropped); filtering and quality are scalars beyond the schema
    assert json.loads(third["columns_json"]) == {"filtering": "none", "quality": "q2"}


def test_every_time_intervals_table_gives_rows(rows: NwbRows) -> None:
    got = [
        (r["table_name"], r["row_index"], r["start_time"], r["stop_time"])
        for r in rows.intervals
    ]
    assert got == [("trials", 0, 0.0, 1.0), ("trials", 1, 2.0, 2.5)]
    assert json.loads(rows.intervals[1]["columns_json"]) == {"label": "b"}


def test_unit_spikes_hold_each_units_times_exactly(rows: NwbRows) -> None:
    got = [
        (r["unit_index"], r["spike_count"], r["spike_times"]) for r in rows.unit_spikes
    ]
    assert got == [(0, 3, [0.5, 1.0, 3.5]), (1, 1, [2.0]), (2, 0, [])]
    assert [r["spike_count"] for r in rows.unit_spikes] == [
        r["spike_count"] for r in rows.units
    ]
    scopes = {
        (r["asset_id"], r["subject"], r["bids_session"]) for r in rows.unit_spikes
    }
    assert scopes == {("asset-1", "sub-X1", "ses-A")}


def test_series_records_shape_dtype_and_timing_without_reading_data(
    rows: NwbRows,
) -> None:
    assert len(rows.series) == 1
    series = rows.series[0]
    assert series["path"] == "/processing/ecephys/LFP/lfp_series"
    assert series["neurodata_type"] == "ElectricalSeries"
    assert (series["shape"], series["dtype"]) == ([50, 4], "float32")
    assert (series["rate_hz"], series["starting_time_s"]) == (1000.0, 0.25)
    assert series["has_timestamps"] is False


def test_headers_subject_and_session_use_the_path_identity(rows: NwbRows) -> None:
    [header] = rows.asset_headers
    assert header["nwb_identifier"] == "id-1"
    assert (header["institution"], header["lab"]) == ("Inst", "Lab")
    assert header["session_start_time"] == START_NAIVE
    assert (header["n_units"], header["n_electrodes"], header["n_trials"]) == (3, 4, 2)
    assert header["format"] == "nwb"
    [subject] = rows.subjects
    assert subject["subject"] == "sub-X1"  # path-derived
    assert subject["source_subject_id"] == "X1"  # the file's own, no prefix
    assert (subject["species"], subject["sex"], subject["age"]) == (
        "Homo sapiens",
        "F",
        "P40Y",
    )
    [session] = rows.sessions
    assert (session["subject"], session["bids_session"]) == ("sub-X1", "ses-A")
    assert session["start_time"] == START_NAIVE


def test_every_row_key_is_a_schema_column_and_required_ones_are_set(
    rows: NwbRows,
) -> None:
    for name in TABLES:
        columns = {c.name: c for c in schema.SPECS[name].columns}
        for row in getattr(rows, name):
            assert set(row) <= set(columns), (name, set(row) - set(columns))
            missing = [c for c, s in columns.items() if s.required and c not in row]
            assert missing == [], (name, missing)
            assert [c for c in row if columns[c].required and row[c] is None] == []


def test_a_file_without_a_path_subject_writes_no_subject_or_session_row() -> None:
    nwb = new_nwbfile(
        session_description="d",
        identifier="id-2",
        session_start_time=START_UTC,
    )
    out = extract_nwb(nwb, AssetScope("a", "d", None, None), NOW)
    assert (out.subjects, out.sessions, out.units, out.electrodes) == ([], [], [], [])
    assert out.asset_headers[0]["n_units"] == 0


def test_every_series_path_opens_in_the_hdf5_file_as_written(
    tmp_path: Path,
) -> None:
    """The catalog path is the HDF5 path (stimulus is under /stimulus/presentation)."""
    nwb = new_nwbfile(
        session_description="d", identifier="id-3", session_start_time=START_UTC
    )
    nwb.add_acquisition(
        TimeSeries(name="events_ttl", data=[1, 2], unit="n/a", timestamps=[0.0, 1.0])
    )
    nwb.add_stimulus(
        TimeSeries(name="movieframe", data=[1, 2], unit="n/a", timestamps=[0.0, 1.0])
    )
    nwb.add_stimulus_template(
        TimeSeries(name="movie", data=[1, 2], unit="n/a", timestamps=[0.0, 1.0])
    )
    nwb.create_processing_module(name="behavior", description="b").add(
        TimeSeries(name="blink", data=[1, 2], unit="n/a", timestamps=[0.0, 1.0])
    )
    path = tmp_path / "kinds.nwb"
    with NWBHDF5IO(str(path), "w") as io:
        io.write(nwb)
    with NWBHDF5IO(str(path), "r", load_namespaces=True) as io:
        out = extract_nwb(io.read(), SCOPE, NOW)
    paths = [row["path"] for row in out.series]
    assert paths == [
        "/acquisition/events_ttl",
        "/processing/behavior/blink",
        "/stimulus/presentation/movieframe",
        "/stimulus/templates/movie",
    ]
    with h5py.File(path, "r") as handle:
        assert [p for p in paths if p not in handle] == []
