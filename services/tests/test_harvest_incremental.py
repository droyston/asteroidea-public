"""Incremental harvest over a request-logging fake archive."""

import json
import os
import re
from io import StringIO
from typing import TYPE_CHECKING

from asteroidea_services.harvest import Target, run
from asteroidea_services.harvest.dandi import LISTING_URL, DandiSource
from asteroidea_services.harvest.openneuro import (
    ENDPOINT,
    LIGHT_QUERY,
    ONE_QUERY,
    QUERY,
    OpenNeuroSource,
)
from asteroidea_services.harvest.progress import Progress
from asteroidea_services.http import HTTPError, Request, Response

if TYPE_CHECKING:
    from pathlib import Path

    from asteroidea_services.harvest.model import Clock, Source

INFO = re.compile(r"/dandisets/(\d+)/versions/([^/]+)/info/$")


def snapshot(tag: str, hexsha: str) -> dict[str, object]:
    return {
        "tag": tag,
        "hexsha": hexsha,
        "created": "2020-05-14T14:56:55.000Z",
        "description": {"Name": "N", "License": "CC0", "DatasetDOI": None},
        "summary": {
            "modalities": ["MRI"],
            "secondaryModalities": [],
            "subjects": ["01"],
            "size": 1,
            "totalFiles": 1,
        },
    }


class Archive:
    """Fake DANDI and OpenNeuro. ``log`` lists every request as a short string."""

    def __init__(self) -> None:
        self.dandi: dict[str, tuple[str, str]] = {
            "000001": ("0.1", "2024-01-01T00:00:00Z"),
            "000002": ("0.1", "2024-01-02T00:00:00Z"),
            "000003": ("0.1", "2024-01-03T00:00:00Z"),
        }
        self.broken: set[str] = set()
        self.nodes: dict[str, dict[str, object]] = {
            f"ds00000{i}": {
                "id": f"ds00000{i}",
                "metadata": {"species": "Human", "affirmedDefaced": None},
                "latestSnapshot": snapshot("1.0.0", f"sha{i}"),
            }
            for i in range(1, 4)
        }
        self.log: list[str] = []

    def __call__(self, request: Request) -> Response:
        if request.method == "GET":
            return self._dandi(request.url)
        return self._openneuro(request)

    def _dandi(self, url: str) -> Response:
        if url == LISTING_URL:
            self.log.append("GET listing")
            results = [
                {
                    "identifier": i,
                    "most_recent_published_version": {"version": v, "modified": m},
                }
                for i, (v, m) in self.dandi.items()
            ]
            return Response(
                200, json.dumps({"results": results, "next": None}).encode()
            )
        match = INFO.search(url)
        assert match, url
        self.log.append(f"GET info {match.group(1)}")
        if match.group(1) in self.broken:
            raise HTTPError(url, "HTTP 500 Internal Server Error", 3)
        body = {"name": f"t{match.group(1)}", "metadata": {"assetsSummary": {}}}
        return Response(200, json.dumps(body).encode())

    def _openneuro(self, request: Request) -> Response:
        assert request.url == ENDPOINT
        assert request.body is not None
        sent = json.loads(request.body)
        variables = sent["variables"]
        if sent["query"] == ONE_QUERY:
            self.log.append(f"POST one {variables['id']}")
            if variables["id"] in self.broken:
                raise HTTPError(ENDPOINT, "HTTP 500 Internal Server Error", 3)
            data = {"dataset": self.nodes[variables["id"]]}
            return Response(200, json.dumps({"data": data}).encode())
        assert sent["query"] in {QUERY, LIGHT_QUERY}
        size = 50 if sent["query"] == QUERY else 100
        kind = "full" if sent["query"] == QUERY else "light"
        start = int(variables["after"] or 0)
        self.log.append(f"POST {kind} page {start}")
        chunk = list(self.nodes.values())[start : start + size]
        info = {
            "count": len(self.nodes),
            "hasNextPage": True,
            "endCursor": str(start + len(chunk)),
        }
        edges = [{"cursor": "c", "node": n} for n in chunk]
        page = {"pageInfo": info, "edges": edges}
        return Response(200, json.dumps({"data": {"datasets": page}}).encode())

    def calls(self, prefix: str) -> list[str]:
        return [line for line in self.log if line.startswith(prefix)]


