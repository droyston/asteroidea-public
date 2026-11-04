"""Session loaders over fakes shaped like production.

``sql()`` is a real DuckDB connection holding the catalog tables with the column
names and types of ``infra/.../schema.py`` and the ``files`` view of ``_sql.py``, so
the loaders' query texts run and return Arrow tables as ``Engine.run`` does.
``open_file`` returns a real ``RemoteFile`` over a fake object store; ``open_nwb``
returns objects shaped like pynwb (groups index by name; a series has ``data``,
``rate``, ``starting_time`` and ``timestamps``).
"""

import importlib
import io
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import duckdb
import numpy as np
import polars as pl
import pyarrow as pa
import pynapple as nap
import pytest

import asteroidea
from asteroidea import _image, _nwb, _session, _sql, analysis
from asteroidea._remote import GetReply, RemoteFile, Source
from asteroidea._run import Run, active
from asteroidea.errors import ExtraMissingError
from asteroidea_infra import schema

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from types import ModuleType

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 10, 26, tzinfo=UTC)
D623, D1775, BIDS = "dandi-000623", "dandi-001775", "ds-bids"
A623 = "7887e844-7100-4384-975e-5ee38dbf153d"
A1775 = "b7763578-7a3f-4919-ba98-38d2705f8400"
CSV_ASSET = "csv-asset-1"
S623, S1775 = "ses-P62CSR2", "ses-Day5"
MOVIE = "stimulus/presentation/movieframe_time"
LFP623 = "/processing/ecephys/LFP_micro/ElectricalSeries"
LFP1775 = "/processing/ecephys/LFP/ElectricalSeries"
Row = dict[str, object]
ARROW = {
    "string": pa.string(),
    "long": pa.int64(),
    "double": pa.float64(),
    "timestamp": pa.timestamp("us"),
    "boolean": pa.bool_(),
    "list<long>": pa.list_(pa.int64()),
    "list<double>": pa.list_(pa.float64()),
}
DEFAULT_FOR = {
    "string": "x",
    "long": 0,
    "double": 0.0,
    "timestamp": NOW.replace(tzinfo=None),
}
# The `files` view of asteroidea._sql, which is private there.
FILES_VIEW = (
    "CREATE VIEW files AS SELECT a.*, h.* EXCLUDE (asset_id, dataset, format) "
    "FROM assets a LEFT JOIN asset_headers h ON a.asset_id = h.asset_id"
)


class Catalog:
    """A DuckDB catalog with every table of ``schema.TABLES`` and the ``files`` view."""

    def __init__(self) -> None:
        self.rows: dict[str, list[Row]] = {spec.name: [] for spec in schema.TABLES}
        self.con = duckdb.connect()

    def add(self, table: str, rows: Sequence[Row]) -> None:
        names = {c.name for c in schema.SPECS[table].columns}
        for row in rows:
            assert set(row) <= names, f"{table} has no column {set(row) - names}"
        self.rows[table].extend(rows)

    def build(self) -> None:
        """Create each table with the schema's column names and types."""
        for spec in schema.TABLES:
            columns = {}
            for col in spec.columns:
                fill = DEFAULT_FOR.get(col.type) if col.required else None
                columns[col.name] = pa.array(
                    [row.get(col.name, fill) for row in self.rows[spec.name]],
                    type=ARROW[col.type],
                )
            self.con.from_arrow(pa.table(columns)).create(spec.name)
        self.con.execute(FILES_VIEW)


class Store:
    """An object store that serves ranges of one object."""

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.requests: list[Mapping[str, str | int]] = []

    def get_object(self, **kwargs: str | int) -> GetReply:
        self.requests.append(kwargs)
        first, last = str(kwargs["Range"]).removeprefix("bytes=").split("-")
        return {"Body": io.BytesIO(self.data[int(first) : int(last) + 1])}


