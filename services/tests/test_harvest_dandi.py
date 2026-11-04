import json
from typing import TYPE_CHECKING

import pytest

from asteroidea_services.harvest.dandi import (
    LISTING_URL,
    DandiSource,
    map_dandiset,
)
from asteroidea_services.harvest.model import Clock, ShapeError, SourceListingError
from asteroidea_services.harvest.progress import Progress
from asteroidea_services.http import Request, Response, replaying, retrying

if TYPE_CHECKING:
    from io import StringIO
    from pathlib import Path

HARVESTED = "2026-10-23T12:00:00Z"


def info(summary: dict[str, object], **metadata: object) -> dict[str, object]:
    return {
        "version": "0.1",
        "name": "T",
        "created": "2024-01-02T03:04:05.678Z",
        "metadata": {"assetsSummary": summary, **metadata},
    }


def test_variables_drive_the_three_flags() -> None:
    record = map_dandiset(
        "1", "0.1", info({"variableMeasured": ["Units", "ElectricalSeries"]}), HARVESTED
    )
    assert (record["has_units"], record["has_lfp"], record["has_ephys_series"]) == (
        True,
        False,
        True,
    )
    record = map_dandiset("1", "0.1", info({"variableMeasured": ["LFP"]}), HARVESTED)
    assert (record["has_units"], record["has_lfp"], record["has_ephys_series"]) == (
        False,
        True,
        False,
    )


def test_species_taxon_label_and_status() -> None:
    species = [
        {
            "name": "Homo sapiens - Human",
            "identifier": "http://purl.obolibrary.org/obo/NCBITaxon_9606",
        }
    ]
    record = map_dandiset("1", "0.1", info({"species": species}), HARVESTED)
    assert record["species"] == [
        {"taxon": "NCBITaxon:9606", "label": "Homo sapiens - Human"}
    ]
    assert record["species_status"] == "stated"


def test_unknown_species_shape_keeps_label_with_null_taxon() -> None:
    record = map_dandiset(
        "1", "0.1", info({"species": [{"name": "Weird", "identifier": "x"}]}), HARVESTED
    )
    assert record["species"] == [{"taxon": None, "label": "Weird"}]


def test_absent_species_is_not_stated() -> None:
    record = map_dandiset("1", "0.1", info({}), HARVESTED)
    assert record["species"] == []
    assert record["species_status"] == "not_stated"


def test_modalities_from_approach_names() -> None:
    approach = [
        {"name": "electrophysiological approach"},
        {"name": "microscopy approach; cell population imaging"},
        {"name": "optical"},
        {"name": "behavioral approach"},
        {"name": "something else"},
    ]
    record = map_dandiset("1", "0.1", info({"approach": approach}), HARVESTED)
    assert record["modalities"] == ["ecephys", "ophys", "behavior", "something else"]


def test_standards_license_counts_and_dates() -> None:
    summary: dict[str, object] = {
        "dataStandard": [
            {"name": "Neurodata Without Borders (NWB)"},
            {"name": "BIDS"},
            {"name": "other"},
        ],
        "numberOfSubjects": 9,
        "numberOfBytes": 10,
        "numberOfFiles": 3,
    }
    record = map_dandiset(
        "1", "0.1", info(summary, license=["spdx:CC0-1.0"], doi="10.1/x"), HARVESTED
    )
    assert record["standards"] == ["NWB", "BIDS"]
    assert record["license"] == {"spdx": "CC0-1.0", "raw": "spdx:CC0-1.0"}
    assert (record["subjects"], record["bytes"], record["files"]) == (9, 10, 3)
    assert record["version_ref"] == "10.1/x"
    assert record["published_at"] == "2024-01-02T03:04:05Z"
    assert record["deid"] == {"affirmed_defaced": None}


def test_missing_counts_and_license_are_null() -> None:
    record = map_dandiset("1", "0.1", info({}), HARVESTED)
    assert (record["subjects"], record["bytes"], record["files"]) == (None, None, None)
    assert record["license"] == {"spdx": None, "raw": None}


