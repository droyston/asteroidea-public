"""GET /catalog/studies and GET /catalog/links over a fake Athena.

Every fake cell is `VarCharValue` text typed by its column (arrays as "[a, b]"), as
Athena answers. The two route bodies are golden files the web rows parse: a test fails
when the committed file differs from the body (compared as parsed JSON, so biome's
layout does not matter). Regenerate with `GOLDEN_WRITE=1`, then
`npm --prefix web exec biome format -- --write e2e/fixtures/ux/`.
"""

import json
import os
from pathlib import Path
from typing import Any, Final

import pytest

from asteroidea_ingest import catalog_api

from .test_catalog_api import FakeAthena, Rows, _get

GOLDEN_DIR: Final = (
    Path(__file__).resolve().parents[2] / "web" / "e2e" / "fixtures" / "ux"
)
V, B, T, D = "varchar", "bigint", "timestamp", "double"
STUDIES = "GET /catalog/studies"
LINKS = "GET /catalog/links"
ECEPHYS, MRI = "dandi-000623", "openneuro-ds004798"
IMPORTER, CALLER, OTHER = "importer-sub", "user-1", "user-2"
WHEN = "2026-10-01 12:00:00.000"
LFP_PATH = "/processing/ecephys/LFP_micro/ElectricalSeries"

DATASET_COLS = [
    ("dataset", V),
    ("title", V),
    ("tier", V),
    ("license", V),
    ("doi", V),
    ("source_archive", V),
    ("source_dataset", V),
    ("source_version", V),
    ("ingest_method", V),
    ("created_by", V),
    ("created_at", T),
    ("asset_count", B),
    ("subject_count", B),
    ("total_bytes", B),
]


def _ds(
    name: str,
    made: tuple[str, str, str],
    counts: tuple[str, str, str],
    source: tuple[str, str, str] | None = None,
) -> list[str | None]:
    """One datasets row; `made` is (tier, ingest_method, created_by)."""
    tier, method, by = made
    archive, dataset, version = source or (None, None, None)
    return [
        name,
        f"Title of {name}",
        tier,
        "CC-BY-4.0" if source else None,
        None,
        archive,
        dataset,
        version,
        method,
        by,
        WHEN,
        *counts,
    ]


DATASET_ROWS: Rows = [
    _ds(
        "dandi-000027",
        ("sandbox", "import", IMPORTER),
        ("1", "1", "10"),
        ("dandi", "000027", "0.210831.2033"),
    ),
    _ds(
        "dandi-000576",
        ("curated", "import", IMPORTER),
        ("2", "1", "2000"),
        ("dandi", "000576", "0.231010.1811"),
    ),
    _ds(
        ECEPHYS,
        ("curated", "import", IMPORTER),
        ("2", "2", "9000"),
        ("dandi", "000623", "0.240827.1809"),
    ),
    _ds("my-upload", ("upload", "upload", CALLER), ("1", "1", "5")),
    _ds(
        MRI,
        ("curated", "import", IMPORTER),
        ("6", "2", "80000"),
        ("openneuro", "ds004798", "1.0.1"),
    ),
    _ds("their-upload", ("upload", "upload", OTHER), ("1", "1", "7")),
]

