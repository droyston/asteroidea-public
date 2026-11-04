"""Curator CLI, offline.

Fixtures under `fixtures/curate/` were recorded once by anonymous GETs
of the live APIs (DANDI REST `/info/` and `/assets/?metadata=true`, OpenNeuro
GraphQL `snapshot` trees, an anonymous ListObjectsV2 of `openneuro.org/ds004798/`,
and bucket-root HEADs), then trimmed to the fields and keys the CLI reads.
"""

import copy
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import pytest
from pyiceberg.catalog.sql import SqlCatalog

from asteroidea_infra import curate, schema
from asteroidea_infra.catalog_init import iceberg_schema
from asteroidea_infra.curate import (
    CurateDeps,
    CurateError,
    ImportRequest,
    Runner,
    Source,
)
from asteroidea_ingest import import_file, import_plan
from asteroidea_ingest.catalog_writer import CatalogWriter

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

FIXTURES: Final = Path(__file__).parent / "fixtures" / "curate"
REPO: Final = Path(__file__).resolve().parents[2]
DANDI: Final = Source.parse("dandi:000623@0.240227.2023")
OPENNEURO: Final = Source.parse("openneuro:ds004798@1.0.5")
DANDI_BASE: Final = (
    "https://api.dandiarchive.org/api/dandisets/000623/versions/0.240227.2023"
)


def _load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


class FakeWeb:
    """`curate.Web` over the recorded fixtures; records every call."""

    def __init__(self, regions: Mapping[str, str] | None = None) -> None:
        self.info = _load("dandi_000623_0.240227.2023_info.json")
        self.assets = _load("dandi_000623_0.240227.2023_assets.json")
        self.trees = _load("openneuro_ds004798_1.0.5_graphql.json")
        self.listing = _load("openneuro_ds004798_list.json")
        self.regions = dict(regions or _load("bucket_regions.json"))
        self.calls: list[str] = []

    def get_json(self, url: str) -> dict[str, Any]:
        self.calls.append(url)
        if url == f"{DANDI_BASE}/info/":
            return copy.deepcopy(self.info)
        if url.startswith(f"{DANDI_BASE}/assets/"):
            return copy.deepcopy(self.assets)
        msg = f"unrecorded GET {url}"
        raise AssertionError(msg)

    def post_json(self, url: str, body: dict[str, Any]) -> dict[str, Any]:
        assert url == import_plan.OPENNEURO_GRAPHQL
        query = body["query"]
        self.calls.append(query)
        if "description" in query:
            name = self.info["name"].replace("Data for", "fMRI data for")
            desc = {"Name": name, "License": "CC0"}
            return {"data": {"snapshot": {"description": desc}}}
        tree = query.split('tree: "')[1].split('"')[0] if "tree:" in query else "root"
        return copy.deepcopy(self.trees[tree])

    def list_objects(self, bucket: str, prefix: str) -> dict[str, int]:
        self.calls.append(f"list {bucket} {prefix}")
        return {o["Key"]: o["Size"] for o in self.listing["Contents"]}

    def bucket_region(self, bucket: str) -> str:
        self.calls.append(f"region {bucket}")
        return self.regions[bucket]


class FakeSessions:
    """Each invoked session reaches COMMITTED and EXTRACTED on its first poll."""

    def __init__(self, final: Mapping[str, Any] | None = None) -> None:
        self.invoked: list[dict[str, Any]] = []
        self.final = dict(final or {"state": "COMMITTED", "extract_state": "EXTRACTED"})
        self.now = 0.0

    def invoke(self, event: dict[str, Any]) -> None:
        self.invoked.append(event)

    def get(self, _sid: str) -> dict[str, Any] | None:
        return dict(self.final)

    def runner(self) -> Runner:
        def sleep(seconds: float) -> None:
            self.now += seconds

        return Runner(
            invoke=self.invoke,
            get_session=self.get,
            clock=lambda: self.now,
            sleep=sleep,
        )


def _deps(
    web: FakeWeb,
    fake: FakeSessions,
    tmp_path: Path,
    existing: Mapping[str, str | None] | None = None,
) -> CurateDeps:
    return CurateDeps(
        web=web,
        existing=lambda _ds: dict(existing or {}),
        runner=fake.runner(),
        imports_dir=tmp_path,
    )


def _request(source: Source, include: str, extra: Any = None) -> ImportRequest:
    globs = tuple(g for g in include.split(",") if g)
    return ImportRequest(source=source, include=globs, extra=extra, caller="curator")