def test_non_object_info_is_a_shape_error() -> None:
    with pytest.raises(ShapeError, match="expected object"):
        map_dandiset("1", "0.1", [], HARVESTED)


def harvest(fixtures: Path, clock: Clock, stderr: StringIO, sleeps: list[float]):
    http = retrying(replaying(fixtures), sleep=sleeps.append)
    return DandiSource(clock).harvest(http, Progress(stderr))


def test_dandi_retry_then_report(
    fixtures: Path, clock: Clock, stderr: StringIO
) -> None:
    sleeps: list[float] = []
    result = harvest(fixtures, clock, stderr, sleeps)
    assert result.listed == 5
    assert [d["id"] for d in result.datasets] == [
        "000003",
        "000016",
        "000029",
        "000576",
    ]
    (failure,) = result.failed
    assert failure["id"] == "000004"
    assert failure["attempts"] == 3
    assert failure["error_type"] == "HTTPError"
    assert failure["message"] == "HTTP 500 Internal Server Error"
    assert failure["url"].endswith("/dandisets/000004/versions/0.220126.1852/info/")
    # 000016: one 503 then the recorded 200 (sleep 1 s); 000004: three 500s (1, 2 s).
    assert sorted(sleeps) == [1.0, 1.0, 2.0]
    assert (
        "dandi: FAILED 000004 HTTPError: HTTP 500 Internal Server Error"
        in stderr.getvalue()
    )


def test_dandi_listing_failure_is_a_source_error(
    clock: Clock, progress: Progress
) -> None:
    def down(_request: Request) -> Response:
        return Response(500, b"")

    with pytest.raises(SourceListingError, match="dandi listing failed: HTTPError"):
        DandiSource(clock).harvest(retrying(down, sleep=lambda _s: None), progress)


def test_dandi_zero_records_is_a_source_error(clock: Clock, progress: Progress) -> None:
    def empty(_request: Request) -> Response:
        return Response(
            200, json.dumps({"count": 0, "next": None, "results": []}).encode()
        )

    with pytest.raises(SourceListingError, match="zero records"):
        DandiSource(clock).harvest(empty, progress)


def test_dandi_follows_next_and_lists_unversioned_as_failed(
    clock: Clock, progress: Progress
) -> None:
    second = "https://api.dandiarchive.org/api/dandisets/?page=2"
    pages = {
        LISTING_URL: {
            "next": second,
            "results": [{"identifier": "A", "most_recent_published_version": None}],
        },
        second: {
            "next": None,
            "results": [
                {"identifier": "B", "most_recent_published_version": {"version": "1"}}
            ],
        },
    }

    def serve(request: Request) -> Response:
        if request.url in pages:
            return Response(200, json.dumps(pages[request.url]).encode())
        return Response(200, json.dumps(info({"numberOfSubjects": 2})).encode())

    result = DandiSource(clock, workers=2).harvest(serve, progress)
    assert [d["id"] for d in result.datasets] == ["B"]
    (failure,) = result.failed
    assert (failure["id"], failure["error_type"], failure["message"]) == (
        "A",
        "ShapeError",
        "no published version",
    )


def test_dandi_malformed_info_is_a_per_record_failure(
    clock: Clock, progress: Progress
) -> None:
    listing = {
        "next": None,
        "results": [
            {"identifier": "A", "most_recent_published_version": {"version": "1"}}
        ],
    }

    def serve(request: Request) -> Response:
        body = (
            listing
            if request.url == LISTING_URL
            else {"metadata": {"assetsSummary": {"numberOfFiles": "x"}}}
        )
        return Response(200, json.dumps(body).encode())

    result = DandiSource(clock).harvest(serve, progress)
    assert result.datasets == []
    assert result.failed[0]["error_type"] == "ShapeError"
    assert "expected integer" in result.failed[0]["message"]
