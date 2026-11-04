"""Dataset and session summaries.

The NWB file and its catalog rows are the CS62 R2 fixture `test_compute_events` and
`test_compute_tuning` use (the real `RemoteFile`, `BlockFile` and h5py path), with
synthetic units. The Athena fake answers every cell as `VarCharValue` text (arrays as
`[a, b]`) and the tests assert `ExecutionParameters` as typed literals. The derived
store fake refuses a conditional write the way S3 does (412 `PreconditionFailed`).
"""

import io
import json
from dataclasses import dataclass, field
from typing import Any, Final, override

import pytest
from botocore.exceptions import ClientError

from asteroidea_ingest import catalog_api, compute_api, dataset_summary, lfp_build
from asteroidea_ingest.catalog_api import CatalogApi
from asteroidea_ingest.compute_api import ComputeApi
from asteroidea_ingest.dataset_summary import SummaryDeps

from .conftest import jwt_event
from .test_compute import Cols, Rows, StubS3
from .test_compute_events import (
    ASSET_ID,
    CSV_BYTES,
    CSV_KEY,
    DATASET,
    NWB_KEY,
    ROWS,
    STAMPS,
    _write,
)
from .test_compute_tuning import LOCATIONS, TuningAthena, synthetic

RAW: Final = "raw-bucket"
DERIVED: Final = "derived-bucket"
SUBJECT: Final = "sub-CS62"
SESSION: Final = "ses-P62CSR2"
COMMITTED: Final = "2026-10-24 17:50:20.193703"
EXTRACTED: Final = "2026-10-25 08:00:01.5"
LFP_PATH: Final = "/processing/ecephys/LFP_macro/ElectricalSeries"
IEEG_PATH: Final = "/processing/ecephys/LFP/ecephys.ieeg"
N_UNITS: Final = 6
TABLE_ROUTE: Final = "GET /catalog/assets/{asset_id}/tables/{table}"
UNIT_TABLE_COLS: Final[Cols] = [
    ("asset_id", "varchar"),
    ("dataset", "varchar"),
    ("subject", "varchar"),
    ("bids_session", "varchar"),
    ("unit_index", "bigint"),
    ("unit_id", "varchar"),
    ("electrode_index", "bigint"),
    ("location", "varchar"),
    ("group_name", "varchar"),
    ("spike_count", "bigint"),
    ("first_spike_s", "double"),
    ("last_spike_s", "double"),
    ("mean_rate_hz", "double"),
    ("columns_json", "varchar"),
]


