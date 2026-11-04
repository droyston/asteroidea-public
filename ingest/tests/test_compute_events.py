"""`event:` align and the `/compute/events` handler on the CS62 R2 rows.

The rows are the SDK's `sdk/tests/fixtures/dandi-000623` files (the session's
`intervals`, the pinned `scenecut_info.csv` and the movie-frame timestamps). A pynwb
file carries those timestamps as `stimulus/presentation/movieframe_time`, so the
frame series is read through the real `RemoteFile`, `BlockFile` and h5py path. The
oracle is `analysis.resolve_events` on the same rows, the resolver the route calls.
"""

import csv
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, override

import numpy as np
import pytest
from pynwb import NWBHDF5IO, NWBFile, TimeSeries

from asteroidea import analysis
from asteroidea_ingest import api_common, compute_api
from asteroidea_ingest.compute_api import ComputeApi

from .test_compute import (
    ASSET_COLS,
    Cols,
    Rows,
    StubAthena,
    StubLabels,
    StubS3,
    _get,
    _texts,
)

new_nwbfile: Any = NWBFile

FIXTURES: Final = Path(__file__).parents[2] / "sdk" / "tests" / "fixtures"
DATASET: Final = "dandi-000623"
CSV_PATH: Final = "stimuli/scenecut_info.csv"
CSV_KEY: Final = f"raw/{DATASET}/{CSV_PATH}"
SERIES: Final = "stimulus/presentation/movieframe_time"
NWB_KEY: Final = (
    f"raw/{DATASET}/sub-CS62/ses-P62CSR2/ecephys/sub-CS62_ses-P62CSR2_ecephys.nwb"
)
SPIKES: Final = ([10.0, 500.0, 501.0, 900.0], [2.0, 491.0])
BASE: Final = {"units": "0,1", "pre_s": "1", "post_s": "2", "bin_s": "0.5"}
ROWS: Final[list[dict[str, Any]]] = json.loads(
    (FIXTURES / DATASET / "intervals.json").read_text("utf-8")
)
ASSET_ID: Final = str(ROWS[0]["asset_id"])
STAMPS: Final = [
    float(t)
    for t in json.loads(
        (FIXTURES / DATASET / "movieframe_timestamps.json").read_text("utf-8")
    )["timestamps"]
]
CSV_BYTES: Final = (FIXTURES / DATASET / "scenecut_info.csv").read_bytes()
CSV_SIZE: Final = str(len(CSV_BYTES))
# The truncated file ends its frame series before the last cuts' first frames.
SHORT: Final = 11_000


def _write(path: Path, stamps: list[float]) -> None:
    nwb = new_nwbfile(
        session_description="cs62 r2 rows",
        identifier="cs62",
        session_start_time=datetime(2019, 4, 1, 9, 30, tzinfo=UTC),
    )
    for times in SPIKES:
        nwb.add_unit(spike_times=times)
    nwb.add_stimulus(
        TimeSeries(
            name="movieframe_time",
            data=np.zeros(len(stamps)),
            unit="s",
            timestamps=np.asarray(stamps),
        )
    )
    with NWBHDF5IO(str(path), "w") as io:
        io.write(nwb)


@pytest.fixture(scope="module", params=[len(STAMPS), SHORT], ids=["full", "short"])
def nwb(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory):
    count = int(request.param)
    path = tmp_path_factory.mktemp("cs62") / "cs62.nwb"
    _write(path, STAMPS[:count])
    return count, path.read_bytes()


def _cell(value: object) -> str | None:
    return None if value is None else str(value)


