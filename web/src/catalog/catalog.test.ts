import { describe, expect, it, vi } from "vitest";
import { json, tokens } from "../testApi";
import { assetsQuery, loadAsset, loadAssets, loadDatasets } from "./catalog";

const CFG = { apiUrl: "https://api.example.test" };

const DATASET = {
  dataset: "dandi-000623",
  title: "A title",
  license: "CC-BY-4.0",
  doi: null,
  source_archive: "dandi",
  source_dataset: "000623",
  source_version: "0.1",
  ingest_method: "import",
  asset_count: 3,
  subject_count: 2,
  total_bytes: 1234,
};
const ASSET = {
  asset_id: "a1",
  path: "sub-1/x.nwb",
  subject: "sub-1",
  bids_session: null,
  datatype: "ephys",
  format: "nwb",
  size_bytes: 10,
  committed_at: "2026-10-01T00:00:00Z",
};

describe("assetsQuery", () => {
  it("leaves out empty filters and page 0", () => {
    expect(assetsQuery({ subject: "", format: "", page: 0 })).toBe("");
    expect(assetsQuery({ subject: "  ", format: "", page: 0 })).toBe("");
  });

  it("maps subject, format and page to the query string", () => {
    expect(assetsQuery({ subject: "sub-1", format: "nwb", page: 2 })).toBe(
      "?subject=sub-1&format=nwb&page=2",
    );
    expect(assetsQuery({ subject: "", format: "nifti", page: 0 })).toBe("?format=nifti");
  });

  it("escapes values", () => {
    expect(assetsQuery({ subject: "a&b=c", format: "", page: 0 })).toBe("?subject=a%26b%3Dc");
  });
});

describe("loadDatasets", () => {
  it("calls GET /catalog/datasets with the bearer token and parses the body", async () => {
    const f = vi.fn<(url: string, init: RequestInit) => Promise<Response>>();
    f.mockResolvedValue(json(200, { datasets: [DATASET], query_ms: 812 }));
    const r = await loadDatasets(CFG, tokens(), f);
    expect(f.mock.calls[0]?.[0]).toBe("https://api.example.test/catalog/datasets");
    expect(f.mock.calls[0]?.[1].headers).toEqual({ authorization: "Bearer tok" });
    expect(r).toEqual({ kind: "loaded", value: { datasets: [DATASET], query_ms: 812 } });
  });

  it("reports a body that is not the contract", async () => {
    const f = vi.fn().mockResolvedValue(json(200, { nope: 1 }));
    const r = await loadDatasets(CFG, tokens(), f);
    expect(r.kind === "error" && r.message).toContain("cannot read");
  });
});

describe("loadAssets", () => {
  it("puts the dataset in the path and the filters in the query", async () => {
    const f = vi.fn<(url: string, init: RequestInit) => Promise<Response>>();
    f.mockResolvedValue(json(200, { assets: [ASSET], page: 1, more: true, query_ms: 5 }));
    const r = await loadAssets(
      CFG,
      "dandi-000623",
      { subject: "sub-1", format: "nwb", page: 1 },
      tokens(),
      f,
    );
    expect(f.mock.calls[0]?.[0]).toBe(
      "https://api.example.test/catalog/datasets/dandi-000623/assets?subject=sub-1&format=nwb&page=1",
    );
    expect(r).toEqual({
      kind: "loaded",
      value: { assets: [ASSET], page: 1, more: true, query_ms: 5 },
    });
  });

  it("sends no query string without filters", async () => {
    const f = vi
      .fn()
      .mockResolvedValue(json(200, { assets: [], page: 0, more: false, query_ms: 1 }));
    await loadAssets(CFG, "d", { subject: "", format: "", page: 0 }, tokens(), f);
    expect(f.mock.calls[0]?.[0]).toBe("https://api.example.test/catalog/datasets/d/assets");
  });

  it("rejects a body without the more flag", async () => {
    const f = vi.fn().mockResolvedValue(json(200, { assets: [], page: 0, query_ms: 1 }));
    const r = await loadAssets(CFG, "d", { subject: "", format: "", page: 0 }, tokens(), f);
    expect(r.kind).toBe("error");
  });
});

describe("loadAsset", () => {
  it("returns the asset row and its header rows", async () => {
    const body = { asset: ASSET, headers: [{ asset_id: "a1", n_units: 12 }] };
    const f = vi.fn<(url: string, init: RequestInit) => Promise<Response>>();
    f.mockResolvedValue(json(200, body));
    const r = await loadAsset(CFG, "a/1", tokens(), f);
    expect(f.mock.calls[0]?.[0]).toBe("https://api.example.test/catalog/assets/a%2F1");
    expect(r).toEqual({ kind: "loaded", value: body });
  });
});

describe("errors", () => {
  it("shows the detail verbatim and carries the code for 400, 403, 404, 500, 502 and 503", async () => {
    for (const status of [400, 403, 404, 500, 502, 503]) {
      const body = { error: `code_${status}`, detail: `server says ${status}` };
      const f = vi.fn().mockResolvedValue(json(status, body));
      expect(await loadDatasets(CFG, tokens(), f)).toEqual({
        kind: "error",
        message: `server says ${status}`,
        status503: status === 503,
        code: `code_${status}`,
        status,
      });
    }
  });

  it("shows the uniform 500's sentence, which names the request id", async () => {
    const detail = "The platform failed on this request. Report request id r-1.";
    const f = vi.fn().mockResolvedValue(json(500, { error: "internal_error", detail }));
    const r = await loadAsset(CFG, "a", tokens(), f);
    expect(r.kind === "error" && r.message).toBe(detail);
  });

  it("shows an older API's error text verbatim, with no code", async () => {
    for (const status of [400, 403, 404, 502, 503]) {
      const f = vi.fn().mockResolvedValue(json(status, { error: `server says ${status}` }));
      expect(await loadDatasets(CFG, tokens(), f)).toEqual({
        kind: "error",
        message: `server says ${status}`,
        status503: status === 503,
        code: null,
        status,
      });
    }
  });

  it("falls back to a status line when the refusal has no body", async () => {
    const f = vi.fn().mockResolvedValue(new Response("", { status: 502 }));
    const r = await loadAsset(CFG, "a", tokens(), f);
    expect(r.kind === "error" && r.message).toBe(
      "The platform API answered HTTP 502 with no message.",
    );
  });

  it("refreshes once on a 401 and ends the session on a second", async () => {
    const refresh = vi.fn().mockResolvedValue(true);
    const f = vi.fn().mockResolvedValue(json(401, {}));
    expect(await loadDatasets(CFG, tokens({ refresh }), f)).toEqual({ kind: "ended" });
    expect(refresh).toHaveBeenCalledTimes(1);
    expect(f).toHaveBeenCalledTimes(2);
  });

  it("reports an unreachable API", async () => {
    const f = vi.fn().mockRejectedValue(new TypeError("Failed to fetch"));
    const r = await loadDatasets(CFG, tokens(), f);
    expect(r.kind === "error" && r.message).toContain("Could not reach the platform API");
  });
});
