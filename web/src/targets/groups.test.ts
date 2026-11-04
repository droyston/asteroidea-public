import { describe, expect, it } from "vitest";
import { buildExport } from "./export";
import { groupPicks, groupScope, pickCount } from "./groups";
import { parseImport } from "./import";
import { TARGETS_SCHEMA, type Target } from "./schema";
import { TargetsStore } from "./store";

const ds = { source: "dandi", dataset_id: "000576", version: "0.231010.1811" } as const;
const whole: Target = { ...ds, path: null, note: "exemplar" };
const a: Target = { ...ds, path: "sub-1/a.nwb", note: "" };
const b: Target = { ...ds, path: "sub-1/b.nwb", note: "keep" };
const on: Target = {
  source: "openneuro",
  dataset_id: "ds000001",
  version: "1.0.0",
  path: null,
  note: "",
};

function doc(targets: Target[]): string {
  return JSON.stringify({ schema: TARGETS_SCHEMA, exported_at: "2026-10-01T00:00:00Z", targets });
}

describe("picks as two levels", () => {
  it("groups files under their dataset; no files means the whole dataset", () => {
    const groups = groupPicks([whole, on, a, b]);
    expect(
      groups.map((g) => [g.dataset.dataset_id, g.entry?.index ?? null, groupScope(g)]),
    ).toEqual([
      ["000576", 0, "2 files"],
      ["ds000001", 1, "whole dataset"],
    ]);
    expect(groups[0]?.files.map((f) => [f.target.path, f.index])).toEqual([
      ["sub-1/a.nwb", 2],
      ["sub-1/b.nwb", 3],
    ]);
    expect(pickCount([whole, on, a, b])).toBe(2);
  });

  it("toggles a file under a dataset and removes a dataset with its files", () => {
    const s = new TargetsStore(null);
    s.add(whole);
    s.add(on);
    s.toggleFile(ds, "sub-1/a.nwb");
    s.toggleFile(ds, "sub-1/b.nwb");
    s.toggleFile(ds, "sub-1/a.nwb");
    expect(s.getSnapshot().targets.map((t) => t.path)).toEqual([null, null, "sub-1/b.nwb"]);
    s.removeDataset(ds);
    expect(s.getSnapshot().targets).toEqual([on]);
  });

  it("exports both levels and reads them back", () => {
    const text = buildExport([whole, a, b, on], new Date("2026-10-27T00:00:00Z"));
    const back = parseImport(text, "targets.json");
    expect(back).toEqual({ ok: true, targets: [whole, a, b, on] });
    if (back.ok)
      expect(groupPicks(back.targets).map(groupScope)).toEqual(["2 files", "whole dataset"]);
  });

  it("reads an older dataset-only export: every dataset whole", () => {
    const back = parseImport(doc([whole, on]), "brief-1.json");
    expect(back.ok).toBe(true);
    if (back.ok) {
      expect(groupPicks(back.targets).map(groupScope)).toEqual(["whole dataset", "whole dataset"]);
    }
  });

  it("reads the older per-file export: files with no dataset entry group under their dataset", () => {
    const back = parseImport(doc([a, b, on]), "old-per-file.json");
    expect(back.ok).toBe(true);
    if (back.ok) {
      const groups = groupPicks(back.targets);
      expect(groups.map((g) => [g.dataset.dataset_id, g.entry, groupScope(g)])).toEqual([
        ["000576", null, "2 files"],
        ["ds000001", expect.objectContaining({ index: 2 }), "whole dataset"],
      ]);
      expect(groups[0]?.files.map((f) => f.target.note)).toEqual(["", "keep"]);
    }
  });
});
