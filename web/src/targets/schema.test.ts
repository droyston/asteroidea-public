import { describe, expect, it } from "vitest";
import { parseTargets, TARGETS_SCHEMA } from "./schema";

const good = {
  schema: TARGETS_SCHEMA,
  exported_at: "2026-10-23T13:00:00Z",
  targets: [
    {
      source: "dandi",
      dataset_id: "000576",
      version: "0.231010.1811",
      path: "sub-03/sub-03_ses-20150304T164400_ecephys.nwb",
      note: "exemplar candidate",
    },
    { source: "openneuro", dataset_id: "ds004993", version: "1.1.2", path: null, note: "" },
  ],
};

function withEntry(patch: Record<string, unknown>): unknown {
  return { ...good, targets: [{ ...good.targets[0], ...patch }] };
}

describe("asteroidea.targets/1 guard", () => {
  it("accepts a valid example", () => {
    expect(parseTargets(good).targets).toHaveLength(2);
  });

  // With the strict key check removed this fails "expected rejection, received accepted".
  it("rejects an entry with a sixth key, naming the key", () => {
    expect(() => parseTargets(withEntry({ size: 14105034 }))).toThrow(/unexpected key "size"/);
  });

  it("rejects a missing key naming it", () => {
    const { note: _note, ...rest } = good.targets[0] as Record<string, unknown>;
    expect(() => parseTargets({ ...good, targets: [rest] })).toThrow(/key "note".*undefined/);
  });

  it.each([
    [{ source: "zenodo" }, /key "source".*"zenodo"/],
    [{ dataset_id: 576 }, /key "dataset_id".*576/],
    [{ version: "" }, /key "version"/],
    [{ path: "" }, /key "path"/],
    [{ path: 3 }, /key "path".*3/],
    [{ note: null }, /key "note".*null/],
  ])("rejects bad value %j", (patch, message) => {
    expect(() => parseTargets(withEntry(patch))).toThrow(message);
  });

  it("names the entry index", () => {
    const doc = { ...good, targets: [good.targets[0], { ...good.targets[1], extra: 1 }] };
    expect(() => parseTargets(doc)).toThrow(/targets\[1\]: unexpected key "extra"/);
  });

  it("rejects an unknown schema string naming it", () => {
    expect(() => parseTargets({ ...good, schema: "asteroidea.targets/2" })).toThrow(
      /unsupported schema string "asteroidea.targets\/2"/,
    );
  });
});
