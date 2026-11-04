import { describe, expect, it, vi } from "vitest";
import { json, tokens } from "../testApi";
import {
  editArtifact,
  formatBytes,
  inputRows,
  loadArtifact,
  loadArtifacts,
  loadUsedBy,
  NOT_AUTHOR,
  parseArtifacts,
  tokenSub,
  withdrawArtifact,
} from "./results";

const CFG = { apiUrl: "https://api.example.test" };
const ROW = {
  artifact_id: "a1",
  name: "peaks.csv",
  size: 2048,
  state: "COMMITTED",
  created_at: 1_792_000_000,
};
const SUMMARY = {
  ...ROW,
  content_type: "text/csv",
  snapshot_id: null,
  finalized_at: 1_792_000_010,
};
const RECORD = {
  ...SUMMARY,
  author_sub: "sub-1",
  author_email: "ada@example.test",
  note: "first pass",
  sources: [{ asset_id: "asset-1", s3_version_id: "v1" }],
  inputs: [
    { kind: "artifact", id: "a0", role: "base" },
    { kind: "table_snapshot", id: "units", version: 42 },
  ],
  run: {
    run_id: "r1",
    function: "rates",
    parameters: { bin: 0.1 },
    defaults_version: "d1",
    sdk_version: "1.2.3",
  },
};
const fetchOf = (res: Response) =>
  vi.fn<(url: string, init: RequestInit) => Promise<Response>>().mockResolvedValue(res);

describe("loadArtifacts", () => {
  it("sends the scope and keeps the contract fields, author email on All", async () => {
    const f = fetchOf(
      json(200, {
        artifacts: [{ ...SUMMARY, author_email: "ada@example.test", run_function: "rates" }],
      }),
    );
    const r = await loadArtifacts(CFG, "all", tokens(), f);
    expect(f.mock.calls[0]?.[0]).toBe("https://api.example.test/artifacts?scope=all");
    expect(f.mock.calls[0]?.[1].headers).toEqual({ authorization: "Bearer tok" });
    expect(r).toEqual({
      kind: "loaded",
      artifacts: [
        {
          ...ROW,
          author_email: "ada@example.test",
          source_asset_ids: [],
          source_asset_count: 0,
          content_type: "text/csv",
          run_id: null,
          run_function: "rates",
        },
      ],
    });
  });

  it("words a 403 and passes the server's text for other failures", async () => {
    const denied = await loadArtifacts(CFG, "mine", tokens(), fetchOf(json(403, {})));
    expect(denied.kind === "error" && denied.message).toContain("researchers group");
    const bad = await loadArtifacts(
      CFG,
      "mine",
      tokens(),
      fetchOf(json(400, { error: "invalid_scope", detail: "scope must be mine or all" })),
    );
    expect(bad).toEqual({ kind: "error", message: "scope must be mine or all" });
  });

  it("ends the session when the token is gone and rejects a broken row", async () => {
    const r = await loadArtifacts(
      CFG,
      "mine",
      tokens({ getAccessToken: () => null }),
      fetchOf(json(200, {})),
    );
    expect(r).toEqual({ kind: "ended" });
    expect(() => parseArtifacts({ artifacts: [{ ...ROW, size: "big" }] })).toThrow(
      /artifacts\[0\]/,
    );
  });
});

describe("loadUsedBy", () => {
  it("asks scope=all with the quoted input id", async () => {
    const f = fetchOf(json(200, { artifacts: [] }));
    await loadUsedBy(CFG, "a/b c", tokens(), f);
    expect(f.mock.calls[0]?.[0]).toBe(
      "https://api.example.test/artifacts?scope=all&input=a%2Fb%20c",
    );
  });

  it("shows invalid_input's sentence", async () => {
    const r = await loadUsedBy(
      CFG,
      "x",
      tokens(),
      fetchOf(json(400, { error: "invalid_input", detail: "input must be an id" })),
    );
    expect(r).toEqual({ kind: "error", message: "input must be an id" });
  });
});