def harvest(
    archive: Archive, out: Path, clock: Clock, *, force: bool = False
) -> tuple[int, str]:
    stderr = StringIO()
    sources: list[Source] = [DandiSource(clock, workers=1), OpenNeuroSource(clock)]
    code = run(sources, archive, Progress(stderr), clock, Target(out, force))
    return code, stderr.getvalue()


def test_first_run_is_a_full_rebuild_with_a_stated_reason(
    tmp_path: Path, clock: Clock
) -> None:
    archive = Archive()
    code, text = harvest(archive, tmp_path / "i.json", clock)
    assert code == 0
    assert f"harvest: full rebuild (no cached index at {tmp_path / 'i.json'})" in text
    assert archive.calls("GET info") == [
        "GET info 000001",
        "GET info 000002",
        "GET info 000003",
    ]
    assert archive.calls("POST full") == ["POST full page 0"]
    assert "harvest: wrote" in text


def test_unchanged_listing_leaves_the_file_untouched(
    tmp_path: Path, clock: Clock
) -> None:
    out = tmp_path / "i.json"
    archive = Archive()
    harvest(archive, out, clock)
    os.utime(out, (1_000_000, 1_000_000))
    before = out.read_bytes()
    archive.log.clear()
    code, text = harvest(archive, out, clock)
    assert code == 0
    assert "harvest: up to date (dandi 3, openneuro 3)" in text
    assert "harvest: wrote" not in text
    assert out.stat().st_mtime == 1_000_000
    assert out.read_bytes() == before
    assert archive.log == ["GET listing", "POST light page 0"]


def test_one_changed_dandiset_fetches_only_its_info(
    tmp_path: Path, clock: Clock
) -> None:
    out = tmp_path / "i.json"
    archive = Archive()
    harvest(archive, out, clock)
    archive.dandi["000002"] = ("0.2", "2024-02-01T00:00:00Z")
    archive.log.clear()
    code, text = harvest(archive, out, clock)
    assert code == 0
    assert archive.calls("GET info") == ["GET info 000002"]
    assert "dandi: changed 1, new 0, removed 0, unchanged 2" in text
    assert "openneuro: changed 0, new 0, removed 0, unchanged 3" in text
    ids = {d["id"]: d for d in json.loads(out.read_text())["datasets"]}
    assert ids["000002"]["version"] == "0.2"
    assert ids["000002"]["version_modified"] == "2024-02-01T00:00:00Z"
    assert ids["000001"]["version"] == "0.1"


def test_modified_time_alone_triggers_a_refetch(tmp_path: Path, clock: Clock) -> None:
    out = tmp_path / "i.json"
    archive = Archive()
    harvest(archive, out, clock)
    archive.dandi["000001"] = ("0.1", "2024-03-01T00:00:00Z")
    archive.log.clear()
    harvest(archive, out, clock)
    assert archive.calls("GET info") == ["GET info 000001"]


def test_new_and_removed_dandisets(tmp_path: Path, clock: Clock) -> None:
    out = tmp_path / "i.json"
    archive = Archive()
    harvest(archive, out, clock)
    del archive.dandi["000001"]
    archive.dandi["000009"] = ("0.1", "2024-04-01T00:00:00Z")
    archive.log.clear()
    _, text = harvest(archive, out, clock)
    assert archive.calls("GET info") == ["GET info 000009"]
    assert "dandi: changed 0, new 1, removed 1, unchanged 2" in text
    ids = [
        d["id"]
        for d in json.loads(out.read_text())["datasets"]
        if d["source"] == "dandi"
    ]
    assert ids == ["000002", "000003", "000009"]


