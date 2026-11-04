"""`GET /compute/overview` and `/compute/spectrogram` over the derived store.

The stub S3 serves a gzip-chunked NWB fixture by `Range` (two versions of the same
raw key, only the first catalogued) and holds the derived bucket, which the real
`lfp_build.build` fills. Nothing here opens the NWB file with h5py except the
expectations, which read the same slice with h5py directly.
"""

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

import h5py
import numpy as np
import pytest
from botocore.exceptions import ClientError
from hdmf.backends.hdf5.h5_utils import H5DataIO
from pynwb import NWBHDF5IO, NWBFile
from pynwb.ecephys import LFP, ElectricalSeries

from asteroidea import analysis
from asteroidea_ingest import compute_api, lfp_build, lfp_overview
from asteroidea_ingest.compute_api import ComputeApi
from asteroidea_ingest.lfp_build import BuildDeps, BuildMessage

from .conftest import jwt_event

if TYPE_CHECKING:
    from pathlib import Path

# pynwb declares its fields dynamically and its NWBFile reads as abstract to ty.
new_nwbfile: Any = NWBFile
RAW: Final = "raw-bucket"
DERIVED: Final = "derived-bucket"
KEY: Final = "raw/ds1/sub-01/ses-A/ecephys/sub-01_ses-A_ecephys.nwb"
ASSET: Final = "33333333-3333-4333-8333-333333333333"
CURRENT: Final = "v1"  # the catalogued version
NEWER: Final = "v2"  # a later version of the same key, different samples
RATE: Final = 1000.0
SECONDS: Final = 1300
CHANNELS: Final = 2
CHUNK_ROWS: Final = 2000
CONVERSION: Final = 1e-9
MICRO: Final = "/processing/ecephys/LFP/LFP_micro"
ROUTE_O: Final = "GET /compute/overview"
ROUTE_S: Final = "GET /compute/spectrogram"
SPEC_ROUTE_QUERY: Final = {
    "asset_id": ASSET,
    "series": MICRO,
    "channel": "1",
    "start_s": "100.3",
    "duration_s": "20",
}
LIMIT_S: Final = 1200


def _write(path: Path, seed: int) -> None:
    nwb = new_nwbfile(
        session_description="d",
        identifier=f"id-{seed}",
        session_start_time=datetime(2019, 4, 1, tzinfo=UTC),
    )
    device = nwb.create_device(name="probe")
    group = nwb.create_electrode_group(
        name="grp", description="g", location="brain", device=device
    )
    for index in range(CHANNELS):
        nwb.add_electrode(
            x=0.0,
            y=0.0,
            z=0.0,
            imp=0.0,
            location=f"L{index}",
            filtering="f",
            group=group,
        )
    region = nwb.create_electrode_table_region(
        region=list(range(CHANNELS)), description="all"
    )
    rng = np.random.default_rng(seed)
    n = int(SECONDS * RATE)
    data = rng.integers(-1000, 1000, size=(n, CHANNELS)).astype("int16")
    data[:, 0] += 300 * np.sin(2 * np.pi * 10.0 * np.arange(n) / RATE).astype("int16")
    lfp: Any = LFP()
    nwb.create_processing_module(name="ecephys", description="e").add(lfp)
    lfp.add_electrical_series(
        ElectricalSeries(
            name="LFP_micro",
            data=H5DataIO(data, compression="gzip", chunks=(CHUNK_ROWS, 1)),
            electrodes=region,
            rate=RATE,
            starting_time=0.5,
            conversion=CONVERSION,
        )
    )
    with NWBHDF5IO(str(path), "w") as io:
        io.write(nwb)


class StubBody:
    def __init__(self, data: bytes) -> None:
        self.data = data

    def read(self, amount: int | None = None, /) -> bytes:
        return self.data if amount is None else self.data[:amount]