# -- sources, listings, metadata ---------------------------------------------------


def test_source_names_the_dataset_archive_dash_id() -> None:
    assert DANDI.dataset == "dandi-000623"
    assert OPENNEURO.dataset == "openneuro-ds004798"


@pytest.mark.parametrize("bad", ["figshare:1@1", "dandi:000623", "dandi@1", "x"])
def test_unknown_source_exits_2(bad: str) -> None:
    with pytest.raises(CurateError) as err:
        Source.parse(bad)
    assert err.value.code == import_plan.EXIT_REFUSED


def test_dandi_listing_reads_title_licence_digest_and_s3_url() -> None:
    plan = import_plan.dandi_plan(FakeWeb(), DANDI)
    assert plan.dataset_meta["license"] == "CC-BY-4.0"
    assert plan.dataset_meta["title"].startswith("Data for: Multimodal single-neuron")
    assert len(plan.files) == 29
    (cs62r2,) = [
        f for f in plan.files if f.relpath.endswith("P62CSR2_behavior+ecephys.nwb")
    ]
    assert cs62r2.bucket == "dandiarchive"
    assert cs62r2.source["url"].startswith(
        "https://dandiarchive.s3.amazonaws.com/blobs/"
    )
    assert cs62r2.source["digest"].startswith("sha2-256:")


def test_include_globs_select_paths() -> None:
    plan = import_plan.dandi_plan(FakeWeb(), DANDI)
    chosen = [
        f.relpath
        for f in plan.files
        if import_plan.included(f.relpath, ["sub-CS41/*", "sub-CS62/*"])
    ]
    assert len(chosen) == 4
    assert all(p.startswith(("sub-CS41/", "sub-CS62/")) for p in chosen)
    one = [
        f.relpath
        for f in plan.files
        if import_plan.included(f.relpath, ["sub-CS62/*P62CSR2*"])
    ]
    assert one == ["sub-CS62/sub-CS62_ses-P62CSR2_behavior+ecephys.nwb"]
    assert import_plan.included("anything", [])


def test_a_missing_licence_exits_1_and_invokes_nothing(tmp_path: Path) -> None:
    web = FakeWeb()
    web.info["metadata"]["license"] = []
    fake = FakeSessions()
    with pytest.raises(CurateError, match="no license; nothing invoked") as err:
        curate.import_source(_request(DANDI, "sub-CS62/*"), _deps(web, fake, tmp_path))
    assert err.value.code == curate.EXIT_FAIL
    assert fake.invoked == []
    assert not any(c.startswith(f"{DANDI_BASE}/assets/") for c in web.calls)
    assert list(tmp_path.iterdir()) == []  # no manifest either


def test_a_missing_title_exits_1() -> None:
    web = FakeWeb()
    web.info["name"] = ""
    with pytest.raises(CurateError, match="no title"):
        import_plan.dandi_plan(web, DANDI)


def test_openneuro_listing_matches_the_snapshot() -> None:
    include = ["dataset_description.json", "participants.tsv", "README", "sub-p62cs/*"]
    plan = import_plan.openneuro_plan(FakeWeb(), OPENNEURO, include)
    assert plan.dataset_meta["license"] == "CC0"
    assert len(plan.files) == 29  # 3 top-level files + 26 of sub-p62cs
    nii = [f for f in plan.files if f.relpath.endswith("_bold.nii.gz")]
    assert nii
    assert nii[0].source["key"] == f"ds004798/{nii[0].relpath}"


def test_an_openneuro_snapshot_mismatch_exits_1() -> None:
    web = FakeWeb()
    contents = web.listing["Contents"]
    first = next(o for o in contents if o["Key"].startswith("ds004798/sub-p62cs/"))
    first["Size"] += 1  # the bucket moved past the pinned snapshot
    with pytest.raises(CurateError, match="snapshot check failed") as err:
        import_plan.openneuro_plan(web, OPENNEURO, ["sub-p62cs/*"])
    assert err.value.code == curate.EXIT_FAIL


def test_an_openneuro_annex_uuid_in_the_bucket_is_not_a_snapshot_file() -> None:
    web = FakeWeb()
    web.listing["Contents"].append({"Key": "ds004798/annex-uuid", "Size": 36})
    include = ["README", "annex-uuid"]  # a glob that selects the bookkeeping key
    plan = import_plan.openneuro_plan(web, OPENNEURO, include)
    assert "annex-uuid" not in {f.relpath for f in plan.files}


