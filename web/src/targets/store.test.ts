import { describe, expect, it } from "vitest";
import { buildExport } from "./export";
import { parseImport, replaceMessage } from "./import";
import type { Target } from "./schema";
import { type KeyValueStorage, STORAGE_KEY, TargetsStore } from "./store";

const file: Target = {
  source: "dandi",
  dataset_id: "000576",
  version: "0.231010.1811",
  path: "sub-03/x.nwb",
  note: "",
};
const whole: Target = { ...file, path: null };

function memory(initial?: string): KeyValueStorage & { data: Map<string, string> } {
  const data = new Map<string, string>(initial === undefined ? [] : [[STORAGE_KEY, initial]]);
  return {
    data,
    getItem: (k) => data.get(k) ?? null,
    setItem: (k, v) => {
      data.set(k, v);
    },
  };
}

describe("TargetsStore", () => {
  it("adds, refuses a duplicate as already picked, and persists", () => {
    const storage = memory();
    const s = new TargetsStore(storage);
    expect(s.add(file)).toEqual({ ok: true });
    expect(s.add({ ...file, note: "other" })).toEqual({ ok: false, reason: "already picked" });
    expect(s.add(whole)).toEqual({ ok: true });
    expect(s.getSnapshot().targets).toHaveLength(2);
    expect(new TargetsStore(storage).getSnapshot().targets).toEqual([file, whole]);
  });

  it("edits a note, removes and clears", () => {
    const s = new TargetsStore(memory());
    s.add(file);
    s.add(whole);
    s.setNote(0, "exemplar candidate");
    expect(s.getSnapshot().targets[0]?.note).toBe("exemplar candidate");
    s.remove(0);
    expect(s.getSnapshot().targets).toEqual([whole]);
    s.clear();
    expect(s.getSnapshot().targets).toEqual([]);
  });

  it("keeps the list in memory and shows an error when the write fails", () => {
    const s = new TargetsStore({
      getItem: () => null,
      setItem: () => {
        throw new DOMException("quota", "QuotaExceededError");
      },
    });
    s.add(file);
    expect(s.getSnapshot().targets).toEqual([file]);
    expect(s.getSnapshot().error).toMatch(/QuotaExceededError/);
  });

  it("ignores a corrupt stored list with a visible reason", () => {
    const s = new TargetsStore(
      memory('{"schema":"asteroidea.targets/1","exported_at":"x","targets":[{"a":1}]}'),
    );
    expect(s.getSnapshot().targets).toEqual([]);
    expect(s.getSnapshot().error).toMatch(/Stored picks ignored.*unexpected key "a"/);
  });

  it("notifies subscribers", () => {
    const s = new TargetsStore(memory());
    let n = 0;
    const off = s.subscribe(() => {
      n += 1;
    });
    s.add(file);
    off();
    s.add(whole);
    expect(n).toBe(1);
  });
});

describe("export and import", () => {
  it("round-trips", () => {
    const text = buildExport([file, whole], new Date("2026-10-23T13:00:00Z"));
    expect(JSON.parse(text).exported_at).toBe("2026-10-23T13:00:00.000Z");
    expect(parseImport(text, "targets.json")).toEqual({ ok: true, targets: [file, whole] });
  });

  it("rejects an invalid file whole with the first reason and the file name", () => {
    const doc = JSON.parse(buildExport([file]));
    doc.targets[0].size = 1;
    const r = parseImport(JSON.stringify(doc), "bad.json");
    expect(r).toEqual({
      ok: false,
      reason: expect.stringMatching(/^bad\.json: targets\[0\]: unexpected key "size"/),
    });
    expect(parseImport("{nope", "bad.json")).toMatchObject({ ok: false });
  });

  it("words the replace confirm with both counts", () => {
    expect(replaceMessage(2, 3, "targets.json")).toBe("Replace 2 picks with 3 from targets.json?");
  });
});
