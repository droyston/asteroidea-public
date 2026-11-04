"""LfpBuildFunction over a pynwb fixture served by a stub S3 by `Range`.

The fixture holds two gzip-chunked LFP ElectricalSeries (one and two channels per
chunk), a timestamps ElectricalSeries (skipped: no rate) and a plain TimeSeries (not
an ElectricalSeries, so never listed). The stub serves the file's bytes by `Range`,
so the builder runs the real `RemoteFile`, `BlockFile` and h5py, and records every
`put_object` in order.
"""

import json
import re
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

import h5py
import numpy as np
import pytest
from hdmf.backends.hdf5.h5_utils import H5DataIO
from pynwb import NWBHDF5IO, NWBFile, TimeSeries
from pynwb.ecephys import LFP, ElectricalSeries

from asteroidea_ingest import lfp_build, lfp_index, lfp_overview
from asteroidea_ingest.lfp_build import BuildDeps, BuildMessage

if TYPE_CHECKING:
    from pathlib import Path

# pynwb declares its fields dynamically and its NWBFile reads as abstract to ty.
new_nwbfile: Any = NWBFile
RAW: Final = "raw-bucket"
DERIVED: Final = "derived-bucket"
KEY: Final = "raw/ds1/sub-01/ses-A/ecephys/sub-01_ses-A_ecephys.nwb"
VERSION: Final = "rv1"
ASSET: Final = "22222222-2222-4222-8222-222222222222"
RATE: Final = 500.0
SECONDS: Final = 12
CHANNELS: Final = 5
CHUNK_ROWS: Final = 700
MICRO: Final = "/processing/ecephys/LFP/LFP_micro"
MACRO: Final = "/processing/ecephys/LFP/LFP_macro"
STAMPED: Final = "/acquisition/stamped"
MESSAGE: Final = BuildMessage(ASSET, "ds1", KEY, VERSION)


def _signal(seed: int) -> np.ndarray:
    t = np.arange(int(SECONDS * RATE)) / RATE
    rng = np.random.default_rng(seed)
    data = rng.normal(scale=1e-6, size=(t.size, CHANNELS))
    data[:, 0] += 1e-4 * np.sin(2 * np.pi * 10.0 * t)
    return data