def _quoted(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _texts(cols: Cols, rows: list[list[object]]) -> tuple[Cols, Rows]:
    return cols, [[None if v is None else str(v) for v in row] for row in rows]


@dataclass
class SummaryAthena(TuningAthena):
    """The CS62 rows plus the summary's dataset and session reads, as Athena text."""

    nwb_size: int = 0
    extra_assets: list[list[object]] = field(default_factory=list)

    def _dataset_answer(self, sql: str) -> tuple[Cols, Rows] | None:
        units = sorted(self.spikes)
        answers: dict[str, tuple[Cols, list[list[object]]]] = {
            dataset_summary.ASSETS_SQL: (
                [
                    ("asset_id", "varchar"),
                    ("subject", "varchar"),
                    ("bids_session", "varchar"),
                    ("datatype", "varchar"),
                    ("suffix", "varchar"),
                    ("format", "varchar"),
                    ("size_bytes", "bigint"),
                    ("s3_key", "varchar"),
                    ("s3_version_id", "varchar"),
                    ("committed_at", "timestamp"),
                ],
                [
                    [
                        ASSET_ID,
                        SUBJECT,
                        SESSION,
                        None,
                        "behavior+ecephys",
                        "nwb",
                        self.nwb_size,
                        NWB_KEY,
                        "v1",
                        COMMITTED,
                    ],
                    [
                        "csv-623",
                        None,
                        None,
                        "stimuli",
                        "info",
                        "csv",
                        len(CSV_BYTES),
                        CSV_KEY,
                        "v1",
                        COMMITTED,
                    ],
                    *self.extra_assets,
                ],
            ),
            dataset_summary.SUBJECTS_SQL: (
                [
                    ("subject", "varchar"),
                    ("species", "varchar"),
                    ("sex", "varchar"),
                    ("age", "varchar"),
                    ("extracted_at", "timestamp"),
                ],
                [[SUBJECT, "Homo sapiens", "M", "P42Y", EXTRACTED]],
            ),
            dataset_summary.SESSIONS_SQL: (
                [
                    ("subject", "varchar"),
                    ("bids_session", "varchar"),
                    ("start_time", "timestamp"),
                    ("extracted_at", "timestamp"),
                ],
                [[SUBJECT, SESSION, "2019-04-01 09:30:00.000", EXTRACTED]],
            ),
            dataset_summary.HEADERS_SQL: (
                [
                    ("asset_id", "varchar"),
                    ("format", "varchar"),
                    ("n_units", "bigint"),
                    ("n_trials", "bigint"),
                    ("nifti_shape", "array"),
                    ("nifti_voxel_mm", "array"),
                    ("tr_seconds", "double"),
                    ("extracted_at", "timestamp"),
                ],
                [
                    [ASSET_ID, "nwb", len(units), 2, None, None, None, EXTRACTED],
                    [
                        "t1",
                        "nifti",
                        None,
                        None,
                        "[176, 256, 256]",
                        "[1.0, 1.0, 1.2]",
                        None,
                        "2026-10-26 10:00:00.0",
                    ],
                ],
            ),
            dataset_summary.SERIES_SQL: (
                [
                    ("asset_id", "varchar"),
                    ("path", "varchar"),
                    ("neurodata_type", "varchar"),
                    ("shape", "array"),
                    ("rate_hz", "double"),
                ],
                [
                    [ASSET_ID, LFP_PATH, "ElectricalSeries", "[764000, 96]", "1000.0"],
                    [ASSET_ID, IEEG_PATH, "ElectricalSeries", "[1000, 8]", "500.0"],
                    [ASSET_ID, "/acquisition/events_ttl", "TimeSeries", "[10]", None],
                ],
            ),
            dataset_summary.UNITS_SQL: (
                [("asset_id", "varchar"), ("location", "varchar"), ("n", "bigint")],
                [
                    [ASSET_ID, loc, sum(1 for u in units if LOCATIONS[u % 2] == loc)]
                    for loc in LOCATIONS
                ],
            ),
            dataset_summary.ELECTRODES_SQL: (
                [
                    ("asset_id", "varchar"),
                    ("location", "varchar"),
                    ("n", "bigint"),
                    ("with_xyz", "bigint"),
                ],
                [[ASSET_ID, "amygdala", 4, 0], [ASSET_ID, None, 2, 2]],
            ),
            dataset_summary.INTERVALS_SQL: (
                [("asset_id", "varchar"), ("table_name", "varchar"), ("n", "bigint")],
                [
                    [ASSET_ID, t, sum(1 for r in ROWS if r["table_name"] == t)]
                    for t in sorted({str(r["table_name"]) for r in ROWS})
                ],
            ),
            dataset_summary.EVENTS_SQL: (
                [("asset_id", "varchar"), ("trial_type", "varchar"), ("n", "bigint")],
                [["ev-x", "go", 3], ["ev-x", "stop", 2]],
            ),
            dataset_summary.SESSION_ASSETS_SQL: (
                [
                    ("asset_id", "varchar"),
                    ("format", "varchar"),
                    ("s3_version_id", "varchar"),
                    ("committed_at", "timestamp"),
                ],
                [[ASSET_ID, "nwb", "v1", COMMITTED]],
            ),
            dataset_summary.SESSION_UNITS_SQL: (
                [("asset_id", "varchar"), ("n", "bigint")],
                [[ASSET_ID, len(units)]],
            ),
            **self._entry_answers(),
        }
        found = answers.get(sql)
        return None if found is None else _texts(*found)

    def _entry_answers(self) -> dict[str, tuple[Cols, list[list[object]]]]:
        """Answer the studies route's seven texts scoped to the dataset."""
        units = sorted(self.spikes)
        (subjects, kinds, assets, nwb, lfp, unit_regions, channel_regions) = (
            catalog_api.DATASET_ENTRY_SQL
        )
        v = "varchar"
        return {
            subjects: (
                [(n, v) for n in ("dataset", "subject", "species", "sex", "age")],
                [[DATASET, SUBJECT, "Homo sapiens", "M", "P42Y"]],
            ),
            kinds: (
                [
                    (n, v)
                    for n in ("dataset", "subject", "datatype", "suffix", "format")
                ],
                [],
            ),
            assets: (
                [(n, v) for n in ("dataset", "subject", "bids_session", "asset_id")],
                [[DATASET, SUBJECT, SESSION, ASSET_ID]],
            ),
            nwb: (
                [
                    *[
                        (n, v)
                        for n in (
                            "dataset",
                            "subject",
                            "bids_session",
                            "asset_id",
                            "suffix",
                        )
                    ],
                    ("n_units", "bigint"),
                    ("n_trials", "bigint"),
                ],
                [
                    [
                        DATASET,
                        SUBJECT,
                        SESSION,
                        ASSET_ID,
                        "behavior+ecephys",
                        len(units),
                        2,
                    ]
                ],
            ),
            lfp: (
                [
                    *[(n, v) for n in ("dataset", "subject", "asset_id", "path")],
                    ("shape", "array"),
                    ("rate_hz", "double"),
                ],
                [[DATASET, SUBJECT, ASSET_ID, LFP_PATH, "[764000, 96]", "1000.0"]],
            ),
            unit_regions: (
                [("dataset", v), ("location", v), ("units", "bigint")],
                [
                    [DATASET, loc, sum(1 for u in units if LOCATIONS[u % 2] == loc)]
                    for loc in sorted(LOCATIONS)
                ],
            ),
            channel_regions: (
                [("dataset", v), ("location", v), ("channels", "bigint")],
                [[DATASET, "amygdala", 4]],
            ),
        }

    def _unit_table(self, params: list[str]) -> tuple[Cols, Rows]:
        """Answer the catalog units table: from the offset, 201 rows at most."""
        offset = int(params[1])
        rows: Rows = [
            [
                ASSET_ID,
                DATASET,
                SUBJECT,
                SESSION,
                str(u),
                f"u{u}",
                str(u % 8),
                LOCATIONS[u % 2],
                "g0",
                str(len(spikes)),
                str(spikes[0]),
                str(spikes[-1]),
                "2.5",
                "{}",
            ]
            for u, spikes in sorted(self.spikes.items())
            if params[0] == ASSET_ID
        ]
        return UNIT_TABLE_COLS, rows[offset : offset + 201]

    @override
    def _answer(self, sql: str, params: list[str]) -> tuple[Cols, Rows]:
        if sql == catalog_api.TABLE_SQL["units"]:
            return self._unit_table(params)
        found = self._dataset_answer(sql)
        if found is not None:
            assert params[0] == DATASET
            if sql == dataset_summary.SESSION_ASSETS_SQL:
                return (
                    found if params == [DATASET, SUBJECT, SESSION] else (found[0], [])
                )
            return found
        return super()._answer(sql, params)


class DerivedStore:
    """The derived bucket: ETags, and S3's refusals of a conditional write."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.etags: dict[str, str] = {}
        self.puts: list[dict[str, Any]] = []
        self.version = 0
        # Called before each put: a write by another builder in between.
        self.before_put: list[Any] = []

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        key = str(kwargs["Key"])
        if key not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return {"Body": io.BytesIO(self.objects[key]), "ETag": self.etags[key]}

    def put_object(self, **kwargs: Any) -> dict[str, Any]:
        if self.before_put:
            self.before_put.pop(0)()
        key = str(kwargs["Key"])
        match = kwargs.get("IfMatch")
        none_match = kwargs.get("IfNoneMatch")
        if (match is not None and self.etags.get(key) != match) or (
            none_match == "*" and key in self.objects
        ):
            raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")
        self.version += 1
        self.objects[key] = bytes(kwargs["Body"])
        self.etags[key] = f'"etag-{self.version}"'
        self.puts.append(kwargs)
        return {"ETag": self.etags[key]}

    def json(self, key: str) -> dict[str, Any]:
        return json.loads(self.objects[key])


class BothBuckets:
    """The builder's one S3 client: the raw bucket by range, the derived store."""

    def __init__(self, raw: StubS3, derived: DerivedStore) -> None:
        self.raw = raw
        self.derived = derived

    def get_object(self, **kwargs: Any) -> Any:
        if kwargs["Bucket"] == DERIVED:
            return self.derived.get_object(**kwargs)
        return self.raw.get_object(**kwargs)

    def head_object(self, **_: Any) -> dict[str, Any]:
        raise AssertionError

    def put_object(self, **kwargs: Any) -> object:
        assert kwargs["Bucket"] == DERIVED
        return self.derived.put_object(**kwargs)


@pytest.fixture(scope="module")
def nwb_bytes(tmp_path_factory: pytest.TempPathFactory) -> bytes:
    path = tmp_path_factory.mktemp("cs62-summary") / "cs62.nwb"
    _write(path, STAMPS)
    return path.read_bytes()


@dataclass
class World:
    athena: SummaryAthena
    raw: StubS3
    store: DerivedStore
    sent: list[dict[str, str]]
    deps: SummaryDeps

    def route(self, path: str, query: dict[str, str]) -> dict[str, Any]:
        """Answer one compute route on the same rows, through the dispatch table."""
        api = ComputeApi(
            athena=self.athena,
            workgroup="wg",
            database="db",
            s3=self.raw,
            raw_bucket=RAW,
            clock=lambda: self.athena.clock,
            sleep=self.athena.sleep,
        )
        event = {**jwt_event(path), "queryStringParameters": query}
        response = api.handle(event, 28_000)
        assert response["statusCode"] == 200, response["body"]
        return json.loads(response["body"])

    def table(self, asset_id: str, table: str) -> dict[str, Any]:
        """Answer the catalog table route's first page on the same rows.

        The body less `query_ms`, the route's own timing figure.
        """
        api = CatalogApi(
            athena=self.athena,
            workgroup="wg",
            database="db",
            clock=lambda: self.athena.clock,
            sleep=self.athena.sleep,
        )
        event = {
            **jwt_event(TABLE_ROUTE),
            "pathParameters": {"asset_id": asset_id, "table": table},
            "queryStringParameters": None,
        }
        response = api.handle(event)
        assert response["statusCode"] == 200, response["body"]
        body = json.loads(response["body"])
        body.pop("query_ms")
        return body


def make_world(data: bytes, n_units: int = N_UNITS, **extra: Any) -> World:
    athena = SummaryAthena(
        size=len(data), spikes=synthetic(n_units), nwb_size=len(data), **extra
    )
    raw = StubS3({NWB_KEY: data, CSV_KEY: CSV_BYTES})
    store: Any = DerivedStore()
    sent: list[dict[str, str]] = []
    deps = SummaryDeps(
        athena=athena,
        workgroup="wg",
        database="db",
        s3=BothBuckets(raw, store),
        raw_bucket=RAW,
        derived_bucket=DERIVED,
        send=sent.append,
        clock=lambda: athena.clock,
        sleep=athena.sleep,
    )
    return World(athena, raw, store, sent, deps)


EXTRA_ASSETS: Final[list[list[object]]] = [
    ["t1", SUBJECT, None, "anat", "T1w", "nifti", 100, "raw/k/t1", "v1", COMMITTED],
    ["dwi-x", "sub-X", None, "dwi", "dwi", "nifti", 50, "raw/k/dwi", "v1", COMMITTED],
    ["ev-x", "sub-X", None, "func", "events", "tsv", 9, "raw/k/ev", "v1", COMMITTED],
]


def _lfp_manifest(store: DerivedStore) -> None:
    key = lfp_build.manifest_key(ASSET_ID, "v1")
    body = {
        "series": [{"path": LFP_PATH}],
        "skipped": [{"path": IEEG_PATH, "reason": "rate 30000 Hz over 2500"}],
    }
    store.objects[key] = json.dumps(body).encode()
    store.etags[key] = '"lfp"'


# --- the per-dataset summary ---


def test_the_dataset_summary_reads_the_catalog_and_the_routes_events(
    nwb_bytes: bytes,
) -> None:
    world = make_world(nwb_bytes, extra_assets=EXTRA_ASSETS)
    _lfp_manifest(world.store)
    out = dataset_summary.summarize_dataset(DATASET, world.deps)
    assert out == {
        "key": f"summary/{DATASET}/_dataset.json",
        "write": "written",
        "sessions_sent": 1,
    }
    body = world.store.json(dataset_summary.dataset_key(DATASET))
    assert body["schema"] == "asteroidea.dataset-summary/1"
    assert body["as_of"] == "2026-10-26T10:00:00"  # the newest extracted_at read
    assert body["athena_queries"] == len(world.athena.started)
    assert body["counts"] == {
        "assets": 5,
        "bytes": len(nwb_bytes) + len(CSV_BYTES) + 159,
        "subjects": 2,
        "sessions": 3,
    }
    # Open set: dwi is not in the table, so it is kept as other:dwi, not dropped.
    assert body["modalities"] == {
        "anat": 1,
        "ieeg": 1,
        "lfp": 1,
        "other:TimeSeries": 1,
        "other:dwi": 1,
        "task": 2,
        "units": 1,
    }
    cs62 = next(s for s in body["subjects"] if s["subject"] == SUBJECT)
    assert cs62["species"] == "Homo sapiens"
    assert cs62["units"] == N_UNITS
    session = next(s for s in cs62["sessions"] if s["session"] == SESSION)
    assert session["session_summary"] == (
        f"summary/{DATASET}/{SUBJECT}/{SESSION}/latest.json"
    )
    assert [(e["path"], e["overview"]) for e in session["lfp"]] == [
        (IEEG_PATH, "skipped: rate 30000 Hz over 2500"),
        (LFP_PATH, "built"),
    ]
    assert session["lfp"][1]["channels"] == 96
    assert session["lfp"][1]["duration_s"] == 764.0
    # The event counts are the /compute/events route's on the same rows.
    route = world.route("GET /compute/events", {"asset_id": ASSET_ID})
    assert {e["type"]: e["n"] for e in session["events"]} == {
        t["name"]: t["n_events"] for t in route["event_types"]
    }
    cuts = next(e for e in session["events"] if e["type"] == "scene_cut")
    assert cuts["levels"]["cut_kind"] == {"change": 13, "continuity": 80}
    assert "scene_id" not in cuts["levels"]  # 93 levels: over the 12-level rule
    no_ses = next(s for s in cs62["sessions"] if s["session"] == "_")
    assert no_ses["nifti"] == [
        {
            "asset_id": "t1",
            "suffix": "T1w",
            "shape": [176, 256, 256],
            "voxel_mm": [1.0, 1.0, 1.2],
        }
    ]
    assert no_ses["session_summary"] is None
    other = next(s for s in body["subjects"] if s["subject"] == "sub-X")
    assert other["sessions"][0]["events"] == [
        {"type": "events", "n": 5, "levels": {"trial_type": {"go": 3, "stop": 2}}}
    ]
    assert body["readiness"]["recipe"] == {"version": "recipe-1", "kind": "file"}
    # The studies route's entry for the dataset, from the route's own fold.
    assert body["catalog"] == {
        "subjects": [
            {
                "subject": SUBJECT,
                "species": "Homo sapiens",
                "sex": "M",
                "age": "P42Y",
                "modalities": ["units", "lfp", "task"],
                "sessions": [
                    {
                        "session": SESSION,
                        "asset_ids": [ASSET_ID],
                        "nwb": [{"asset_id": ASSET_ID, "n_units": N_UNITS}],
                    }
                ],
                "units": N_UNITS,
                "trials": 2,
                "lfp": [
                    {
                        "asset_id": ASSET_ID,
                        "path": LFP_PATH,
                        "channels": 96,
                        "rate_hz": 1000.0,
                    }
                ],
            }
        ],
        "regions": [
            {"region": "amygdala", "units": 3, "channels": 4},
            {"region": "hippocampus", "units": 3, "channels": 0},
        ],
    }
    assert body["readiness"]["electrodes"] == {
        "total": 6,
        "with_coordinates": 2,
        "with_region": 4,
    }
    assert world.sent == [
        {
            "kind": "session_summary",
            "dataset": DATASET,
            "subject": SUBJECT,
            "session": SESSION,
        }
    ]


def test_every_dataset_read_passes_the_dataset_as_a_typed_literal(
    nwb_bytes: bytes,
) -> None:
    world = make_world(nwb_bytes)
    dataset_summary.build_dataset(DATASET, world.deps)
    reads = {
        q["QueryString"]: q["ExecutionParameters"]
        for q in world.athena.started
        if q["QueryString"] in dataset_summary.DATASET_QUERIES
    }
    assert reads == {sql: [_quoted(DATASET)] for sql in dataset_summary.DATASET_QUERIES}
    # The stored studies entry's seven texts bind the dataset at every placeholder.
    entry_reads = {
        q["QueryString"]: q["ExecutionParameters"]
        for q in world.athena.started
        if q["QueryString"] in catalog_api.DATASET_ENTRY_SQL
    }
    assert entry_reads == {
        sql: [_quoted(DATASET)] * sql.count("?")
        for sql in catalog_api.DATASET_ENTRY_SQL
    }
    for sql in catalog_api.DATASET_ENTRY_SQL:
        assert "?" in sql
        assert DATASET not in sql
    for sql in dataset_summary.DATASET_QUERIES:
        assert "?" in sql
        assert DATASET not in sql


def test_a_missing_lfp_manifest_reads_as_not_built(nwb_bytes: bytes) -> None:
    world = make_world(nwb_bytes)
    body = dataset_summary.build_dataset(DATASET, world.deps).summary
    lfp = body["subjects"][0]["sessions"][0]["lfp"]
    assert {e["overview"] for e in lfp} == {"skipped: not built"}


@pytest.mark.parametrize(
    ("kind", "name", "expected"),
    [
        ("anat", "T1w", "anat"),
        ("func", "bold", "func"),
        ("fmap", "epi", "fmap"),
        ("dwi", "dwi", "other:dwi"),
        ("anat", "FLAIR", "other:FLAIR"),
    ],
)
def test_the_modality_table_is_an_open_set(kind: str, name: str, expected: str) -> None:
    assert dataset_summary.modality(kind, name) == expected


def test_an_ieeg_series_and_an_lfp_series_are_two_modalities() -> None:
    assert dataset_summary.series_modality(IEEG_PATH, "ElectricalSeries") == "ieeg"
    assert dataset_summary.series_modality(LFP_PATH, "ElectricalSeries") == "lfp"
    path = "/processing/behavior/Position/SpatialSeries"
    assert dataset_summary.series_modality(path, "SpatialSeries") == "behavior"
    raw = "/acquisition/ElectricalSeries"
    assert dataset_summary.series_modality(raw, "ElectricalSeries") == (
        "other:ElectricalSeries"
    )


# --- the conditional write ---


KEY: Final = "summary/ds/_dataset.json"


def test_a_first_write_creates_only_when_absent() -> None:
    store: Any = DerivedStore()
    assert dataset_summary.write_if_newer(store, DERIVED, KEY, {"as_of": "a"}) == (
        "written"
    )
    assert store.puts[0]["IfNoneMatch"] == "*"
    assert "IfMatch" not in store.puts[0]


def test_a_rewrite_matches_the_etag_it_read() -> None:
    store: Any = DerivedStore()
    dataset_summary.write_if_newer(store, DERIVED, KEY, {"as_of": "a"})
    etag = store.etags[KEY]
    dataset_summary.write_if_newer(store, DERIVED, KEY, {"as_of": "b"})
    assert store.puts[1]["IfMatch"] == etag
    assert store.json(KEY) == {"as_of": "b"}


def test_a_stored_newer_summary_is_kept() -> None:
    store: Any = DerivedStore()
    dataset_summary.write_if_newer(store, DERIVED, KEY, {"as_of": "b"})
    assert dataset_summary.write_if_newer(store, DERIVED, KEY, {"as_of": "a"}) == (
        "kept newer"
    )
    assert store.json(KEY) == {"as_of": "b"}
    assert len(store.puts) == 1


def test_a_write_in_between_is_refused_then_re_read_and_retried() -> None:
    store: Any = DerivedStore()
    dataset_summary.write_if_newer(store, DERIVED, KEY, {"as_of": "a"})

    def other_writer() -> None:
        store.objects[KEY] = json.dumps({"as_of": "a2"}).encode()
        store.etags[KEY] = '"other"'

    store.before_put.append(other_writer)
    assert dataset_summary.write_if_newer(store, DERIVED, KEY, {"as_of": "b"}) == (
        "written"
    )
    assert store.puts[-1]["IfMatch"] == '"other"'
    assert store.json(KEY) == {"as_of": "b"}


def test_a_key_that_never_settles_is_an_error() -> None:
    store: Any = DerivedStore()
    dataset_summary.write_if_newer(store, DERIVED, KEY, {"as_of": "a"})
    count = 0

    def other_writer() -> None:
        nonlocal count
        count += 1
        store.etags[KEY] = f'"other-{count}"'

    store.before_put.extend([other_writer] * dataset_summary.WRITE_ATTEMPTS)
    with pytest.raises(dataset_summary.SummaryError, match="still changing"):
        dataset_summary.write_if_newer(store, DERIVED, KEY, {"as_of": "b"})
    assert count == dataset_summary.WRITE_ATTEMPTS


# --- the per-session summary ---


def _rates_query(event_type: str, units: str = "all") -> dict[str, str]:
    return {
        "asset_id": ASSET_ID,
        "units": units,
        "align": f"event:{event_type}",
        "pre_s": "1",
        "post_s": "2",
        "bin_s": "0.05",
        "zscore": "1",
        "raster": "0",
    }


def _session(world: World) -> dict[str, Any]:
    return dataset_summary.summarize_session(DATASET, SUBJECT, SESSION, world.deps)


def _stored(world: World) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    latest = world.store.json(dataset_summary.latest_key(DATASET, SUBJECT, SESSION))
    manifest = world.store.json(latest["manifest"])
    prefix = latest["manifest"].rsplit("/", 1)[0]
    parts = {name: world.store.json(f"{prefix}/{name}") for name in manifest["parts"]}
    return manifest, parts


def test_each_stored_part_equals_the_route_body_for_the_same_inputs(
    nwb_bytes: bytes,
) -> None:
    world = make_world(nwb_bytes)
    assert _session(world)["outcome"] == "built"
    manifest, parts = _stored(world)
    assert manifest["parts"] == [
        "events.json",
        "units.json",
        "rates-scene_cut.json",
        "tuning-scene_cut-cut_kind.json",
        "rates-recognition_onset.json",
        "tuning-recognition_onset-familiarity.json",
    ]
    assert parts.pop("events.json") == world.route(
        "GET /compute/events", {"asset_id": ASSET_ID}
    )
    assert parts.pop("units.json") == world.table(ASSET_ID, "units")
    assert manifest["units_table"] == {"rows": N_UNITS, "more": False}
    for name, body in parts.items():
        kind, event_type, *attribute = name.removesuffix(".json").split("-")
        if kind == "rates":
            route = world.route("GET /compute/rates", _rates_query(event_type))
        else:
            query = {
                "asset_id": ASSET_ID,
                "event": event_type,
                "attribute": attribute[0],
                "units": "all",
            }
            route = world.route("GET /compute/tuning", query)
        assert body == route, name
    # scene_id (93 levels) is never tried; an attribute the route refuses is listed
    # and the next one in recipe order is tried.
    assert [(s["part"], s["code"]) for s in manifest["skipped"]] == [
        ("tuning-recognition_onset-actual_response.json", "too_few_events_per_level"),
        ("tuning-recognition_onset-response_correct.json", "too_few_events_per_level"),
    ]


def test_the_manifest_is_written_last_and_latest_points_at_it(
    nwb_bytes: bytes,
) -> None:
    world = make_world(nwb_bytes)
    done = _session(world)
    keys = [p["Key"] for p in world.store.puts]
    latest = dataset_summary.latest_key(DATASET, SUBJECT, SESSION)
    prefix = f"summary/{DATASET}/{SUBJECT}/{SESSION}/{done['digest']}"
    assert keys[-2:] == [f"{prefix}/manifest.json", latest]
    assert all(k.startswith(f"{prefix}/") for k in keys[:-1])
    manifest = world.store.json(f"{prefix}/manifest.json")
    assert manifest["schema"] == "asteroidea.session-summary/1.1"
    assert manifest["display"] == dataset_summary.DISPLAY
    assert manifest["units"] == {"total": N_UNITS, "stored": N_UNITS}
    assert manifest["inputs"]["assets"] == [
        {"asset_id": ASSET_ID, "s3_version_id": "v1"}
    ]
    assert world.store.json(latest) == {
        "digest": done["digest"],
        "manifest": f"{prefix}/manifest.json",
        "as_of": "2026-10-24T17:50:20",
    }


def test_a_rebuild_of_the_same_inputs_writes_no_part(nwb_bytes: bytes) -> None:
    world = make_world(nwb_bytes)
    first = _session(world)
    count = len(world.store.puts)
    again = _session(world)
    assert again == {
        "outcome": "exists",
        "digest": first["digest"],
        "latest": "written",
    }
    assert [p["Key"] for p in world.store.puts[count:]] == [
        dataset_summary.latest_key(DATASET, SUBJECT, SESSION)
    ]


def test_the_digest_moves_with_each_input() -> None:
    assets: list[dict[str, object]] = [{"asset_id": "a", "s3_version_id": "v1"}]
    base = dataset_summary.session_digest("recipe-1", "2026.10", "c1", assets)
    assert base != dataset_summary.session_digest("recipe-2", "2026.10", "c1", assets)
    assert base != dataset_summary.session_digest("recipe-1", "2026.11", "c1", assets)
    assert base != dataset_summary.session_digest("recipe-1", "2026.10", "c2", assets)
    moved: list[dict[str, object]] = [{"asset_id": "a", "s3_version_id": "v2"}]
    assert base != dataset_summary.session_digest("recipe-1", "2026.10", "c1", moved)


def test_a_session_over_the_unit_bound_stores_the_first_units(
    nwb_bytes: bytes,
) -> None:
    total = compute_api.MAX_UNITS + 3
    world = make_world(nwb_bytes, n_units=total)
    _session(world)
    manifest, parts = _stored(world)
    assert manifest["units"] == {"total": total, "stored": compute_api.MAX_UNITS}
    rates = parts["rates-scene_cut.json"]
    assert [u["unit_index"] for u in rates["units"]] == list(
        range(compute_api.MAX_UNITS)
    )
    route = world.route(
        "GET /compute/rates",
        _rates_query("scene_cut", units=f"0-{compute_api.MAX_UNITS - 1}"),
    )
    assert rates == route


def test_a_units_table_over_one_page_stores_the_first_page_and_says_so(
    nwb_bytes: bytes,
) -> None:
    total = catalog_api.PAGE_SIZE + 3
    world = make_world(nwb_bytes, n_units=total)
    _session(world)
    manifest, parts = _stored(world)
    units = parts["units.json"]
    assert (units["page"], units["more"]) == (0, True)
    assert [r["unit_index"] for r in units["rows"]] == list(
        range(catalog_api.PAGE_SIZE)
    )
    assert units == world.table(ASSET_ID, "units")
    assert manifest["units_table"] == {"rows": catalog_api.PAGE_SIZE, "more": True}


def test_the_builder_version_moves_the_digest_so_a_rebuild_stores_new_parts() -> None:
    """A summary-1 manifest has no events or units part; the code input moves."""
    assert dataset_summary.code_version().startswith("summary-2+")


def test_a_session_with_no_nwb_file_writes_nothing(nwb_bytes: bytes) -> None:
    world = make_world(nwb_bytes)
    out = dataset_summary.summarize_session(DATASET, "sub-X", "_", world.deps)
    assert out == {"outcome": "skipped", "reason": "no NWB file in the session"}
    assert world.store.puts == []


# --- messages ---


@pytest.mark.parametrize(
    "body",
    [
        {"kind": "summary", "dataset": "../x"},
        {"kind": "summary"},
        {"kind": "session_summary", "dataset": DATASET, "subject": "a/b"},
        {"kind": "other", "dataset": DATASET},
    ],
)
def test_a_bad_message_is_refused(nwb_bytes: bytes, body: dict[str, str]) -> None:
    world = make_world(nwb_bytes)
    with pytest.raises(dataset_summary.SummaryError):
        dataset_summary.run_message(body, world.deps)
    assert world.athena.started == []
