"""Commit provenance columns, `datasets`, checksum algorithm.

Every test runs against PyIceberg's local SQL catalog (`conftest.catalog`).
"""

import base64
import hashlib
from typing import TYPE_CHECKING, Any

import pyarrow as pa
import pytest
from pyiceberg.catalog.sql import SqlCatalog
from pyiceberg.exceptions import CommitFailedException
from pyiceberg.table import Table

from asteroidea_infra import schema
from asteroidea_infra.catalog_init import iceberg_schema, run
from asteroidea_ingest import commit, sessions
from asteroidea_ingest.catalog_writer import (
    MAX_FAILED_RETRIES,
    CatalogWriter,
    UnknownColumnError,
)

if TYPE_CHECKING:
    from pathlib import Path

SHA_HEX = hashlib.sha256(b"payload").hexdigest()
SHA_B64 = base64.b64encode(hashlib.sha256(b"payload").digest()).decode()
UPLOAD_ID = "6f1c2b8e-3d4a-4c5b-9e6f-7a8b9c0d1e2f"
META = {"title": "Movie watching", "license": "CC-BY-4.0", "description": "d"}


def _sid(n: int) -> str:
    return f"11111111-1111-4111-8111-{n:012d}"


def _commit_one(h: Any, n: int, **overrides: Any) -> str:
    """Stub one successful delivery for session `n` and run it."""
    sid = _sid(n)
    relpath = f"sub-{n:02d}/anat/sub-{n:02d}_T1w.nii"
    h.get_item(
        session_id=sid,
        relpath=relpath,
        raw_key=f"raw/ds1/{relpath}",
        landing_key=f"landing/{sid}/{relpath}",
        **overrides,
    )
    h.head()
    h.update_ok()  # claim ACCEPTED
    h.copy(version=f"rv{n}", key=f"raw/ds1/{relpath}")
    h.update_ok()  # COMMITTED
    h.extract_sent(sid)
    return str(h.run(sid=sid))


def _committed_values(h: Any) -> list[dict[str, Any]]:
    """Capture the ExpressionAttributeValues of every UpdateItem call."""
    captured: list[dict[str, Any]] = []
    h.ddb.meta.events.register(
        "before-parameter-build.dynamodb.UpdateItem",
        lambda params, **_: captured.append(
            sessions.unmarshal(params["ExpressionAttributeValues"])
        ),
    )
    return captured


# -- 10 in a row, replay ---------------------------------------------------------


def test_ten_commits_in_a_row_give_ten_rows_and_one_dataset(harness: Any) -> None:
    h = harness
    outcomes = [_commit_one(h, n, dataset_meta=META) for n in range(10)]
    h.finish()
    assert outcomes == [sessions.COMMITTED] * 10
    rows = h.rows()
    assert len(rows) == 10
    assert len({r["session_id"] for r in rows}) == 10
    (dataset,) = h.rows(schema.DATASETS)
    assert dataset["dataset"] == "ds1"
    assert dataset["title"] == META["title"]


def test_a_replayed_delivery_gives_no_row(harness: Any) -> None:
    h = harness
    assert _commit_one(h, 1) == sessions.COMMITTED
    # The same message delivered again: the item is COMMITTED, so the commit stops
    # at the read; no head, copy or update is stubbed, so any of them would raise.
    h.get_item(session_id=_sid(1), state=sessions.COMMITTED)
    assert h.run(sid=_sid(1)) == commit.SKIPPED
    h.finish()
    assert len(h.rows()) == 1
    assert len(h.rows(schema.DATASETS)) == 1


def test_replayed_rows_are_not_appended_again_by_the_writer(harness: Any) -> None:
    h = harness
    assert _commit_one(h, 1) == sessions.COMMITTED
    (row,) = h.rows()
    (dataset,) = h.rows(schema.DATASETS)
    writer = CatalogWriter(h.catalog)
    writer.append_if_absent(schema.TABLE, {"session_id": row["session_id"]}, [row])
    writer.append_if_absent(schema.DATASETS, {"dataset": "ds1"}, [dataset])
    assert len(h.rows()) == 1
    assert len(h.rows(schema.DATASETS)) == 1


# -- provenance ------------------------------------------------------------------


def test_upload_row_carries_the_provenance_columns(harness: Any) -> None:
    h = harness
    assert (
        _commit_one(
            h, 3, upload_id=UPLOAD_ID, dataset_meta=META, ingest_method="upload"
        )
        == sessions.COMMITTED
    )
    h.finish()
    (row,) = h.rows()
    assert set(row) == {c.name for c in schema.ASSETS.columns}
    assert row["upload_id"] == UPLOAD_ID
    assert row["ingest_method"] == "upload"
    assert row["format"] == "nifti"
    assert row["license"] == "CC-BY-4.0"
    assert row["ingested_by"] == "user-1"
    assert row["checksum_sha256"] is None
    assert row["source_archive"] is None