def test_openneuro_new_snapshot_species_and_removal(
    tmp_path: Path, clock: Clock
) -> None:
    out = tmp_path / "i.json"
    archive = Archive()
    harvest(archive, out, clock)
    archive.nodes["ds000001"]["latestSnapshot"] = snapshot("1.1.0", "newsha")
    archive.nodes["ds000002"]["metadata"] = {
        "species": "Mouse",
        "affirmedDefaced": None,
    }
    del archive.nodes["ds000003"]
    archive.nodes["ds000004"] = {
        "id": "ds000004",
        "metadata": {"species": None, "affirmedDefaced": None},
        "latestSnapshot": snapshot("1.0.0", "sha4"),
    }
    archive.log.clear()
    _, text = harvest(archive, out, clock)
    assert sorted(archive.calls("POST one")) == [
        "POST one ds000001",
        "POST one ds000002",
        "POST one ds000004",
    ]
    assert "openneuro: changed 2, new 1, removed 1, unchanged 0" in text
    by_id = {d["id"]: d for d in json.loads(out.read_text())["datasets"]}
    assert by_id["ds000001"]["version"] == "1.1.0"
    assert "ds000003" not in by_id


def test_schema_mismatch_forces_a_full_rebuild_with_the_reason(
    tmp_path: Path, clock: Clock
) -> None:
    out = tmp_path / "i.json"
    archive = Archive()
    harvest(archive, out, clock)
    doc = json.loads(out.read_text())
    doc["schema"] = "asteroidea.dataset-index/1"
    out.write_text(json.dumps(doc))
    archive.log.clear()
    _, text = harvest(archive, out, clock)
    assert (
        "harvest: full rebuild (cached schema 'asteroidea.dataset-index/1' is not "
        "'asteroidea.dataset-index/2')" in text
    )
    assert len(archive.calls("GET info")) == 3
    assert json.loads(out.read_text())["schema"] == "asteroidea.dataset-index/2"


def test_force_rebuilds_in_full_even_when_nothing_changed(
    tmp_path: Path, clock: Clock
) -> None:
    out = tmp_path / "i.json"
    archive = Archive()
    harvest(archive, out, clock)
    archive.log.clear()
    _, text = harvest(archive, out, clock, force=True)
    assert "harvest: full rebuild (--force)" in text
    assert len(archive.calls("GET info")) == 3
    assert archive.calls("POST light") == []


def test_failed_refresh_keeps_the_old_record_marked_stale(
    tmp_path: Path, clock: Clock
) -> None:
    out = tmp_path / "i.json"
    archive = Archive()
    harvest(archive, out, clock)
    old = {d["id"]: d for d in json.loads(out.read_text())["datasets"]}
    archive.dandi["000002"] = ("0.2", "2024-02-01T00:00:00Z")
    archive.nodes["ds000002"]["latestSnapshot"] = snapshot("2.0.0", "sha-new")
    archive.broken = {"000002", "ds000002"}
    code, text = harvest(archive, out, clock)
    assert code == 0
    doc = json.loads(out.read_text())
    by_id = {d["id"]: d for d in doc["datasets"]}
    assert by_id["000002"] == old["000002"]
    assert by_id["ds000002"] == old["ds000002"]
    for source, ident in (("dandi", "000002"), ("openneuro", "ds000002")):
        (failure,) = doc["sources"][source]["failed"]
        assert failure["id"] == ident
        assert failure["stale"] is True
        assert "HTTP 500" in failure["message"]
        assert doc["sources"][source]["ok"] == 3
    assert "FAILED 000002" in text


def test_failed_refresh_of_a_new_record_is_not_stale(
    tmp_path: Path, clock: Clock
) -> None:
    out = tmp_path / "i.json"
    archive = Archive()
    harvest(archive, out, clock)
    archive.dandi["000009"] = ("0.1", "2024-04-01T00:00:00Z")
    archive.broken = {"000009"}
    harvest(archive, out, clock)
    doc = json.loads(out.read_text())
    (failure,) = doc["sources"]["dandi"]["failed"]
    assert (failure["id"], failure["stale"]) == ("000009", False)
    assert "000009" not in {d["id"] for d in doc["datasets"]}


def test_a_persistent_failure_does_not_rewrite_the_file(
    tmp_path: Path, clock: Clock
) -> None:
    out = tmp_path / "i.json"
    archive = Archive()
    harvest(archive, out, clock)
    archive.dandi["000002"] = ("0.2", "2024-02-01T00:00:00Z")
    archive.broken = {"000002"}
    harvest(archive, out, clock)
    os.utime(out, (1_000_000, 1_000_000))
    _, text = harvest(archive, out, clock)
    assert "up to date" in text
    assert out.stat().st_mtime == 1_000_000
