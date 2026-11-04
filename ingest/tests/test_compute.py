"""`GET /compute/rates` over a pynwb fixture, stub Athena and stub S3.

The fixture file holds three units (spike times known by hand), a trials table with a
`condition` column and a stimulus TimeSeries with timestamps. The stub S3 serves the
file's bytes by `Range`, so the file source runs the real `RemoteFile`, `BlockFile`
and h5py path.
"""

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

import numpy as np
import pytest
from pynwb import NWBHDF5IO, NWBFile, TimeSeries

from asteroidea import analysis
from asteroidea_ingest import compute_api
from asteroidea_ingest.compute_api import ComputeApi

from . import ddb_rules
from .conftest import jwt_event

if TYPE_CHECKING:
    from pathlib import Path

# pynwb declares its fields dynamically and its NWBFile reads as abstract to ty.
new_nwbfile: Any = NWBFile

NWB_ID: Final = "nwb1"
BUCKET: Final = "raw-bucket"
SPIKES: Final = ([0.5, 1.0, 1.1, 2.5, 5.2], [1.05, 2.05], [])
ONSETS: Final = (1.0, 2.0, 5.0)
CONDITIONS: Final = ("a", "b", "a")
STAMPS: Final = tuple(1.0 + k for k in range(10))  # frame f -> 1.0 + (f - 1)
CUTS_CSV: Final = b"shot,shot_start_fr\n1,1\n2,3\n3,11\n"
ROUTE: Final = "GET /compute/rates"
NWB_KEY: Final = "raw/ds1/sub-1/ses-1/ecephys/sub-1_ses-1_task-x_ecephys.nwb"
SERIES_PATH: Final = "/stimulus/presentation/movieframe_time"
BASE: Final = {
    "asset_id": NWB_ID,
    "units": "0",
    "pre_s": "1",
    "post_s": "1",
    "bin_s": "0.5",
}
# Hand-derived for unit 0, onsets 1, 2, 5 and bins [-1,-.5) [-.5,0) [0,.5) [.5,1):
# relative spikes -0.5, 0.0, 0.1 | -1.0, -0.9, 0.5 | 0.2 give counts 2, 1, 3, 1 over
# 3 trials x 0.5 s.
EXPECT_ALL: Final = [2 / 1.5, 1 / 1.5, 3 / 1.5, 1 / 1.5]


def _write(path: Path) -> None:
    nwb = new_nwbfile(
        session_description="d",
        identifier="id-1",
        session_start_time=datetime(2019, 4, 1, 9, 30, tzinfo=UTC),
    )
    for times in SPIKES:
        nwb.add_unit(spike_times=times)
    nwb.add_trial_column(name="condition", description="condition")
    for start, cond in zip(ONSETS, CONDITIONS, strict=True):
        nwb.add_trial(start_time=start, stop_time=start + 0.5, condition=cond)
    nwb.add_stimulus(
        TimeSeries(
            name="movieframe_time",
            data=np.zeros(10),
            unit="s",
            timestamps=np.asarray(STAMPS),
        )
    )
    with NWBHDF5IO(str(path), "w") as io:
        io.write(nwb)


@pytest.fixture(scope="module")
def nwb_bytes(tmp_path_factory: pytest.TempPathFactory) -> bytes:
    path = tmp_path_factory.mktemp("compute") / "fixture.nwb"
    _write(path)
    return path.read_bytes()


class StubBody:
    def __init__(self, data: bytes) -> None:
        self.data = data

    def read(self, amount: int | None = None, /) -> bytes:
        return self.data if amount is None else self.data[:amount]


class StubS3:
    """Serves objects by key; `get_object` honours `Range: bytes=a-b`."""

    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects = objects
        self.requests: list[dict[str, str | int]] = []

    def get_object(self, **kwargs: str | int) -> Any:
        self.requests.append(kwargs)
        data = self.objects[str(kwargs["Key"])]
        found = re.fullmatch(r"bytes=(\d+)-(\d+)", str(kwargs["Range"]))
        assert found is not None
        return {"Body": StubBody(data[int(found[1]) : int(found[2]) + 1])}


def _asset(asset_id: str, key: str, size: int, subject: str = "sub-1") -> list[str]:
    return [asset_id, "ds1", subject, "ses-1", key, "v1", str(size), "nwb"]


