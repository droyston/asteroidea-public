"""`make summaries` (backfill), offline: a SQLite catalog and a fake queue."""

import json
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import pytest
from botocore.exceptions import ClientError
from pyiceberg.catalog.sql import SqlCatalog

from asteroidea_infra import schema, summaries
from asteroidea_infra.catalog_init import iceberg_schema
from asteroidea_infra.runner import execute
from asteroidea_infra.sandbox_guard import EXIT_NO_SANDBOX
from asteroidea_ingest import commit, dataset_summary
from asteroidea_ingest.catalog_writer import CatalogWriter

if TYPE_CHECKING:
    from collections.abc import Iterator

    from pyiceberg.catalog import Catalog

REPO: Final = Path(__file__).resolve().parents[2]
QUEUE: Final = "https://sqs.us-east-1.amazonaws.com/0/asteroidea-lfp-build"


class FakeSqs:
    """The LFP build queue: refuses an empty body or any argument it does not take."""

    def __init__(self, fail_on: str = "") -> None:
        self.bodies: list[dict[str, Any]] = []
        self.fail_on = fail_on

    def send_message(self, **kw: str) -> dict[str, Any]:
        assert set(kw) == {"QueueUrl", "MessageBody"}  # a standard queue: no group
        assert kw["QueueUrl"] == QUEUE
        assert kw["MessageBody"]
        body = json.loads(kw["MessageBody"])
        if body["dataset"] == self.fail_on:
            error = {"Error": {"Code": "AccessDenied", "Message": "denied"}}
            raise ClientError(error, "SendMessage")
        self.bodies.append(body)
        return {"MessageId": str(len(self.bodies))}


def _row(asset: str, dataset: str) -> dict[str, Any]:
    return {
        "asset_id": asset,
        "session_id": f"s-{asset}",
        "dataset": dataset,
        "s3_key": f"raw/{dataset}/sub-01/sub-01.nwb",
        "s3_version_id": "v1",
        "size_bytes": 100,
        "checksum_crc64nvme": "c",
        "extension": ".nwb",
        "committed_at": commit.utc_now_naive(),
        "format": commit.file_format(".nwb"),
    }


@pytest.fixture
def catalog(tmp_path: Path) -> Iterator[Catalog]:
    cat = SqlCatalog(
        "test",
        uri=f"sqlite:///{tmp_path}/catalog.db",
        warehouse=f"file://{tmp_path}/warehouse",
    )
    cat.create_namespace(schema.DATABASE)
    cat.create_table(f"{schema.DATABASE}.{schema.TABLE}", iceberg_schema(schema.ASSETS))
    writer = CatalogWriter(cat)
    for row in (_row("a1", "ds2"), _row("a2", "ds1"), _row("a3", "ds2")):
        writer.append_if_absent(schema.TABLE, {"asset_id": row["asset_id"]}, [row])
    yield cat
    cat.engine.dispose()


def test_lists_each_catalog_dataset_once_sorted(catalog: Catalog) -> None:
    assert summaries.list_datasets(catalog) == ["ds1", "ds2"]


def test_sends_one_summary_message_per_dataset(
    catalog: Catalog, capsys: pytest.CaptureFixture[str]
) -> None:
    sqs = FakeSqs()
    code = summaries.send_all(sqs, QUEUE, summaries.list_datasets(catalog))
    assert code == 0
    assert sqs.bodies == [
        {"kind": "summary", "dataset": "ds1"},
        {"kind": "summary", "dataset": "ds2"},
    ]
    # The builder's own kind name, so the queue routes it to the summary build.
    assert {b["kind"] for b in sqs.bodies} == {dataset_summary.SUMMARY}
    out = capsys.readouterr().out.splitlines()
    assert out == ["SENT ds1", "SENT ds2", "summaries: 2 sent, 0 failed"]


def test_a_failed_send_is_named_and_the_rest_still_go(
    capsys: pytest.CaptureFixture[str],
) -> None:
    sqs = FakeSqs(fail_on="ds1")
    assert summaries.send_all(sqs, QUEUE, ["ds1", "ds2"]) == summaries.EXIT_FAIL
    assert sqs.bodies == [{"kind": "summary", "dataset": "ds2"}]
    out = capsys.readouterr().out
    assert "FAIL ds1: " in out
    assert "AccessDenied" in out
    assert "summaries: 1 sent, 1 failed" in out


def test_no_dataset_is_a_fail(capsys: pytest.CaptureFixture[str]) -> None:
    assert summaries.send_all(FakeSqs(), QUEUE, []) == summaries.EXIT_FAIL
    assert "FAIL no dataset" in capsys.readouterr().out


def test_the_module_refuses_without_the_sandbox_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ASTEROIDEA_PROFILE", raising=False)
    monkeypatch.delenv("ASTEROIDEA_ACCOUNT", raising=False)
    monkeypatch.setattr(sys, "argv", ["summaries", "--dataset", "ds1"])
    with pytest.raises(SystemExit) as exc:
        summaries.main()
    assert exc.value.code == EXIT_NO_SANDBOX


def test_an_empty_dataset_flag_is_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["summaries", "--dataset", ""])
    with pytest.raises(SystemExit) as exc:
        summaries.main()
    assert exc.value.code == summaries.EXIT_USAGE


def test_make_summaries_refuses_without_the_sandbox_env(tmp_path: Path) -> None:
    """The recipe's guard exits 4 (make reports `Error 4`) before any Python runs."""
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in {"ASTEROIDEA_PROFILE", "ASTEROIDEA_ACCOUNT"}
    }
    missing = tmp_path / "no-sandbox.env"
    proc = execute(
        ["make", "--no-print-directory", "summaries", f"SANDBOX_ENV={missing}"],
        cwd=REPO,
        env=env,
        timeout=60,
    )
    assert proc.code != 0
    assert f"Error {EXIT_NO_SANDBOX}" in proc.err
    assert proc.out.startswith("refusing: ASTEROIDEA_PROFILE and ASTEROIDEA_ACCOUNT")
    assert "SENT" not in proc.out


def test_the_recipe_runs_the_module_behind_the_guard() -> None:
    make = (REPO / "Makefile").read_text()
    recipe = make.split("\nsummaries:", 1)[1].split("\n\n", 1)[0]
    assert "## LIVE admin:" in recipe.splitlines()[0]
    assert "$(SANDBOX_GUARD)" in recipe
    assert "python -m asteroidea_infra.summaries" in recipe
    assert '--dataset "$(DATASET)"' in recipe