@dataclass
class World:
    """Raw versions served by `Range`; the derived bucket as a dict; a fake clock."""

    raw: dict[tuple[str, str], bytes]
    derived: dict[str, bytes] = field(default_factory=dict)
    raw_gets: list[dict[str, str | int]] = field(default_factory=list)
    derived_gets: list[str] = field(default_factory=list)
    now: float = 0.0
    raw_get_cost_s: float = 0.0

    def get_object(self, **kwargs: str | int) -> Any:
        key = str(kwargs["Key"])
        if kwargs["Bucket"] == DERIVED:
            self.derived_gets.append(key)
            if key not in self.derived:
                error: Any = {"Error": {"Code": "NoSuchKey", "Message": key}}
                raise ClientError(error, "GetObject")
            return {"Body": StubBody(self.derived[key])}
        assert kwargs["Bucket"] == RAW
        self.raw_gets.append(kwargs)
        self.now += self.raw_get_cost_s
        version = str(kwargs.get("VersionId") or NEWER)  # unpinned reads the latest
        found = re.fullmatch(r"bytes=(\d+)-(\d+)", str(kwargs["Range"]))
        assert found is not None
        data = self.raw[(key, version)]
        return {"Body": StubBody(data[int(found[1]) : int(found[2]) + 1])}

    def head_object(self, **kwargs: str) -> dict[str, Any]:
        return {"ContentLength": len(self.raw[(kwargs["Key"], kwargs["VersionId"])])}

    def put_object(self, **kwargs: Any) -> dict[str, Any]:
        assert kwargs["Bucket"] == DERIVED
        self.derived[str(kwargs["Key"])] = bytes(kwargs["Body"])
        return {}

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def clock(self) -> float:
        return self.now


