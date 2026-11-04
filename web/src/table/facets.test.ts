import { describe, expect, it } from "vitest";
import { readGolden } from "../index/golden";
import { type Dataset, parseIndex } from "../index/schema";
import {
  applyFilters,
  emptyFilters,
  facetValues,
  NOT_STATED,
  speciesKeys,
  speciesLabels,
  toggle,
} from "./facets";

const rows: Dataset[] = parseIndex(readGolden()).datasets;
const labels = speciesLabels(rows);

function find(source: string, id: string): Dataset {
  const d = rows.find((r) => r.source === source && r.id === id);
  if (d === undefined) throw new Error(`${source} ${id} missing from golden index`);
  return d;
}

describe("species facet keys", () => {
  const dandiHuman = find("dandi", "000576");
  const openneuroHuman = rows.find(
    (r) => r.source === "openneuro" && r.species.some((s) => s.label === "Human"),
  );

  it("DANDI 'Homo sapiens - Human' and OpenNeuro 'Human' share one facet value", () => {
    expect(dandiHuman.species[0]?.label).toBe("Homo sapiens - Human");
    expect(openneuroHuman).toBeDefined();
    expect(speciesKeys(dandiHuman)).toEqual(["NCBITaxon:9606"]);
    expect(openneuroHuman && speciesKeys(openneuroHuman)).toEqual(["NCBITaxon:9606"]);
    const human = facetValues(rows, emptyFilters(), "species", labels).filter(
      (v) => v.key === "NCBITaxon:9606",
    );
    expect(human).toHaveLength(1);
    expect(human[0]?.label).toBe("Homo sapiens - Human");
  });

  it("choosing the human value keeps both archives and drops not-stated rows", () => {
    const out = applyFilters(rows, toggle(emptyFilters(), "species", "NCBITaxon:9606"));
    expect(out).toContain(dandiHuman);
    expect(out.some((r) => r.source === "openneuro")).toBe(true);
    expect(out.every((r) => r.species.some((s) => s.taxon === "NCBITaxon:9606"))).toBe(true);
    expect(out.some((r) => r.species_status === "not_stated")).toBe(false);
  });

  it("not stated is its own value", () => {
    const notStated = rows.filter((r) => r.species_status === "not_stated");
    expect(notStated.length).toBeGreaterThan(0);
    for (const r of notStated) expect(speciesKeys(r)).toEqual([NOT_STATED]);
    const v = facetValues(rows, emptyFilters(), "species", labels).find(
      (x) => x.key === NOT_STATED,
    );
    expect(v).toEqual({ key: NOT_STATED, label: "not stated", count: notStated.length });
  });

  it("an unmapped stated label is keyed as text", () => {
    const baboon = rows.find((r) => r.species.some((s) => s.label === "baboon"));
    expect(baboon && speciesKeys(baboon)).toEqual(["text:baboon"]);
  });
});

describe("other filters", () => {
  it("has Units and has LFP narrow to rows with both flags", () => {
    let f = toggle(emptyFilters(), "species", "NCBITaxon:9606");
    f = { ...f, hasUnits: true, hasLfp: true };
    const out = applyFilters(rows, f);
    expect(out.map((r) => r.id)).toEqual(["000576"]);
  });

  it("text search matches title or id, case-insensitively", () => {
    const f = { ...emptyFilters(), text: "000576" };
    expect(applyFilters(rows, f).map((r) => r.id)).toEqual(["000576"]);
    const g = { ...emptyFilters(), text: "BALLOON analog" };
    expect(applyFilters(rows, g).map((r) => r.id)).toContain("ds000001");
  });

  it("a size range excludes rows with unknown size and keeps the bounds inclusive", () => {
    const d = find("dandi", "000003");
    const bytes = d.bytes ?? 0;
    const out = applyFilters(rows, { ...emptyFilters(), bytes: { min: bytes, max: bytes } });
    expect(out).toContain(d);
    expect(out.every((r) => r.bytes === bytes)).toBe(true);
  });

  it("licence facet puts unmapped licences under one value", () => {
    const v = facetValues(rows, emptyFilters(), "licence", labels).find(
      (x) => x.key === "unmapped",
    );
    const expected = rows.filter((r) => r.license.spdx === null).length;
    expect(v?.count).toBe(expected);
  });

  it("facet counts ignore the facet's own selection", () => {
    const f = toggle(emptyFilters(), "source", "dandi");
    const sources = facetValues(rows, f, "source", labels);
    expect(sources.find((s) => s.key === "openneuro")?.count).toBe(
      rows.filter((r) => r.source === "openneuro").length,
    );
  });
});
