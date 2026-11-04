import json
from io import StringIO
from typing import TYPE_CHECKING

from asteroidea_services.harvest import main
from asteroidea_services.harvest.progress import Progress
from asteroidea_services.http import Request, Response

if TYPE_CHECKING:
    from pathlib import Path

NOW = "2026-10-23T12:00:00Z"


def test_golden_index(fixtures: Path, tmp_path: Path) -> None:
    out = tmp_path / "index.json"
    code = main(
        ["--replay", str(fixtures), "--out", str(out), "--now", NOW],
        Progress(StringIO()),
    )
    assert code == 0
    expected = (fixtures / "expected-index.json").read_text(encoding="utf-8")
    assert out.read_text(encoding="utf-8") == expected
    assert not (tmp_path / "index.json.tmp").exists()


def test_index_shape_summary(fixtures: Path) -> None:
    index = json.loads((fixtures / "expected-index.json").read_text(encoding="utf-8"))
    assert index["schema"] == "asteroidea.dataset-index/2"
    assert index["sources"]["dandi"]["listed"] == 5
    assert index["sources"]["dandi"]["ok"] == 4
    assert index["sources"]["openneuro"]["null_nodes"] == 1
    assert {d["source"] for d in index["datasets"]} == {"dandi", "openneuro"}
    assert len(index["datasets"]) == 4 + 126


def test_listing_failure_exits_1_and_leaves_the_previous_index(
    fixtures: Path, tmp_path: Path
) -> None:
    out = tmp_path / "index.json"
    out.write_text("previous\n", encoding="utf-8")
    broken = tmp_path / "replay"
    broken.mkdir()
    for path in fixtures.glob("*.json"):
        if path.name != "expected-index.json":
            (broken / path.name).write_bytes(path.read_bytes())
    for path in broken.iterdir():
        if json.loads(path.read_text())["method"] == "POST":
            path.unlink()  # every OpenNeuro page is now a replay miss
    stderr = StringIO()
    code = main(
        ["--replay", str(broken), "--out", str(out), "--now", NOW], Progress(stderr)
    )
    assert code == 1
    assert out.read_text(encoding="utf-8") == "previous\n"
    assert not (tmp_path / "index.json.tmp").exists()
    last = stderr.getvalue().splitlines()[-1]
    assert last.startswith("harvest: openneuro listing failed:")
    assert "ReplayMissError" in last or "no recorded response" in last


def test_progress_lines_name_each_source(fixtures: Path, tmp_path: Path) -> None:
    stderr = StringIO()
    main(
        ["--replay", str(fixtures), "--out", str(tmp_path / "i.json"), "--now", NOW],
        Progress(stderr),
    )
    text = stderr.getvalue()
    assert "dandi: listed 5, ok 4, failed 1" in text
    assert "openneuro: 50 of 127 records" in text
    assert "openneuro: listed 127, ok 126, null nodes 1" in text
    assert "harvest: wrote" in text


def test_request_is_hashable_for_sets() -> None:
    assert Request("GET", "https://a.test/") == Request("GET", "https://a.test/")
    assert Response(200, b"") == Response(200, b"")
