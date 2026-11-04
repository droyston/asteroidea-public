import json
from typing import TYPE_CHECKING

import pytest

from asteroidea_services.harvest.model import Clock, ShapeError, SourceListingError
from asteroidea_services.harvest.openneuro import (
    ENDPOINT,
    OpenNeuroSource,
    map_dataset,
    map_license,
    map_species,
)
from asteroidea_services.http import Request, Response, Transport, replaying, retrying

if TYPE_CHECKING:
    from io import StringIO
    from pathlib import Path

    from asteroidea_services.harvest.progress import Progress

HARVESTED = "2026-10-23T12:00:00Z"


def node(identifier: str = "ds000001", **overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "id": identifier,
        "metadata": {"species": None, "affirmedDefaced": None},
        "latestSnapshot": {
            "tag": "1.0.0",
            "hexsha": "abc",
            "created": "2020-05-14T14:56:55.000Z",
            "description": {"Name": "A name", "License": "CC0", "DatasetDOI": "10.1/x"},
            "summary": {
                "modalities": ["MRI"],
                "secondaryModalities": ["mri_functional", "MRI"],
                "subjects": ["01", "02"],
                "size": 100,
                "totalFiles": 7,
            },
        },
    }
    return {**base, **overrides}


class Pages:
    """A fake GraphQL server over nodes (None = null edge), 50 per page."""

    def __init__(
        self, nodes: list[dict[str, object] | None], *, claimed: int | None = None
    ) -> None:
        self.nodes = nodes
        self.claimed = len(nodes) if claimed is None else claimed
        self.calls = 0

    def __call__(self, request: Request) -> Response:
        assert request.url == ENDPOINT
        assert request.body is not None
        after = json.loads(request.body)["variables"]["after"]
        start = 0 if after is None else int(after)
        chunk = self.nodes[start : start + 50]
        self.calls += 1
        edges = [None if n is None else {"cursor": "c", "node": n} for n in chunk]
        # hasNextPage is deliberately always true: the harvester must not trust it.
        info = {
            "count": self.claimed,
            "hasNextPage": True,
            "endCursor": str(start + len(chunk)),
        }
        return Response(
            200,
            json.dumps(
                {"data": {"datasets": {"pageInfo": info, "edges": edges}}}
            ).encode(),
        )


def run(http: Transport, clock: Clock, progress: Progress):
    return OpenNeuroSource(clock).harvest(http, progress)


def test_species_null_and_empty_are_not_stated() -> None:
    assert map_species(None) == ([], "not_stated")
    assert map_species("") == ([], "not_stated")
    assert map_species("   ") == ([], "not_stated")


@pytest.mark.parametrize(
    ("text", "taxon"),
    [
        ("Human", "NCBITaxon:9606"),
        (" homo sapiens ", "NCBITaxon:9606"),
        ("mouse", "NCBITaxon:10090"),
        ("Mice", "NCBITaxon:10090"),
        ("RAT", "NCBITaxon:10116"),
        ("macaque", "NCBITaxon:9544"),
        ("Rhesus", "NCBITaxon:9544"),
        ("baboon", None),
    ],
)
def test_species_table_is_case_insensitive_and_misses_keep_the_text(
    text: str, taxon: str | None
) -> None:
    species, status = map_species(text)
    assert status == "stated"
    assert species == [{"taxon": taxon, "label": text.strip()}]


@pytest.mark.parametrize(
    ("raw", "spdx"),
    [
        ("CC0", "CC0-1.0"),
        ("CC0 1.0", "CC0-1.0"),
        ("PDDL", "PDDL-1.0"),
        ("CC-BY-4.0", "CC-BY-4.0"),
        ("CC BY 4.0", "CC-BY-4.0"),
        (
            "See http://www.opendatacommons.org/licenses/pddl/1.0/ for the text",
            "PDDL-1.0",
        ),
        ("All rights reserved", None),
    ],
)
def test_license_table(raw: str, spdx: str | None) -> None:
    assert map_license(raw) == {"spdx": spdx, "raw": raw}


def test_license_null() -> None:
    assert map_license(None) == {"spdx": None, "raw": None}


def test_dataset_mapping() -> None:
    record = map_dataset(node(), HARVESTED)
    assert record["modalities"] == ["mri", "mri_functional"]
    assert (record["subjects"], record["bytes"], record["files"]) == (2, 100, 7)
    assert (record["version"], record["version_ref"]) == ("1.0.0", "abc")
    assert record["standards"] == ["BIDS"]
    assert (record["has_units"], record["has_lfp"], record["has_ephys_series"]) == (
        False,
        False,
        False,
    )
    assert record["published_at"] == "2020-05-14T14:56:55Z"
    assert (
        record["urls"]["landing"]
        == "https://openneuro.org/datasets/ds000001/versions/1.0.0"
    )
    assert record["deid"] == {"affirmed_defaced": None}