def _nwb(path: Path, *, with_lfp: bool = True) -> None:
    nwb = new_nwbfile(
        session_description="d",
        identifier="id-1",
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
    nwb.add_acquisition(
        TimeSeries(
            name="other",
            data=np.arange(100, dtype="float64"),
            unit="V",
            rate=100.0,
        )
    )
    if with_lfp:
        lfp: Any = LFP()
        nwb.create_processing_module(name="ecephys", description="e").add(lfp)
        for name, width, seed in (("LFP_micro", 1, 1), ("LFP_macro", 2, 2)):
            lfp.add_electrical_series(
                ElectricalSeries(
                    name=name,
                    data=H5DataIO(
                        _signal(seed), compression="gzip", chunks=(CHUNK_ROWS, width)
                    ),
                    electrodes=region,
                    rate=RATE,
                    starting_time=0.25,
                    conversion=1e-3,
                )
            )
        nwb.add_acquisition(
            ElectricalSeries(
                name="stamped",
                data=H5DataIO(_signal(3)[:50], chunks=(10, 1)),
                electrodes=region,
                timestamps=np.arange(50) / RATE,
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
class StubS3:
    """Serves one raw version by `Range`; records each put in order."""

    blob: bytes
    gets: list[tuple[int, int]] = field(default_factory=list)
    puts: list[str] = field(default_factory=list)
    objects: dict[str, bytes] = field(default_factory=dict)
    fail_put: str | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    def get_object(self, **kwargs: str | int) -> Any:
        assert (kwargs["Bucket"], kwargs["Key"]) == (RAW, KEY)
        assert kwargs["VersionId"] == VERSION
        found = re.fullmatch(r"bytes=(\d+)-(\d+)", str(kwargs["Range"]))
        assert found is not None
        start, stop = int(found[1]), int(found[2]) + 1
        with self.lock:
            self.gets.append((start, stop))
        return {"Body": StubBody(self.blob[start:stop])}

    def head_object(self, **kwargs: str) -> dict[str, Any]:
        assert (kwargs["Bucket"], kwargs["Key"], kwargs["VersionId"]) == (
            RAW,
            KEY,
            VERSION,
        )
        return {"ContentLength": len(self.blob)}

    def put_object(self, **kwargs: Any) -> dict[str, Any]:
        assert kwargs["Bucket"] == DERIVED
        key = str(kwargs["Key"])
        if self.fail_put is not None and key.endswith(self.fail_put):
            msg = f"put failed: {key}"
            raise RuntimeError(msg)
        self.puts.append(key)
        self.objects[key] = bytes(kwargs["Body"])
        return {}


@pytest.fixture(scope="module")
def nwb_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("lfp-build") / "f.nwb"
    _nwb(path)
    return path


def _build(path: Path) -> tuple[StubS3, dict[str, Any]]:
    s3 = StubS3(path.read_bytes())
    manifest = lfp_build.build(MESSAGE, BuildDeps(s3, RAW, DERIVED))
    return s3, manifest


@pytest.fixture(scope="module")
def built(nwb_path: Path) -> tuple[StubS3, dict[str, Any]]:
    return _build(nwb_path)


def _manifest_key() -> str:
    return f"lfp/{ASSET}/{VERSION}/manifest.json"


def test_manifest_lists_both_lfp_series_and_skips_the_stamped_one(
    built: tuple[StubS3, dict[str, Any]],
) -> None:
    s3, returned = built
    manifest = json.loads(s3.objects[_manifest_key()])
    assert manifest == returned
    assert [s["path"] for s in manifest["series"]] == [MACRO, MICRO]
    assert [s["path"] for s in manifest["skipped"]] == [STAMPED]
    assert "timestamps" in manifest["skipped"][0]["reason"]
    assert manifest["builder_version"] == lfp_build.BUILDER_VERSION
    assert (manifest["asset_id"], manifest["s3_version_id"]) == (ASSET, VERSION)
    for entry in manifest["series"]:
        assert len(s3.objects[entry["index_key"]]) == entry["index_bytes"]
        assert len(s3.objects[entry["overview_key"]]) == entry["overview_bytes"]


def test_every_data_object_is_written_before_the_manifest(
    built: tuple[StubS3, dict[str, Any]],
) -> None:
    s3, manifest = built
    assert s3.puts[-1] == _manifest_key()
    data = {e[k] for e in manifest["series"] for k in ("index_key", "overview_key")}
    assert set(s3.puts[:-1]) == data
    assert len(s3.puts) == len(data) + 1


def test_keys_follow_the_derived_store_layout(
    built: tuple[StubS3, dict[str, Any]],
) -> None:
    _, manifest = built
    for entry in manifest["series"]:
        slug = lfp_build.series_slug(entry["path"])
        assert entry["slug"] == slug
        assert entry["index_key"] == f"lfp/{ASSET}/{VERSION}/{slug}/index.bin"
        assert entry["overview_key"] == f"lfp/{ASSET}/{VERSION}/{slug}/overview.bin"
    assert lfp_build.series_slug(MICRO).startswith("processing.ecephys.LFP.LFP_micro-")
    assert lfp_build.series_slug("/a b/c") != lfp_build.series_slug("/a_b/c")


@pytest.mark.parametrize("path", [MICRO, MACRO])
def test_index_decodes_a_window_equal_to_h5py(
    built: tuple[StubS3, dict[str, Any]], nwb_path: Path, path: str
) -> None:
    s3, manifest = built
    (entry,) = [e for e in manifest["series"] if e["path"] == path]
    index = lfp_index.loads(s3.objects[entry["index_key"]])
    blob = nwb_path.read_bytes()
    with h5py.File(nwb_path, "r") as f:
        data = f[path]["data"]
        for channel in range(CHANNELS):
            ranges = lfp_index.plan_window(index, channel, 1.3, 2.9)
            got = lfp_index.decode_window(
                index, [blob[r.start : r.stop] for r in ranges], channel, 1.3, 2.9
            )
            first = round(1.3 * RATE)
            want = data[first : first + round(2.9 * RATE), channel]
            assert np.array_equal(got, want)


@pytest.mark.parametrize("path", [MICRO, MACRO])
def test_overview_decodes_and_equals_the_h5py_built_one(
    built: tuple[StubS3, dict[str, Any]], nwb_path: Path, path: str
) -> None:
    s3, manifest = built
    (entry,) = [e for e in manifest["series"] if e["path"] == path]
    got = lfp_overview.loads(s3.objects[entry["overview_key"]])
    with h5py.File(nwb_path, "r") as f:
        index = lfp_index.build_index(f, path)
        dset = f[path]["data"]
        want = lfp_overview.build_overview(lambda a, b: dset[:, a:b], index)
    assert got.db.shape == (CHANNELS, SECONDS, entry["overview_shape"][2])
    assert entry["overview_shape"] == list(got.db.shape)
    assert np.array_equal(got.db, want.db)
    assert got.starting_time == 0.25


def test_the_block_reader_fetches_each_chunk_once_per_column(nwb_path: Path) -> None:
    """Each chunk is read once: the bytes fetched equal the stored chunk bytes."""
    with h5py.File(nwb_path, "r") as f:
        index = lfp_index.build_index(f, MACRO)  # two channels per chunk
    blob = nwb_path.read_bytes()
    fetched: list[int] = []

    def fetch(ranges: list[lfp_index.ByteRange]) -> list[bytes]:
        fetched.extend(r.length for r in ranges)
        return [blob[r.start : r.stop] for r in ranges]

    lfp_overview.build_overview(lfp_build.column_reader(index, fetch), index)
    assert sum(fetched) == int(index.byte_size.sum())


def test_decode_column_equals_h5py(nwb_path: Path) -> None:
    blob = nwb_path.read_bytes()
    with h5py.File(nwb_path, "r") as f:
        index = lfp_index.build_index(f, MACRO)
        data = f[MACRO]["data"][:]
    for column in range(index.grid[1]):
        ranges = lfp_index.plan_column(index, column)
        got = lfp_index.decode_column(
            index, [blob[r.start : r.stop] for r in ranges], column
        )
        assert np.array_equal(got, data[:, 2 * column : 2 * column + 2])
    with pytest.raises(lfp_index.WindowError):
        lfp_index.plan_column(index, index.grid[1])


def test_a_rebuild_rewrites_the_same_keys_with_the_same_data(
    built: tuple[StubS3, dict[str, Any]], nwb_path: Path
) -> None:
    first, _ = built
    again, _ = _build(nwb_path)
    assert again.puts == first.puts
    for key in first.puts[:-1]:
        assert again.objects[key] == first.objects[key]


def test_a_file_without_electrical_series_writes_a_manifest_with_no_series(
    tmp_path: Path,
) -> None:
    path = tmp_path / "plain.nwb"
    _nwb(path, with_lfp=False)
    s3, manifest = _build(path)
    assert s3.puts == [_manifest_key()]
    assert manifest["series"] == []
    assert manifest["skipped"] == []


def test_a_failed_data_write_leaves_no_manifest(nwb_path: Path) -> None:
    s3 = StubS3(nwb_path.read_bytes(), fail_put="overview.bin")
    with pytest.raises(RuntimeError, match="put failed"):
        lfp_build.build(MESSAGE, BuildDeps(s3, RAW, DERIVED))
    assert _manifest_key() not in s3.objects


def test_run_batch_parses_the_message_and_refuses_a_missing_field(
    nwb_path: Path,
) -> None:
    s3 = StubS3(nwb_path.read_bytes())
    event = {"Records": [{"body": json.dumps(MESSAGE.body())}]}
    assert lfp_build.run_batch(event, BuildDeps(s3, RAW, DERIVED)) == [ASSET]
    bad = {"Records": [{"body": json.dumps({"asset_id": ASSET})}]}
    with pytest.raises(ValueError, match="dataset, s3_key, s3_version_id"):
        lfp_build.run_batch(bad, BuildDeps(s3, RAW, DERIVED))


def test_a_long_range_is_fetched_in_parts_and_joined(
    nwb_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(lfp_build, "MAX_GET_BYTES", 1000)
    blob = nwb_path.read_bytes()
    s3 = StubS3(blob)
    source = lfp_build.Source(RAW, KEY, VERSION, len(blob))
    fetcher = lfp_build.RangeFetcher(source, s3, workers=4)
    span = lfp_index.ByteRange(10, 10 + 2 * 1000 + 5)
    (got,) = fetcher.fetch([span])
    assert got == blob[span.start : span.stop]
    assert sorted(s3.gets) == [(10, 1010), (1010, 2010), (2010, 2015)]
    assert fetcher.requests == 3
    assert fetcher.bytes_fetched == span.length


def test_a_series_over_the_lfp_rate_is_listed_as_skipped_and_never_built(
    tmp_path: Path,
) -> None:
    """A 30 kHz ElectricalSeries gets no index and no overview (the builder default)."""
    nwb = new_nwbfile(
        session_description="d",
        identifier="id-2",
        session_start_time=datetime(2019, 4, 1, tzinfo=UTC),
    )
    device = nwb.create_device(name="probe")
    group = nwb.create_electrode_group(
        name="grp", description="g", location="brain", device=device
    )
    for index in range(2):
        nwb.add_electrode(
            x=0.0,
            y=0.0,
            z=0.0,
            imp=0.0,
            location=f"L{index}",
            filtering="f",
            group=group,
        )
    region = nwb.create_electrode_table_region(region=[0, 1], description="all")
    rng = np.random.default_rng(5)
    nwb.add_acquisition(
        ElectricalSeries(
            name="raw30k",
            data=H5DataIO(
                rng.normal(size=(60_000, 2)), compression="gzip", chunks=(6000, 1)
            ),
            electrodes=region,
            rate=30_000.0,
        )
    )
    path = tmp_path / "raw30k.nwb"
    with NWBHDF5IO(str(path), "w") as io:
        io.write(nwb)
    s3, manifest = _build(path)
    assert manifest["series"] == []
    assert manifest["skipped"] == [
        {"path": "/acquisition/raw30k", "reason": "rate 30000 Hz over 2500"}
    ]
    assert s3.puts == [_manifest_key()]


# -- the summary kinds ---------------------------------------------


def _records(*bodies: dict[str, str]) -> dict[str, Any]:
    return {"Records": [{"body": json.dumps(b)} for b in bodies]}


def test_the_summary_kinds_go_to_the_summary_builder_with_one_set_of_clients(
    nwb_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    s3 = StubS3(nwb_path.read_bytes())
    deps = BuildDeps(s3, RAW, DERIVED)
    handled: list[tuple[dict[str, str], object]] = []
    made: list[object] = []

    def run_message(body: dict[str, str], summary: object) -> str:
        handled.append((body, summary))
        return f"{body['kind']}:{body['dataset']}"

    def factory() -> Any:
        made.append(object())
        return made[-1]

    monkeypatch.setattr(lfp_build.dataset_summary, "run_message", run_message)
    summary = {"kind": "summary", "dataset": "ds1"}
    session = {"kind": "session_summary", "dataset": "ds1", "subject": "sub-01"}
    session |= {"session": "ses-A"}
    event = _records(summary, MESSAGE.body(), session)
    done = lfp_build.run_batch(event, deps, factory)
    assert done == ["summary:ds1", ASSET, "session_summary:ds1"]
    assert [b for b, _ in handled] == [summary, session]
    assert len(made) == 1  # one set of clients per batch
    assert all(s is made[0] for _, s in handled)
    assert _manifest_key() in s3.objects  # the message with no kind is an LFP build


def test_an_lfp_batch_makes_no_summary_clients(nwb_path: Path) -> None:
    def factory() -> Any:
        raise AssertionError

    event = _records(MESSAGE.body() | {"kind": "lfp"})
    deps = BuildDeps(StubS3(nwb_path.read_bytes()), RAW, DERIVED)
    assert lfp_build.run_batch(event, deps, factory) == [ASSET]


def test_an_unknown_kind_or_a_summary_without_clients_is_refused(
    nwb_path: Path,
) -> None:
    deps = BuildDeps(StubS3(nwb_path.read_bytes()), RAW, DERIVED)
    with pytest.raises(ValueError, match="unknown build message kind 'overview'"):
        lfp_build.run_batch(_records({"kind": "overview"}), deps)
    with pytest.raises(ValueError, match="needs the summary clients"):
        lfp_build.run_batch(_records({"kind": "summary", "dataset": "ds1"}), deps)


def test_the_summary_clients_come_from_the_environment(
    nwb_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    deps = BuildDeps(StubS3(nwb_path.read_bytes()), RAW, DERIVED)
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.delenv("ATHENA_WORKGROUP", raising=False)
    monkeypatch.setenv("LFP_BUILD_QUEUE_URL", "https://sqs.example/q")
    monkeypatch.setenv("GLUE_DATABASE", "asteroidea")
    with pytest.raises(KeyError, match="ATHENA_WORKGROUP"):
        lfp_build.summary_deps(deps)
    monkeypatch.setenv("ATHENA_WORKGROUP", "wg")
    made = lfp_build.summary_deps(deps)
    assert (made.workgroup, made.database) == ("wg", "asteroidea")
    assert (made.raw_bucket, made.derived_bucket, made.s3) == (RAW, DERIVED, deps.s3)
    assert made.send is not None