def test_a_legacy_item_commits_as_an_upload_with_null_source(harness: Any) -> None:
    h = harness
    assert _commit_one(h, 4) == sessions.COMMITTED  # no upload_id, meta, method
    (row,) = h.rows()
    assert row["ingest_method"] == "upload"
    assert row["upload_id"] is None
    assert row["license"] is None
    (dataset,) = h.rows(schema.DATASETS)
    assert dataset["ingest_method"] == "upload"
    assert dataset["created_by"] == "user-1"
    assert dataset["title"] is None
    assert dataset["tier"] is None  # null reads as upload here, curated for import


def test_dataset_row_writes_the_session_items_tier(harness: Any) -> None:
    """`dataset_meta.tier` lands in `datasets.tier`."""
    h = harness
    meta = {**META, "tier": "sandbox"}
    assert _commit_one(h, 5, dataset_meta=meta) == sessions.COMMITTED
    (dataset,) = h.rows(schema.DATASETS)
    assert set(dataset) == {c.name for c in schema.DATASETS_SPEC.columns}
    assert dataset["tier"] == "sandbox"


def test_first_commit_of_a_dataset_writes_its_row_later_ones_do_not(
    harness: Any,
) -> None:
    h = harness
    _commit_one(h, 1, dataset_meta=META)
    _commit_one(h, 2, dataset_meta={**META, "title": "a later title"})
    (dataset,) = h.rows(schema.DATASETS)
    assert dataset["title"] == META["title"]


def test_import_row_records_source_and_sha256(harness: Any) -> None:
    h = harness
    source = {
        "archive": "dandi",
        "dataset": "000623",
        "version": "0.240227.2023",
        "path": "sub-CS62/sub-CS62_ses-R2_ecephys+image.nwb",
        "url": "https://dandiarchive.s3.amazonaws.com/blobs/c68/ff6/x",
        "digest": f"sha2-256:{SHA_HEX}",
    }
    h.get_item(
        expected_checksum={"algorithm": "SHA256", "value": SHA_B64},
        client_crc64nvme=None,
        ingest_method="import",
        owner_sub="import:curator",
        source=source,
        dataset_meta={**META, "doi": "10.48324/dandi.000623", "url": "https://x"},
    )
    h.s3_stub.add_response(
        "head_object",
        {"ContentLength": 100, "ChecksumSHA256": SHA_B64, "VersionId": "lv1"},
    )
    h.update_ok()
    h.copy(crc="Q1JDQ1JDQ1I=")
    h.update_ok()
    h.extract_sent()
    assert h.run() == sessions.COMMITTED
    h.finish()
    (row,) = h.rows()
    assert row["checksum_sha256"] == SHA_HEX
    assert row["checksum_crc64nvme"] == "Q1JDQ1JDQ1I="
    assert (row["source_archive"], row["source_dataset"]) == ("dandi", "000623")
    assert row["source_digest"] == f"sha2-256:{SHA_HEX}"
    assert row["ingested_by"] == "import:curator"
    (dataset,) = h.rows(schema.DATASETS)
    assert dataset["doi"] == "10.48324/dandi.000623"
    assert dataset["source_version"] == "0.240227.2023"
    assert dataset["ingest_method"] == "import"


# -- checksum algorithm ----------------------------------------------------------


def test_sha256_mismatch_in_landing_is_incomplete(harness: Any) -> None:
    h = harness
    h.get_item(expected_checksum={"algorithm": "SHA256", "value": SHA_B64})
    other = base64.b64encode(hashlib.sha256(b"other").digest()).decode()
    h.s3_stub.add_response(
        "head_object",
        {"ContentLength": 100, "ChecksumSHA256": other, "VersionId": "lv1"},
    )
    h.update_ok()
    assert h.run() == sessions.INCOMPLETE
    h.finish()  # no copy stubbed
    assert h.rows() == []


def test_unsupported_checksum_algorithm_is_incomplete_naming_it(harness: Any) -> None:
    h = harness
    values = _committed_values(h)
    h.get_item(expected_checksum={"algorithm": "MD5", "value": "x"})
    h.head()
    h.update_ok()
    assert h.run() == sessions.INCOMPLETE
    h.finish()
    assert values[-1][":error"] == {"reason": "unsupported checksum algorithm 'MD5'"}
    assert h.rows() == []


# -- session item ----------------------------------------------------------------


def test_committed_sets_extract_state_pending_in_the_same_write(harness: Any) -> None:
    h = harness
    values = _committed_values(h)
    assert _commit_one(h, 1) == sessions.COMMITTED
    claim, committed = values
    assert ":extract_state" not in claim
    assert committed[":state"] == sessions.COMMITTED
    assert committed[":extract_state"] == "PENDING"


# -- writer ----------------------------------------------------------------------


def _asset(sid: str) -> dict[str, Any]:
    return {
        "asset_id": f"a-{sid}",
        "session_id": sid,
        "dataset": "ds1",
        "s3_key": "k",
        "s3_version_id": "v",
        "size_bytes": 1,
        "checksum_crc64nvme": "c",
        "extension": ".nii",
        "committed_at": commit.utc_now_naive(),
    }


