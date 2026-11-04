"""GET /catalog/assets?ids= over a fake Athena.

Every fake cell is `VarCharValue` text typed by its column, as Athena answers, and
each id reaches Athena as its own typed `ExecutionParameters` literal. The body is
the golden `web/e2e/fixtures/ux/catalog-assets.golden.json` the results page parses
(regenerate with `GOLDEN_WRITE=1`).
"""

import uuid
from typing import Any, Final

from asteroidea_ingest import catalog_api
from asteroidea_ingest.athena_query import render_param

from .test_catalog_api import ATHENA_PARAM_MAX, FakeAthena, Rows, _get
from .test_catalog_studies import check_golden

ROUTE: Final = "GET /catalog/assets"
V, T = "varchar", "timestamp"
COLS: Final = [
    ("asset_id", V),
    ("dataset", V),
    ("s3_key", V),
    ("subject", V),
    ("bids_session", V),
    ("datatype", V),
    ("suffix", V),
    ("format", V),
    ("s3_version_id", V),
    ("committed_at", T),
    ("latest_version", V),
]
ECEPHYS, MRI = "dandi-000623", "openneuro-ds004798"
NWB: Final = "6b1f0c2e-6f43-5b0e-9d2a-8a3c1f5e7d10"
EVENTS: Final = "0d9c3e1a-2b4f-5c6d-8e7f-1a2b3c4d5e6f"
T1W: Final = "9a8b7c6d-5e4f-5a3b-8c2d-1e0f9a8b7c6d"
ABSENT: Final = "11111111-2222-5333-8444-555555555555"
ROWS: Rows = [
    # Athena answers in its own order; the body follows the order asked.
    [
        T1W,
        MRI,
        f"raw/{MRI}/sub-CS62/anat/sub-CS62_T1w.nii.gz",
        "CS62",
        None,
        "anat",
        "T1w",
        "nifti",
        "9KJ2xWold",
        "2026-09-01 08:00:00.000",
        "Zq81newer",
    ],
    [
        NWB,
        ECEPHYS,
        f"raw/{ECEPHYS}/sub-CS62/sub-CS62_ses-1_ecephys.nwb",
        "CS62",
        "1",
        "ecephys",
        "ecephys",
        "nwb",
        "3HL4kqcurrent",
        "2026-09-14 10:00:00.000",
        "3HL4kqcurrent",
    ],
    [
        EVENTS,
        ECEPHYS,
        f"raw/{ECEPHYS}/sub-CS62/ses-1/sub-CS62_ses-1_events.tsv",
        "CS62",
        "1",
        None,
        "events",
        "tsv",
        "9KJ2xWcurrent",
        "2026-09-14 10:00:00.000",
        "9KJ2xWcurrent",
    ],
]


def _ask(ids: str, rows: Rows = ROWS) -> tuple[FakeAthena, int, dict[str, Any]]:
    fake = FakeAthena((COLS, rows))
    status, body = _get(fake, ROUTE, query={"ids": ids})
    return fake, status, body


def test_body_matches_the_golden() -> None:
    _, status, body = _ask(f"{NWB},{EVENTS},{T1W},{ABSENT}")
    assert status == 200
    check_golden("catalog-assets.golden.json", body)


def test_entries_follow_the_order_asked_and_name_the_missing() -> None:
    _, _, body = _ask(f"{EVENTS},{ABSENT},{NWB},{T1W}")
    assets = body["assets"]
    assert isinstance(assets, list)
    assert [a["asset_id"] for a in assets] == [EVENTS, NWB, T1W]
    assert body["missing"] == [ABSENT]


def test_current_is_whether_the_version_is_the_newest_of_its_file() -> None:
    _, _, body = _ask(f"{NWB},{T1W}")
    assets = body["assets"]
    assert isinstance(assets, list)
    nwb, t1w = assets
    assert (nwb["version"], nwb["current"]) == ("3HL4kqcurrent", True)
    assert (t1w["version"], t1w["current"]) == ("9KJ2xWold", False)


def test_entry_names_the_file_in_research_terms() -> None:
    _, _, body = _ask(NWB)
    assets = body["assets"]
    assert isinstance(assets, list)
    assert assets[0] == {
        "asset_id": NWB,
        "dataset": ECEPHYS,
        "subject": "CS62",
        "session": "1",
        "datatype": "ecephys",
        "suffix": "ecephys",
        "format": "nwb",
        "path": "sub-CS62/sub-CS62_ses-1_ecephys.nwb",
        "version": "3HL4kqcurrent",
        "committed_at": "2026-09-14 10:00:00.000",
        "current": True,
    }


def test_each_id_is_its_own_typed_parameter_and_duplicates_go_once() -> None:
    fake, status, _ = _ask(f"{NWB},{EVENTS},{NWB},")
    assert status == 200
    (started,) = fake.started
    assert started["QueryString"] == catalog_api.batch_sql(2)
    assert started["QueryString"].count("?") == 2
    assert started["ExecutionParameters"] == [render_param(NWB), render_param(EVENTS)]
    assert started["ExecutionParameters"] == [f"'{NWB}'", f"'{EVENTS}'"]


def test_a_hundred_ids_fit_athena_parameter_limits() -> None:
    ids = [str(uuid.UUID(int=i)) for i in range(catalog_api.MAX_BATCH_IDS)]
    fake, status, body = _ask(",".join(ids), rows=[])
    assert status == 200
    assert body["missing"] == ids
    (started,) = fake.started
    params = started["ExecutionParameters"]
    assert len(params) == catalog_api.MAX_BATCH_IDS
    assert all(len(p) <= ATHENA_PARAM_MAX for p in params)


def test_one_joined_parameter_would_be_refused_by_athena() -> None:
    # The teeth of the fake's limit: 100 ids joined into one value pass 1024 characters.
    ids = [str(uuid.UUID(int=i)) for i in range(catalog_api.MAX_BATCH_IDS)]
    assert len(render_param(",".join(ids))) > ATHENA_PARAM_MAX


def test_refuses_no_ids_too_many_and_a_bad_id() -> None:
    too_many = ",".join(
        str(uuid.UUID(int=i)) for i in range(catalog_api.MAX_BATCH_IDS + 1)
    )
    for ids in ("", ",", too_many, "a'b", f"{NWB},../x"):
        fake, status, body = _ask(ids)
        assert status == 400, ids
        assert body["error"] == "invalid_ids", ids
        assert fake.started == [], ids


def test_no_ids_parameter_is_a_400() -> None:
    fake = FakeAthena((COLS, ROWS))
    status, body = _get(fake, ROUTE)
    assert (status, body["error"]) == (400, "invalid_ids")


def test_curators_only_caller_is_refused() -> None:
    fake = FakeAthena((COLS, ROWS))
    status, _ = _get(fake, ROUTE, query={"ids": NWB}, groups="[curators]")
    assert status == 403