def test_affirmed_defaced_is_carried() -> None:
    record = map_dataset(
        node(metadata={"species": "Human", "affirmedDefaced": True}), HARVESTED
    )
    assert record["deid"] == {"affirmed_defaced": True}
    assert record["species"] == [{"taxon": "NCBITaxon:9606", "label": "Human"}]


def test_node_without_id_is_a_shape_error() -> None:
    with pytest.raises(ShapeError, match="node without id"):
        map_dataset({"latestSnapshot": {}}, HARVESTED)


def test_openneuro_null_node_skipped_and_counted(
    clock: Clock, progress: Progress
) -> None:
    nodes: list[dict[str, object] | None] = [node("ds000001"), None, node("ds000003")]
    result = run(Pages(nodes), clock, progress)
    assert [d["id"] for d in result.datasets] == ["ds000001", "ds000003"]
    assert result.null_nodes == 1
    assert result.listed == 3


def test_openneuro_null_node_inside_a_present_edge(
    clock: Clock, progress: Progress
) -> None:
    def serve(_request: Request) -> Response:
        info = {"count": 2, "endCursor": "2"}
        edges = [{"cursor": "a", "node": None}, {"cursor": "b", "node": node()}]
        return Response(
            200,
            json.dumps(
                {"data": {"datasets": {"pageInfo": info, "edges": edges}}}
            ).encode(),
        )

    result = run(serve, clock, progress)
    assert (result.null_nodes, len(result.datasets)) == (1, 1)


def test_openneuro_pagination_stops_at_the_count_not_at_has_next_page(
    clock: Clock, progress: Progress
) -> None:
    nodes: list[dict[str, object] | None] = [node(f"ds{i:06d}") for i in range(120)]
    pages = Pages(nodes)
    result = run(pages, clock, progress)
    assert result.listed == 120
    assert len(result.datasets) == 120
    assert pages.calls == 3


def test_openneuro_empty_page_stops_early(clock: Clock, progress: Progress) -> None:
    pages = Pages([node("ds000001")], claimed=500)
    result = run(pages, clock, progress)
    assert result.listed == 1
    assert pages.calls == 2


def test_openneuro_no_snapshot_is_a_failure(clock: Clock, progress: Progress) -> None:
    nodes: list[dict[str, object] | None] = [
        node("ds000001", latestSnapshot=None),
        node("ds000002"),
    ]
    result = run(Pages(nodes), clock, progress)
    (failure,) = result.failed
    assert (failure["id"], failure["error_type"]) == ("ds000001", "NoSnapshot")
    assert [d["id"] for d in result.datasets] == ["ds000002"]


def test_openneuro_failure_line_names_url_and_attempts(
    clock: Clock, progress: Progress, stderr: StringIO
) -> None:
    result = run(Pages([node("ds000001", latestSnapshot=None)]), clock, progress)
    (failure,) = result.failed
    assert failure["url"] == "https://openneuro.org/datasets/ds000001"
    assert failure["attempts"] == 1
    assert (
        "openneuro: FAILED ds000001 NoSnapshot: latestSnapshot is null"
        " (https://openneuro.org/datasets/ds000001, 1 attempt(s))\n"
    ) in stderr.getvalue()


def test_openneuro_malformed_node_is_a_per_record_failure(
    clock: Clock, progress: Progress
) -> None:
    bad = node("ds000001", metadata={"species": 3})
    result = run(Pages([bad, node("ds000002")]), clock, progress)
    (failure,) = result.failed
    assert (failure["id"], failure["error_type"]) == ("ds000001", "ShapeError")
    assert len(result.datasets) == 1


def test_openneuro_listing_failure_after_retries_names_source_and_cause(
    clock: Clock, progress: Progress
) -> None:
    def timeout(_request: Request) -> Response:
        message = "timed out"
        raise TimeoutError(message)

    with pytest.raises(
        SourceListingError,
        match=r"openneuro listing failed: HTTPError: TimeoutError: timed out",
    ):
        run(retrying(timeout, sleep=lambda _s: None), clock, progress)


def test_openneuro_graphql_error_without_data_is_a_listing_error(
    clock: Clock, progress: Progress
) -> None:
    def broken(_request: Request) -> Response:
        return Response(
            200, json.dumps({"errors": [{"message": "boom"}], "data": None}).encode()
        )

    with pytest.raises(SourceListingError, match="boom"):
        run(broken, clock, progress)


def test_openneuro_zero_records_is_a_source_error(
    clock: Clock, progress: Progress
) -> None:
    with pytest.raises(SourceListingError, match="zero records"):
        run(Pages([], claimed=5), clock, progress)


def test_openneuro_fixture_pagination_with_the_real_null_page(
    fixtures: Path, clock: Clock, progress: Progress
) -> None:
    result = run(replaying(fixtures), clock, progress)
    assert result.listed == 127
    assert result.null_nodes == 1
    assert len(result.datasets) == 126
    states = {(d["id"], d["species_status"]) for d in result.datasets}
    assert any(status == "stated" for _, status in states)
    assert any(status == "not_stated" for _, status in states)
