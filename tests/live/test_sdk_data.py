"""Live tests: browse, sql(), open_nwb, .nii.gz, upload_dataset.

Run with `make live T=tests/live/test_sdk_data.py`. Needs both datasets imported.
Every wait is bounded; a missed performance bound is reported as OVER-BUDGET by
the assertion text, with the measured numbers printed first.
"""

import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import pytest

from asteroidea import _browse, _image, _nwb, _query, _sample, _sql, _upload
from asteroidea.errors import QueryError

if TYPE_CHECKING:
    from asteroidea._auth import Auth

MIB: Final = 1024 * 1024
SQL_WITHIN: Final = 10.0
NWB_WITHIN: Final = 60.0
NWB_BYTES: Final = 64 * MIB
GZ_WITHIN: Final = 20.0
GZ_BYTES: Final = 32 * MIB
EXTRACT_WITHIN: Final = 300.0
DANDI: Final = "dandi-000623"
OPENNEURO: Final = "openneuro-ds004798"
CS62_R2: Final = "1d50b507-a8a1-58d5-83a6-199f232a1582"
GZ_KEY: Final = (
    "raw/openneuro-ds004798/sub-p62cs/ses-001/func/"
    "sub-p62cs_ses-001_task-movie_run-1_bold.nii.gz"
)


def report(line: str) -> None:
    """Write a measured number to the run output and to `measurements.txt`."""
    sys.stdout.write(line + "\n")
    scratch = os.environ.get("ASTEROIDEA_LIVE_SCRATCH")
    if scratch is not None and scratch != "":
        with (Path(scratch) / "measurements.txt").open("a") as handle:
            handle.write(line + "\n")


def rows(table: Any) -> list[dict[str, Any]]:
    return list(table.to_pylist())


def count(auth: Auth, table: str) -> int:
    return int(
        _sql.sql(auth, "SELECT count(*) AS n FROM query_table(?)", [table]).to_pylist()[
            0
        ]["n"]
    )


def test_sql_phases_first_second_and_after_a_commit(auth: Auth) -> None:
    """sql() phases: a cold call, a repeat on the same tables, a call after a commit."""
    _sql.reset_cache()
    timings: list[float] = []
    counts: list[int] = []
    for _ in range(2):
        started = time.monotonic()
        counts.append(count(auth, "assets"))
        timings.append(time.monotonic() - started)
    dataset, sample = _sample.write_sample("phase")
    _upload.Uploader(auth).upload(sample, dataset, sandbox=True)
    started = time.monotonic()
    counts.append(count(auth, "assets"))
    timings.append(time.monotonic() - started)
    report(
        f"sql() phases: first {timings[0]:.2f} s, second {timings[1]:.2f} s, "
        f"after a commit {timings[2]:.2f} s; assets {counts}"
    )
    assert counts[0] == counts[1]
    assert counts[2] == counts[1] + 1
    assert max(timings) <= SQL_WITHIN, f"OVER-BUDGET: {timings}"


def test_browse_lists_both_datasets_with_titles_and_licences(auth: Auth) -> None:
    found = {r["dataset"]: r for r in rows(_browse.datasets(auth))}
    assert found[DANDI]["license"] == "CC-BY-4.0"
    assert found[OPENNEURO]["license"] == "CC0"
    assert found[DANDI]["title"]
    assert found[OPENNEURO]["title"]


def test_browse_subjects_sessions_and_assets(auth: Auth) -> None:
    subjects = {(r["dataset"], r["subject"]) for r in rows(_browse.subjects(auth))}
    assert {(DANDI, "sub-CS41"), (DANDI, "sub-CS62")} <= subjects
    only = rows(_browse.subjects(auth, dataset=DANDI))
    assert {r["dataset"] for r in only} == {DANDI}
    assert all(r["subject_key"] for r in only)
    sessions = {(r["dataset"], r["subject"]) for r in rows(_browse.sessions(auth))}
    assert {(DANDI, "sub-CS41"), (DANDI, "sub-CS62")} <= sessions
    assert _browse.sessions(auth, dataset=DANDI, subject="sub-CS62").num_rows == 2
    assert _browse.assets(auth, dataset=OPENNEURO).num_rows == 57
    assert _browse.assets(auth, dataset=DANDI, file_format="nwb").num_rows == 4


@pytest.mark.parametrize("name", _sql.TABLE_NAMES)
def test_sql_count_equals_pyiceberg_count_on_every_table(auth: Auth, name: str) -> None:
    started = time.monotonic()
    duck = count(auth, name)
    took = time.monotonic() - started
    catalog = _query.open_catalog(auth)
    expected = catalog.load_table(f"asteroidea.{name}").scan().to_arrow().num_rows
    report(f"sql() {name}: duckdb {duck} == pyiceberg {expected} in {took:.2f} s")
    assert duck == expected
    assert took <= SQL_WITHIN, f"OVER-BUDGET: {took:.2f} s > {SQL_WITHIN} s"


def test_amygdala_units_by_subject(auth: Auth) -> None:
    result = rows(
        _sql.sql(
            auth,
            "SELECT subject, count(*) AS n FROM units "
            "WHERE location ILIKE '%amygdala%' GROUP BY subject ORDER BY subject",
        )
    )
    report(f"amygdala units by subject: {result}")
    assert len(result) == 2
    assert sum(r["n"] for r in result) == 72