SUBJECT_ROWS: Rows = [
    ["dandi-000027", "anm1", None, None, None],
    ["dandi-000576", "01", "Homo sapiens", "F", "P30Y"],
    [ECEPHYS, "CS41", "Homo sapiens", "M", "P41Y"],
    [ECEPHYS, "CS62", "Homo sapiens", "F", "P62Y"],
    ["my-upload", "u1", None, None, None],
    [MRI, "CS41", None, None, None],
    [MRI, "CS62", None, None, None],
    ["their-upload", "u2", None, None, None],
]
KIND_ROWS: Rows = [
    [MRI, "CS41", None, "scans", "tsv"],
    [MRI, "CS41", "anat", "T1w", "nifti"],
    [MRI, "CS41", "func", "bold", "nifti"],
    [MRI, "CS41", "func", "events", "tsv"],
    [MRI, "CS62", "anat", "T1w", "nifti"],
    ["my-upload", "u1", None, None, "csv"],
    ["their-upload", "u2", "beh", "beh", "tsv"],
]
SESSION_ASSET_ROWS: Rows = [
    ["dandi-000027", "anm1", None, "a27"],
    ["dandi-000576", "01", "1", "a576"],
    ["dandi-000576", "01", "2", "a576b"],
    [ECEPHYS, "CS41", "1", "e41"],
    [ECEPHYS, "CS62", "1", "e62"],
    ["my-upload", "u1", None, "up1"],
    [MRI, "CS41", None, "m41-scans"],
    [MRI, "CS41", None, "m41-t1"],
    [MRI, "CS41", None, "m41-bold"],
    [MRI, "CS41", None, "m41-events"],
    [MRI, "CS62", None, "m62-t1"],
    [MRI, "CS62", None, "m62-t1json"],
    ["their-upload", "u2", None, "up2"],
]
NWB_ROWS: Rows = [
    ["dandi-000027", "anm1", None, "a27", "ecephys", None, None],
    ["dandi-000576", "01", "1", "a576", "ecephys", "12", "0"],
    ["dandi-000576", "01", "2", "a576b", "ecephys", "0", "0"],
    [ECEPHYS, "CS41", "1", "e41", "ecephys+image", "44", "120"],
    [ECEPHYS, "CS62", "1", "e62", "ecephys+image", "0", "90"],
]
LFP_ROWS: Rows = [
    ["dandi-000576", "01", "a576b", LFP_PATH, "[360000, 8]", "1000.0"],
    [ECEPHYS, "CS41", "e41", LFP_PATH, "[3600000, 64]", "1000.0"],
]
UNIT_REGION_ROWS: Rows = [
    ["dandi-000576", "hippocampus", "12"],
    [ECEPHYS, "amygdala left", "20"],
    [ECEPHYS, "hippocampus right", "24"],
]
CHANNEL_REGION_ROWS: Rows = [
    ["dandi-000576", "hippocampus", "8"],
    [ECEPHYS, "Left ACC", "8"],
    [ECEPHYS, "amygdala left", "32"],
    [ECEPHYS, "hippocampus right", "32"],
]
STUDY_ROWS: Rows = [
    [
        "bmovie",
        "Human single neurons and fMRI during movie watching",
        "Keles, Rutishauser",
        "Rutishauser lab",
        "Keles et al. (2024)",
        "10.1038/s41597-024-03029-1",
    ],
]
STUDY_DATASET_ROWS: Rows = [["bmovie", ECEPHYS, "ephys"], ["bmovie", MRI, "mri"]]
LINK_COLS = [
    ("dataset", V),
    ("subject", V),
    ("subject_key", V),
    ("status", V),
    ("linked_by", V),
    ("linked_at", T),
    ("basis", V),
    ("note", V),
]
LINK_ROWS: Rows = [
    [ECEPHYS, "CS41", "bmovie-CS41", "pending", None, None, None, None],
    [ECEPHYS, "CS62", "bmovie-CS62", "pending", None, None, None, "age differs"],
    [MRI, "CS41", "bmovie-CS41", "pending", None, None, None, None],
    [MRI, "CS62", "bmovie-CS62", "pending", None, None, None, None],
]


