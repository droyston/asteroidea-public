// The results parsers over the bodies the real handlers answer (web/e2e/fixtures/ux/*.golden.json,
// written by ingest/tests/test_catalog_batch.py and test_artifacts_golden.py).
import { describe, expect, it, vi } from "vitest";
import recordGolden from "../../e2e/fixtures/ux/artifact-record.golden.json" with { type: "json" };
import allGolden from "../../e2e/fixtures/ux/artifacts-all.golden.json" with { type: "json" };
import assetsGolden from "../../e2e/fixtures/ux/catalog-assets.golden.json" with { type: "json" };
import { json, tokens } from "../testApi";
import {
  BRANCH_ROWS,
  chainModel,
  FROM_RESULTS,
  listModel,
  MAX_BRANCHES,
  OTHER_DATA,
} from "./model";
import {
  type Artifact,
  type AssetName,
  loadAssetNames,
  parseArtifacts,
  parseAssetNames,
  parseRecord,
} from "./results";

const CFG = { apiUrl: "https://api.example.test" };

describe("the results goldens", () => {
  it("parses the All listing with the asset ids each result used", () => {
    const rows = parseArtifacts(allGolden);
    expect(rows.map((r) => r.name)).toEqual([
      "tuning-curves.csv",
      "spike-rates.csv",
      "baseline-rates.csv",
    ]);
    expect(rows[1]?.source_asset_ids).toHaveLength(2);
    expect(rows[1]?.source_asset_count).toBe(2);
    expect(rows[0]?.source_asset_ids).toEqual([]);
  });

  it("parses the batch lookup, with current and not current", () => {
    const names = parseAssetNames(assetsGolden);
    expect(names.map((n) => [n.subject, n.session, n.current])).toEqual([
      ["CS62", "1", true],
      ["CS62", "1", true],
      ["CS62", null, false],
    ]);
  });

  it("parses the record the chain draws", () => {
    const { record } = parseRecord(recordGolden);
    expect(record.inputs.map((i) => i.role)).toEqual(["spikes", "alignment", "baseline", null]);
    expect(record.run?.function).toBe("analysis.rates");
  });

  it("names more than 100 ids in batches of 100", async () => {
    const ids = Array.from({ length: 150 }, (_, i) => `id-${i}`);
    const fetchFn = vi.fn(async (_url: string, _init: RequestInit) =>
      json(200, { assets: [], missing: [] }),
    );
    const r = await loadAssetNames(CFG, [...ids, "id-0"], tokens(), fetchFn);
    expect(r.kind).toBe("loaded");
    expect(fetchFn).toHaveBeenCalledTimes(2);
    const first = String(fetchFn.mock.calls[0]?.[0]);
    expect(first.split("ids=")[1]?.split(",")).toHaveLength(100);
  });
});

describe("the results place model", () => {
  const all = parseArtifacts(allGolden);
  const names = new Map(parseAssetNames(assetsGolden).map((n) => [n.asset_id, n]));
  const studies = [{ study: "bmovie", title: "Movie watching", datasets: ["dandi-000623"] }];
  const base = { me: null, studies: [], open: null, selected: null, mineCount: 0 } as const;

  it("groups by study then session, with a branch for results made from results", () => {
    const m = listModel({
      ...base,
      scope: "all",
      rows: all,
      mineCount: 2,
      me: "ada@example.test",
      names,
      studies,
    });
    expect(m.branches.map((b) => [b.label, b.count])).toEqual([
      ["Movie watching", 2],
      [FROM_RESULTS, 1],
    ]);
    expect(m.branches[0]?.sessions.map((s) => s.label)).toEqual(["CS62 · ses-1"]);
    expect(m.timeline.dots.filter((d) => d.mine)).toHaveLength(2);
    expect(m.rows.map((r) => r.name)).toEqual(["spike-rates.csv", "baseline-rates.csv"]);
  });

  it("keeps at most 10 counted branches and lists 4 newest plus a count", () => {
    const template = all[1] as Artifact;
    const many = Array.from({ length: 30 }, (_, i) => ({
      ...template,
      artifact_id: `r-${i}`,
      created_at: 1_792_000_000 - i,
      source_asset_ids: [`d${i % 15}`],
    }));
    const one = [...names.values()][0] as AssetName;
    const fake = new Map(
      Array.from({ length: 15 }, (_, i): [string, AssetName] => [
        `d${i}`,
        { ...one, asset_id: `d${i}`, dataset: `ds-${i}` },
      ]),
    );
    const m = listModel({ ...base, scope: "all", rows: many, names: fake });
    expect(m.branches).toHaveLength(MAX_BRANCHES);
    // 15 datasets of 2 results each: the 9 largest (ties by name) stay, the other 6 are one branch.
    expect(m.branches.find((b) => b.label === OTHER_DATA)?.count).toBe(12);
    expect(m.branches.reduce((n, b) => n + b.count, 0)).toBe(30);
    const one0 = many.map((r) => ({ ...r, source_asset_ids: ["d0"] }));
    const big = listModel({ ...base, scope: "all", rows: one0, names: fake });
    expect(big.rows).toHaveLength(BRANCH_ROWS);
    expect(big.more).toBe(26);
  });

  it("draws one result as a chain: roles, current, the readable run, used by", () => {
    const { record } = parseRecord(recordGolden);
    const c = chainModel({
      record,
      names,
      known: new Map(all.map((a) => [a.artifact_id, a])),
      usedBy: [all[0] as Artifact],
      sub: record.author_sub,
    });
    expect(c.inputs.map((i) => [i.kind, i.role, i.current])).toEqual([
      ["RECORDING", "spikes", true],
      ["EVENTS", "alignment", true],
      ["RESULT", "baseline", null],
    ]);
    expect(c.inputs[2]?.title).toBe("baseline-rates.csv");
    expect(c.run?.title).toBe("Firing rates around scene cut");
    expect(c.run?.lines).toContainEqual({ label: "Bin", value: "100 ms" });
    expect(c.run?.lines).toContainEqual({ label: "Catalog", value: "units snapshot 8123456789" });
    expect(c.mine).toBe(true);
    expect(c.author).toBe("ada@example.test");
    expect(c.deleteRefusal).toContain("Delete is refused");
    const other = chainModel({ record, names, known: new Map(), usedBy: [], sub: "someone" });
    expect(other.mine).toBe(false);
    expect(other.deleteRefusal).toBeNull();
  });
});