ASSET_COLS: Final = [
    ("asset_id", "varchar"),
    ("dataset", "varchar"),
    ("subject", "varchar"),
    ("bids_session", "varchar"),
    ("s3_key", "varchar"),
    ("s3_version_id", "varchar"),
    ("size_bytes", "bigint"),
    ("format", "varchar"),
]
Cols = list[tuple[str, str]]
Rows = list[list[str | None]]


@dataclass
class StubAthena:
    """Routes each query by its table; every query succeeds at once unless told."""

    size: int
    catalog_spikes: bool = True
    states: list[str] = field(default_factory=lambda: ["SUCCEEDED"])
    clock: float = 0.0
    started: list[dict[str, Any]] = field(default_factory=list)
    stopped: list[str] = field(default_factory=list)
    results: dict[str, tuple[Cols, Rows]] = field(default_factory=dict)
    # Units the file holds; past len(SPIKES) a unit has no spikes.
    n_units: int = len(SPIKES)
    # A `units.spike_count` for every unit, for the body pre-check; None counts them.
    spike_count: int | None = None

    def _spikes_of(self, unit: int) -> list[float]:
        return list(SPIKES[unit]) if unit < len(SPIKES) else []

    def _unit_row(self, unit: int) -> list[str | None]:
        times = self._spikes_of(unit)
        count = len(times) if self.spike_count is None else self.spike_count
        first, last = (0.0, 10.0) if self.spike_count is not None else (0.5, 5.2)
        return [str(unit), str(count), str(first), str(last)]

    def sleep(self, seconds: float) -> None:
        self.clock += seconds

    def assets(self) -> dict[str, list[str]]:
        return {
            NWB_ID: _asset(NWB_ID, NWB_KEY, self.size),
            "ev1": _asset("ev1", NWB_KEY.replace("ecephys.nwb", "events.tsv"), 10),
            "ev2": _asset(
                "ev2", NWB_KEY.replace("task-x_ecephys", "task-y_events"), 10
            ),
            "evsub": _asset(
                "evsub",
                NWB_KEY.replace("ecephys.nwb", "events.tsv"),
                10,
                subject="sub-2",
            ),
            "csv1": _asset("csv1", "raw/ds1/stimuli/cuts.csv", len(CUTS_CSV)),
        }

    def _answer(self, sql: str, params: list[str]) -> tuple[Cols, Rows]:
        if "FROM assets" in sql:
            row = self.assets().get(params[0])
            return ASSET_COLS, [] if row is None else [[*row]]
        if "FROM unit_spikes" in sql:
            wanted = [int(u) for u in params[1].split(",")]
            cols: Cols = [("unit_index", "bigint"), ("spike_times", "array")]
            rows: Rows = [
                [str(u), str(self._spikes_of(u))] for u in wanted if u < self.n_units
            ]
            return cols, rows if self.catalog_spikes else []
        if "FROM units" in sql:
            every = range(self.n_units)
            wanted = (
                every if len(params) == 1 else [int(u) for u in params[1].split(",")]
            )
            unit_cols: Cols = [
                ("unit_index", "bigint"),
                ("spike_count", "bigint"),
                ("first_spike_s", "double"),
                ("last_spike_s", "double"),
            ]
            return unit_cols, [self._unit_row(u) for u in wanted if u < self.n_units]
        if "FROM intervals" in sql:
            cols = [
                ("row_index", "bigint"),
                ("start_time", "double"),
                ("columns_json", "varchar"),
            ]
            trials: Rows = [
                [str(i), str(t), json.dumps({"condition": c})]
                for i, (t, c) in enumerate(zip(ONSETS, CONDITIONS, strict=True))
            ]
            return cols, trials if params[1] == "trials" else []
        if "FROM events" in sql:
            cols = [
                ("row_index", "bigint"),
                ("onset_s", "double"),
                ("trial_type", "varchar"),
                ("columns_json", "varchar"),
            ]
            kinds = ("go", "stop", "go")
            return cols, [
                [str(i), str(t), k, None]
                for i, (t, k) in enumerate(zip(ONSETS, kinds, strict=True))
            ]
        if "FROM series" in sql:
            cols = [
                ("path", "varchar"),
                ("has_timestamps", "boolean"),
                ("rate_hz", "double"),
                ("starting_time_s", "double"),
            ]
            found = params[1] == SERIES_PATH
            return cols, [[SERIES_PATH, "true", None, None]] if found else []
        msg = f"unrouted query {sql}"
        raise AssertionError(msg)

    def start_query_execution(self, **kwargs: Any) -> dict[str, Any]:
        self.started.append(kwargs)
        qid = f"q{len(self.started)}"
        self.results[qid] = self._answer(
            kwargs["QueryString"],
            [_unquote(p) for p in kwargs.get("ExecutionParameters", [])],
        )
        return {"QueryExecutionId": qid}

    def get_query_execution(self, **_: Any) -> dict[str, Any]:
        state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        return {"QueryExecution": {"Status": {"State": state}}}

    def get_query_results(self, **kwargs: Any) -> dict[str, Any]:
        cols, rows = self.results[kwargs["QueryExecutionId"]]
        body = [
            {"Data": [{} if v is None else {"VarCharValue": v} for v in r]}
            for r in rows
        ]
        return {
            "ResultSet": {
                "Rows": [{"Data": [{"VarCharValue": n} for n, _ in cols]}, *body],
                "ResultSetMetadata": {
                    "ColumnInfo": [{"Name": n, "Type": t} for n, t in cols]
                },
            }
        }

    def stop_query_execution(self, **kwargs: Any) -> dict[str, Any]:
        self.stopped.append(kwargs["QueryExecutionId"])
        return {}


