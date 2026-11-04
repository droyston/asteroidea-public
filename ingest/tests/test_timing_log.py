"""One structured timing line per slow-route request, never a credential."""

import json
from typing import TYPE_CHECKING, Any

from asteroidea_ingest import timing_log

from .test_catalog_api import DOWNLOAD_COLS, FakeAthena, _download
from .test_labels_api import ASSET, Rig, _post

if TYPE_CHECKING:
    import pytest

ROW: list[str | None] = ["id1", "raw/ds1/f1.nii.gz", "v-abc123", "nifti", "4096"]


def _lines(out: str) -> list[dict[str, Any]]:
    found = [json.loads(x) for x in out.splitlines() if x.startswith("{")]
    return [x for x in found if "timing" in x]


def test_download_emits_one_timing_line_without_the_url(
    capsys: pytest.CaptureFixture[str],
) -> None:
    timing_log.reset()
    _, body = _download(FakeAthena((DOWNLOAD_COLS, [ROW])))
    out = capsys.readouterr().out
    (line,) = _lines(out)
    assert line["timing"] == "catalog_download"
    assert set(line["phases"]) == {"athena_ms", "presign_ms", "total_ms"}
    assert all(isinstance(v, int) for v in line["phases"].values())
    assert line["cold"] is True
    assert line["asset_id"] == "id1"
    assert body["url"] not in out
    assert "X-Amz" not in out
    assert "Bearer" not in out
    _download(FakeAthena((DOWNLOAD_COLS, [ROW])))
    assert _lines(capsys.readouterr().out)[0]["cold"] is False


def test_download_of_an_unknown_asset_still_emits_without_a_presign_phase(
    capsys: pytest.CaptureFixture[str],
) -> None:
    timing_log.reset()
    _download(FakeAthena((DOWNLOAD_COLS, [])))
    (line,) = _lines(capsys.readouterr().out)
    assert set(line["phases"]) == {"athena_ms", "total_ms"}


def test_label_post_emits_one_timing_line_without_the_sub(
    capsys: pytest.CaptureFixture[str],
) -> None:
    timing_log.reset()
    rig = Rig()
    _post(rig, sub="alice-sub-secret")
    out = capsys.readouterr().out
    (line,) = _lines(out)
    assert line["timing"] == "labels_create"
    assert set(line["phases"]) == {
        "provenance_ms",
        "athena_ms",
        "glue_ms",
        "s3_ms",
        "ddb_ms",
        "total_ms",
    }
    assert all(isinstance(v, int) for v in line["phases"].values())
    assert line["cold"] is True
    assert line["asset_id"] == ASSET
    assert "alice-sub-secret" not in out
    _post(rig)
    assert _lines(capsys.readouterr().out)[0]["cold"] is False


def test_download_line_says_cache_miss_then_hit_and_a_hit_skips_athena_ms(
    capsys: pytest.CaptureFixture[str],
) -> None:
    timing_log.reset()
    fake = FakeAthena((DOWNLOAD_COLS, [ROW]))
    _download(fake)
    _download(fake)
    miss, hit = _lines(capsys.readouterr().out)
    assert miss["cache"] == "miss"
    assert hit["cache"] == "hit"
    assert set(hit["phases"]) == {"presign_ms", "total_ms"}


def test_label_line_has_no_cache_key(capsys: pytest.CaptureFixture[str]) -> None:
    timing_log.reset()
    _post(Rig())
    (line,) = _lines(capsys.readouterr().out)
    assert "cache" not in line