def test_an_openneuro_extra_data_file_still_exits_1() -> None:
    web = FakeWeb()
    web.listing["Contents"].append({"Key": "ds004798/annex-uuid", "Size": 36})
    web.listing["Contents"].append(
        {"Key": "ds004798/sub-p62cs/anat/extra.nii.gz", "Size": 7}
    )
    with pytest.raises(CurateError, match=r"extra\.nii\.gz") as err:
        import_plan.openneuro_plan(web, OPENNEURO, ["README", "sub-p62cs/*"])
    assert err.value.code == curate.EXIT_FAIL


# -- mode selection -------------------------------------------------------------


def test_mode_is_s3_for_a_bucket_in_the_platform_region_else_https() -> None:
    web = FakeWeb()
    dandi = import_plan.dandi_plan(web, DANDI).files[:2]
    neuro = import_plan.openneuro_plan(web, OPENNEURO, ["README"]).files
    modes = curate.choose_modes(web, [*dandi, *neuro])
    assert {modes[f.relpath] for f in dandi} == {import_file.MODE_HTTPS}  # us-east-2
    assert {modes[f.relpath] for f in neuro} == {import_file.MODE_S3}  # us-east-1
    assert web.calls.count("region dandiarchive") == 1  # once per bucket


def test_mode_follows_the_region_not_the_archive_name() -> None:
    web = FakeWeb(regions={"dandiarchive": "us-east-1", "openneuro.org": "eu-west-1"})
    dandi = import_plan.dandi_plan(web, DANDI).files[:1]
    neuro = import_plan.openneuro_plan(web, OPENNEURO, ["README"]).files
    modes = curate.choose_modes(web, [*dandi, *neuro])
    assert modes[dandi[0].relpath] == import_file.MODE_S3
    assert modes[neuro[0].relpath] == import_file.MODE_HTTPS


def test_ancillary_files_always_stream() -> None:
    extra = json.loads((REPO / "curation/imports/000623-stimuli.json").read_text())
    files = import_plan.extra_files({**extra, "dataset": "000623"})
    assert curate.choose_modes(FakeWeb(regions={}), files) == {
        "stimuli/scenecut_info.csv": import_file.MODE_HTTPS
    }
    (csv_file,) = files
    assert csv_file.ancillary
    assert csv_file.size == 4399
    assert csv_file.source["license"] == "BSD-3-Clause"
    assert "8c35290b74a9522bf27cbcae44dcb88ff2a1d58f" in csv_file.source["url"]


# -- the run --------------------------------------------------------------------------


def test_import_writes_the_manifest_invokes_each_file_and_passes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    extra = json.loads((REPO / "curation/imports/000623-stimuli.json").read_text())
    fake = FakeSessions()
    code = curate.import_source(
        _request(DANDI, "sub-CS41/*,sub-CS62/*", extra),
        _deps(FakeWeb(), fake, tmp_path),
    )
    assert code == 0
    (run_dir,) = tmp_path.iterdir()
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert set(manifest) == {"run_id", "source", "dataset", "dataset_meta", "files"}
    assert manifest["run_id"] == run_dir.name
    assert manifest["source"] == "dandi:000623@0.240227.2023"
    assert manifest["dataset"] == "dandi-000623"
    assert len(manifest["files"]) == 5
    event = manifest["files"][0]
    assert set(event) == {
        "mode",
        "dataset",
        "relpath",
        "size",
        "source",
        "dataset_meta",
        "caller",
        "ancillary",
        "session_id",
    }
    assert event["session_id"] == import_file.session_id(
        "dandi-000623", event["relpath"], "0.240227.2023"
    )
    # Each invoke is the ImportFunction event; ImportFunction accepts every one.
    assert len(fake.invoked) == 5
    for invoked in fake.invoked:
        assert "session_id" not in invoked
        import_file.ImportEvent.parse(invoked)
    out = capsys.readouterr().out
    assert sum(line.startswith("PASS ") for line in out.splitlines()) == 5
    assert "summary: 5 PASS (0 already imported), 0 FAIL" in out