def _unquote(wire: str) -> str:
    """Undo `render_param` for a str, as Athena reads the literal."""
    if wire.startswith("'"):
        return wire[1:-1].replace("''", "'")
    return wire


@dataclass
class StubLabels:
    """A DynamoDB stand-in holding label items; `query` honours the partition key.

    `leaky` makes `query` ignore the key condition: the bug the handler's own asset
    check guards against.
    """

    items: list[dict[str, Any]]
    leaky: bool = False

    def query(self, **kwargs: Any) -> dict[str, Any]:
        ddb_rules.check(kwargs)
        asset = kwargs["ExpressionAttributeValues"][":a"]["S"]
        rows = [i for i in self.items if self.leaky or i["asset_id"] == asset]
        return {"Items": [_to_item(i) for i in rows]}


def _to_item(item: dict[str, Any]) -> dict[str, Any]:
    return {
        k: {"N": str(v)} if isinstance(v, float) else {"S": v} for k, v in item.items()
    }


def _label(
    asset: str, label_id: str, name: str, start: float, author: str
) -> dict[str, Any]:
    return {
        "asset_id": asset,
        "label_id": label_id,
        "name": name,
        "start_s": start,
        "stop_s": start + 1.0,
        "author_sub": author,
    }


LABELS: Final = [
    _label(NWB_ID, "l1", "burst", 1.0, "alice"),
    _label(NWB_ID, "l2", "burst", 2.0, "bob"),
    _label(NWB_ID, "l3", "other", 5.0, "alice"),
    _label("other-asset", "l9", "burst", 3.0, "alice"),
]


def _api(
    nwb_bytes: bytes,
    *,
    catalog: bool = True,
    labels: StubLabels | None = None,
    n_units: int = len(SPIKES),
    spike_count: int | None = None,
) -> tuple[ComputeApi, StubAthena]:
    athena = StubAthena(
        size=len(nwb_bytes),
        catalog_spikes=catalog,
        n_units=n_units,
        spike_count=spike_count,
    )
    s3 = StubS3({NWB_KEY: nwb_bytes, "raw/ds1/stimuli/cuts.csv": CUTS_CSV})
    api = ComputeApi(
        athena=athena,
        workgroup="wg",
        database="db",
        s3=s3,
        raw_bucket=BUCKET,
        clock=lambda: athena.clock,
        sleep=athena.sleep,
        labels=labels if labels is not None else StubLabels(list(LABELS)),
        labels_table="labels",
    )
    return api, athena