class CsAthena(StubAthena):
    """The CS62 R2 asset, its stimulus CSV and its catalog rows, as Athena text."""

    @override
    def _answer(self, sql: str, params: list[str]) -> tuple[Cols, Rows]:
        if "FROM assets" in sql:
            rows: Rows = []
            if "s3_key = ?" in sql:
                if params == [DATASET, CSV_KEY]:
                    rows = [
                        ["csv-623", DATASET, None, None, CSV_KEY, "v1", CSV_SIZE, "csv"]
                    ]
            elif params[0] == ASSET_ID:
                key = NWB_KEY
                rows = [[ASSET_ID, DATASET, "sub-CS62", "ses-P62CSR2", key, "v1"]]
                rows[0] += [str(self.size), "nwb"]
            elif params[0] == "csv-623":
                rows = [
                    ["csv-623", DATASET, None, None, CSV_KEY, "v1", CSV_SIZE, "csv"]
                ]
            return ASSET_COLS, rows
        if "FROM intervals" in sql:
            cols: Cols = [
                ("table_name", "varchar"),
                ("row_index", "bigint"),
                ("start_time", "double"),
                ("stop_time", "double"),
                ("columns_json", "varchar"),
            ]
            names = [c for c, _ in cols]
            mine = params == [ASSET_ID]
            return cols, [[_cell(r[n]) for n in names] for r in ROWS] if mine else []
        if "FROM unit_spikes" in sql:
            wanted = [int(u) for u in params[1].split(",")]
            spike_cols: Cols = [("unit_index", "bigint"), ("spike_times", "array")]
            return spike_cols, [[str(u), str(list(SPIKES[u]))] for u in wanted]
        if "FROM units" in sql:
            unit_cols: Cols = [
                ("unit_index", "bigint"),
                ("spike_count", "bigint"),
                ("first_spike_s", "double"),
                ("last_spike_s", "double"),
            ]
            return unit_cols, [
                [str(u), str(len(t)), str(t[0]), str(t[-1])]
                for u, t in enumerate(SPIKES)
            ]
        if "FROM series" in sql:
            assert params == [ASSET_ID, f"/{SERIES}"]
            series_cols: Cols = [
                ("path", "varchar"),
                ("has_timestamps", "boolean"),
                ("rate_hz", "double"),
                ("starting_time_s", "double"),
            ]
            return series_cols, [[f"/{SERIES}", "true", None, None]]
        msg = f"unrouted query {sql}"
        raise AssertionError(msg)


def _api(size_bytes: bytes) -> tuple[ComputeApi, CsAthena]:
    athena = CsAthena(size=len(size_bytes))
    s3 = StubS3({NWB_KEY: size_bytes, CSV_KEY: CSV_BYTES})
    api = ComputeApi(
        athena=athena,
        workgroup="wg",
        database="db",
        s3=s3,
        raw_bucket="raw-bucket",
        clock=lambda: athena.clock,
        sleep=athena.sleep,
        labels=StubLabels([]),
        labels_table="labels",
    )
    return api, athena


def _oracle(count: int) -> list[analysis.AlignmentEvent]:
    with (FIXTURES / DATASET / "scenecut_info.csv").open(encoding="utf-8") as f:
        cuts = list(csv.DictReader(f))
    sources = analysis.EventSources(
        ASSET_ID, intervals=ROWS, csv={CSV_PATH: cuts}, frames={SERIES: STAMPS[:count]}
    )
    return analysis.resolve_events(analysis.load_recipe(DATASET), sources)


def _onsets(events: list[analysis.AlignmentEvent], name: str, **attrs: str) -> Any:
    kept = [
        e
        for e in events
        if e.event_type == name
        and all(str(e.attributes.get(k)) == v for k, v in attrs.items())
    ]
    return np.asarray([e.onset_s for e in kept])


@pytest.mark.parametrize(
    ("align", "name", "attrs", "full_count"),
    [
        ("event:scene_cut", "scene_cut", {}, 93),
        ("event:recognition_onset", "recognition_onset", {}, 40),
        ("event:scene_cut:cut_kind=change", "scene_cut", {"cut_kind": "change"}, 13),
        (
            "event:recognition_onset:familiarity=novel",
            "recognition_onset",
            {"familiarity": "novel"},
            16,
        ),
        ("event:movie_onset", "movie_onset", {}, 1),
    ],
)
def test_event_align_on_the_cs62_r2_rows(
    nwb: tuple[int, bytes],
    align: str,
    name: str,
    attrs: dict[str, str],
    full_count: int,
) -> None:
    count, data = nwb
    api, athena = _api(data)
    status, body = _get(api, {**BASE, "asset_id": ASSET_ID, "align": align})
    assert status == 200, body
    onsets = _onsets(_oracle(count), name, **attrs)
    if count == len(STAMPS):
        assert body["n_trials"] == full_count
    assert body["n_trials"] == onsets.size
    spikes = {u: np.asarray(t) for u, t in enumerate(SPIKES)}
    want = analysis.rates(spikes, onsets, 1.0, 2.0, 0.5)
    for row, unit in enumerate(body["units"]):
        np.testing.assert_allclose(unit["rate_hz"], want.rate_hz[row], rtol=1e-5)
    texts = _texts(athena)
    assert texts[compute_api.ALL_INTERVALS_SQL] == [f"'{ASSET_ID}'"]
    if name == "scene_cut":
        assert texts[compute_api.STIMULUS_SQL] == [f"'{DATASET}'", f"'{CSV_KEY}'"]
        assert texts[compute_api.SERIES_SQL] == [f"'{ASSET_ID}'", f"'/{SERIES}'"]
        frames = [int(float(r["shot_start_fr"])) for r in _cuts()]
        assert body["missing_frames"] == [f for f in frames if f > count]
    else:
        assert compute_api.STIMULUS_SQL not in texts  # only the named type is read
        assert "missing_frames" not in body


