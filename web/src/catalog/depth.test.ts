import { describe, expect, it, vi } from "vitest";
import { json, tokens } from "../testApi";
import { filePath, loadDataset, loadSubjectFiles, parseDataset, subjectPath } from "./depth";

const CFG = { apiUrl: "https://api.example.test" };

const BODY = {
  dataset: "dandi-000623",
  facts: {
    dataset: "dandi-000623",
    title: "Brain movie recordings",
    license: "CC-BY-4.0",
    doi: null,
    source_archive: "dandi",
    source_dataset: "000623",
    source_version: "0.230625.0237",
    asset_count: 3,
    subject_count: 2,
    total_bytes: 3500000000,
  },
  subjects: [{ subject: "sub-CS41", species: "Homo sapiens", sex: "F", age: "P40Y" }],
  sessions: [
    {
      subject: "sub-CS41",
      bids_session: "ses-1",
      start_time: "2026-01-02T03:04:05Z",
      description: null,
    },
  ],
  formats: [{ format: "nwb", count: 3, bytes: 3500000000 }],
  query_ms: 120,
};

describe("parseDataset", () => {
  it("reads subjects, sessions, formats and query_ms", () => {
    const d = parseDataset(BODY);
    expect(d.subjects).toEqual([
      { subject: "sub-CS41", species: "Homo sapiens", sex: "F", age: "P40Y" },
    ]);
    expect(d.sessions[0]?.description).toBeNull();
    expect(d.formats).toEqual([{ format: "nwb", count: 3, bytes: 3500000000 }]);
    expect(d.query_ms).toBe(120);
  });

  it("reads the header facts, nulls kept", () => {
    expect(parseDataset(BODY).facts).toEqual(BODY.facts);
  });

  it("keeps a numeric age as text and a null sum as null", () => {
    const d = parseDataset({
      ...BODY,
      subjects: [{ subject: "s", species: null, sex: null, age: 7 }],
      formats: [{ format: null, count: 1, bytes: null }],
    });
    expect(d.subjects[0]?.age).toBe("7");
    expect(d.formats[0]).toEqual({ format: null, count: 1, bytes: null });
  });

  it("names what is wrong with a body that is not the contract", () => {
    expect(() => parseDataset({ ...BODY, subjects: undefined })).toThrow('no "subjects" list');
    expect(() => parseDataset({ ...BODY, formats: [{ format: "x", count: "3" }] })).toThrow(
      '"count" is not a number',
    );
    expect(() => parseDataset({ ...BODY, query_ms: undefined })).toThrow('"query_ms"');
    expect(() => parseDataset({ ...BODY, facts: undefined })).toThrow('no "facts" object');
    expect(() => parseDataset({ ...BODY, facts: { ...BODY.facts, asset_count: "3" } })).toThrow(
      '"asset_count" is not a number',
    );
    expect(() => parseDataset("x")).toThrow("not an object");
  });
});

describe("loaders", () => {
  it("loadDataset calls GET /catalog/datasets/{dataset} with the bearer token", async () => {
    const f = vi.fn<(url: string, init: RequestInit) => Promise<Response>>();
    f.mockResolvedValue(json(200, BODY));
    const r = await loadDataset(CFG, "dandi-000623", tokens(), f);
    expect(f.mock.calls[0]?.[0]).toBe("https://api.example.test/catalog/datasets/dandi-000623");
    expect(f.mock.calls[0]?.[1].headers).toEqual({ authorization: "Bearer tok" });
    expect(r.kind).toBe("loaded");
  });

  it("loadDataset shows the server's refusal text", async () => {
    const f = vi.fn().mockResolvedValue(json(404, { error: "no such dataset" }));
    const r = await loadDataset(CFG, "nope", tokens(), f);
    expect(r).toEqual({
      kind: "error",
      message: "no such dataset",
      status503: false,
      code: null,
      status: 404,
    });
  });

  it("loadSubjectFiles asks the assets route for one subject and page", async () => {
    const f = vi.fn<(url: string, init: RequestInit) => Promise<Response>>();
    f.mockResolvedValue(json(200, { assets: [], page: 1, more: false, query_ms: 5 }));
    await loadSubjectFiles(CFG, "dandi-000623", "sub-CS41", 1, tokens(), f);
    expect(f.mock.calls[0]?.[0]).toBe(
      "https://api.example.test/catalog/datasets/dandi-000623/assets?subject=sub-CS41&page=1",
    );
  });
});

describe("paths", () => {
  it("encode each segment", () => {
    expect(subjectPath("d", "a b")).toBe("/data/d/a%20b");
    expect(filePath("x/y")).toBe("/files/x%2Fy");
  });
});