def test_a_write_through_sql_is_refused(auth: Auth) -> None:
    before = count(auth, "datasets")
    with pytest.raises(QueryError):
        _sql.sql(auth, "DELETE FROM datasets")
    with pytest.raises(QueryError):
        _sql.sql(auth, "INSERT INTO datasets SELECT * FROM datasets")
    target = (
        f"s3://{auth.config.raw_bucket}/raw/_sdk_write_probe/{uuid.uuid4()}.parquet"
    )
    with pytest.raises(QueryError):
        _sql.sql(auth, f"COPY (SELECT 1 AS x) TO '{target}' (FORMAT parquet)")
    assert count(auth, "datasets") == before


def test_subject_links_join_cs62_to_p62cs(auth: Auth) -> None:
    result = rows(
        _sql.sql(
            auth,
            "SELECT a.subject AS a, b.subject AS b FROM subject_links a "
            "JOIN subject_links b ON a.subject_key = b.subject_key "
            "AND a.dataset = 'dandi-000623' AND b.dataset = 'openneuro-ds004798' "
            "WHERE a.subject = 'sub-CS62'",
        )
    )
    assert result == [{"a": "sub-CS62", "b": "sub-p62cs"}]


def test_nwb_open_units_and_lfp_slice(auth: Auth) -> None:
    started = time.monotonic()
    nwb: Any = _nwb.open_nwb(auth, CS62_R2)
    units = nwb.units.to_dataframe()
    series = nwb.processing["ecephys"]["LFP_macro"]["ElectricalSeries"]
    block = series.data[0:1000, 0:8]
    took = time.monotonic() - started
    fetched = nwb.remote_file.bytes_fetched
    report(
        f"NWB open: {took:.1f} s, {fetched / MIB:.1f} MiB in "
        f"{nwb.block_file.requests} requests; units {len(units)}, slice {block.shape}"
    )
    assert block.shape == (1000, 8)
    assert len(units) > 0
    assert took <= NWB_WITHIN, f"OVER-BUDGET: {took:.1f} s > {NWB_WITHIN} s"
    assert fetched <= NWB_BYTES, f"OVER-BUDGET: {fetched / MIB:.1f} MiB > 64 MiB"


def test_gz_volume_zero_of_the_bold_run(auth: Auth) -> None:
    found = rows(
        _sql.sql(
            auth,
            "SELECT asset_id, size_bytes FROM assets WHERE s3_key = ?",
            [GZ_KEY],
        )
    )
    assert [r["size_bytes"] for r in found] == [406002058]
    started = time.monotonic()
    image: Any = _image.open_image(auth, found[0]["asset_id"])
    volume = image.dataobj[..., 0]
    took = time.monotonic() - started
    fetched = image.extra["remote_file"].bytes_fetched
    report(f"gz volume: {took:.1f} s, {fetched / MIB:.1f} MiB; volume {volume.shape}")
    assert volume.ndim == 3
    assert took <= GZ_WITHIN, f"OVER-BUDGET: {took:.1f} s > {GZ_WITHIN} s"
    assert fetched <= GZ_BYTES, f"OVER-BUDGET: {fetched / MIB:.1f} MiB > 32 MiB"


def test_upload_dataset_commits_then_extracts(auth: Auth, tmp_path: Path) -> None:
    dataset = f"sdk-live-{uuid.uuid4().hex[:8]}"
    _, sample = _sample.write_sample()
    root = tmp_path / dataset
    nifti = root / "sub-01" / "anat" / "sub-01_T1w.nii"
    nifti.parent.mkdir(parents=True)
    nifti.write_bytes(sample.read_bytes())
    (nifti.parent / "sub-01_T1w.json").write_text(json.dumps({"RepetitionTime": 2.0}))
    description = {"Name": "SDK live synthetic", "License": "CC0", "BIDSVersion": "1.9"}
    (root / "dataset_description.json").write_text(json.dumps(description))
    started = time.monotonic()
    uploader = _upload.Uploader(auth)
    result = _upload.upload_dataset(uploader, root, dataset, sandbox=True)
    report(f"upload_dataset: 3 files committed in {time.monotonic() - started:.1f} s")
    assert result.failed == []
    assert sorted(r.path for r in result.committed) == [
        "dataset_description.json",
        "sub-01/anat/sub-01_T1w.json",
        "sub-01/anat/sub-01_T1w.nii",
    ]
    nifti_result = next(r for r in result.committed if r.path.endswith(".nii"))
    state = uploader.state(nifti_result.session_id)
    assert state["state"] == "COMMITTED"
    assert "extract_error" not in state
    waited = time.monotonic()
    header: list[dict[str, Any]] = []
    while not header and time.monotonic() - waited < EXTRACT_WITHIN:
        header = rows(
            _sql.sql(
                auth,
                "SELECT format, nifti_shape FROM asset_headers WHERE asset_id = ?",
                [nifti_result.asset_id],
            )
        )
        if not header:
            time.sleep(5)
    report(f"extraction row after {time.monotonic() - waited:.0f} s")
    assert header
    assert header[0]["format"] == "nifti"
    assert list(header[0]["nifti_shape"]) == list(_sample.SHAPE)
    meta = [r for r in rows(_browse.datasets(auth)) if r["dataset"] == dataset]
    assert [(r["title"], r["license"]) for r in meta] == [("SDK live synthetic", "CC0")]