def _cuts() -> list[dict[str, str]]:
    with (FIXTURES / DATASET / "scenecut_info.csv").open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


@pytest.mark.parametrize(
    ("align", "status", "code"),
    [
        ("event:nope", 404, "no_such_event_type"),
        ("event:scene_cut:colour=red", 404, "no_such_attribute"),
        ("event:scene_cut:noequals", 400, "malformed_align"),
    ],
)
def test_event_align_refusals_carry_their_code(
    nwb: tuple[int, bytes], align: str, status: int, code: str
) -> None:
    api, _ = _api(nwb[1])
    got, body = _get(api, {**BASE, "asset_id": ASSET_ID, "align": align})
    assert (got, body["error"]) == (status, code)


def _request(query: dict[str, str]) -> api_common.Request:
    return api_common.Request(
        route=api_common.ROUTES_BY_KEY["GET /compute/rates"],
        sub="alice",
        email=None,
        groups=frozenset({"researchers"}),
        path_params={},
        query=query,
        domain="",
        raw_body=None,
        is_base64=False,
        now=0,
        request_id="local",
        remaining_ms=28_000,
    )


def test_the_events_handler_lists_types_attributes_averaging_and_missing_frames(
    nwb: tuple[int, bytes],
) -> None:
    count, data = nwb
    api, _ = _api(data)
    resp = api.events(_request({"asset_id": ASSET_ID}), api_common.Timing())
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["recipe"] == {
        "version": "recipe-1",
        "default": False,
        "lfp_clock_offset_s": 10.0,
    }
    by_name = {t["name"]: t for t in body["event_types"]}
    assert list(by_name) == ["scene_cut", "recognition_onset", "movie_onset"]
    oracle = _oracle(count)
    for name, entry in by_name.items():
        want = [e for e in oracle if e.event_type == name]
        assert entry["n_events"] == len(want)
        assert entry["averaging"] == want[0].averaging
        assert [e["onset_s"] for e in entry["events"]] == [e.onset_s for e in want]
        assert [e["attributes"] for e in entry["events"]] == [
            dict(e.attributes) for e in want
        ]
    assert by_name["movie_onset"]["averaging"] == "single"
    assert by_name["recognition_onset"]["n_events"] == 40
    rec = by_name["recognition_onset"]["events"]
    assert sum(e["attributes"].get("familiarity") == "novel" for e in rec) == 16
    frames = [int(float(r["shot_start_fr"])) for r in _cuts()]
    late = [f for f in frames if f > count]
    assert by_name["scene_cut"]["missing_frames"] == late
    assert by_name["scene_cut"]["n_events"] == 93 - len(late)
    if count == len(STAMPS):
        assert late == []
        cuts = [e["attributes"]["cut_kind"] for e in by_name["scene_cut"]["events"]]
        assert (cuts.count("change"), cuts.count("continuity")) == (13, 80)
    else:
        assert late  # the short file drops the last cuts and names their frames
    assert "missing_frames" not in by_name["recognition_onset"]


def test_the_events_handler_refuses_a_missing_or_non_nwb_asset(
    nwb: tuple[int, bytes],
) -> None:
    api, _ = _api(nwb[1])
    with pytest.raises(api_common.ApiError) as missing:
        api.events(_request({"asset_id": "nope"}), api_common.Timing())
    assert missing.value.status == 404
    with pytest.raises(api_common.ApiError) as wrong:
        api.events(_request({"asset_id": "csv-623"}), api_common.Timing())
    assert (wrong.value.status, wrong.value.code) == (422, "not_nwb")