@pytest.fixture(scope="module")
def paths(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    folder = tmp_path_factory.mktemp("compute-lfp")
    first, second = folder / "a.nwb", folder / "b.nwb"
    _write(first, 1)
    _write(second, 2)
    return first, second


@pytest.fixture(scope="module")
def built(paths: tuple[Path, Path]) -> World:
    world = World(
        raw={(KEY, CURRENT): paths[0].read_bytes(), (KEY, NEWER): paths[1].read_bytes()}
    )
    message = BuildMessage(ASSET, "ds1", KEY, CURRENT)
    lfp_build.build(message, BuildDeps(world, RAW, DERIVED))
    return world


ASSET_COLS: Final = [
    "asset_id",
    "dataset",
    "subject",
    "bids_session",
    "s3_key",
    "s3_version_id",
    "size_bytes",
    "format",
]


def _unquote(wire: str) -> str:
    """Undo `render_param` for a str, as Athena reads the literal."""
    if wire.startswith("'"):
        return wire[1:-1].replace("''", "'")
    return wire


class StubAthena:
    """Answers the asset query from a table of (key, version); nothing else."""

    def __init__(self, world: World, assets: dict[str, tuple[str, str]]) -> None:
        self.world = world
        self.assets = assets
        self.results: dict[str, list[list[str]]] = {}
        self.wire: list[list[str]] = []

    def start_query_execution(self, **kwargs: Any) -> dict[str, Any]:
        self.wire.append(list(kwargs["ExecutionParameters"]))
        asset_id = _unquote(kwargs["ExecutionParameters"][0])
        found = self.assets.get(asset_id)
        rows: list[list[str]] = []
        if found is not None:
            size = len(self.world.raw[(found[0], CURRENT)])
            rows = [[asset_id, "ds1", "sub-01", "ses-A", found[0], found[1]]]
            rows[0] += [str(size), "nwb"]
        qid = f"q{len(self.results)}"
        self.results[qid] = rows
        return {"QueryExecutionId": qid}

    def get_query_execution(self, **_: Any) -> dict[str, Any]:
        return {"QueryExecution": {"Status": {"State": "SUCCEEDED"}}}

    def get_query_results(self, **kwargs: Any) -> dict[str, Any]:
        rows = self.results[kwargs["QueryExecutionId"]]
        types = ["varchar"] * 6 + ["bigint", "varchar"]
        body = [{"Data": [{"VarCharValue": v} for v in r]} for r in rows]
        return {
            "ResultSet": {
                "Rows": [{"Data": [{"VarCharValue": n} for n in ASSET_COLS]}, *body],
                "ResultSetMetadata": {
                    "ColumnInfo": [
                        {"Name": n, "Type": t}
                        for n, t in zip(ASSET_COLS, types, strict=True)
                    ]
                },
            }
        }

    def stop_query_execution(self, **_: Any) -> dict[str, Any]:
        return {}


MANIFEST_FOR: Final = f"lfp/{ASSET}/{CURRENT}/manifest.json"


def _handmade(world: World, asset: str, version: str, manifest: dict[str, Any]) -> None:
    world.derived[f"lfp/{asset}/{version}/manifest.json"] = json.dumps(
        manifest
    ).encode()


def _api(world: World, *, presign: bool = True) -> ComputeApi:
    assets = {
        ASSET: (KEY, CURRENT),
        "other-version": (KEY, "v9"),  # a version nobody built
        "skips": (KEY, CURRENT),
        "no-index": (KEY, CURRENT),
    }
    # Hand-made manifests for the refusal paths; the built one is untouched.
    skips = {
        "builder_version": "x",
        "series": [],
        "skipped": [
            {"path": "/acquisition/hi", "reason": "rate 30000 Hz over 2500"},
            {
                "path": "/acquisition/ts",
                "reason": "UnsupportedLayoutError: no rate (timestamps series)",
            },
        ],
    }
    _handmade(world, "skips", CURRENT, skips)
    gone = {
        "builder_version": "x",
        "series": [
            {
                "path": MICRO,
                "index_key": "lfp/missing/index.bin",
                "overview_key": "lfp/missing/overview.bin",
                "overview_shape": [1, 1, 1],
            }
        ],
        "skipped": [],
    }
    _handmade(world, "no-index", CURRENT, gone)
    athena = StubAthena(world, assets)
    return ComputeApi(
        athena=athena,
        workgroup="wg",
        database="db",
        s3=world,
        raw_bucket=RAW,
        derived_bucket=DERIVED,
        clock=world.clock,
        sleep=world.sleep,
        presign=(lambda key, ttl: f"https://signed.example/{key}?ttl={ttl}")
        if presign
        else None,
    )


def _call(
    api: ComputeApi,
    route: str,
    query: dict[str, str],
    *,
    remaining_ms: int = 28_000,
    groups: str = "[researchers]",
) -> tuple[int, dict[str, Any], int]:
    event = {**jwt_event(route, groups=groups), "queryStringParameters": query}
    resp = api.handle(event, remaining_ms)
    body = json.loads(resp["body"], parse_constant=_no_constant)
    return resp["statusCode"], body, len(resp["body"].encode())


def _no_constant(token: str) -> None:
    msg = f"invalid JSON constant {token} in the body"
    raise AssertionError(msg)


def _h5_window(path: Path, channel: int, start_s: float, duration_s: float) -> Any:
    first = round(start_s * RATE)
    with h5py.File(path, "r") as f:
        data = f[MICRO]["data"][first : first + round(duration_s * RATE), channel]
    return data.astype(np.float64) * CONVERSION


def test_a_window_equals_the_shared_spectrogram_on_the_h5py_slice(
    built: World, paths: tuple[Path, Path]
) -> None:
    status, body, _ = _call(_api(built), ROUTE_S, SPEC_ROUTE_QUERY)
    assert status == 200
    want = _h5_window(paths[0], 1, 100.3, 20.0)
    spec = analysis.spectrogram(want, RATE, 1.0, 0.5)
    keep = spec.freqs <= 250.0
    assert body["freqs_hz"] == np.round(spec.freqs[keep], 4).tolist()
    assert body["power_db"] == np.round(spec.power_db[keep], 1).tolist()
    t0 = round(100.3 * RATE) / RATE
    assert body["times_s"] == np.round(t0 + spec.times, 4).tolist()
    assert len(body["power_db"]) == 251
    assert body["rate_hz"] == RATE
    assert body["starting_time_s"] == 0.5


def test_the_trace_is_the_minmax_decimation_of_the_same_window(
    built: World, paths: tuple[Path, Path]
) -> None:
    _, body, _ = _call(_api(built), ROUTE_S, SPEC_ROUTE_QUERY)
    want = _h5_window(paths[0], 1, 100.3, 20.0)
    dec = analysis.decimate_minmax(want, RATE, compute_api.TRACE_POINTS)
    trace = body["trace"]
    assert len(trace["values"]) == len(trace["times_s"]) <= compute_api.TRACE_POINTS
    assert trace["values"] == pytest.approx(dec.samples.tolist(), rel=1e-5)
    assert min(trace["values"]) == pytest.approx(float(want.min()), rel=1e-5)
    assert max(trace["values"]) == pytest.approx(float(want.max()), rel=1e-5)


def test_every_raw_read_is_pinned_to_the_catalogued_version(
    built: World, paths: tuple[Path, Path]
) -> None:
    built.raw_gets.clear()
    status, body, _ = _call(_api(built), ROUTE_S, SPEC_ROUTE_QUERY)
    assert status == 200
    assert built.raw_gets
    assert {g["VersionId"] for g in built.raw_gets} == {CURRENT}
    assert {g["Key"] for g in built.raw_gets} == {KEY}
    # The newer version holds other samples: reading it would change the answer.
    other = analysis.spectrogram(_h5_window(paths[1], 1, 100.3, 20.0), RATE, 1.0, 0.5)
    assert (
        body["power_db"] != np.round(other.power_db[other.freqs <= 250.0], 1).tolist()
    )
    assert body["requests"] == len(built.raw_gets)
    assert body["bytes_read"] > 0


def test_a_window_reads_only_its_chunks_through_a_small_pool(built: World) -> None:
    built.raw_gets.clear()
    _call(_api(built), ROUTE_S, SPEC_ROUTE_QUERY)
    # 20 s at 1 kHz is 20,000 samples: 10 or 11 chunks of 2,000, one range each
    # (the two channels interleave, so no two chunks of one channel are adjacent).
    assert 10 <= len(built.raw_gets) <= 11
    assert compute_api.WINDOW_WORKERS == 8


def test_the_spectrogram_route_never_opens_the_file_with_h5py(
    built: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*_: object, **__: object) -> None:
        msg = "h5py.File was called on the spectrogram path"
        raise AssertionError(msg)

    monkeypatch.setattr(h5py, "File", refuse)
    assert _call(_api(built), ROUTE_S, SPEC_ROUTE_QUERY)[0] == 200


def test_the_overview_round_trips_the_built_object(built: World) -> None:
    status, body, size = _call(
        _api(built), ROUTE_O, {"asset_id": ASSET, "series": MICRO}
    )
    assert status == 200
    assert size < compute_api.MAX_BODY_BYTES
    manifest = json.loads(built.derived[MANIFEST_FOR])
    (entry,) = manifest["series"]
    view = lfp_overview.loads(built.derived[entry["overview_key"]])
    assert body["power_db"] == np.round(view.db.astype(np.float64), 1).tolist()
    assert body["times_s"] == np.round(view.times, 4).tolist()
    assert body["band_edges_hz"] == pytest.approx(view.edges.tolist(), rel=1e-5)
    assert body["channels"] == [0, 1]
    assert (len(body["power_db"]), len(body["power_db"][0])) == (CHANNELS, SECONDS)
    assert len(body["power_db"][0][0]) == len(body["band_edges_hz"]) - 1
    assert body["rate_hz"] == RATE
    assert body["starting_time_s"] == 0.5
    assert body["built_at"] == manifest["built_at"]
    assert body["builder_version"] == lfp_build.BUILDER_VERSION


def test_the_series_may_be_given_without_its_leading_slash(built: World) -> None:
    query = {"asset_id": ASSET, "series": MICRO.lstrip("/")}
    assert _call(_api(built), ROUTE_O, query)[0] == 200


def test_a_large_overview_is_a_presigned_get_of_the_binary_object(
    built: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(compute_api, "INLINE_OVERVIEW_VALUES", 1000)
    built.derived_gets.clear()
    status, body, _ = _call(_api(built), ROUTE_O, {"asset_id": ASSET, "series": MICRO})
    assert status == 200
    manifest = json.loads(built.derived[MANIFEST_FOR])
    (entry,) = manifest["series"]
    assert body["url"] == f"https://signed.example/{entry['overview_key']}?ttl=900"
    assert body["expires_in"] == compute_api.PRESIGN_TTL_S
    assert body["bytes"] == len(built.derived[entry["overview_key"]])
    assert body["shape"] == entry["overview_shape"]
    assert "power_db" not in body
    assert entry["overview_key"] not in built.derived_gets  # not even downloaded


def test_an_overview_whose_json_passes_the_cap_is_presigned_too(
    built: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(compute_api, "MAX_BODY_BYTES", 100_000)
    status, body, _ = _call(_api(built), ROUTE_O, {"asset_id": ASSET, "series": MICRO})
    assert status == 200
    assert "url" in body
    assert "power_db" not in body


def test_a_missing_overview_is_404_with_the_being_prepared_message(
    built: World,
) -> None:
    status, body, _ = _call(
        _api(built), ROUTE_O, {"asset_id": "other-version", "series": MICRO}
    )
    assert (status, body) == (
        404,
        {
            "error": "overview_not_built",
            "detail": "overview not built for this file version",
        },
    )


def test_a_missing_index_is_404(built: World) -> None:
    api = _api(built)
    not_built = {
        "error": "index_not_built",
        "detail": "index not built for this file version",
    }
    query = {**SPEC_ROUTE_QUERY, "asset_id": "other-version"}
    assert _call(api, ROUTE_S, query)[0:2] == (404, not_built)
    query = {**SPEC_ROUTE_QUERY, "asset_id": "no-index"}
    assert _call(api, ROUTE_S, query)[0:2] == (404, not_built)


def test_unknown_asset_and_series_are_404(built: World) -> None:
    api = _api(built)
    assert _call(api, ROUTE_O, {"asset_id": "nope", "series": MICRO})[:2] == (
        404,
        {"error": "not_found", "detail": "no such asset"},
    )
    status, body, _ = _call(api, ROUTE_O, {"asset_id": ASSET, "series": "/nope"})
    assert status == 404
    assert "no such series" in body["detail"]


def test_a_skipped_series_is_404_and_a_timestamps_series_is_422(
    built: World,
) -> None:
    api = _api(built)
    query = {**SPEC_ROUTE_QUERY, "asset_id": "skips"}
    status, body, _ = _call(api, ROUTE_S, {**query, "series": "/acquisition/hi"})
    assert (status, body["error"], body["detail"]) == (
        404,
        "not_found",
        "series not indexed: rate 30000 Hz over 2500",
    )
    status, body, _ = _call(api, ROUTE_S, {**query, "series": "/acquisition/ts"})
    assert status == 422
    assert "no uniform rate" in body["detail"]


@pytest.mark.parametrize(
    "change",
    [
        {"duration_s": str(LIMIT_S + 1)},
        {"duration_s": "0.5"},
        {"duration_s": "nan"},
        {"start_s": "-1"},
        {"channel": "-1"},
        {"channel": "x"},
        {"channel": "5"},  # outside the series
        {"start_s": "5000"},  # past the last sample
    ],
)
def test_a_bad_window_is_400(built: World, change: dict[str, str]) -> None:
    status, body, _ = _call(_api(built), ROUTE_S, {**SPEC_ROUTE_QUERY, **change})
    assert status == 400, body


def test_a_missing_parameter_is_400_and_the_group_is_checked(built: World) -> None:
    api = _api(built)
    query = {k: v for k, v in SPEC_ROUTE_QUERY.items() if k != "series"}
    assert _call(api, ROUTE_S, query)[0] == 400
    assert _call(api, ROUTE_O, {"asset_id": ASSET})[0] == 400
    assert _call(api, ROUTE_S, SPEC_ROUTE_QUERY, groups="[others]")[0] == 403


def test_the_limit_is_the_response_size_math() -> None:
    per_second = 2 * (compute_api.SPEC_BINS * compute_api.BYTES_PER_VALUE + 10)
    fixed = compute_api.SPEC_BINS * 8 + 2 * compute_api.TRACE_POINTS * 10 + 1000
    ceiling = (compute_api.MAX_BODY_BYTES - fixed) // per_second
    assert compute_api.SPEC_BINS == 251
    assert per_second == 4036
    assert ceiling == 1288
    assert compute_api.MAX_SPECTROGRAM_WINDOW_S == LIMIT_S <= ceiling
    assert per_second * LIMIT_S + fixed < compute_api.MAX_BODY_BYTES


def test_a_window_at_the_limit_stays_under_five_mebibytes(built: World) -> None:
    query = {**SPEC_ROUTE_QUERY, "channel": "0", "start_s": "0", "duration_s": "1200"}
    status, body, size = _call(_api(built), ROUTE_S, query)
    assert status == 200
    assert size < compute_api.MAX_BODY_BYTES
    assert len(body["times_s"]) == 2 * LIMIT_S - 1
    assert len(body["power_db"]) * len(body["power_db"][0]) == 251 * (2 * LIMIT_S - 1)
    assert body["requests"] == 600  # 1,200 s of 2,000-sample chunks, one range each


def test_the_deadline_is_503_once_the_reads_have_used_the_budget(built: World) -> None:
    world = World(raw=built.raw, derived=built.derived, raw_get_cost_s=3.0)
    status, body, _ = _call(_api(world), ROUTE_S, SPEC_ROUTE_QUERY)
    assert (status, body["error"]) == (503, "slow")
    assert "ran out of time" in body["detail"]
    # Entry budget is remaining - 2 s: with 2 s left nothing can run.
    assert _call(_api(built), ROUTE_S, SPEC_ROUTE_QUERY, remaining_ms=2_000)[0] == 503


def test_the_asset_lookup_sends_a_quoted_varchar(built: World) -> None:
    """The asset id reaches Athena as a quoted literal, never bare."""
    api = _api(built)
    assert _call(api, ROUTE_O, {"asset_id": ASSET, "series": MICRO})[0] == 200
    assert isinstance(api.athena, StubAthena)
    assert api.athena.wire == [[f"'{ASSET}'"]]