describe("loadArtifact", () => {
  it("reads the record and the presigned link", async () => {
    const f = fetchOf(json(200, { record: RECORD, url: "https://bucket.s3.test/x?sig=1" }));
    const r = await loadArtifact(CFG, "a 1", tokens(), f);
    expect(f.mock.calls[0]?.[0]).toBe("https://api.example.test/artifacts/a%201");
    expect(r.kind === "loaded" && r.url).toBe("https://bucket.s3.test/x?sig=1");
    expect(r.kind === "loaded" && r.record.author_email).toBe("ada@example.test");
    expect(r.kind === "loaded" && r.record.run?.function).toBe("rates");
  });

  it("reads a version 1 record without a run, and words a 404", async () => {
    const { run: _run, inputs: _inputs, ...v1 } = RECORD;
    const r = await loadArtifact(
      CFG,
      "a1",
      tokens(),
      fetchOf(json(200, { record: v1, url: "https://b.test/x" })),
    );
    expect(r.kind === "loaded" && r.record.run).toBeNull();
    const gone = await loadArtifact(
      CFG,
      "a1",
      tokens(),
      fetchOf(json(404, { error: "not_found", detail: "no such artifact" })),
    );
    expect(gone.kind === "error" && gone.message).toContain("not found");
  });

  it("refuses a link that is not https", async () => {
    const r = await loadArtifact(
      CFG,
      "a1",
      tokens(),
      fetchOf(json(200, { record: RECORD, url: "javascript:alert(1)" })),
    );
    expect(r.kind).toBe("error");
  });
});

describe("editArtifact and withdrawArtifact", () => {
  it("PATCHes name and note and reads the summary back", async () => {
    const f = fetchOf(
      json(200, { artifact: { ...SUMMARY, name: "new.csv", author_email: "ada@example.test" } }),
    );
    const r = await editArtifact(CFG, "a1", { name: "new.csv", note: null }, tokens(), f);
    expect(f.mock.calls[0]?.[0]).toBe("https://api.example.test/artifacts/a1");
    expect(f.mock.calls[0]?.[1].method).toBe("PATCH");
    expect(f.mock.calls[0]?.[1].body).toBe('{"name":"new.csv","note":null}');
    expect(r.kind === "done" && r.artifact?.name).toBe("new.csv");
  });

  it("POSTs withdraw with no body", async () => {
    const f = fetchOf(json(200, { state: "WITHDRAWN", artifact_id: "a1" }));
    const r = await withdrawArtifact(CFG, "a1", tokens(), f);
    expect(f.mock.calls[0]?.[0]).toBe("https://api.example.test/artifacts/a1/withdraw");
    expect(f.mock.calls[0]?.[1].method).toBe("POST");
    expect(f.mock.calls[0]?.[1].body).toBeUndefined();
    expect(r).toEqual({ kind: "done", artifact: null });
  });

  it("carries the code and sentence of a not_author refusal", async () => {
    const r = await withdrawArtifact(
      CFG,
      "a1",
      tokens(),
      fetchOf(
        json(403, { error: NOT_AUTHOR, detail: "Only the author of this result can change it." }),
      ),
    );
    expect(r).toEqual({
      kind: "error",
      message: "Only the author of this result can change it.",
      code: NOT_AUTHOR,
    });
  });
});

describe("inputRows", () => {
  it("lists files, then results and tables, with links only where a page exists", async () => {
    const f = fetchOf(json(200, { record: RECORD, url: "https://b.test/x" }));
    const r = await loadArtifact(CFG, "a1", tokens(), f);
    if (r.kind !== "loaded") throw new Error("not loaded");
    expect(inputRows(r.record)).toEqual([
      { kind: "file", id: "asset-1", version: "v1", role: null, href: "/files/asset-1" },
      { kind: "result", id: "a0", version: null, role: "base", href: "/results/a0" },
      { kind: "table", id: "units", version: "42", role: null, href: null },
    ]);
  });
});

describe("tokenSub", () => {
  const jwt = (claims: object) => `h.${btoa(JSON.stringify(claims)).replace(/=+$/, "")}.s`;
  it("reads sub, and gives null for a token that is not a JWT", () => {
    expect(tokenSub(jwt({ sub: "sub-1" }))).toBe("sub-1");
    expect(tokenSub("access-1")).toBeNull();
    expect(tokenSub(null)).toBeNull();
  });
});

describe("formatBytes", () => {
  it("scales to the largest whole unit", () => {
    expect(formatBytes(12)).toBe("12 B");
    expect(formatBytes(1536)).toBe("1.5 KiB");
    expect(formatBytes(5 * 1024 ** 3)).toBe("5.0 GiB");
  });
});
