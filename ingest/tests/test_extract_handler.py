"""ExtractFunction: dispatch, claim, table order, idempotence, FAILED."""

import gzip
import io
import json
import math
import sys
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final, override

import numpy as np
import pytest
from botocore.exceptions import ClientError, EndpointConnectionError, ReadTimeoutError
from botocore.response import StreamingBody
from botocore.stub import ANY
from nibabel.nifti1 import Nifti1Header
from pyiceberg.catalog.sql import SqlCatalog
from pynwb import NWBHDF5IO, NWBFile

from asteroidea_infra import schema
from asteroidea_infra.catalog_init import iceberg_schema
from asteroidea_ingest import sessions
from asteroidea_ingest.catalog_writer import CatalogWriter
from asteroidea_ingest.extract import bids, derive, handler, retry, rows
from asteroidea_ingest.extract.handler import ExtractDeps
from asteroidea_ingest.sessions import SessionTable

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence
    from pathlib import Path

    from pyiceberg.catalog import Catalog

BUCKET: Final = "raw-bucket"
TABLE: Final = "sessions"
SID: Final = "11111111-1111-4111-8111-111111111111"
ASSET: Final = "22222222-2222-4222-8222-222222222222"
NWB_RELPATH: Final = "sub-01/ses-A/ecephys/sub-01_ses-A_ecephys.nwb"
NOW: Final = 2000
ANY_UPDATE: Final = {
    "TableName": ANY,
    "Key": ANY,
    "UpdateExpression": ANY,
    "ConditionExpression": ANY,
    "ExpressionAttributeNames": ANY,
    "ExpressionAttributeValues": ANY,
}
new_nwbfile: Any = NWBFile


@pytest.fixture
def catalog(tmp_path: Path) -> Iterator[Catalog]:
    cat = SqlCatalog(
        "test",
        uri=f"sqlite:///{tmp_path}/catalog.db",
        warehouse=f"file://{tmp_path}/warehouse",
    )
    cat.create_namespace("asteroidea")
    for spec in schema.TABLES:
        cat.create_table(f"asteroidea.{spec.name}", iceberg_schema(spec))
    yield cat
    cat.engine.dispose()


class RecordingWriter(CatalogWriter):
    """A real writer that remembers which tables it was asked to append to."""

    def __init__(self, catalog: Catalog) -> None:
        super().__init__(catalog)
        self.calls: list[str] = []
        self.replaced: list[str] = []
        self.fail_on: str | None = None

    @override
    def replace(
        self, table_name: str, key: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]
    ) -> int | None:
        if table_name == self.fail_on:
            msg = f"boom on {table_name}"
            raise RuntimeError(msg)
        self.replaced.append(table_name)
        return super().replace(table_name, key, rows)

    @override
    def append_if_absent(
        self, table_name: str, key: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]
    ) -> int:
        self.calls.append(table_name)
        return super().append_if_absent(table_name, key, rows)


@dataclass
class Run:
    h: Any
    catalog: Catalog
    nwb_path: Path | None = None
    writer: RecordingWriter = field(init=False)
    updates: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.writer = RecordingWriter(self.catalog)
        self.h.ddb.meta.events.register(
            "before-parameter-build.dynamodb.UpdateItem",
            lambda params, **_: self.updates.append(
                {
                    "values": sessions.unmarshal(params["ExpressionAttributeValues"]),
                    "set": params["UpdateExpression"],
                }
            ),
        )

    def deps(self) -> ExtractDeps:
        path = self.nwb_path

        def opener(url: str) -> NWBHDF5IO:
            assert "versionId=rv1" in url, url
            assert path is not None
            return NWBHDF5IO(str(path), "r", load_namespaces=True)

        return ExtractDeps(
            table=SessionTable(self.h.ddb, TABLE),
            s3=self.h.s3,
            writer=self.writer,
            bucket=BUCKET,
            open_nwb=opener,
        )

    def session(self, relpath: str, **extra: Any) -> None:
        self.h.get_item(
            state=sessions.COMMITTED,
            relpath=relpath,
            asset_id=ASSET,
            raw_version_id="rv1",
            raw_key=f"raw/ds1/{relpath}",
            extract_state="PENDING",
            **extra,
        )

    def claim_ok(self) -> None:
        self.h.ddb_stub.add_response("update_item", {}, expected_params=ANY_UPDATE)

    def run(self) -> str:
        return handler.extract({"session_id": SID}, self.deps(), now=NOW)

    def final_state(self) -> str:
        return str(self.updates[-1]["values"][":state"])

    def count(self, table: str) -> int:
        return len(self.h.rows(table))

    def body(self, data: bytes, rng: str, relpath: str) -> None:
        self.h.s3_stub.add_response(
            "get_object",
            {"Body": StreamingBody(io.BytesIO(data), len(data))},
            expected_params={
                "Bucket": BUCKET,
                "Key": f"raw/ds1/{relpath}",
                "VersionId": "rv1",
                "Range": rng,
            },
        )


