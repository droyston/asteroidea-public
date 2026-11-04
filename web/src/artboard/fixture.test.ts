// Pins CATALOG_FIXTURE's shape: an edit to the fixture is a regression until this test is
// changed with it, in review.
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { CATALOG_FIXTURE as F, RATES_CS62, rates152, UNITS_152, UNITS_CS62 } from "./fixture";

const live = F.studies.datasets.filter((d) => !F.synthetic_datasets.includes(d.dataset));

describe("CATALOG_FIXTURE", () => {
  it("holds the live catalog's rows as listed in the frozen catalog listing", () => {
    expect(live).toHaveLength(42);
    expect(F.live_dataset_count).toBe(42);
    expect(live.filter((d) => d.ingest_method === "import")).toHaveLength(6);
    expect(live.filter((d) => d.ingest_method === "upload")).toHaveLength(36);
    expect(F.studies.datasets.filter((d) => d.tier === "sandbox")).toHaveLength(37);
    expect(F.studies.datasets.filter((d) => d.tier === "curated").map((d) => d.dataset)).toEqual([
      "dandi-000576",
      "dandi-000623",
      "dandi-001775",
      "openneuro-ds004473",
      "openneuro-ds004798",
      "synthetic-24p-ephys",
      "synthetic-24p-mri",
    ]);
  });

  it("marks every awkward state the live catalog lacks as synthetic", () => {
    expect(F.synthetic_datasets).toEqual([
      "synthetic-24p-ephys",
      "synthetic-24p-mri",
      "upload-other-researcher",
    ]);
    expect(F.variants.mine_two.map((d) => [d.dataset, d.mine, d.tier])).toEqual([
      ["upload-mine-a", true, "upload"],
      ["upload-mine-b", true, "upload"],
    ]);
    expect(F.studies.datasets.filter((d) => d.mine)).toHaveLength(0);
    const p62 = F.studies.datasets
      .find((d) => d.dataset === "openneuro-ds004798")
      ?.subjects.find((s) => s.subject === "sub-p62cs");
    expect(p62?.sessions).toEqual([
      { session: "ses-mri", asset_ids: ["asset-p62cs-t1w"], nwb: [] },
    ]);
    const s24 = F.studies.datasets.find((d) => d.dataset === "synthetic-24p-ephys");
    expect(s24?.subjects).toHaveLength(24);
  });

  it("has five studies, one across DANDI 000623 and OpenNeuro ds004798", () => {
    expect(F.studies.studies.map((s) => s.study)).toEqual([
      "movie-watching",
      "spatial-working-memory",
      "seeg-two-choice",
      "amygdala-aversive-stimulation",
      "synthetic-24-participants",
    ]);
    expect(F.studies.studies[0]?.datasets.map((d) => d.dataset)).toEqual([
      "dandi-000623",
      "openneuro-ds004798",
    ]);
  });

  it("has four pending live links (CS41, CS62) and one synthetic confirmed pair", () => {
    const links = F.links.links;
    expect(links.filter((l) => l.status === "pending").map((l) => l.subject)).toEqual([
      "sub-CS41",
      "sub-p41cs",
      "sub-CS62",
      "sub-p62cs",
    ]);
    expect(links.filter((l) => l.status === "confirmed").map((l) => l.subject_key)).toEqual([
      "synthetic-s01",
      "synthetic-s01",
    ]);
  });

  it("keeps the real region labels verbatim", () => {
    const d623 = F.studies.datasets.find((d) => d.dataset === "dandi-000623");
    expect(d623?.regions).toHaveLength(12);
    expect(d623?.regions.reduce((n, r) => n + r.units, 0)).toBe(102);
    const d576 = F.studies.datasets.find((d) => d.dataset === "dandi-000576");
    expect(d576?.regions.map((r) => r.region)).toContain(
      "Amyg, Right Amygdala mAmyg, medial amygdala",
    );
    expect(d576?.regions.reduce((n, r) => n + r.units, 0)).toBe(42);
  });

  it("carries the CS62 R2 rates body: 44 units, 93 scene cuts, 60 bins, one null z", () => {
    expect(RATES_CS62.units).toHaveLength(44);
    expect(RATES_CS62.n_trials).toBe(93);
    expect(F.session.n_trials).toBe(93);
    expect(RATES_CS62.align).toBe("event:scene_cut");
    expect(RATES_CS62.edges).toHaveLength(61);
    expect(RATES_CS62.units.filter((u) => u.z === null).map((u) => u.unit_index)).toEqual([
      F.session.null_z_unit,
    ]);
    expect(RATES_CS62.defaults.response_window_s).toEqual([0, 1]);
  });

  it("has a units table that agrees with the live body (indices and side-kept labels)", () => {
    expect(UNITS_CS62.rows.map((r) => r.unit_index)).toEqual(
      RATES_CS62.units.map((u) => u.unit_index),
    );
    const counts = new Map<string, number>();
    for (const r of UNITS_CS62.rows) counts.set(r.location, (counts.get(r.location) ?? 0) + 1);
    expect(Object.fromEntries(counts)).toEqual({
      "Right vmPFC": 2,
      "Right ACC": 1,
      "Right preSMA": 6,
      "Right amygdala": 35,
    });
  });

  it("builds the 152-unit body from a fixed formula (same twice, the shape of the CS62 R2 body)", () => {
    expect(UNITS_152.rows).toHaveLength(152);
    const a = rates152();
    expect(a.units).toHaveLength(152);
    expect(a.units.every((u) => u.z?.length === 60)).toBe(true);
    expect(JSON.stringify(rates152())).toBe(JSON.stringify(a));
  });

  // The handler test writes the golden bodies. The fixture's bodies must carry the
  // same keys at every level the golden shows, and the same timestamp text.
  it("matches the shape of the golden studies and links bodies", () => {
    const read = (name: string) =>
      JSON.parse(
        readFileSync(new URL(`../../e2e/fixtures/ux/${name}`, import.meta.url), "utf8"),
      ) as Record<string, Record<string, unknown>[]>;
    const g = read("catalog-studies.golden.json");
    const gl = read("catalog-links.golden.json");
    const keys = (o: object | undefined) => Object.keys(o ?? {}).sort();
    const first = <T>(xs: readonly T[] | undefined): T | undefined => xs?.[0];
    expect(keys(F.studies)).toEqual(keys(g));
    expect(keys(F.studies.studies[0])).toEqual(keys(first(g.studies)));
    const gd = first(g.datasets) as { subjects: { sessions: object[] }[]; created_at: string };
    const fd = F.studies.datasets.find((d) => d.dataset === "dandi-000623");
    expect(keys(fd)).toEqual(keys(gd));
    expect(keys(fd?.subjects[0])).toEqual(keys(gd.subjects[0]));
    expect(keys(fd?.subjects[0]?.sessions[0])).toEqual(keys(gd.subjects[0]?.sessions[0]));
    expect(fd?.created_at).toMatch(/^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3}$/);
    expect(gd.created_at).toMatch(/^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3}$/);
    expect(keys(F.links)).toEqual(keys(gl));
    expect(keys(F.links.links[0])).toEqual(keys(first(gl.links)));
  });
});