def test_a_rerun_prints_already_imported_and_invokes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeSessions()
    relpath = "sub-CS62/sub-CS62_ses-P62CSR2_behavior+ecephys.nwb"
    code = curate.import_source(
        _request(DANDI, "sub-CS62/*P62CSR2*"),
        _deps(FakeWeb(), fake, tmp_path, existing={relpath: "0.240227.2023"}),
    )
    assert code == 0
    assert fake.invoked == []
    assert f"PASS {relpath} already imported" in capsys.readouterr().out


def test_a_second_version_is_refused_with_exit_2(tmp_path: Path) -> None:
    fake = FakeSessions()
    relpath = "sub-CS62/sub-CS62_ses-P62CSR2_behavior+ecephys.nwb"
    with pytest.raises(CurateError, match="second version") as err:
        curate.import_source(
            _request(DANDI, "sub-CS62/*"),
            _deps(FakeWeb(), fake, tmp_path, existing={relpath: "0.230101.0000"}),
        )
    assert err.value.code == import_plan.EXIT_REFUSED
    assert fake.invoked == []  # refused before any invoke


def test_a_failed_file_prints_fail_and_exits_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeSessions(
        final={"state": "INCOMPLETE", "error": {"reason": "source digest mismatch"}}
    )
    code = curate.import_source(
        _request(DANDI, "sub-CS62/*P62CSR2*"), _deps(FakeWeb(), fake, tmp_path)
    )
    assert code == curate.EXIT_FAIL
    out = capsys.readouterr().out
    assert "FAIL sub-CS62/sub-CS62_ses-P62CSR2_behavior+ecephys.nwb INCOMPLETE" in out
    assert "source digest mismatch" in out


def test_a_file_that_never_finishes_fails_after_30_minutes() -> None:
    fake = FakeSessions(final={"state": "COMMITTED", "extract_state": "PENDING"})
    lines = curate.run(
        import_plan.events(
            import_plan.dandi_plan(FakeWeb(), DANDI),
            import_plan.dandi_plan(FakeWeb(), DANDI).files[:1],
            {import_plan.dandi_plan(FakeWeb(), DANDI).files[0].relpath: "https"},
            "curator",
        ),
        fake.runner(),
    )
    (line,) = lines.values()
    assert line.startswith("FAIL timed out after 30 min")
    assert "extract_state=PENDING" in line
    assert fake.now > curate.FILE_TIMEOUT_SECONDS


def test_at_most_four_imports_are_in_flight() -> None:
    plan = import_plan.dandi_plan(FakeWeb(), DANDI)
    files = plan.files[:9]
    planned = import_plan.events(
        plan, files, dict.fromkeys((f.relpath for f in files), "https"), "c"
    )
    polls: dict[str, int] = {}
    in_flight: list[int] = []
    invoked: set[str] = set()
    done: set[str] = set()

    def invoke(event: dict[str, Any]) -> None:
        invoked.add(event["relpath"])
        in_flight.append(len(invoked - done))

    sids = {e["session_id"]: e["relpath"] for e in planned}

    def get(sid: str) -> dict[str, Any] | None:
        polls[sid] = polls.get(sid, 0) + 1
        if polls[sid] < 2:
            return None
        done.add(sids[sid])
        return {"state": "COMMITTED", "extract_state": "EXTRACTED"}

    clock = [0.0]
    runner = Runner(
        invoke=invoke,
        get_session=get,
        clock=lambda: clock[0],
        sleep=lambda s: clock.__setitem__(0, clock[0] + s),
    )
    results = curate.run(planned, runner)
    assert len(results) == 9
    assert max(in_flight) == 4  # at most 4 in flight (a literal, not the constant)


# -- crosswalk, studies, tiers --------------------------------------


def _naive(hour: int, minute: int, second: int) -> datetime:
    """Return 2026-10-24 at the given UTC time, without tzinfo (a `timestamp`)."""
    moment = datetime(2026, 10, 24, hour, minute, second, tzinfo=UTC)
    return moment.replace(tzinfo=None)


LINK_HEADER: Final = "dataset,subject,subject_key,status,linked_by,linked_at,basis,note"


@pytest.fixture
def catalog(tmp_path: Path) -> Iterator[SqlCatalog]:
    cat = SqlCatalog(
        "t", uri=f"sqlite:///{tmp_path}/c.db", warehouse=f"file://{tmp_path}/w"
    )
    cat.create_namespace("asteroidea")
    for name in ("subject_links", "datasets", "studies", "study_datasets"):
        spec = schema.SPECS[name]
        cat.create_table(f"asteroidea.{spec.name}", iceberg_schema(spec))
    yield cat
    cat.engine.dispose()