def test_commit_failed_is_retried_after_reload_and_recheck(
    harness: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = harness
    original = Table.append
    calls: list[int] = []

    def fail_twice(self: Table, df: Any) -> None:
        calls.append(1)
        if len(calls) <= 2:  # another writer moved the table: nothing landed
            raise CommitFailedException
        original(self, df)

    monkeypatch.setattr(Table, "append", fail_twice)
    CatalogWriter(h.catalog).append_if_absent(
        schema.TABLE, {"session_id": "s-1"}, [_asset("s-1")]
    )
    assert len(calls) == 3
    assert len(h.rows()) == 1


def test_commit_failed_after_the_other_writer_landed_is_not_appended(
    harness: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = harness
    original = Table.append
    calls: list[int] = []

    def other_writer_wins(self: Table, df: Any) -> None:
        calls.append(1)
        original(self, df)  # the same row lands through another writer
        raise CommitFailedException

    monkeypatch.setattr(Table, "append", other_writer_wins)
    CatalogWriter(h.catalog).append_if_absent(
        schema.TABLE, {"session_id": "s-1"}, [_asset("s-1")]
    )
    assert len(calls) == 1  # the re-check found the row
    assert len(h.rows()) == 1


def test_commit_failed_every_time_raises_after_the_bound(
    harness: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = harness
    calls: list[int] = []

    def always_fail(_self: Table, _df: Any) -> None:
        calls.append(1)
        raise CommitFailedException

    monkeypatch.setattr(Table, "append", always_fail)
    with pytest.raises(CommitFailedException):
        CatalogWriter(h.catalog).append_if_absent(
            schema.TABLE, {"session_id": "s-1"}, [_asset("s-1")]
        )
    assert len(calls) == MAX_FAILED_RETRIES + 1
    assert h.rows() == []


def test_multi_column_key_and_multi_row_append(harness: Any) -> None:
    h = harness
    h.catalog.create_table(
        "asteroidea.subjects", iceberg_schema(schema.SPECS["subjects"])
    )
    now = commit.utc_now_naive()
    rows = [
        {
            "dataset": "ds1",
            "subject": f"sub-{n}",
            "source": "nwb",
            "source_asset_id": "a",
            "extracted_at": now,
        }
        for n in (1, 2)
    ]
    writer = CatalogWriter(h.catalog)
    writer.append_if_absent("subjects", {"dataset": "ds1", "subject": "sub-1"}, rows)
    writer.append_if_absent("subjects", {"dataset": "ds1", "subject": "sub-1"}, rows)
    writer.append_if_absent("subjects", {"dataset": "ds2", "subject": "sub-1"}, rows)
    assert len(h.rows("subjects")) == 4


def test_a_row_naming_an_unknown_column_is_refused(harness: Any) -> None:
    h = harness
    with pytest.raises(UnknownColumnError, match="not_a_column"):
        CatalogWriter(h.catalog).append_if_absent(
            schema.TABLE,
            {"session_id": "s-1"},
            [{**_asset("s-1"), "not_a_column": 1}],
        )
    assert h.rows() == []


# -- Legacy-shaped table -------------------------------------------------------------


def test_a_legacy_shaped_row_reads_null_in_the_new_columns(
    tmp_path: Path, harness: Any
) -> None:
    h = harness
    cat = SqlCatalog(
        "legacy",
        uri=f"sqlite:///{tmp_path}/legacy.db",
        warehouse=f"file://{tmp_path}/legacy-warehouse",
    )
    try:
        cat.create_namespace("asteroidea")
        legacy_spec = schema.TableSpec(
            schema.TABLE, schema.BASE_ASSETS_COLUMNS, ("session_id",)
        )
        legacy = cat.create_table("asteroidea.assets", iceberg_schema(legacy_spec))
        legacy.append(
            pa.Table.from_pylist(
                [_asset("legacy-session")], schema=legacy.schema().as_arrow()
            )
        )
        run(cat, f"file://{tmp_path}/legacy-warehouse", dry_run=False)  # catalog-init
        # A commit after the evolve writes the new columns next to the legacy row.
        h.catalog = cat
        assert _commit_one(h, 7, upload_id=UPLOAD_ID) == sessions.COMMITTED
        rows = {r["session_id"]: r for r in h.rows()}
    finally:
        cat.engine.dispose()
    old, new = rows["legacy-session"], rows[_sid(7)]
    for column in schema.PROVENANCE_COLUMNS:
        assert old[column.name] is None, column.name
    assert new["upload_id"] == UPLOAD_ID
    assert new["format"] == "nifti"


def test_a_files_own_licence_wins_over_the_datasets(harness: Any) -> None:
    h = harness
    assert (
        _commit_one(
            h,
            8,
            dataset_meta=META,
            source={"archive": "https", "license": "BSD-3-Clause"},
        )
        == sessions.COMMITTED
    )
    (row,) = h.rows()
    assert row["license"] == "BSD-3-Clause"
    (dataset,) = h.rows(schema.DATASETS)
    assert dataset["license"] == META["license"]