def _get(
    api: ComputeApi,
    query: dict[str, str],
    *,
    remaining_ms: int = 28_000,
    groups: str = "[researchers]",
) -> tuple[int, dict[str, Any]]:
    event = {**jwt_event(ROUTE, groups=groups), "queryStringParameters": query}
    resp = api.handle(event, remaining_ms)
    # strict parse: a NaN or Infinity token in the body fails the test
    body = json.loads(resp["body"], parse_constant=_no_constant)
    return resp["statusCode"], body


def _no_constant(token: str) -> None:
    msg = f"invalid JSON constant {token} in the body"
    raise AssertionError(msg)


def test_intervals_rates_match_the_hand_derived_psth(nwb_bytes: bytes) -> None:
    api, _ = _api(nwb_bytes)
    status, body = _get(api, {**BASE, "align": "intervals:trials"})
    assert status == 200
    assert body["source"] == "catalog"
    assert body["n_trials"] == 3
    assert body["edges"] == [-1.0, -0.5, 0.0, 0.5, 1.0]
    unit = body["units"][0]
    assert unit["unit_index"] == 0
    assert unit["rate_hz"] == pytest.approx(EXPECT_ALL, rel=1e-5)
    assert "z" not in unit
    assert unit["raster"] == {
        "trial": [0, 0, 0, 1, 1, 1, 2],
        "t": [-0.5, 0.0, 0.1, -1.0, -0.9, 0.5, 0.2],
    }
    direct = analysis.rates(
        {0: np.asarray(SPIKES[0])}, np.asarray(ONSETS), 1.0, 1.0, 0.5
    )
    assert unit["rate_hz"] == pytest.approx(direct.rate_hz[0].tolist(), rel=1e-5)


def test_the_condition_filter_keeps_matching_trials(nwb_bytes: bytes) -> None:
    api, _ = _api(nwb_bytes)
    status, body = _get(api, {**BASE, "align": "intervals:trials:condition=a"})
    assert status == 200
    assert body["n_trials"] == 2  # onsets 1 and 5: counts 0, 1, 3, 0 over 2 x 0.5 s
    assert body["units"][0]["rate_hz"] == pytest.approx([0, 1, 3, 0])


def test_an_unknown_table_or_column_is_404(nwb_bytes: bytes) -> None:
    api, _ = _api(nwb_bytes)
    assert _get(api, {**BASE, "align": "intervals:nope"})[0] == 404
    assert _get(api, {**BASE, "align": "intervals:trials:nope=a"})[0] == 404


def test_events_with_a_mismatched_entity_key_are_422(nwb_bytes: bytes) -> None:
    api, _ = _api(nwb_bytes)
    for other in ("ev2", "evsub"):  # other task entity; other subject
        status, body = _get(api, {**BASE, "align": f"events:{other}"})
        assert status == 422, other
        assert "same dataset, subject, session" in body["detail"]


def test_events_with_a_matching_entity_key_are_accepted(nwb_bytes: bytes) -> None:
    api, _ = _api(nwb_bytes)
    status, body = _get(api, {**BASE, "align": "events:ev1"})
    assert status == 200
    assert body["units"][0]["rate_hz"] == pytest.approx(EXPECT_ALL, rel=1e-5)
    status, body = _get(api, {**BASE, "align": "events:ev1:trial_type=go"})
    assert (status, body["n_trials"]) == (200, 2)
    assert _get(api, {**BASE, "align": "events:nope"})[0] == 404


def test_frames_lists_the_out_of_range_frame(nwb_bytes: bytes) -> None:
    api, _ = _api(nwb_bytes)
    align = f"frames:csv1:shot_start_fr:{SERIES_PATH}"
    status, body = _get(api, {**BASE, "align": align})
    assert status == 200
    assert body["missing_frames"] == [11]
    assert body["n_trials"] == 2  # frames 1 and 3 -> 1.0 s and 3.0 s
    assert body["units"][0]["rate_hz"] == pytest.approx([0, 2, 2, 0])
    assert body["source"] == "catalog"
    assert _get(api, {**BASE, "align": f"frames:csv1:nope:{SERIES_PATH}"})[0] == 404
    assert _get(api, {**BASE, "align": "frames:csv1:shot_start_fr:/x"})[0] == 404
    assert _get(api, {**BASE, "align": f"frames:nope:c:{SERIES_PATH}"})[0] == 404