def studies_fake() -> FakeAthena:
    return FakeAthena(
        (
            [(n, V) for n in ("study", "title", "authors", "lab", "citation", "doi")],
            STUDY_ROWS,
        ),
        ([("study", V), ("dataset", V), ("role", V)], STUDY_DATASET_ROWS),
        (DATASET_COLS, DATASET_ROWS),
        (
            [(n, V) for n in ("dataset", "subject", "species", "sex", "age")],
            SUBJECT_ROWS,
        ),
        (
            [(n, V) for n in ("dataset", "subject", "datatype", "suffix", "format")],
            KIND_ROWS,
        ),
        (
            [(n, V) for n in ("dataset", "subject", "bids_session", "asset_id")],
            SESSION_ASSET_ROWS,
        ),
        (
            [
                *[
                    (n, V)
                    for n in (
                        "dataset",
                        "subject",
                        "bids_session",
                        "asset_id",
                        "suffix",
                    )
                ],
                ("n_units", B),
                ("n_trials", B),
            ],
            NWB_ROWS,
        ),
        (
            [
                *[(n, V) for n in ("dataset", "subject", "asset_id", "path")],
                ("shape", "array"),
                ("rate_hz", D),
            ],
            LFP_ROWS,
        ),
        ([("dataset", V), ("location", V), ("units", B)], UNIT_REGION_ROWS),
        ([("dataset", V), ("location", V), ("channels", B)], CHANNEL_REGION_ROWS),
    )


def check_golden(name: str, body: dict[str, Any]) -> None:
    """Fail when the committed golden differs from `body`; GOLDEN_WRITE=1 rewrites."""
    path = GOLDEN_DIR / name
    if os.environ.get("GOLDEN_WRITE") == "1":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(body, indent=2) + "\n")
    assert path.exists(), f"{path} is missing; run with GOLDEN_WRITE=1"
    committed = json.loads(path.read_text())
    assert committed == body, f"{path} differs from the body; GOLDEN_WRITE=1 rewrites"