def _rows(catalog: SqlCatalog, name: str) -> list[dict[str, Any]]:
    table = catalog.load_table(f"asteroidea.{name}")
    return list(table.scan().to_arrow().to_pylist())


def _link(**over: str) -> str:
    row = {
        "dataset": "ds-a",
        "subject": "sub-1",
        "subject_key": "k1",
        "status": "pending",
        "linked_by": "me@example.org",
        "linked_at": "2026-10-24T12:53:02-04:00",
        "basis": "why",
        "note": "n",
    }
    row.update(over)
    return LINK_HEADER + "\n" + ",".join(row.values()) + "\n"


def test_the_bmovie_crosswalk_links_two_patients_across_both_datasets(
    catalog: SqlCatalog,
) -> None:
    text = (REPO / "curation/subject-links/bmovie.csv").read_text()
    rows = curate.crosswalk_rows(text, curated_by="curator")
    assert {(r["dataset"], r["subject"]) for r in rows} == {
        ("dandi-000623", "sub-CS41"),
        ("openneuro-ds004798", "sub-p41cs"),
        ("dandi-000623", "sub-CS62"),
        ("openneuro-ds004798", "sub-p62cs"),
    }
    keys = {r["subject"]: r["subject_key"] for r in rows}
    assert (
        keys["sub-CS41"] == keys["sub-p41cs"] != keys["sub-CS62"] == keys["sub-p62cs"]
    )
    assert all(r["status"] == "pending" and r["basis"] for r in rows)
    assert all(r["linked_at"] == _naive(16, 53, 2) for r in rows)
    assert curate.crosswalk(catalog, rows) == curate.Counts(4, 0, 0)
    assert curate.crosswalk(catalog, rows) == curate.Counts(0, 0, 4)


def test_the_committed_bmovie_csv_has_no_email_placeholder() -> None:
    text = (REPO / "curation/subject-links/bmovie.csv").read_text()
    assert "OWNER_SIGN_IN_EMAIL" not in text


def test_a_changed_link_is_replaced_and_an_unchanged_one_kept(
    catalog: SqlCatalog,
) -> None:
    first = curate.crosswalk_rows(_link(), curated_by="c")
    assert curate.crosswalk(catalog, first) == curate.Counts(1, 0, 0)
    confirmed = curate.crosswalk_rows(_link(status="confirmed"), curated_by="c")
    assert curate.crosswalk(catalog, confirmed) == curate.Counts(0, 1, 0)
    assert [r["status"] for r in _rows(catalog, "subject_links")] == ["confirmed"]
    assert curate.crosswalk(catalog, confirmed) == curate.Counts(0, 0, 1)


@pytest.mark.parametrize(
    "text",
    [
        "dataset,subject,subject_key,note\nds,sub-1,k,n\n",  # the old header
        _link(dataset="Bad"),
        _link(subject="one"),
        _link(subject_key=""),
        _link(status="maybe"),
        _link(linked_at="yesterday"),
        _link(linked_at="2026-10-24T12:53:02"),  # no offset
    ],
)
def test_a_malformed_crosswalk_is_refused(text: str) -> None:
    with pytest.raises(CurateError):
        curate.crosswalk_rows(text, curated_by="c")


def test_the_email_placeholder_is_refused_with_a_sentence() -> None:
    with pytest.raises(CurateError, match="put your sign-in email"):
        curate.crosswalk_rows(_link(linked_by="OWNER_SIGN_IN_EMAIL"), curated_by="c")


STUDY_HEADER: Final = "study,title,authors,lab,citation,doi,note"
SD_HEADER: Final = "study,dataset,role"


def _studies(*lines: str) -> str:
    return "\n".join([STUDY_HEADER, *lines]) + "\n"


def _sds(*lines: str) -> str:
    return "\n".join([SD_HEADER, *lines]) + "\n"