def test_catalog_and_file_sources_give_identical_arrays(nwb_bytes: bytes) -> None:
    query = {**BASE, "units": "0,1,2", "align": "intervals:trials", "zscore": "1"}
    from_catalog = _get(_api(nwb_bytes)[0], query)[1]
    api, _ = _api(nwb_bytes, catalog=False)
    from_file = _get(api, query)[1]
    assert (from_catalog["source"], from_file["source"]) == ("catalog", "file")
    for key in ("edges", "units", "n_trials"):
        assert from_catalog[key] == from_file[key]


def test_a_unit_with_no_baseline_variation_has_null_z(nwb_bytes: bytes) -> None:
    api, _ = _api(nwb_bytes)
    query = {**BASE, "units": "0,2", "align": "intervals:trials", "zscore": "1"}
    status, body = _get(api, query)
    assert status == 200
    assert [u["unit_index"] for u in body["units"]] == [0, 2]
    # unit 2 has no spikes: zero baseline deviation, so z is None, encoded as null
    assert body["units"][1]["z"] is None
    assert body["units"][1]["rate_hz"] == [0, 0, 0, 0]


def test_a_nan_in_the_result_is_a_500_not_invalid_json(
    nwb_bytes: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = analysis.rates

    def poisoned(*args: Any) -> analysis.Rates:
        out = real(*args)
        out.rate_hz[0, 0] = np.nan
        return out

    monkeypatch.setattr(analysis, "rates", poisoned)
    api, _ = _api(nwb_bytes)
    status, body = _get(api, {**BASE, "align": "intervals:trials"})
    assert (status, body["error"]) == (500, "internal_error")
    assert body["detail"].startswith("ValueError:")


def test_a_body_over_the_cap_is_413_naming_the_size_and_the_knob(
    nwb_bytes: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    api, _ = _api(nwb_bytes)
    status, body = _get(api, {**BASE, "align": "intervals:trials"})
    assert status == 200
    size = len(json.dumps(body, separators=(",", ":")))
    monkeypatch.setattr(compute_api, "MAX_BODY_BYTES", size // 2)
    status, body = _get(api, {**BASE, "align": "intervals:trials"})
    assert (status, body["error"]) == (413, "too_large")
    assert "bytes" in body["detail"]
    assert "raster=0" in body["detail"]


def test_the_real_cap_is_five_mebibytes() -> None:
    assert compute_api.MAX_BODY_BYTES == 5 * 1024 * 1024


@pytest.mark.parametrize(
    "override",
    [
        # A list past MAX_UNITS (152) passes the 256-character field cap first.
        {"units": ",".join(str(u) for u in range(compute_api.MAX_UNITS + 1))},
        {"units": "1,1"},
        {"units": "x"},
        {"pre_s": "10.5"},
        {"post_s": "-1"},
        {"bin_s": "0.0005"},
        {"bin_s": "0.001", "pre_s": "2", "post_s": "1"},  # 3000 bins
        {"pre_s": "nan"},
        {"align": "label:"},
        {"align": "label_name:"},
        {"align": "intervals:"},
        {"align": "intervals:trials:noequals"},
        {"align": "events:ev1:other=1"},
        {"align": "frames:csv1:col"},
        {"zscore": "maybe"},
    ],
)
def test_bounds_and_malformed_align_are_400(
    nwb_bytes: bytes, override: dict[str, str]
) -> None:
    api, athena = _api(nwb_bytes)
    status, _ = _get(api, {**BASE, "align": "intervals:trials", **override})
    assert status == 400
    assert athena.started == []


def test_the_bounds_edge_is_accepted(nwb_bytes: bytes) -> None:
    api, _ = _api(nwb_bytes)
    edge = {"pre_s": "1", "post_s": "1", "bin_s": "0.001", "align": "intervals:trials"}
    status, body = _get(api, {**BASE, **edge})  # exactly 2,000 bins
    assert status == 200
    assert len(body["units"][0]["rate_hz"]) == 2000


def test_group_asset_and_unit_refusals(nwb_bytes: bytes) -> None:
    api, athena = _api(nwb_bytes)
    query = {**BASE, "align": "intervals:trials"}
    assert _get(api, query, groups="[other]")[0] == 403
    assert athena.started == []
    assert _get(api, {**query, "asset_id": "nope"})[0] == 404
    assert _get(api, {**query, "units": "0,99"})[0] == 404


class Context:
    def __init__(self, remaining_ms: int) -> None:
        self.remaining_ms = remaining_ms

    def get_remaining_time_in_millis(self) -> int:
        return self.remaining_ms


def test_503_when_athena_outlasts_the_deadline(nwb_bytes: bytes) -> None:
    api, athena = _api(nwb_bytes)
    athena.states = ["RUNNING"]
    status, body = _get(api, {**BASE, "align": "intervals:trials"}, remaining_ms=2500)
    assert status == 503  # 0.5 s of budget; polls advance the clock 0.2 s each
    assert body == {
        "error": "slow",
        "detail": "the catalog is slow to answer; try again",
    }
    assert athena.stopped


def test_503_when_the_work_starts_past_the_deadline(nwb_bytes: bytes) -> None:
    api, _ = _api(nwb_bytes)
    status, body = _get(api, {**BASE, "align": "intervals:trials"}, remaining_ms=1000)
    assert (status, body) == (503, {"error": "slow", "detail": compute_api.SLOW})


def test_the_handler_takes_the_deadline_from_the_context(
    nwb_bytes: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    api, athena = _api(nwb_bytes)
    monkeypatch.setenv("ATHENA_WORKGROUP", "wg")
    monkeypatch.setenv("GLUE_DATABASE", "db")
    monkeypatch.setenv("RAW_BUCKET", BUCKET)
    monkeypatch.setenv("DERIVED_BUCKET", "derived-bucket")  # the LFP routes
    monkeypatch.setenv("LABELS_TABLE", "labels")
    clients = {"athena": athena, "s3": api.s3, "dynamodb": api.labels}
    monkeypatch.setattr(compute_api.boto3, "client", lambda name, **_: clients[name])
    event = {
        **jwt_event(ROUTE),
        "queryStringParameters": {**BASE, "align": "intervals:trials"},
    }
    late = compute_api.handler(event, Context(1500))
    assert late["statusCode"] == 503
    roomy = compute_api.handler(event, Context(28_000))
    assert roomy["statusCode"] == 200


def test_the_unit_list_is_a_quoted_varchar_even_for_one_unit(nwb_bytes: bytes) -> None:
    """A bare `0` is an integer to Athena and `split(0, ',')` fails (seen live)."""
    api, athena = _api(nwb_bytes)
    status, _ = _get(api, {**BASE, "units": "0", "align": "intervals:trials"})
    assert status == 200
    by_text = {s["QueryString"]: s["ExecutionParameters"] for s in athena.started}
    assert by_text[compute_api.UNITS_SQL] == [f"'{NWB_ID}'", "'0'"]
    assert by_text[compute_api.SPIKES_SQL] == [f"'{NWB_ID}'", "'0'"]
    assert by_text[compute_api.ASSET_SQL] == [f"'{NWB_ID}'"]
    assert by_text[compute_api.INTERVALS_SQL] == [f"'{NWB_ID}'", "'trials'"]


def test_label_aligns_to_one_trial(nwb_bytes: bytes) -> None:
    api, _ = _api(nwb_bytes)
    status, body = _get(api, {**BASE, "align": "label:l2", "raster": "true"})
    assert status == 200, body
    assert body["n_trials"] == 1
    # unit 0 spikes around the onset 2.0 (stop_s is ignored by the alignment)
    assert body["units"][0]["raster"]["t"] == [-1.0, -0.9, 0.5]


def test_label_name_gathers_every_authors_labels_on_the_asset(
    nwb_bytes: bytes,
) -> None:
    api, _ = _api(nwb_bytes)
    status, body = _get(api, {**BASE, "align": "label_name:burst"})
    assert status == 200, body
    assert body["n_trials"] == 2  # alice's and bob's; not "other", not the other asset
    _, one = _get(api, {**BASE, "align": "label_name:other"})
    assert one["n_trials"] == 1


@pytest.mark.parametrize(
    "align", ["label:l9", "label:nope", "label_name:missing", "label_name:other:x"]
)
def test_unknown_or_foreign_labels_are_404(nwb_bytes: bytes, align: str) -> None:
    api, _ = _api(nwb_bytes)
    status, _ = _get(api, {**BASE, "align": align})
    assert status == 404


def test_a_foreign_label_is_refused_even_if_the_query_leaks(nwb_bytes: bytes) -> None:
    """The asset check holds when the table read ignores its key condition."""
    api, _ = _api(nwb_bytes, labels=StubLabels(list(LABELS), leaky=True))
    assert _get(api, {**BASE, "align": "label:l9"})[0] == 404
    _, body = _get(api, {**BASE, "align": "label_name:burst"})
    assert body["n_trials"] == 2


# --- unit selection ---


def _texts(athena: StubAthena) -> dict[str, list[str]]:
    return {s["QueryString"]: s["ExecutionParameters"] for s in athena.started}


def test_units_all_refused_at_bound_plus_one(nwb_bytes: bytes) -> None:
    assert compute_api.MAX_UNITS == 152  # the 5 MiB body binds first
    api, athena = _api(nwb_bytes, n_units=153)
    status, body = _get(api, {**BASE, "units": "all", "align": "intervals:trials"})
    assert status == 400
    assert body["error"] == "too_many_units"
    assert "153" in body["detail"]
    assert "152" in body["detail"]
    assert _texts(athena)[compute_api.UNITS_ALL_SQL] == [f"'{NWB_ID}'"]
    assert compute_api.SPIKES_SQL not in _texts(athena)  # refused before the spikes


def test_units_all_at_the_bound_is_accepted(nwb_bytes: bytes) -> None:
    api, athena = _api(nwb_bytes, n_units=152)
    status, body = _get(
        api, {**BASE, "units": "all", "align": "intervals:trials", "raster": "0"}
    )
    assert status == 200
    assert [u["unit_index"] for u in body["units"]] == list(range(152))
    listed = ",".join(str(u) for u in range(152))
    assert _texts(athena)[compute_api.SPIKES_SQL] == [f"'{NWB_ID}'", f"'{listed}'"]


def test_a_range_resolves_against_the_files_units(nwb_bytes: bytes) -> None:
    api, athena = _api(nwb_bytes)
    status, body = _get(api, {**BASE, "units": "1-99", "align": "intervals:trials"})
    assert status == 200
    assert [u["unit_index"] for u in body["units"]] == [1, 2]
    assert _texts(athena)[compute_api.SPIKES_SQL] == [f"'{NWB_ID}'", "'1,2'"]
    status, body = _get(api, {**BASE, "units": "0-0", "align": "intervals:trials"})
    assert status == 200
    assert [u["unit_index"] for u in body["units"]] == [0]
    assert body["units"][0]["rate_hz"] == pytest.approx(EXPECT_ALL, rel=1e-5)
    status, body = _get(api, {**BASE, "units": "7-9", "align": "intervals:trials"})
    assert status == 404


def test_a_range_past_the_bound_is_refused(nwb_bytes: bytes) -> None:
    api, _ = _api(nwb_bytes, n_units=400)
    status, body = _get(api, {**BASE, "units": "10-162", "align": "intervals:trials"})
    assert status == 400
    assert body["error"] == "too_many_units"
    assert "153" in body["detail"]


@pytest.mark.parametrize("units", ["3-1", "a-b", "1-2-3", "-1", "1-", "1 - 2"])
def test_a_malformed_range_is_400_with_its_code(nwb_bytes: bytes, units: str) -> None:
    api, athena = _api(nwb_bytes)
    status, body = _get(api, {**BASE, "units": units, "align": "intervals:trials"})
    assert status == 400
    assert body["error"] == "malformed_range"
    assert athena.started == []


def test_the_body_precheck_refuses_from_spike_count(nwb_bytes: bytes) -> None:
    # 100 units of 200,000 spikes over [0, 10] s; 3 trials of 2 s windows each
    # overlap the span by 2 s: 100 x 200,000 x 6 / 10 x 12 bytes = 144 MB.
    api, athena = _api(nwb_bytes, n_units=100, spike_count=200_000)
    query = {**BASE, "units": "all", "align": "intervals:trials"}
    status, body = _get(api, query)
    assert status == 413
    assert body["error"] == "body_estimate_too_large"
    assert compute_api.SPIKES_SQL not in _texts(athena)
    status, _ = _get(api, {**query, "raster": "0"})  # no raster, no pre-check
    assert status == 200