class Fakes:
    """The three reads of ``_session.Backend``, with counters."""

    def __init__(self, catalog: Catalog) -> None:
        self.catalog = catalog
        self.stores: dict[str, Store] = {}
        self.nwb: dict[str, object] = {}
        self.opened_nwb: list[str] = []
        self.opened_file: list[str] = []

    def sql(self, query: str, params: Sequence[str]) -> pa.Table:
        return self.catalog.con.execute(query, list(params)).to_arrow_table()

    def open_file(self, asset_id: str) -> RemoteFile:
        self.opened_file.append(asset_id)
        store = self.stores[asset_id]
        source = Source("raw", f"raw/{asset_id}", "v1", len(store.data))
        return RemoteFile(source, client=lambda: store, renewed=lambda: store)

    def open_nwb(self, asset_id: str) -> object:
        self.opened_nwb.append(asset_id)
        return self.nwb[asset_id]


def _intervals(dataset: str) -> list[Row]:
    text = (FIXTURES / dataset / "intervals.json").read_text("utf-8")
    return cast("list[Row]", json.loads(text))


def _series_obj(
    *,
    data: np.ndarray,
    rate: float | None = None,
    start: float = 0.0,
    stamps: np.ndarray | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(data=data, rate=rate, starting_time=start, timestamps=stamps)


def _wired(
    catalog: Catalog,
    rng: np.random.Generator,
    dataset: str,
    asset: str,
    span: tuple[float, float],
) -> None:
    scope = _intervals(dataset)[0]
    for unit in range(4):
        times = np.sort(rng.uniform(*span, size=300))
        where = {
            "asset_id": asset,
            "dataset": dataset,
            "subject": scope["subject"],
            "bids_session": scope["bids_session"],
            "unit_index": unit,
            "spike_count": 300,
        }
        catalog.add("units", [{**where, "location": ["AMY", "AMY", "HIP", None][unit]}])
        catalog.add("unit_spikes", [{**where, "spike_times": times.tolist()}])


def _asset(dataset: str, asset: str, key: str, **extra: object) -> Row:
    return {
        "asset_id": asset,
        "dataset": dataset,
        "s3_key": f"raw/{dataset}/{key}",
        "format": "nwb",
        "extension": ".nwb",
        **extra,
    }


def _series_row(asset: str, dataset: str, subject: str, session: str, path: str) -> Row:
    return {
        "asset_id": asset,
        "dataset": dataset,
        "subject": subject,
        "bids_session": session,
        "path": path,
        "neurodata_type": "TimeSeries",
    }


@pytest.fixture
def fakes() -> Fakes:
    rng = np.random.default_rng(7)
    catalog = Catalog()
    fk = Fakes(catalog)
    # dandi-000623: one NWB file and the ancillary CSV (no subject)
    catalog.add(
        "assets",
        [
            _asset(
                D623,
                A623,
                "sub-CS62/sub-CS62_ses-P62CSR2_behavior+ecephys.nwb",
                subject="sub-CS62",
                bids_session=S623,
            ),
            _asset(D623, CSV_ASSET, "stimuli/scenecut_info.csv", format="csv"),
            _asset(
                D1775,
                A1775,
                "sub-UT06/sub-UT06_ses-Day5_behavior+ecephys.nwb",
                subject="sub-UT06",
                bids_session=S1775,
            ),
            _asset(BIDS, "bids-1", "sub-01/anat/sub-01_T1w.nii.gz", subject="sub-01"),
            _asset(BIDS, "bids-2", "sub-01/func/sub-01_events.tsv", subject="sub-01"),
            _asset(BIDS, "bids-3", "sub-02/anat/sub-02_T1w.nii.gz", subject="sub-02"),
        ],
    )
    catalog.add(
        "sessions",
        [
            {
                "dataset": D623,
                "subject": "sub-CS62",
                "bids_session": S623,
                "start_time": datetime(2023, 5, 1, 9, 0, tzinfo=UTC).replace(
                    tzinfo=None
                ),
                "description": "bmovie",
            }
        ],
    )
    catalog.add("intervals", _intervals(D623))
    catalog.add("intervals", _intervals(D1775))
    _wired(catalog, rng, D623, A623, (0.0, 1200.0))
    _wired(catalog, rng, D1775, A1775, (3349699.0, 3357979.0))
    catalog.add(
        "series",
        [
            _series_row(A623, D623, "sub-CS62", S623, "/" + MOVIE),
            _series_row(A623, D623, "sub-CS62", S623, LFP623),
            _series_row(A1775, D1775, "sub-UT06", S1775, LFP1775),
        ],
    )
    catalog.add(
        "events",
        [
            {
                "asset_id": "bids-2",
                "dataset": BIDS,
                "subject": "sub-01",
                "events_for": "task-x",
                "row_index": i,
                "onset_s": 1.5 * i,
                "duration_s": 0.5,
                "trial_type": "go" if i % 2 else "stop",
            }
            for i in range(6)
        ],
    )
    catalog.build()
    fk.stores[CSV_ASSET] = Store((FIXTURES / D623 / "scenecut_info.csv").read_bytes())
    movie = json.loads(
        (FIXTURES / D623 / "movieframe_timestamps.json").read_text("utf-8")
    )
    lfp_data = np.arange(30000 * 3, dtype=np.float64).reshape(30000, 3)
    fk.nwb[A623] = SimpleNamespace(
        stimulus={
            "movieframe_time": _series_obj(
                data=np.array([], dtype=object),
                stamps=np.asarray(movie["timestamps"]),
            )
        },
        processing={
            "ecephys": {
                "LFP_micro": {
                    "ElectricalSeries": _series_obj(data=lfp_data, rate=1000.0)
                }
            }
        },
    )
    stamps = 3349699.35 + np.arange(30000) * 0.001
    fk.nwb[A1775] = SimpleNamespace(
        processing={
            "ecephys": {
                "LFP": {"ElectricalSeries": _series_obj(data=lfp_data, stamps=stamps)}
            }
        },
    )
    return fk


def _load(fk: Fakes, dataset: str, subject: str, session: str) -> _session.Session:
    return _session.load_session(dataset, subject, session, cast("Any", fk))


def _counts(frame: pl.DataFrame) -> dict[str, int]:
    return dict(frame.group_by("event_type").len().iter_rows())


def test_sessions_index_keys_start_time_and_implicit_session(fakes: Fakes) -> None:
    table = _session.sessions_index(cast("Any", fakes))
    rows = {r["session_key"]: r for r in table.to_pylist()}
    assert set(rows) == {
        f"{D623}/sub-CS62/{S623}",
        f"{D1775}/sub-UT06/{S1775}",
        f"{BIDS}/sub-01/_",
        f"{BIDS}/sub-02/_",
    }
    first = rows[f"{D623}/sub-CS62/{S623}"]
    assert first["description"] == "bmovie"
    assert first["start_time"] is not None
    assert rows[f"{BIDS}/sub-01/_"]["start_time"] is None  # the CSV has no subject
    assert table.column_names == [
        "session_key",
        "dataset",
        "subject",
        "session",
        "start_time",
        "description",
    ]


def test_files_are_the_sessions_assets(fakes: Fakes) -> None:
    session = _load(fakes, D623, "sub-CS62", S623)
    assert session.files["asset_id"].to_list() == [A623]
    assert "n_units" in session.files.columns  # the files view joins asset_headers
    implicit = _load(fakes, BIDS, "sub-01", "_")
    assert sorted(implicit.files["asset_id"].to_list()) == ["bids-1", "bids-2"]


def test_units_frame_has_location(fakes: Fakes) -> None:
    units = _load(fakes, D623, "sub-CS62", S623).units
    assert units["unit_index"].to_list() == [0, 1, 2, 3]
    assert units["location"].to_list() == ["AMY", "AMY", "HIP", None]


def test_spikes_are_sorted_numpy_arrays_by_unit(fakes: Fakes) -> None:
    spikes = _load(fakes, D623, "sub-CS62", S623).spikes
    assert sorted(spikes) == [0, 1, 2, 3]
    for times in spikes.values():
        assert isinstance(times, np.ndarray)
        assert times.dtype == np.float64
        assert times.size == 300
        assert np.all(np.diff(times) >= 0)
    stored = fakes.sql(
        "SELECT spike_times FROM unit_spikes WHERE unit_index = 2 AND dataset = ?",
        [D623],
    ).to_pylist()[0]["spike_times"]
    assert spikes[2].tolist() == stored


def test_events_000623_from_intervals_csv_and_frame_series(fakes: Fakes) -> None:
    session = _load(fakes, D623, "sub-CS62", S623)
    assert _counts(session.events) == {
        "scene_cut": 93,
        "recognition_onset": 40,
        "movie_onset": 1,
    }
    assert set(session.events["clock_asset_id"]) == {A623}
    cuts = session.events.filter(pl.col("event_type") == "scene_cut")
    assert set(cuts["source_kind"]) == {"stimulus_file"}
    assert fakes.opened_file == [CSV_ASSET]
    assert fakes.opened_nwb == [A623]
    assert fakes.stores[CSV_ASSET].requests, "the CSV is read by ranged requests"
    recog = session.events.filter(pl.col("event_type") == "recognition_onset")
    assert set(json.loads(recog["attributes"][0])) == {
        "actual_response",
        "response_correct",
        "familiarity",
    }


def test_events_001775_from_intervals(fakes: Fakes) -> None:
    session = _load(fakes, D1775, "sub-UT06", S1775)
    assert _counts(session.events) == {"trial_start": 20, "reward_grasp": 20}
    assert fakes.opened_file == []
    assert fakes.opened_nwb == []  # no stimulus_file type: no NWB read


def test_events_of_a_bids_only_dataset_use_the_default_recipe(fakes: Fakes) -> None:
    session = _load(fakes, BIDS, "sub-01", "_")
    assert _counts(session.events) == {"events": 6}
    kinds = [json.loads(a)["trial_type"] for a in session.events["attributes"]]
    assert kinds == ["stop", "go", "stop", "go", "stop", "go"]


def test_missing_ancillary_csv_names_the_catalog_key(fakes: Fakes) -> None:
    fakes.catalog.con.execute("DELETE FROM assets WHERE asset_id = ?", [CSV_ASSET])
    with pytest.raises(
        ValueError, match=r"raw/dandi-000623/stimuli/scenecut_info\.csv"
    ):
        _load(fakes, D623, "sub-CS62", S623)


def test_lfp_applies_the_recipe_offset_000623(fakes: Fakes) -> None:
    session = _load(fakes, D623, "sub-CS62", S623)
    window = session.lfp(LFP623, start_s=2.0, duration_s=0.5)
    assert window.lfp_clock_offset_s == 10.0
    # trials-clock 2.0 s is LFP-clock 12.0 s: sample 12000 of a 1 kHz series
    assert window.samples.shape == (500, 3)
    assert window.samples[0, 0] == 12000 * 3
    assert window.times_s[0] == pytest.approx(2.0)
    assert window.times_s[-1] == pytest.approx(2.499)
    picked = session.lfp(LFP623, [2, 0], start_s=2.0, duration_s=0.01)
    assert picked.samples[0].tolist() == [12000 * 3 + 2, 12000 * 3]


def test_lfp_has_no_offset_for_001775_and_reads_timestamps(fakes: Fakes) -> None:
    session = _load(fakes, D1775, "sub-UT06", S1775)
    window = session.lfp(LFP1775, start_s=3349700.0, duration_s=0.01)
    assert window.lfp_clock_offset_s == 0.0
    first = int(np.ceil((3349700.0 - 3349699.35) / 0.001 - 1e-6))
    assert window.samples[0, 0] == first * 3
    assert window.times_s[0] == pytest.approx(3349699.35 + first * 0.001)
    assert window.times_s.size == window.samples.shape[0] == 10


def test_lfp_unknown_series_lists_the_known_ones(fakes: Fakes) -> None:
    session = _load(fakes, D623, "sub-CS62", S623)
    with pytest.raises(ValueError, match="LFP_micro"):
        session.lfp(
            "/processing/ecephys/Nope/ElectricalSeries", start_s=0, duration_s=1
        )


def test_rates_psth_screen_and_labels_000623(fakes: Fakes) -> None:
    session = _load(fakes, D623, "sub-CS62", S623)
    got = session.rates("recognition_onset")
    defaults = analysis.load_defaults(D623)
    assert got["label"].unique().to_list() == ["exploratory screen"]
    assert got["defaults_version"].unique().to_list() == [defaults.version]
    assert got["recipe_version"].unique().to_list() == ["recipe-1"]
    assert (got["pre_s"][0], got["post_s"][0], got["bin_s"][0]) == (1.0, 2.0, 0.25)
    assert got["n_events"][0] == 40
    assert got["bin_start_s"].n_unique() == 12
    assert got["location"].unique().sort().to_list() == [None, "AMY", "HIP"]
    onsets = np.array(
        [
            e.onset_s
            for e in session.alignment_events
            if e.event_type == "recognition_onset"
        ]
    )
    direct = analysis.rates(session.spikes, onsets, 1.0, 2.0, 0.25)
    unit2 = got.filter(pl.col("unit_index") == 2)["rate_hz"].to_numpy()
    assert unit2 == pytest.approx(direct.rate_hz[2])
    assert {"response_hz", "reference_hz", "p", "q", "screened"} <= set(got.columns)
    assert got["p"].null_count() < got.height  # an average type is screened


def test_rates_arguments_override_the_window(fakes: Fakes) -> None:
    session = _load(fakes, D1775, "sub-UT06", S1775)
    got = session.rates("trial_start", pre_s=0.5, post_s=1.0, bin_s=0.5)
    assert (got["pre_s"][0], got["post_s"][0], got["bin_s"][0]) == (0.5, 1.0, 0.5)
    assert got["bin_start_s"].n_unique() == 3
    assert got["n_events"][0] == 20
    assert got["defaults_version"][0] == analysis.load_defaults(D1775).version


def test_rates_for_an_unknown_event_type_names_the_known_ones(fakes: Fakes) -> None:
    session = _load(fakes, D1775, "sub-UT06", S1775)
    with pytest.raises(ValueError, match="reward_grasp"):
        session.rates("nope")


def test_tuning_screen_001775_choice(fakes: Fakes) -> None:
    session = _load(fakes, D1775, "sub-UT06", S1775)
    got = session.tuning("trial_start", "choice")
    defaults = analysis.load_defaults(D1775)
    assert got.height == 4
    assert got["label"].unique().to_list() == ["exploratory screen"]
    assert got["defaults_version"].unique().to_list() == [defaults.version]
    assert got["recipe_version"].unique().to_list() == ["recipe-1"]
    assert json.loads(got["level_counts"][0]) == {"L": 10, "R": 10}
    assert got["n_permutations"][0] == 2000
    assert set(json.loads(got["level_means"][0])) == {"L", "R"}
    direct = analysis.tuning(
        session.spikes,
        session.alignment_events,
        session.recipe,
        defaults,
        {0: "AMY", 1: "AMY", 2: "HIP"},
    )
    mine = next(r for r in direct if r.attribute == "choice")
    assert got["p"].to_list() == [r.p for r in mine.rows]


def test_rates_and_tuning_in_a_run_record_the_defaults_and_recipe_versions(
    fakes: Fakes,
) -> None:
    # `defaults` and `recipe` inputs, each a text version.
    session = _load(fakes, D1775, "sub-UT06", S1775)
    version = analysis.load_defaults(D1775).version
    with Run("screen") as r:
        session.rates("trial_start")
        session.tuning("trial_start", "choice")
    assert list(r.inputs.values()) == [
        {
            "kind": "defaults",
            "id": f"analysis_defaults/{D1775}",
            "version": version,
            "role": "rates",
        },
        {
            "kind": "recipe",
            "id": f"recipes/{D1775}",
            "version": "recipe-1",
            "role": "rates",
        },
    ]
    assert r.record()["run"]["defaults_version"] == version


def test_tuning_alone_in_a_run_records_its_versions(fakes: Fakes) -> None:
    session = _load(fakes, D1775, "sub-UT06", S1775)
    with Run("screen") as r:
        session.tuning("trial_start", "choice")
    assert [(i["kind"], i["role"]) for i in r.inputs.values()] == [
        ("defaults", "tuning"),
        ("recipe", "tuning"),
    ]


def test_rates_outside_a_run_record_nothing(fakes: Fakes) -> None:
    session = _load(fakes, D1775, "sub-UT06", S1775)
    session.rates("trial_start")
    assert active() is None


def test_tuning_names_a_missing_attribute(fakes: Fakes) -> None:
    session = _load(fakes, D1775, "sub-UT06", S1775)
    with pytest.raises(ValueError, match=r"trial_start\.nope"):
        session.tuning("trial_start", "nope")


def test_units_in_two_files_are_refused(fakes: Fakes) -> None:
    fakes.catalog.con.execute(
        "INSERT INTO units (asset_id, dataset, subject, bids_session, unit_index, "
        "spike_count) VALUES ('other', ?, 'sub-UT06', ?, 0, 1)",
        [D1775, S1775],
    )
    with pytest.raises(ValueError, match="several files"):
        _load(fakes, D1775, "sub-UT06", S1775)


def test_the_package_exports_the_loaders() -> None:
    for name in ("sessions_index", "load_session", "Session"):
        assert name in asteroidea.__all__


def test_to_pynapple_units_are_a_tsgroup_with_location(fakes: Fakes) -> None:
    session = _load(fakes, D623, "sub-CS62", S623)
    units = session.to_pynapple().units
    assert isinstance(units, nap.TsGroup)
    assert list(units.keys()) == [0, 1, 2, 3]
    assert [len(units[k]) for k in units] == [300, 300, 300, 300]
    assert units["location"].to_list() == ["AMY", "AMY", "HIP", ""]
    assert units[2].t.tolist() == pytest.approx(session.spikes[2].tolist())


def test_to_pynapple_events_one_intervalset_per_type(fakes: Fakes) -> None:
    for ds, subject, ses, counts in (
        (
            D623,
            "sub-CS62",
            S623,
            {"scene_cut": 93, "recognition_onset": 40, "movie_onset": 1},
        ),
        (D1775, "sub-UT06", S1775, {"trial_start": 20, "reward_grasp": 20}),
    ):
        events = _load(fakes, ds, subject, ses).to_pynapple().events
        assert {k: len(v) for k, v in events.items()} == counts
        assert all(isinstance(v, nap.IntervalSet) for v in events.values())


def test_to_pynapple_point_event_lasts_one_microsecond(fakes: Fakes) -> None:
    session = _load(fakes, D623, "sub-CS62", S623)
    cuts = session.to_pynapple().events["scene_cut"]
    source = [e for e in session.alignment_events if e.event_type == "scene_cut"]
    assert all(e.stop_s is None for e in source)
    assert cuts.start[0] == pytest.approx(min(e.onset_s for e in source))
    assert (cuts.end - cuts.start).tolist() == pytest.approx([1e-6] * 93, abs=1e-9)


def test_to_pynapple_interval_event_keeps_its_stop(fakes: Fakes) -> None:
    session = _load(fakes, D1775, "sub-UT06", S1775)
    source = [e for e in session.alignment_events if e.event_type == "trial_start"]
    span = session.to_pynapple().events["trial_start"]
    assert span.start[0] == pytest.approx(min(e.onset_s for e in source))
    first = min(source, key=lambda e: e.onset_s)
    assert first.stop_s is not None
    assert span.end[0] == pytest.approx(first.stop_s)


def test_to_pynapple_lfp_is_a_tsdframe_on_the_trials_clock(fakes: Fakes) -> None:
    session = _load(fakes, D623, "sub-CS62", S623)
    window = session.lfp(LFP623, start_s=2.0, duration_s=0.5)
    frame = session.to_pynapple(window).lfp
    assert isinstance(frame, nap.TsdFrame)
    assert frame.shape == (500, 3)
    assert list(frame.columns) == [0, 1, 2]
    assert frame.t[0] == pytest.approx(2.0)
    assert frame.values[0, 0] == 12000 * 3
    assert session.to_pynapple().lfp is None


def test_to_pynapple_without_the_extra_names_it(
    fakes: Fakes, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = importlib.import_module

    def refuse(name: str, package: str | None = None) -> ModuleType:
        if name == "pynapple":
            raise ImportError(name)
        return real(name, package)

    session = _load(fakes, D623, "sub-CS62", S623)
    monkeypatch.setattr(importlib, "import_module", refuse)
    with pytest.raises(ExtraMissingError, match=r"asteroidea\[pynapple\]"):
        session.to_pynapple()


class _CountedData:
    """An array whose slice reads are recorded, like an h5py dataset."""

    def __init__(self, data: np.ndarray) -> None:
        self.data = data
        self.shape = data.shape
        self.reads: list[tuple[int, int]] = []

    def __len__(self) -> int:
        return int(self.data.shape[0])

    def __getitem__(self, key: slice) -> np.ndarray:
        self.reads.append((int(key.start), int(key.stop)))
        return self.data[key]


def _burst_lfp(times: np.ndarray, onsets: Sequence[float]) -> np.ndarray:
    # channel 0: unit noise plus a 10 Hz sine of amplitude 1 for 1 s after each
    # onset; channel 1: noise only
    rng = np.random.default_rng(11)
    data = rng.standard_normal((times.size, 2))
    for onset in onsets:
        inside = (times >= onset) & (times < onset + 1.0)
        data[inside, 0] += np.sin(2 * np.pi * 10.0 * (times[inside] - onset))
    return data


def _alpha_rise(frame: pl.DataFrame, channel: int) -> float:
    rows = frame.filter((pl.col("channel") == channel) & (pl.col("band") == "alpha"))
    post = rows.filter((pl.col("time_s") > 0.25) & (pl.col("time_s") < 0.75))
    pre = rows.filter(pl.col("time_s") < 0)
    return float(post["mean"].to_numpy().mean() - pre["mean"].to_numpy().mean())


def test_band_power_000623_applies_the_10_s_offset(fakes: Fakes) -> None:
    session = _load(fakes, D623, "sub-CS62", S623)
    cuts = [e.onset_s for e in session.alignment_events if e.event_type == "scene_cut"]
    times = np.arange(0.0, 478.0, 0.001)  # LFP clock; ends before the last cut
    # the bursts sit on the LFP clock: trials onset + 10 s
    data = _CountedData(_burst_lfp(times, [o + 10.0 for o in cuts]))
    nwb = cast("Any", fakes.nwb[A623])
    nwb.processing["ecephys"]["LFP_micro"]["ElectricalSeries"].data = data
    frame = session.band_power("scene_cut", bands=["alpha", "beta"])
    assert frame["series"].unique().to_list() == [LFP623]
    assert set(frame["band"].unique().to_list()) == {"alpha", "beta"}
    assert frame["lfp_clock_offset_s"].unique().to_list() == [10.0]
    assert frame["baseline"].unique().to_list() == ["zscore_over_time"]
    assert frame["label"].unique().to_list() == ["exploratory screen"]
    assert frame["defaults_version"].unique().to_list() == ["defaults-1"]
    # cuts whose read [onset + 10 - 3, onset + 10 + 4] leaves 0..478 s are dropped
    late = sum(1 for o in cuts if o + 14.0 > 478.0)
    assert late > 0
    assert frame["n_dropped"].unique().to_list() == [late]
    assert frame["n"].unique().to_list() == [len(cuts) - late]
    assert _alpha_rise(frame, 0) > 0.5
    assert abs(_alpha_rise(frame, 1)) < 0.5
    # per-event windows only: fewer samples read than the recording holds
    assert sum(hi - lo for lo, hi in data.reads) < 0.9 * times.size


def test_band_power_001775_timestamps_and_no_offset(fakes: Fakes) -> None:
    session = _load(fakes, D1775, "sub-UT06", S1775)
    trials = [
        e.onset_s for e in session.alignment_events if e.event_type == "trial_start"
    ]
    # 500 Hz by timestamps, covering only the first five trials
    count = int((trials[4] - trials[0] + 40.0) * 500)
    stamps = (trials[0] - 20.0) + np.arange(count) / 500.0
    nwb = cast("Any", fakes.nwb[A1775])
    series = nwb.processing["ecephys"]["LFP"]["ElectricalSeries"]
    series.data = _CountedData(_burst_lfp(stamps, trials))
    series.timestamps = stamps
    frame = session.band_power("trial_start", series=LFP1775, channels=[0, 1])
    assert frame["lfp_clock_offset_s"].unique().to_list() == [0.0]
    assert frame["baseline"].unique().to_list() == ["pre_window_mean"]
    assert frame["n"].unique().to_list() == [5]
    assert frame["n_dropped"].unique().to_list() == [len(trials) - 5]
    assert _alpha_rise(frame, 0) > 3.0  # dB
    assert abs(_alpha_rise(frame, 1)) < 1.0


def test_band_power_in_a_run_records_the_defaults_and_recipe_versions(
    fakes: Fakes,
) -> None:
    # Band power records its versions as rates does
    session = _load(fakes, D1775, "sub-UT06", S1775)
    trials = [
        e.onset_s for e in session.alignment_events if e.event_type == "trial_start"
    ]
    count = int((trials[4] - trials[0] + 40.0) * 500)
    stamps = (trials[0] - 20.0) + np.arange(count) / 500.0
    nwb = cast("Any", fakes.nwb[A1775])
    series = nwb.processing["ecephys"]["LFP"]["ElectricalSeries"]
    series.data = _CountedData(_burst_lfp(stamps, trials))
    series.timestamps = stamps
    version = analysis.load_defaults(D1775).version
    with Run("bands") as r:
        session.band_power("trial_start", series=LFP1775, channels=[0, 1])
    assert list(r.inputs.values()) == [
        {
            "kind": "defaults",
            "id": f"analysis_defaults/{D1775}",
            "version": version,
            "role": "band_power",
        },
        {
            "kind": "recipe",
            "id": f"recipes/{D1775}",
            "version": "recipe-1",
            "role": "band_power",
        },
    ]
    assert r.record()["run"]["defaults_version"] == version


def test_a_run_records_the_assets_the_default_backend_reads(
    fakes: Fakes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """load_session and Session.lfp read CSV and NWB bytes: a run records each."""
    monkeypatch.setattr(_session, "default_auth", lambda: None)
    monkeypatch.setattr(
        _sql, "sql", lambda _auth, query, params: fakes.sql(query, params)
    )
    monkeypatch.setattr(
        _image, "open_asset", lambda _auth, asset_id: fakes.open_file(asset_id)
    )
    monkeypatch.setattr(
        _nwb,
        "read_nwb",
        lambda remote: fakes.nwb[remote.source.key.removeprefix("raw/")],
    )
    fakes.stores[A623] = Store(b"nwb bytes")
    with Run("rates") as r:
        session = _session.load_session(D623, "sub-CS62", S623)
        session.lfp(LFP623, start_s=2.0, duration_s=0.01)
    assets = [i for i in r.record()["inputs"] if i["kind"] == "asset"]
    assert assets == [
        {"kind": "asset", "id": CSV_ASSET, "version": "v1", "role": "session"},
        {"kind": "asset", "id": A623, "version": "v1", "role": "session"},
    ]
