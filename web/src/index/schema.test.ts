import { describe, expect, it } from "vitest";
import { readGolden } from "./golden";
import { INDEX_SCHEMA, parseIndex, SchemaError } from "./schema";

type Doc = { datasets: Record<string, unknown>[]; [k: string]: unknown };

function golden(): Doc {
  return readGolden() as Doc;
}

describe("parseIndex on the golden index", () => {
  it("accepts every record the harvester wrote", () => {
    const doc = golden();
    const index = parseIndex(doc);
    expect(index.schema).toBe(INDEX_SCHEMA);
    expect(index.datasets).toHaveLength(doc.datasets.length);
    expect(index.datasets.length).toBeGreaterThan(0);
  });

  it("keeps every field of every record", () => {
    const doc = golden();
    const index = parseIndex(doc);
    expect(index.datasets).toEqual(doc.datasets);
  });

  it("reads the per-source summaries", () => {
    const index = parseIndex(golden());
    expect(index.sources.dandi?.failed).toHaveLength(1);
    expect(index.sources.openneuro?.null_nodes).toBe(1);
  });
});

describe("parseIndex rejections name the record, field and value", () => {
  it("names a dropped field", () => {
    const doc = golden();
    const i = doc.datasets.findIndex((d) => d.id === "000576");
    const record = doc.datasets[i];
    if (record === undefined) throw new Error("000576 missing from golden index");
    delete record.has_lfp;
    expect(() => parseIndex(doc)).toThrow(
      new SchemaError(
        `index record ${i} (dandi 000576): field has_lfp expected boolean, got undefined`,
      ),
    );
  });

  it("names a wrong nested type", () => {
    const doc = golden();
    const record = doc.datasets[0];
    if (record === undefined) throw new Error("golden index empty");
    record.species = [{ taxon: 9606, label: "x" }];
    expect(() => parseIndex(doc)).toThrow(
      /field species\[0\]\.taxon expected string or null, got number/,
    );
  });

  it("rejects an unknown schema major, naming the value", () => {
    const doc = golden();
    doc.schema = "asteroidea.dataset-index/3";
    expect(() => parseIndex(doc)).toThrow(
      'index.json: unsupported schema string "asteroidea.dataset-index/3"; this page reads asteroidea.dataset-index/2',
    );
  });

  it("requires version_modified on every record (schema /2)", () => {
    const doc = golden();
    const record = doc.datasets[0];
    if (record === undefined) throw new Error("golden index empty");
    delete record.version_modified;
    expect(() => parseIndex(doc)).toThrow(/field version_modified expected string or null/);
  });

  it("requires stale on every failure entry (schema /2)", () => {
    const doc = golden();
    const sources = doc.sources as Record<string, { failed: Record<string, unknown>[] }>;
    const failure = sources.dandi?.failed[0];
    if (failure === undefined) throw new Error("golden index has no dandi failure");
    delete failure.stale;
    expect(() => parseIndex(doc)).toThrow(/failed\[0\]\.stale expected boolean/);
  });

  it("rejects a species_status outside the vocabulary", () => {
    const doc = golden();
    const record = doc.datasets[0];
    if (record === undefined) throw new Error("golden index empty");
    record.species_status = "unknown";
    expect(() => parseIndex(doc)).toThrow(/species_status expected "stated" \| "not_stated"/);
  });
});
