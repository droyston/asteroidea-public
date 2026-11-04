import { describe, expect, it } from "vitest";
import { readGolden } from "../index/golden";
import { parseIndex } from "../index/schema";
import {
  applyFind,
  bestFit,
  COLUMNS,
  catalogJoin,
  emptyFind,
  facetCounts,
  filtersFromSearch,
  filtersToSearch,
  histogram,
  matrix,
  SPECIES,
  SUBJECT_BINS,
} from "./model";

const rows = parseIndex(readGolden()).datasets;

describe("matrix", () => {
  it("counts the whole index per species row and column, from the same classifier the filter uses", () => {
    const m = matrix(rows);
    for (const sp of SPECIES) {
      for (const col of COLUMNS) {
        const f = emptyFind();
        f.species = new Set([sp.key]);
        f.has = new Set([col.key]);
        expect(m.get(`${sp.key}|${col.key}`) ?? 0).toBe(applyFind(rows, f).length);
      }
    }
  });

  it("the human row counts the DANDI and OpenNeuro human datasets as one species", () => {
    const f = emptyFind();
    f.species = new Set(["human"]);
    const shown = applyFind(rows, f);
    expect(shown.some((d) => d.source === "dandi" && d.id === "000576")).toBe(true);
    expect(shown.some((d) => d.source === "openneuro")).toBe(true);
  });
});

describe("filters in the URL", () => {
  it("round-trips every filter", () => {
    const f = emptyFind();
    f.text = "amygdala & co";
    f.species = new Set(["human", "mouse"]);
    f.has = new Set(["units", "lfp"]);
    f.source = new Set(["dandi"]);
    f.standard = new Set(["NWB"]);
    f.licence = new Set(["CC-BY-4.0"]);
    f.subjects = { min: 5, max: 100 };
    f.bytes = { min: null, max: 5e10 };
    const search = filtersToSearch(f);
    expect(search.startsWith("?")).toBe(true);
    expect(filtersFromSearch(search)).toEqual(f);
  });

  it("empty filters give no query and unknown keys are ignored", () => {
    expect(filtersToSearch(emptyFind())).toBe("");
    expect(filtersFromSearch("?sp=nonsense&has=bogus&subj=x-y&zzz=1")).toEqual(emptyFind());
  });
});

describe("facets and histograms", () => {
  it("facet counts skip the facet's own choice and the histogram bins sum to the rows with a value", () => {
    const f = emptyFind();
    f.source = new Set(["dandi"]);
    const sources = facetCounts(rows, f, "source");
    expect(sources.find((v) => v.key === "openneuro")?.count).toBeGreaterThan(0);
    const bins = histogram(rows, emptyFind(), "subjects", SUBJECT_BINS);
    expect(bins.reduce((a, b) => a + b, 0)).toBe(rows.filter((d) => d.subjects !== null).length);
  });
});

describe("best fit", () => {
  it("returns at most n datasets, all matching, units-first", () => {
    const f = emptyFind();
    f.has = new Set(["units"]);
    const top = bestFit(rows, f, 4);
    expect(top.length).toBeLessThanOrEqual(4);
    expect(top.every((d) => d.has_units)).toBe(true);
  });
});

describe("catalog join", () => {
  it("matches index ids to the catalog's source archive and dataset", () => {
    const join = catalogJoin([
      {
        dataset: "dandi-000576",
        title: null,
        license: null,
        doi: null,
        source_archive: "dandi",
        source_dataset: "000576",
        source_version: null,
        ingest_method: null,
        asset_count: 0,
        subject_count: 0,
        total_bytes: 0,
      },
    ]);
    expect(join.get("dandi:000576")).toBe("dandi-000576");
    expect(join.get("dandi:000004")).toBeUndefined();
  });
});