def _by_name(items: list[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    return {str(item[key]): item for item in items}


def test_studies_with_no_stored_summary_runs_three_texts_then_the_seven() -> None:
    # No summary store: every dataset (six, over the scoped fallback's two) takes
    # its entry from the seven unscoped texts, run together after the table reads.
    fake = studies_fake()
    status, _ = _get(fake, STUDIES)
    assert status == 200
    assert [s["QueryString"] for s in fake.started] == [
        catalog_api.STUDIES_SQL,
        catalog_api.STUDY_DATASETS_SQL,
        catalog_api.DATASETS_SQL,
        *catalog_api.ENTRY_SQL,
    ]
    assert catalog_api.ENTRY_SQL == (
        catalog_api.ALL_SUBJECTS_SQL,
        catalog_api.KINDS_SQL,
        catalog_api.SESSION_ASSETS_SQL,
        catalog_api.NWB_SQL,
        catalog_api.LFP_SQL,
        catalog_api.UNIT_REGIONS_SQL,
        catalog_api.CHANNEL_REGIONS_SQL,
    )
    assert all("ExecutionParameters" not in s for s in fake.started)
    # two rounds: the three start before any poll, then the seven start together
    assert fake.events[:3] == ["start"] * 3
    second = fake.events.index("start", 3)
    assert fake.events[second : second + 7] == ["start"] * 7
    assert fake.events.count("start") == 10
    assert f"n <= {catalog_api.SESSION_ASSET_IDS}" in catalog_api.SESSION_ASSETS_SQL


def test_studies_body_matches_the_golden() -> None:
    status, body = _get(studies_fake(), STUDIES)
    assert status == 200
    check_golden("catalog-studies.golden.json", body)


def test_studies_one_study_two_releases_and_a_dataset_in_no_study() -> None:
    _, body = _get(studies_fake(), STUDIES)
    (study,) = body["studies"]
    assert study["study"] == "bmovie"
    assert study["datasets"] == [
        {"dataset": ECEPHYS, "role": "ephys"},
        {"dataset": MRI, "role": "mri"},
    ]
    assert "note" not in study
    in_a_study = {d["dataset"] for d in study["datasets"]}
    names = [d["dataset"] for d in body["datasets"]]
    assert "dandi-000576" in names
    assert "dandi-000576" not in in_a_study


def test_studies_mine_compares_created_by_with_the_caller_and_hides_it() -> None:
    _, body = _get(studies_fake(), STUDIES)
    datasets = _by_name(body["datasets"], "dataset")
    assert datasets["my-upload"]["mine"] is True
    assert datasets["their-upload"]["mine"] is False
    assert datasets[ECEPHYS]["mine"] is False
    assert all("created_by" not in d for d in body["datasets"])
    text = json.dumps(body)
    assert IMPORTER not in text
    assert OTHER not in text
    assert CALLER not in text
    tiers = {name: d["tier"] for name, d in datasets.items()}
    assert tiers["dandi-000027"] == "sandbox"
    assert tiers["my-upload"] == "upload"


def test_studies_subjects_modalities_sessions_and_lfp() -> None:
    _, body = _get(studies_fake(), STUDIES)
    datasets = _by_name(body["datasets"], "dataset")
    cs41 = _by_name(datasets[ECEPHYS]["subjects"], "subject")["CS41"]
    assert cs41["modalities"] == ["units", "lfp", "task"]
    assert (cs41["units"], cs41["trials"]) == (44, 120)
    assert cs41["sessions"] == [
        {
            "session": "1",
            "asset_ids": ["e41"],
            "nwb": [{"asset_id": "e41", "n_units": 44}],
        }
    ]
    assert cs41["lfp"] == [
        {"asset_id": "e41", "path": LFP_PATH, "channels": 64, "rate_hz": 1000.0}
    ]
    mri41 = _by_name(datasets[MRI]["subjects"], "subject")["CS41"]
    # the MRI-only session has no NWB; an unseen kind of file is other:, never dropped
    assert mri41["modalities"] == ["anat", "func", "task", "other:scans"]
    (session,) = mri41["sessions"]
    assert session["session"] == "_"
    assert session["nwb"] == []
    assert session["asset_ids"] == ["m41-scans", "m41-t1", "m41-bold", "m41-events"]
    assert mri41["lfp"] == []
    # an NWB with no units, trials or LFP is other: its suffix
    anm1 = datasets["dandi-000027"]["subjects"][0]
    assert anm1["modalities"] == ["other:ecephys"]
    assert anm1["sessions"][0]["nwb"] == [{"asset_id": "a27", "n_units": None}]
    their = datasets["their-upload"]["subjects"][0]
    assert their["modalities"] == ["other:beh"]


def test_studies_regions_are_verbatim_labels_with_units_and_channels() -> None:
    _, body = _get(studies_fake(), STUDIES)
    datasets = _by_name(body["datasets"], "dataset")
    assert datasets[ECEPHYS]["regions"] == [
        {"region": "Left ACC", "units": 0, "channels": 8},
        {"region": "amygdala left", "units": 20, "channels": 32},
        {"region": "hippocampus right", "units": 24, "channels": 32},
    ]
    assert datasets[MRI]["regions"] == []


def test_file_kind_names_and_other() -> None:
    assert catalog_api.file_kind("func", "events", "tsv") == "task"
    assert catalog_api.file_kind("ieeg", "ieeg", "edf") == "ieeg"
    assert catalog_api.file_kind("dwi", "dwi", "nifti") == "other:dwi"
    assert catalog_api.file_kind(None, "scans", "tsv") == "other:scans"
    assert catalog_api.file_kind(None, None, "csv") == "other:csv"
    assert catalog_api.file_kind(None, None, None) == "other:unknown"


def test_studies_413_over_the_body_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(catalog_api, "MAX_BODY_BYTES", 100)
    status, body = _get(studies_fake(), STUDIES)
    assert status == 413
    assert body["error"] == "too_large"


def links_fake(rows: Rows = LINK_ROWS) -> FakeAthena:
    return FakeAthena((LINK_COLS, rows))


def test_links_body_matches_the_golden() -> None:
    fake = links_fake()
    status, body = _get(fake, LINKS, query={"study": "bmovie"})
    assert status == 200
    check_golden("catalog-links.golden.json", body)
    assert body["links"][0] == {
        "dataset": ECEPHYS,
        "subject": "CS41",
        "subject_key": "bmovie-CS41",
        "status": "pending",
        "linked_by": None,
        "linked_at": None,
        "basis": None,
        "note": None,
    }


@pytest.mark.parametrize(
    ("query", "form", "values"),
    [
        (None, (), []),
        ({"study": "bmovie"}, ("study",), ["'bmovie'"]),
        ({"subject_key": "bmovie-CS41"}, ("subject_key",), ["'bmovie-CS41'"]),
        (
            {"dataset": ECEPHYS, "subject": "CS41"},
            ("dataset", "subject"),
            [f"'{ECEPHYS}'", "'CS41'"],
        ),
        ({"subject_key": "o'brien"}, ("subject_key",), ["'o''brien'"]),
    ],
)
def test_links_each_form_binds_its_values_as_typed_literals(
    query: dict[str, str] | None, form: tuple[str, ...], values: list[str]
) -> None:
    fake = links_fake()
    status, _ = _get(fake, LINKS, query=query)
    assert status == 200
    (started,) = fake.started
    assert started["QueryString"] == catalog_api.LINKS_SQL[form]
    assert started.get("ExecutionParameters", []) == values


def test_links_texts_read_one_row_past_the_cap() -> None:
    assert catalog_api.MAX_LINKS == 5000
    for sql in catalog_api.LINKS_SQL.values():
        assert sql.endswith(f"LIMIT {catalog_api.MAX_LINKS + 1}")
        assert "coalesce(status, 'pending')" in sql


def test_links_5000_rows_pass_and_5001_are_too_many() -> None:
    row = LINK_ROWS[0]
    status, body = _get(links_fake([row] * 5000), LINKS)
    assert (status, len(body["links"])) == (200, 5000)
    status, body = _get(links_fake([row] * 5001), LINKS)
    assert (status, body["error"]) == (400, "too_many")


@pytest.mark.parametrize(
    "query",
    [
        {"study": "bmovie", "subject_key": "k"},
        {"dataset": ECEPHYS},
        {"subject": "CS41"},
        {"study": "bmovie", "dataset": ECEPHYS, "subject": "CS41"},
        {"dataset": "Bad Name", "subject": "CS41"},
        {"study": "x" * 65},
    ],
)
def test_links_refuses_other_forms_before_any_query(query: dict[str, str]) -> None:
    fake = links_fake()
    status, body = _get(fake, LINKS, query=query)
    assert (status, body["error"]) == (400, "invalid_request")
    assert fake.started == []


def test_links_query_ignores_empty_values() -> None:
    sql, values = catalog_api.links_query({"study": "", "subject_key": "k"})
    assert (sql, values) == (catalog_api.LINKS_SQL[("subject_key",)], ["k"])


def test_links_413_over_the_body_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(catalog_api, "MAX_BODY_BYTES", 100)
    status, body = _get(links_fake(), LINKS)
    assert (status, body["error"]) == (413, "too_large")


def test_datasets_list_gains_tier_and_mine_without_created_by() -> None:
    fake = FakeAthena((DATASET_COLS, DATASET_ROWS))
    status, body = _get(fake, "GET /catalog/datasets")
    assert status == 200
    datasets = _by_name(body["datasets"], "dataset")
    assert datasets["my-upload"]["mine"] is True
    assert datasets["their-upload"]["mine"] is False
    assert datasets["dandi-000027"]["tier"] == "sandbox"
    assert all("created_by" not in d for d in body["datasets"])
    assert "ExecutionParameters" not in fake.started[0]


def test_datasets_text_reads_a_null_tier_by_ingest_method() -> None:
    sql = catalog_api.DATASETS_SQL
    assert (
        "coalesce(d.tier, CASE WHEN d.ingest_method = 'import' THEN 'curated' "
        "ELSE 'upload' END) AS tier"
    ) in sql