def _nwb(path: Path) -> None:
    nwb = new_nwbfile(
        session_description="d",
        identifier="id-1",
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
    nwb.add_unit(spike_times=[1.0, 2.0], electrodes=[1])
    nwb.add_trial(start_time=0.0, stop_time=1.0)
    with NWBHDF5IO(str(path), "w") as io_:
        io_.write(nwb)


@pytest.fixture
def run(harness: Any, catalog: Catalog) -> Run:
    return Run(harness, catalog)


def test_nwb_writes_every_table_once_in_order_and_marks_extracted(
    run: Run, tmp_path: Path
) -> None:
    run.nwb_path = tmp_path / "f.nwb"
    _nwb(run.nwb_path)
    run.session(NWB_RELPATH)
    run.claim_ok()
    run.claim_ok()  # EXTRACTED
    assert run.run() == handler.EXTRACTED
    # no TimeSeries in the file, so no `series` call
    assert run.writer.calls == [
        "electrodes",
        "units",
        "intervals",
        "unit_spikes",
        "subjects",
        "sessions",
        "asset_headers",
    ]
    assert [run.count(t) for t in ("electrodes", "units", "intervals")] == [2, 1, 1]
    [spikes] = run.h.rows("unit_spikes")
    assert (spikes["unit_index"], spikes["spike_count"]) == (0, 2)
    assert [run.count(t) for t in ("subjects", "sessions", "asset_headers")] == [1] * 3
    [unit] = run.h.rows("units")
    assert (unit["spike_count"], unit["location"]) == (2, "L1")
    assert (unit["subject"], unit["bids_session"]) == ("sub-01", "ses-A")
    assert run.final_state() == "EXTRACTED"
    run.h.finish()


def test_a_rerun_appends_nothing_new(run: Run, tmp_path: Path) -> None:
    run.nwb_path = tmp_path / "f.nwb"
    _nwb(run.nwb_path)
    for _ in range(2):
        run.session(NWB_RELPATH)
        run.claim_ok()
        run.claim_ok()
        assert run.run() == handler.EXTRACTED
    assert [run.count(t) for t in ("electrodes", "units", "asset_headers")] == [2, 1, 1]


def _nifti_header(shape: tuple[int, ...], zooms: tuple[float, ...]) -> bytes:
    header: Any = Nifti1Header()
    header.set_data_shape(shape)
    header.set_zooms(zooms)
    header.set_data_dtype(np.int16)
    header.set_xyzt_units("mm", "sec")
    buffer = io.BytesIO()
    header.write_to(buffer)
    return buffer.getvalue()


def test_nii_reads_the_first_352_bytes_and_parses_the_header(run: Run) -> None:
    relpath = "sub-01/func/sub-01_task-x_bold.nii"
    raw = _nifti_header((4, 5, 6, 7), (2.0, 2.0, 3.0, 1.5)) + b"\0" * 4
    run.session(relpath)
    run.claim_ok()
    run.body(raw, "bytes=0-351", relpath)
    run.claim_ok()
    assert run.run() == handler.EXTRACTED
    [header] = run.h.rows("asset_headers")
    assert header["format"] == "nifti"
    assert header["nifti_shape"] == [4, 5, 6, 7]
    assert header["nifti_voxel_mm"] == [2.0, 2.0, 3.0]
    assert (header["nifti_datatype"], header["tr_seconds"]) == ("int16", 1.5)
    run.h.finish()


def test_nii_gz_inflates_the_first_bytes_of_a_truncated_stream(run: Run) -> None:
    relpath = "sub-01/anat/sub-01_T1w.nii.gz"
    voxels = b"\0" * 4 + bytes(range(256)) * 800
    full = gzip.compress(_nifti_header((10, 11, 12), (1.0, 1.0, 1.0)) + voxels)
    head = full[: 64 * 1024]
    run.session(relpath)
    run.claim_ok()
    run.body(head, "bytes=0-65535", relpath)
    run.claim_ok()
    assert run.run() == handler.EXTRACTED
    [header] = run.h.rows("asset_headers")
    assert header["nifti_shape"] == [10, 11, 12]
    assert header["tr_seconds"] is None
    run.h.finish()


def test_a_json_sidecar_goes_to_sidecar_json(run: Run) -> None:
    relpath = "sub-01/func/sub-01_task-x_bold.json"
    text = b'{"RepetitionTime": 2, "TaskName": "x"}'
    run.session(relpath, size=len(text))
    run.claim_ok()
    run.body(text, f"bytes=0-{len(text) - 1}", relpath)
    run.claim_ok()
    assert run.run() == handler.EXTRACTED
    [header] = run.h.rows("asset_headers")
    assert json.loads(header["sidecar_json"]) == {"RepetitionTime": 2, "TaskName": "x"}
    assert header["format"] == "json"


def test_participants_tsv_gives_subject_rows_and_a_header(run: Run) -> None:
    relpath = "participants.tsv"
    text = b"participant_id\tsex\tage\nsub-01\tF\t30\nsub-02\tn/a\t41\n"
    run.session(relpath, size=len(text))
    run.claim_ok()
    run.body(text, f"bytes=0-{len(text) - 1}", relpath)
    run.claim_ok()
    assert run.run() == handler.EXTRACTED
    got = sorted(
        (r["subject"], r["sex"], r["age"], r["source"]) for r in run.h.rows("subjects")
    )
    assert got == [
        ("sub-01", "F", "30", "participants.tsv"),
        ("sub-02", None, "41", "participants.tsv"),
    ]
    assert run.writer.calls[-1] == "asset_headers"
    assert run.h.rows("asset_headers")[0]["format"] == "tsv"


def test_events_tsv_gives_event_rows_and_a_header(run: Run) -> None:
    key = "sub-01/ses-A/func/sub-01_ses-A_task-x_run-1"
    relpath = f"{key}_events.tsv"
    text = (
        b"onset\tduration\ttrial_type\tresponse\n"
        b"1.5\t0.5\tgo\tyes\n"
        b"4.25\tn/a\tn/a\tn/a\n"
    )
    run.session(relpath, size=len(text))
    run.claim_ok()
    run.body(text, f"bytes=0-{len(text) - 1}", relpath)
    run.claim_ok()
    assert run.run() == handler.EXTRACTED
    assert run.writer.calls == ["events", "asset_headers"]
    first, second = run.h.rows("events")
    assert [
        (
            r["events_for"],
            r["row_index"],
            r["onset_s"],
            r["duration_s"],
            r["trial_type"],
        )
        for r in (first, second)
    ] == [(key, 0, 1.5, 0.5, "go"), (key, 1, 4.25, None, None)]
    assert json.loads(first["columns_json"]) == {"response": "yes"}
    assert second["columns_json"] is None
    assert (first["subject"], first["bids_session"]) == ("sub-01", "ses-A")
    assert run.h.rows("asset_headers")[0]["format"] == "tsv"
    assert run.final_state() == "EXTRACTED"


def test_events_tsv_over_the_text_limit_is_refused(run: Run) -> None:
    relpath = "sub-01/func/sub-01_task-x_events.tsv"
    run.session(relpath, size=bids.MAX_TEXT_BYTES + 1)
    run.claim_ok()
    run.claim_ok()
    with pytest.raises(bids.BidsError, match="limit"):
        run.run()
    assert run.writer.calls == []
    run.h.finish()


def test_an_unknown_format_is_skipped_and_writes_nothing(run: Run) -> None:
    run.session("sub-01/beh/sub-01_events.csv")
    run.claim_ok()
    run.claim_ok()
    assert run.run() == handler.SKIPPED
    assert run.writer.calls == []
    assert run.final_state() == "SKIPPED"
    run.h.finish()


def test_a_session_that_cannot_be_claimed_does_nothing(run: Run) -> None:
    run.session(NWB_RELPATH)
    run.h.ddb_stub.add_client_error(
        "update_item",
        service_error_code="ConditionalCheckFailedException",
        expected_params=ANY_UPDATE,
    )
    assert run.run() == handler.NOT_CLAIMED
    assert run.writer.calls == []
    run.h.finish()


def test_a_failure_sets_failed_and_the_guard_records_class_and_message(
    run: Run,
) -> None:
    relpath = "sub-01/func/sub-01_task-x_bold.json"
    text = b"{not json"
    attempts = {"Attributes": {"extract_attempts": {"N": "1"}}}
    run.h.ddb_stub.add_response(
        "update_item",
        attempts,
        expected_params=ANY_UPDATE | {"ReturnValues": "UPDATED_NEW"},
    )
    run.session(relpath, size=len(text))
    run.claim_ok()
    run.body(text, f"bytes=0-{len(text) - 1}", relpath)
    run.claim_ok()  # FAILED
    run.claim_ok()  # extract_error
    outcomes = handler.run_batch(
        {"Records": [{"body": json.dumps({"session_id": SID})}]}, run.deps(), now=NOW
    )
    assert outcomes == ["ERROR"]
    assert run.updates[-2]["values"][":state"] == "FAILED"
    error = run.updates[-1]["values"][":extract_error"]
    assert error["class"] == "BidsError"
    assert "not valid JSON" in error["message"]
    assert run.writer.calls == []
    run.h.finish()


def test_row_columns_come_from_the_schema(run: Run, tmp_path: Path) -> None:
    run.nwb_path = tmp_path / "f.nwb"
    _nwb(run.nwb_path)
    run.session(NWB_RELPATH)
    run.claim_ok()
    run.claim_ok()
    run.run()
    names_checked = (
        "electrodes",
        "units",
        "unit_spikes",
        "intervals",
        "subjects",
        "asset_headers",
    )
    for name in names_checked:
        names = {c.name for c in schema.SPECS[name].columns}
        assert set(run.h.rows(name)[0]) == names


def _flaky_deps(
    run: Run, failures: list[Exception], sleeps: list[float]
) -> ExtractDeps:
    """Deps whose opener raises each of `failures` in turn, then opens the file."""
    good = run.deps()

    def opener(url: str) -> NWBHDF5IO:
        if failures:
            raise failures.pop(0)
        return good.open_nwb(url)

    return replace(good, open_nwb=opener, sleep=sleeps.append)


def test_a_transient_read_error_is_retried_in_the_invocation(
    run: Run, tmp_path: Path
) -> None:
    """One S3 hiccup, no FAILED, no operator step."""
    run.nwb_path = tmp_path / "f.nwb"
    _nwb(run.nwb_path)
    run.session(NWB_RELPATH)
    run.claim_ok()
    run.claim_ok()  # EXTRACTED, no FAILED write in between
    sleeps: list[float] = []
    failures: list[Exception] = [EndpointConnectionError(endpoint_url="https://s3")]
    deps = _flaky_deps(run, failures, sleeps)
    now = NOW
    assert handler.extract({"session_id": SID}, deps, now=now) == handler.EXTRACTED
    assert sleeps == [retry.BACKOFF_SECONDS[0]]
    assert run.final_state() == "EXTRACTED"
    assert run.count("asset_headers") == 1
    run.h.finish()


def test_a_deterministic_error_goes_to_failed_at_once(run: Run, tmp_path: Path) -> None:
    run.nwb_path = tmp_path / "f.nwb"
    _nwb(run.nwb_path)
    run.session(NWB_RELPATH)
    run.claim_ok()
    run.claim_ok()  # FAILED
    sleeps: list[float] = []
    deps = _flaky_deps(run, [ValueError("not an NWB file")], sleeps)
    with pytest.raises(ValueError, match="not an NWB file"):
        handler.extract({"session_id": SID}, deps, now=NOW)
    assert sleeps == []
    assert run.final_state() == "FAILED"
    run.h.finish()


def test_a_transient_error_that_never_clears_ends_in_failed(
    run: Run, tmp_path: Path
) -> None:
    run.nwb_path = tmp_path / "f.nwb"
    _nwb(run.nwb_path)
    run.session(NWB_RELPATH)
    run.claim_ok()
    run.claim_ok()  # FAILED after the retries
    sleeps: list[float] = []
    errors: list[Exception] = [ReadTimeoutError(endpoint_url="https://s3")] * 3
    deps = _flaky_deps(run, errors, sleeps)
    with pytest.raises(ReadTimeoutError):
        handler.extract({"session_id": SID}, deps, now=NOW)
    assert sleeps == list(retry.BACKOFF_SECONDS)
    assert run.final_state() == "FAILED"
    run.h.finish()


# -- re-extraction ---------------------------------------------------------------

TRACKING: Final = f"reextract:run1:{ASSET}"


def _message(relpath: str = NWB_RELPATH, **extra: str) -> dict[str, str]:
    return {
        "session_id": TRACKING,
        "asset_id": ASSET,
        "dataset": "ds1",
        "relpath": relpath,
        "s3_key": f"raw/ds1/{relpath}",
        "s3_version_id": "rv1",
        "file_format": "nwb",
        "size": "100",
        **extra,
    }


def _reextract(run: Run, message: dict[str, str]) -> list[str]:
    run.claim_ok()  # the tracking item update
    return handler.run_batch(
        {"Records": [{"body": json.dumps(message)}]}, run.deps(), now=NOW
    )


def test_reextract_replaces_instead_of_doubling_and_marks_done(
    run: Run, tmp_path: Path
) -> None:
    run.nwb_path = tmp_path / "f.nwb"
    _nwb(run.nwb_path)
    for _ in range(2):
        assert _reextract(run, _message()) == [handler.REEXTRACTED]
    counts = [run.count(t) for t in ("electrodes", "units", "unit_spikes", "intervals")]
    assert counts == [2, 1, 1, 1]  # a second extraction without replace would double
    assert run.count("asset_headers") == 1
    values = run.updates[-1]["values"]
    assert values[":reextract_state"] == "DONE"
    assert values[":tables_replaced"] == [
        "series",
        "electrodes",
        "units",
        "intervals",
        "unit_spikes",
        "asset_headers",
    ]
    run.h.finish()


def test_reextract_replaces_per_asset_tables_in_order_and_appends_the_rest(
    run: Run, tmp_path: Path
) -> None:
    run.nwb_path = tmp_path / "f.nwb"
    _nwb(run.nwb_path)
    _reextract(run, _message())
    assert run.writer.replaced == [
        "series",
        "electrodes",
        "units",
        "intervals",
        "unit_spikes",
        "asset_headers",
    ]
    assert run.writer.calls == ["subjects", "sessions"]  # append-if-absent only
    run.h.finish()


def test_reextract_never_touches_the_session_state_fields(
    run: Run, tmp_path: Path
) -> None:
    run.nwb_path = tmp_path / "f.nwb"
    _nwb(run.nwb_path)
    _reextract(run, _message())
    (update,) = run.updates
    assert update["set"].count("=") == len(update["values"])
    assert not {":state", ":extract_state", ":extract_error"} & update["values"].keys()
    assert "#state " not in update["set"]
    run.h.finish()


def test_reextract_failure_records_class_message_and_tables_replaced(
    run: Run, tmp_path: Path
) -> None:
    run.nwb_path = tmp_path / "f.nwb"
    _nwb(run.nwb_path)
    run.writer.fail_on = "intervals"
    assert _reextract(run, _message()) == ["ERROR"]  # never raises
    values = run.updates[-1]["values"]
    assert values[":reextract_state"] == "FAILED"
    error = values[":reextract_error"]
    assert error["class"] == "RuntimeError"
    assert error["message"] == "boom on intervals"
    assert error["tables_replaced"] == ["series", "electrodes", "units"]
    assert run.count("asset_headers") == 0  # asset_headers is last: not reached
    run.h.finish()


def test_reextract_of_a_format_without_an_extractor_is_failed_not_raised(
    run: Run,
) -> None:
    message = _message("sub-01/anat/sub-01_T1w.png", file_format="png")
    assert _reextract(run, message) == ["ERROR"]
    error = run.updates[-1]["values"][":reextract_error"]
    assert error["class"] == "NoExtractorError"
    assert error["tables_replaced"] == []
    run.h.finish()


def test_reextract_that_cannot_record_its_outcome_still_does_not_raise(
    run: Run, tmp_path: Path
) -> None:
    run.nwb_path = tmp_path / "f.nwb"
    _nwb(run.nwb_path)
    run.h.ddb_stub.add_client_error(
        "update_item", service_error_code="InternalServerError"
    )
    outcomes = handler.run_batch(
        {"Records": [{"body": json.dumps(_message())}]}, run.deps(), now=NOW
    )
    assert outcomes == ["ERROR"]
    run.h.finish()


# -- LFP build trigger -----------------------------------------------------------


def _build_message() -> dict[str, str]:
    return {
        "asset_id": ASSET,
        "dataset": "ds1",
        "s3_key": f"raw/ds1/{NWB_RELPATH}",
        "s3_version_id": "rv1",
    }


def test_an_nwb_extraction_sends_exactly_one_lfp_build_message(
    run: Run, tmp_path: Path
) -> None:
    run.nwb_path = tmp_path / "f.nwb"
    _nwb(run.nwb_path)
    run.session(NWB_RELPATH, dataset="ds1")
    run.claim_ok()
    run.claim_ok()
    sent: list[dict[str, str]] = []
    deps = replace(run.deps(), request_lfp_build=sent.append)
    assert handler.extract({"session_id": SID}, deps, now=NOW) == handler.EXTRACTED
    assert sent == [_build_message()]
    run.h.finish()


def test_an_nwb_reextraction_sends_exactly_one_lfp_build_message(
    run: Run, tmp_path: Path
) -> None:
    run.nwb_path = tmp_path / "f.nwb"
    _nwb(run.nwb_path)
    run.claim_ok()  # the tracking item update
    sent: list[dict[str, str]] = []
    deps = replace(run.deps(), request_lfp_build=sent.append)
    event = {"Records": [{"body": json.dumps(_message())}]}
    assert handler.run_batch(event, deps, now=NOW) == [handler.REEXTRACTED]
    assert sent == [_build_message()]
    run.h.finish()


def test_a_nifti_extraction_sends_no_lfp_build_message(run: Run) -> None:
    relpath = "sub-01/func/sub-01_task-x_bold.nii"
    raw = _nifti_header((4, 5, 6, 7), (2.0, 2.0, 3.0, 1.5)) + b"\0" * 4
    run.session(relpath)
    run.claim_ok()
    run.body(raw, "bytes=0-351", relpath)
    run.claim_ok()
    sent: list[dict[str, str]] = []
    deps = replace(run.deps(), request_lfp_build=sent.append)
    assert handler.extract({"session_id": SID}, deps, now=NOW) == handler.EXTRACTED
    assert sent == []
    run.h.finish()


def test_a_failed_lfp_build_send_does_not_change_the_extraction(
    run: Run, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    run.nwb_path = tmp_path / "f.nwb"
    _nwb(run.nwb_path)
    run.session(NWB_RELPATH)
    run.claim_ok()
    run.claim_ok()  # EXTRACTED, with no FAILED write
    calls: list[dict[str, str]] = []

    def broken(message: dict[str, str]) -> None:
        calls.append(message)
        msg = "queue unavailable"
        raise RuntimeError(msg)

    deps = replace(run.deps(), request_lfp_build=broken)
    assert handler.extract({"session_id": SID}, deps, now=NOW) == handler.EXTRACTED
    assert len(calls) == 1  # sent once: no retry of the extraction
    assert run.final_state() == "EXTRACTED"
    assert run.count("asset_headers") == 1
    assert "could not queue the LFP build" in caplog.text
    run.h.finish()


def test_the_sqs_sender_sends_the_message_to_the_standard_queue() -> None:
    sent: list[dict[str, str]] = []

    class Queue:
        def send_message(self, **kwargs: str) -> object:
            sent.append(kwargs)
            return {}

    url = "https://sqs.us-east-1.amazonaws.com/000000000000/asteroidea-lfp-build"
    handler.sqs_lfp_sender(Queue(), url)(_build_message())
    assert sent == [{"QueueUrl": url, "MessageBody": json.dumps(_build_message())}]


# -- re-extract derive and rows-by-pointer commit ---------------------------------

DERIVED_BUCKET: Final = "derived-bucket"
RUN_ID: Final = "run1"
FROZEN: Final = datetime(2026, 10, 26, 9, 30, 15, 123456, tzinfo=UTC).replace(
    tzinfo=None
)
EVENTS_KEY: Final = "sub-01/ses-A/func/sub-01_ses-A_task-x_run-1"
EVENTS_TEXT: Final = (
    b"onset\ttrial_type\tresponse\n1.5\tgo\tyes\n4.25\tn/a\tn/a\n"
    b"1e-9\tstop\t0.30000000000000004\n"
)
PARTICIPANTS_TEXT: Final = b"participant_id\tsex\tage\nsub-01\tF\t30\nsub-02\tn/a\t41\n"
REPLACED_OR_APPENDED: Final = sorted(handler.REPLACED_TABLES | handler.APPENDED_TABLES)


class FakeStore:
    """An in-memory derived bucket: put and whole-object get, NoSuchKey when absent."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.order: list[str] = []

    def put_object(self, **kwargs: Any) -> object:
        assert kwargs["Bucket"] == DERIVED_BUCKET
        self.objects[kwargs["Key"]] = bytes(kwargs["Body"])
        self.order.append(kwargs["Key"])
        return {}

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        assert kwargs["Bucket"] == DERIVED_BUCKET
        if kwargs["Key"] not in self.objects:
            raise ClientError(
                {"Error": {"Code": "NoSuchKey", "Message": ""}}, "GetObject"
            )
        return {"Body": io.BytesIO(self.objects[kwargs["Key"]])}

    def manifest(self) -> dict[str, Any]:
        return json.loads(self.objects[rows.manifest_key(ASSET, RUN_ID)])

    def set_manifest(self, manifest: dict[str, Any]) -> None:
        self.objects[rows.manifest_key(ASSET, RUN_ID)] = rows.encode_json(manifest)


@pytest.fixture
def pointer_catalog(tmp_path: Path) -> Iterator[SqlCatalog]:
    """Yield a second, empty catalog for the pointer path."""
    path = tmp_path / "pointer"
    path.mkdir()
    cat = SqlCatalog(
        "test", uri=f"sqlite:///{path}/catalog.db", warehouse=f"file://{path}/warehouse"
    )
    cat.create_namespace("asteroidea")
    for spec in schema.TABLES:
        cat.create_table(f"asteroidea.{spec.name}", iceberg_schema(spec))
    yield cat
    cat.engine.dispose()


def _table_rows(cat: Catalog) -> dict[str, list[dict[str, Any]]]:
    return {
        spec.name: cat.load_table(f"asteroidea.{spec.name}")
        .scan()
        .to_arrow()
        .to_pylist()
        for spec in schema.TABLES
    }


@pytest.fixture
def frozen(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both paths stamp the same `extracted_at`, so whole rows compare."""
    monkeypatch.setattr(handler, "utc_now_naive", lambda: FROZEN)
    monkeypatch.setattr(derive, "utc_now_naive", lambda: FROZEN)


def _pointer_deps(run: Run, store: FakeStore, writer: CatalogWriter) -> ExtractDeps:
    return replace(
        run.deps(), writer=writer, derived=store, derived_bucket=DERIVED_BUCKET
    )


def _derive(run: Run, store: FakeStore, message: dict[str, str]) -> dict[str, str]:
    deps = _pointer_deps(run, store, run.writer)
    return derive.derive({**message, "run_id": RUN_ID}, deps, now=NOW)


def _commit_pointer(
    run: Run, store: FakeStore, writer: CatalogWriter, body: dict[str, str]
) -> list[str]:
    run.claim_ok()  # the tracking item update
    event = {"Records": [{"body": json.dumps(body)}]}
    return handler.run_batch(event, _pointer_deps(run, store, writer), now=NOW)


CASES: Final = {
    "nwb": (NWB_RELPATH, b""),
    "events": (f"{EVENTS_KEY}_events.tsv", EVENTS_TEXT),
    "participants": ("participants.tsv", PARTICIPANTS_TEXT),
}


@pytest.mark.usefixtures("frozen")
@pytest.mark.parametrize("case", sorted(CASES))
def test_the_pointer_path_commits_what_the_in_handler_reextract_commits(
    run: Run, tmp_path: Path, pointer_catalog: SqlCatalog, case: str
) -> None:
    relpath, text = CASES[case]
    message = _message(relpath, size=str(len(text) or 100))
    if case == "nwb":
        run.nwb_path = tmp_path / "f.nwb"
        _nwb(run.nwb_path)
    # old path: the in-handler re-extract, into this run's catalog
    if text:
        run.body(text, f"bytes=0-{len(text) - 1}", relpath)
    assert _reextract(run, message) == [handler.REEXTRACTED]
    old_done = run.updates[-1]["values"]
    old_writes = (list(run.writer.replaced), list(run.writer.calls))
    # new path: derive (no catalog write), then the pointer commit into a fresh catalog
    store = FakeStore()
    other = RecordingWriter(pointer_catalog)
    run.writer = RecordingWriter(run.catalog)
    if text:
        run.body(text, f"bytes=0-{len(text) - 1}", relpath)
    result = _derive(run, store, message)
    assert result["outcome"] == derive.DERIVED
    assert (run.writer.replaced, run.writer.calls) == ([], [])  # derive writes none
    body = {k: v for k, v in result.items() if k != "outcome"}
    assert _commit_pointer(run, store, other, body) == [handler.REEXTRACTED]
    new_done = run.updates[-1]["values"]
    assert (other.replaced, other.calls) == old_writes  # same tables, same order
    assert new_done[":reextract_state"] == old_done[":reextract_state"] == "DONE"
    assert new_done[":tables_replaced"] == old_done[":tables_replaced"]
    old_rows, new_rows = _table_rows(run.catalog), _table_rows(pointer_catalog)
    for name in REPLACED_OR_APPENDED:
        assert new_rows[name] == old_rows[name], name
    assert sum(len(old_rows[name]) for name in REPLACED_OR_APPENDED) > 1
    run.h.finish()


@pytest.mark.usefixtures("frozen")
def test_derive_writes_the_bundle_then_the_manifest_under_rows(
    run: Run, tmp_path: Path
) -> None:
    run.nwb_path = tmp_path / "f.nwb"
    _nwb(run.nwb_path)
    store = FakeStore()
    result = _derive(run, store, _message())
    assert result == {
        "outcome": "DERIVED",
        "asset_id": ASSET,
        "run_id": RUN_ID,
        "manifest_key": f"rows/{ASSET}/{RUN_ID}/manifest.json",
    }
    assert store.order == [
        f"rows/{ASSET}/{RUN_ID}/rows.jsonl.gz",
        f"rows/{ASSET}/{RUN_ID}/manifest.json",
    ]
    manifest = store.manifest()
    tables = [(o["op"], o["table"], o["rows"]) for o in manifest["operations"]]
    assert tables == [
        ("replace", "series", 0),
        ("replace", "electrodes", 2),
        ("replace", "units", 1),
        ("replace", "intervals", 1),
        ("replace", "unit_spikes", 1),
        ("append_if_absent", "subjects", 1),
        ("append_if_absent", "sessions", 1),
        ("replace", "asset_headers", 1),
    ]
    assert manifest["lfp_build"] is True
    assert all(not table for table in _table_rows(run.catalog).values())
    run.h.finish()


def test_a_derive_failure_marks_the_item_failed_and_writes_nothing(run: Run) -> None:
    store = FakeStore()
    run.claim_ok()  # the FAILED write
    message = _message("sub-01/anat/sub-01_T1w.png", file_format="png")
    assert _derive(run, store, message)["outcome"] == "ERROR"
    values = run.updates[-1]["values"]
    assert values[":reextract_state"] == "FAILED"
    assert values[":reextract_error"]["class"] == "NoExtractorError"
    assert values[":tables_replaced"] == []
    assert store.objects == {}
    run.h.finish()


def test_derive_function_deps_come_from_its_environment_and_load_no_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Part 2: SESSIONS_TABLE, RAW_BUCKET, DERIVED_BUCKET; S3 and DynamoDB."""
    asked: list[str] = []

    def client(name: str) -> str:
        asked.append(name)
        return f"<{name}>"

    monkeypatch.setattr(derive.boto3, "client", client)
    monkeypatch.setenv("SESSIONS_TABLE", "sessions")
    monkeypatch.setenv("RAW_BUCKET", "raw")
    monkeypatch.setenv("DERIVED_BUCKET", "derived")
    deps = derive.handler_deps()
    assert sorted(asked) == ["dynamodb", "s3"]  # no Glue: the writer only records
    assert (deps.table.name, deps.bucket, deps.derived_bucket) == (
        "sessions",
        "raw",
        "derived",
    )
    assert deps.s3 == deps.derived == "<s3>"
    assert isinstance(deps.writer, derive.RowCollector)


@pytest.mark.usefixtures("frozen")
def test_the_derive_handler_derives_one_map_item(
    run: Run, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Map item is the tracking id, the asset fields and run_id (reextract.py)."""
    run.nwb_path = tmp_path / "f.nwb"
    _nwb(run.nwb_path)
    store = FakeStore()
    deps = _pointer_deps(run, store, run.writer)
    monkeypatch.setattr(derive, "handler_deps", lambda: deps)
    result = derive.handler({**_message(), "run_id": RUN_ID}, object())
    assert result == {"outcome": "DERIVED", **derive.pointer_body(ASSET, RUN_ID)}
    assert store.order[-1] == f"rows/{ASSET}/{RUN_ID}/manifest.json"
    run.h.finish()


def _derived_nwb(run: Run, tmp_path: Path) -> FakeStore:
    run.nwb_path = tmp_path / "f.nwb"
    _nwb(run.nwb_path)
    store = FakeStore()
    assert _derive(run, store, _message())["outcome"] == "DERIVED"
    return store


def _refused(run: Run, store: FakeStore, body: dict[str, str] | None = None) -> str:
    """Commit by pointer; assert FAILED with nothing written; return the message."""
    body = body or derive.pointer_body(ASSET, RUN_ID)
    assert _commit_pointer(run, store, run.writer, body) == ["ERROR"]
    values = run.updates[-1]["values"]
    assert values[":reextract_state"] == "FAILED"
    assert values[":reextract_error"]["tables_replaced"] == []
    assert values[":tables_replaced"] == []
    assert (run.writer.replaced, run.writer.calls) == ([], [])
    assert all(not table for table in _table_rows(run.catalog).values())
    run.h.finish()
    return str(values[":reextract_error"]["message"])


def test_a_missing_manifest_is_refused(run: Run) -> None:
    message = _refused(run, FakeStore())
    assert message == f"manifest rows/{ASSET}/{RUN_ID}/manifest.json is missing"


def test_a_bundle_hash_mismatch_is_refused(run: Run, tmp_path: Path) -> None:
    store = _derived_nwb(run, tmp_path)
    key = rows.bundle_key(ASSET, RUN_ID)
    store.objects[key] = gzip.compress(gzip.decompress(store.objects[key]) + b" ")
    message = _refused(run, store)
    assert message == f"row bundle {key} sha256 differs from the manifest"


def test_a_table_hash_mismatch_is_refused(run: Run, tmp_path: Path) -> None:
    store = _derived_nwb(run, tmp_path)
    key = rows.bundle_key(ASSET, RUN_ID)
    lines = gzip.decompress(store.objects[key]).split(b"\n")
    assert b'"spike_count":2' in lines[2]
    lines[2] = lines[2].replace(b'"spike_count":2', b'"spike_count":3')
    store.objects[key] = gzip.compress(b"\n".join(lines))
    manifest = store.manifest()  # a consistent bundle hash, a stale table hash
    manifest["bundle"]["sha256"] = rows.sha256(store.objects[key])
    store.set_manifest(manifest)
    assert _refused(run, store) == "rows of units sha256 differs from the manifest"


def test_a_dropped_operation_is_refused(run: Run, tmp_path: Path) -> None:
    store = _derived_nwb(run, tmp_path)
    manifest = store.manifest()
    del manifest["operations"][2]
    store.set_manifest(manifest)
    assert _refused(run, store) == "row bundle holds 8 operations, manifest lists 7"


def test_a_manifest_for_another_asset_is_refused(run: Run, tmp_path: Path) -> None:
    store = _derived_nwb(run, tmp_path)
    manifest = store.manifest()
    manifest["asset_id"] = "33333333-3333-4333-8333-333333333333"
    store.set_manifest(manifest)
    message = _refused(run, store)
    assert message.startswith("manifest is for asset 33333333-")


def test_a_manifest_for_another_run_is_refused(run: Run, tmp_path: Path) -> None:
    store = _derived_nwb(run, tmp_path)
    manifest = store.manifest()
    manifest["run_id"] = "run0"
    store.set_manifest(manifest)
    message = _refused(run, store)
    assert message == (
        f"manifest is for asset {ASSET} run run0, not asset {ASSET} run {RUN_ID}"
    )


def test_a_pointer_to_another_key_is_refused(run: Run, tmp_path: Path) -> None:
    store = _derived_nwb(run, tmp_path)
    body = {**derive.pointer_body(ASSET, RUN_ID), "manifest_key": "lfp/x/manifest.json"}
    message = _refused(run, store, body)
    assert message == "manifest key lfp/x/manifest.json is not this asset and run's"


@pytest.mark.parametrize(
    ("op", "table"),
    [("replace", "assets"), ("replace", "subjects"), ("append_if_absent", "units")],
)
def test_a_table_outside_the_replaced_tables_is_refused(
    run: Run, tmp_path: Path, op: str, table: str
) -> None:
    store = _derived_nwb(run, tmp_path)
    manifest = store.manifest()
    manifest["operations"][1] |= {"op": op, "table": table}
    store.set_manifest(manifest)
    assert _refused(run, store) == (
        f"manifest names table {table} ({op}), which a re-extract never writes"
    )


def test_a_replace_of_another_assets_rows_is_refused(run: Run, tmp_path: Path) -> None:
    store = _derived_nwb(run, tmp_path)
    manifest = store.manifest()
    manifest["operations"][1]["key"] = {"asset_id": "other"}
    store.set_manifest(manifest)
    message = _refused(run, store)
    assert message == (
        "manifest replaces electrodes rows of key {'asset_id': 'other'}, not this asset"
    )


def test_a_manifest_not_ending_with_asset_headers_is_refused(
    run: Run, tmp_path: Path
) -> None:
    store = _derived_nwb(run, tmp_path)
    manifest = store.manifest()
    ops = manifest["operations"]
    ops[-1], ops[-2] = ops[-2], ops[-1]
    store.set_manifest(manifest)
    assert _refused(run, store) == "manifest does not end with asset_headers"


def test_a_pointer_with_no_derived_store_is_refused(run: Run, tmp_path: Path) -> None:
    store = _derived_nwb(run, tmp_path)
    run.claim_ok()
    deps = replace(run.deps(), derived=store)  # no bucket configured
    body = json.dumps(derive.pointer_body(ASSET, RUN_ID))
    assert handler.run_batch({"Records": [{"body": body}]}, deps, now=NOW) == ["ERROR"]
    error = run.updates[-1]["values"][":reextract_error"]
    assert error["message"] == (
        "no derived store is configured for a rows-by-pointer message"
    )
    run.h.finish()


def test_a_pointer_commit_failure_names_the_tables_already_replaced(
    run: Run, tmp_path: Path
) -> None:
    store = _derived_nwb(run, tmp_path)
    run.writer.fail_on = "intervals"
    body = derive.pointer_body(ASSET, RUN_ID)
    assert _commit_pointer(run, store, run.writer, body) == ["ERROR"]
    error = run.updates[-1]["values"][":reextract_error"]
    assert error["message"] == "boom on intervals"
    assert error["tables_replaced"] == ["series", "electrodes", "units"]
    assert run.count("asset_headers") == 0  # asset_headers is last: not reached
    run.h.finish()


def test_an_nwb_pointer_commit_sends_exactly_one_lfp_build_message(
    run: Run, tmp_path: Path
) -> None:
    store = _derived_nwb(run, tmp_path)
    run.claim_ok()
    sent: list[dict[str, str]] = []
    deps = replace(_pointer_deps(run, store, run.writer), request_lfp_build=sent.append)
    body = json.dumps(derive.pointer_body(ASSET, RUN_ID))
    outcome = handler.run_batch({"Records": [{"body": body}]}, deps, now=NOW)
    assert outcome == [handler.REEXTRACTED]
    assert sent == [_build_message()]
    run.h.finish()


def test_rows_round_trip_their_value_types_and_refuse_others() -> None:
    batch = [
        {
            "a": None,
            "b": True,
            "c": 3,
            "d": 0.1 + 0.2,
            "e": "x",
            "f": FROZEN,
            "g": [1.0, -0.0, 5e-324, float("nan")],
            "h": [1, 2],
        }
    ]
    [back] = rows.decode_rows(rows.encode_rows(batch))
    assert back.keys() == batch[0].keys()
    assert back["d"] == 0.30000000000000004
    assert back["f"] == FROZEN
    assert isinstance(back["c"], int)
    assert isinstance(back["g"][0], float)
    assert math.copysign(1.0, back["g"][1]) == -1.0
    assert back["g"][2] == 5e-324
    assert math.isnan(back["g"][3])
    with pytest.raises(rows.RowsError, match="type dict"):
        rows.encode_rows([{"x": {"y": 1}}])
    with pytest.raises(rows.RowsError, match="not a tagged datetime"):
        rows.decode_rows(b'[{"x":{"y":1}}]')


def test_a_bundle_over_the_bound_fails_the_derive_and_is_never_written(
    run: Run, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rows, "MAX_BUNDLE_BYTES", 64)
    run.nwb_path = tmp_path / "f.nwb"
    _nwb(run.nwb_path)
    store = FakeStore()
    run.claim_ok()  # the FAILED write
    assert _derive(run, store, _message())["outcome"] == "ERROR"
    error = run.updates[-1]["values"][":reextract_error"]
    assert error["class"] == "RowsError"
    assert error["message"].endswith("bytes, over 64")
    assert store.objects == {}
    run.h.finish()


CS62_UNITS: Final = 44
CS62_SPIKES: Final = 76_279
CS62_ELECTRODES: Final = 96
CS62_TRIALS: Final = 41
# The CS62-shaped bundle below (2026-10-26, seed 62, times uniform on 0..8192 s):
# measured 677,839 B gzip (1,447,585 B raw); MAX_BUNDLE_BYTES (64 MiB) is 99 times that.
CS62_BUNDLE_CEILING: Final = 2 * 1024 * 1024


def _cs62_shaped(path: Path) -> None:
    """Write a file shaped like CS62 R2: 96 electrodes, 44 units."""
    nwb = new_nwbfile(
        session_description="cs62-shaped",
        identifier="cs62",
        session_start_time=datetime(2019, 4, 1, tzinfo=UTC),
    )
    device = nwb.create_device(name="probe")
    group = nwb.create_electrode_group(
        name="grp", description="g", location="brain", device=device
    )
    for index in range(CS62_ELECTRODES):
        nwb.add_electrode(
            x=0.1 * index,
            y=0.2,
            z=0.3,
            imp=0.0,
            location=f"L{index}",
            filtering="f",
            group=group,
        )
    rng = np.random.default_rng(62)
    counts = rng.multinomial(CS62_SPIKES, [1 / CS62_UNITS] * CS62_UNITS)
    for unit, count in enumerate(counts):
        times = np.sort(rng.uniform(0.0, 8192.0, int(count)))
        nwb.add_unit(spike_times=times, electrodes=[unit % CS62_ELECTRODES])
    for trial in range(CS62_TRIALS):
        nwb.add_trial(start_time=10.0 * trial, stop_time=10.0 * trial + 5.0)
    with NWBHDF5IO(str(path), "w") as io_:
        io_.write(nwb)


def test_a_cs62_shaped_bundle_is_far_under_the_bound(
    run: Run, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run.nwb_path = tmp_path / "cs62.nwb"
    _cs62_shaped(run.nwb_path)
    store = FakeStore()
    assert _derive(run, store, _message())["outcome"] == "DERIVED"
    manifest = store.manifest()
    spikes = next(o for o in manifest["operations"] if o["table"] == "unit_spikes")
    assert spikes["rows"] == CS62_UNITS
    size = manifest["bundle"]["bytes"]
    raw = len(gzip.decompress(store.objects[rows.bundle_key(ASSET, RUN_ID)]))
    with capsys.disabled():
        sys.stdout.write(f"\nCS62-shaped bundle: {size} B gzip, {raw} B raw\n")
    assert size <= CS62_BUNDLE_CEILING < rows.MAX_BUNDLE_BYTES
    run.h.finish()


# -- dataset summary trigger --------------------------------------

SUMMARY_MESSAGE: Final = {"kind": "summary", "dataset": "ds1"}


def test_an_extraction_sends_exactly_one_summary_message(
    run: Run, tmp_path: Path
) -> None:
    run.nwb_path = tmp_path / "f.nwb"
    _nwb(run.nwb_path)
    run.session(NWB_RELPATH, dataset="ds1")
    run.claim_ok()
    run.claim_ok()
    sent: list[dict[str, str]] = []
    deps = replace(run.deps(), request_summary=sent.append)
    assert handler.extract({"session_id": SID}, deps, now=NOW) == handler.EXTRACTED
    assert sent == [SUMMARY_MESSAGE]
    run.h.finish()


def test_a_skipped_file_sends_exactly_one_summary_message(run: Run) -> None:
    run.session("sub-01/beh/sub-01_events.csv")
    run.claim_ok()
    run.claim_ok()
    sent: list[dict[str, str]] = []
    deps = replace(run.deps(), request_summary=sent.append)
    assert handler.extract({"session_id": SID}, deps, now=NOW) == handler.SKIPPED
    assert sent == [SUMMARY_MESSAGE]
    run.h.finish()


def test_a_session_not_claimed_or_failed_sends_no_summary_message(run: Run) -> None:
    sent: list[dict[str, str]] = []
    deps = replace(run.deps(), request_summary=sent.append)
    run.h.get_item(state=sessions.FINALIZING)
    assert handler.extract({"session_id": SID}, deps, now=NOW) == handler.NOT_CLAIMED
    relpath = "sub-01/func/sub-01_task-x_bold.json"
    text = b"{not json"
    run.session(relpath, size=len(text))
    run.claim_ok()
    run.body(text, f"bytes=0-{len(text) - 1}", relpath)
    run.claim_ok()  # FAILED
    with pytest.raises(bids.BidsError):
        handler.extract({"session_id": SID}, deps, now=NOW)
    assert sent == []
    run.h.finish()


def test_a_reextraction_sends_exactly_one_summary_message(
    run: Run, tmp_path: Path
) -> None:
    run.nwb_path = tmp_path / "f.nwb"
    _nwb(run.nwb_path)
    run.claim_ok()  # the tracking item update
    sent: list[dict[str, str]] = []
    deps = replace(run.deps(), request_summary=sent.append)
    event = {"Records": [{"body": json.dumps(_message())}]}
    assert handler.run_batch(event, deps, now=NOW) == [handler.REEXTRACTED]
    assert sent == [SUMMARY_MESSAGE]
    run.h.finish()


@pytest.mark.usefixtures("frozen")
def test_a_rows_by_pointer_commit_sends_exactly_one_summary_message(
    run: Run, tmp_path: Path, pointer_catalog: SqlCatalog
) -> None:
    run.nwb_path = tmp_path / "f.nwb"
    _nwb(run.nwb_path)
    store = FakeStore()
    result = _derive(run, store, _message())
    body = {k: v for k, v in result.items() if k != "outcome"}
    sent: list[dict[str, str]] = []
    deps = replace(
        _pointer_deps(run, store, RecordingWriter(pointer_catalog)),
        request_summary=sent.append,
    )
    run.claim_ok()  # the tracking item update
    event = {"Records": [{"body": json.dumps(body)}]}
    assert handler.run_batch(event, deps, now=NOW) == [handler.REEXTRACTED]
    assert sent == [SUMMARY_MESSAGE]
    run.h.finish()


def test_a_failed_summary_send_does_not_change_the_extraction(
    run: Run, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    run.nwb_path = tmp_path / "f.nwb"
    _nwb(run.nwb_path)
    run.session(NWB_RELPATH)
    run.claim_ok()
    run.claim_ok()  # EXTRACTED, with no FAILED write
    calls: list[dict[str, str]] = []

    def broken(message: dict[str, str]) -> None:
        calls.append(message)
        msg = "queue unavailable"
        raise RuntimeError(msg)

    deps = replace(run.deps(), request_summary=broken)
    assert handler.extract({"session_id": SID}, deps, now=NOW) == handler.EXTRACTED
    assert calls == [SUMMARY_MESSAGE]
    assert run.final_state() == "EXTRACTED"
    assert "could not queue the summary of ds1" in caplog.text
    run.h.finish()
