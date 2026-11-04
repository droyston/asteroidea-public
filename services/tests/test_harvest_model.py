from datetime import UTC, datetime

import pytest

from asteroidea_services.harvest.model import (
    ShapeError,
    as_list,
    as_obj,
    dedupe,
    names,
    opt_int,
    opt_str,
    stamp,
    str_list,
    taxon_from_url,
    utc_second,
)


def test_taxon_from_dandi_identifier_url() -> None:
    assert (
        taxon_from_url("http://purl.obolibrary.org/obo/NCBITaxon_9606")
        == "NCBITaxon:9606"
    )


@pytest.mark.parametrize("value", [None, "", "RRID:SCR_015242", "NCBITaxon_9606/extra"])
def test_unknown_identifier_shape_has_no_taxon(value: str | None) -> None:
    assert taxon_from_url(value) is None


def test_utc_second_truncates_and_converts() -> None:
    assert utc_second("2023-10-10T18:11:47.645341+00:00") == "2023-10-10T18:11:47Z"
    assert utc_second("2023-10-10T20:11:47+02:00") == "2023-10-10T18:11:47Z"
    assert utc_second(None) is None


def test_utc_second_rejects_garbage() -> None:
    with pytest.raises(ShapeError, match="bad timestamp"):
        utc_second("yesterday")


def test_stamp_formats_the_clock() -> None:
    assert stamp(datetime(2026, 10, 23, 12, 0, 0, tzinfo=UTC)) == "2026-10-23T12:00:00Z"


def test_typed_accessors_reject_wrong_shapes() -> None:
    with pytest.raises(ShapeError, match="expected object"):
        as_obj([], "x")
    with pytest.raises(ShapeError, match="expected array"):
        as_list({}, "x")
    with pytest.raises(ShapeError, match="expected string"):
        opt_str({"k": 1}, "k")
    with pytest.raises(ShapeError, match="expected integer"):
        opt_int({"k": True}, "k")
    with pytest.raises(ShapeError, match="array of strings"):
        str_list({"k": [1]}, "k")


def test_absent_and_null_are_none_or_empty() -> None:
    assert opt_str({"k": None}, "k") is None
    assert opt_int({}, "k") is None
    assert str_list({}, "k") == []
    assert names({"k": [{"name": "a"}, {"other": 1}]}, "k") == ["a"]


def test_dedupe_keeps_first_seen_order() -> None:
    assert dedupe(["b", "a", "b"]) == ["b", "a"]