def test_studies_make_both_tables_equal_the_csvs(catalog: SqlCatalog) -> None:
    studies = curate.studies_rows(
        _studies("movie,Movie,A;B,Lab,Cite,10.1/x,", "mouse,Mouse,,,,,n"),
        _sds("movie,ds-a,ephys", "movie,ds-b,fmri", "mouse,ds-c,"),
        curated_by="c",
    )
    assert curate.load_studies(catalog, studies) == (
        curate.Counts(2, 0, 0),
        curate.Counts(3, 0, 0),
    )
    assert curate.load_studies(catalog, studies) == (
        curate.Counts(0, 0, 2),
        curate.Counts(0, 0, 3),
    )
    smaller = curate.studies_rows(
        _studies("movie,Movie 2,A;B,Lab,Cite,10.1/x,"),
        _sds("movie,ds-a,ephys"),
        curated_by="c",
    )
    assert curate.load_studies(catalog, smaller) == (
        curate.Counts(0, 1, 0, 1),
        curate.Counts(0, 0, 1, 2),
    )
    assert [r["title"] for r in _rows(catalog, "studies")] == ["Movie 2"]
    assert [r["dataset"] for r in _rows(catalog, "study_datasets")] == ["ds-a"]


def test_a_dataset_in_two_studies_is_refused() -> None:
    with pytest.raises(CurateError, match="ds-a"):
        curate.studies_rows(
            _studies("one,One,,,,,", "two,Two,,,,,"),
            _sds("one,ds-a,x", "two,ds-a,y"),
            curated_by="c",
        )


@pytest.mark.parametrize("slug", ["Movie", "movie_watching", "-movie", "a b", ""])
def test_a_study_slug_must_be_lowercase_hyphenated(slug: str) -> None:
    with pytest.raises(CurateError, match="slug"):
        curate.studies_rows(_studies(f"{slug},T,,,,,"), _sds(), curated_by="c")


def test_a_study_dataset_naming_an_unknown_study_is_refused() -> None:
    with pytest.raises(CurateError, match="ghost"):
        curate.studies_rows(
            _studies("one,One,,,,,"), _sds("ghost,ds-a,x"), curated_by="c"
        )


def _seed_dataset(catalog: SqlCatalog, name: str, **over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "dataset": name,
        "title": "Title " + name,
        "description": "desc",
        "license": "CC0",
        "source_archive": None,
        "source_dataset": None,
        "source_version": None,
        "doi": "10.1/y",
        "url": None,
        "ingest_method": "upload",
        "created_by": "sub-123",
        "created_at": _naive(18, 0, 0),
        "tier": None,
    }
    row.update(over)
    CatalogWriter(catalog).append_if_absent("datasets", {"dataset": name}, [row])
    return row


def test_tiers_rewrite_the_full_row_and_change_only_tier(catalog: SqlCatalog) -> None:
    before = {n: _seed_dataset(catalog, n) for n in ("conc-1", "conc-2", "keep-1")}
    rows = curate.tiers_rows(
        "dataset,tier,note\nconc-1,sandbox,leftover\nconc-2,curated,\n"
    )
    assert curate.tiers(catalog, rows) == curate.Counts(0, 2, 0)
    after = {r["dataset"]: r for r in _rows(catalog, "datasets")}
    assert after["conc-1"] == {**before["conc-1"], "tier": "sandbox"}
    assert after["conc-2"] == {**before["conc-2"], "tier": "curated"}
    assert after["keep-1"] == before["keep-1"]
    assert curate.tiers(catalog, rows) == curate.Counts(0, 0, 2)


def test_a_tier_for_an_absent_dataset_writes_nothing(catalog: SqlCatalog) -> None:
    _seed_dataset(catalog, "conc-1")
    rows = curate.tiers_rows("dataset,tier,note\nconc-1,sandbox,\nghost,sandbox,\n")
    with pytest.raises(CurateError, match="ghost"):
        curate.tiers(catalog, rows)
    assert _rows(catalog, "datasets")[0]["tier"] is None


@pytest.mark.parametrize(
    "text",
    [
        "dataset,tier\nconc-1,sandbox\n",
        "dataset,tier,note\nBad,sandbox,\n",
        "dataset,tier,note\nconc-1,mine,\n",
        "dataset,tier,note\nconc-1,sandbox,\nconc-1,curated,\n",
    ],
)
def test_a_malformed_tiers_csv_is_refused(text: str) -> None:
    with pytest.raises(CurateError):
        curate.tiers_rows(text)


def test_the_committed_csvs_load_clean() -> None:
    curate.studies_rows(
        (REPO / "curation/studies/studies.csv").read_text(),
        (REPO / "curation/studies/study_datasets.csv").read_text(),
        curated_by="c",
    )
    rows = curate.tiers_rows((REPO / "curation/tiers/tiers.csv").read_text())
    assert {r["tier"] for r in rows} <= set(curate.TIERS)
    assert {"dataset": "dandi-000027", "tier": "sandbox"} in rows
