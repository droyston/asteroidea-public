// The result preview and the "From the same run" strip: the
// model picks how a type draws, the pure view draws it from the presigned link, the fetchers ask for the
// inline link, the attachment link and one run's files.
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import recordGolden from "../../e2e/fixtures/ux/artifact-record.golden.json" with { type: "json" };
import allGolden from "../../e2e/fixtures/ux/artifacts-all.golden.json" with { type: "json" };
import { json, tokens } from "../testApi";
import { chainModel, isImage, previewFallback, previewKind, stripModel } from "./model";
import { PREVIEW_LOADING, ResultChain, RunStrip } from "./ResultsView";
import { loadDownload, loadRun, parseArtifacts, parseRecord } from "./results";

const CFG = { apiUrl: "https://api.example.test" };
const URL_ = "https://bucket.s3.test/x.png?X-Amz-Signature=1";
const NOOP = () => undefined;
const ON = {
  busy: false,
  onDownload: NOOP,
  onRename: NOOP,
  onNote: NOOP,
  onWithdraw: NOOP,
  onDelete: NOOP,
};
const fetchOf = (body: unknown) =>
  vi.fn<(url: string, init: RequestInit) => Promise<Response>>().mockResolvedValue(json(200, body));

function chainOf(contentType: string) {
  const { record } = parseRecord(recordGolden);
  return chainModel({
    record: { ...record, content_type: contentType },
    names: new Map(),
    known: new Map(),
    usedBy: [],
    sub: null,
  });
}

function page(contentType: string, url: string | null): string {
  return renderToStaticMarkup(
    createElement(ResultChain, { chain: chainOf(contentType), on: ON, previewUrl: url }),
  );
}

describe("previewKind", () => {
  it("draws PNG and JPEG as images and HTML as a page; every other type says so", () => {
    expect(previewKind("image/png")).toEqual({ kind: "image" });
    expect(previewKind("IMAGE/JPEG")).toEqual({ kind: "image" });
    expect(previewKind("text/html; charset=utf-8")).toEqual({ kind: "html" });
    expect(previewKind("text/csv")).toEqual({
      kind: "none",
      text: "Preview not built for text/csv; download it",
    });
    expect(previewKind(null)).toEqual({ kind: "none", text: previewFallback(null) });
    expect(isImage("image/svg+xml")).toBe(false);
  });
});

describe("the preview", () => {
  it("draws an image from the presigned link", () => {
    const html = page("image/png", URL_);
    expect(html).toContain(`<img src="${URL_.replace(/&/g, "&amp;")}"`);
    expect(html).toContain('data-kind="image"');
    expect(html).not.toContain("<iframe");
  });

  it("draws HTML in a frame sandboxed to scripts only, never same-origin", () => {
    const html = page("text/html", URL_);
    expect(html).toMatch(/<iframe[^>]*sandbox="allow-scripts"/);
    expect(html).not.toContain("allow-same-origin");
    expect(html).not.toContain("<img");
  });

  it("keeps the box with a sentence for another type, and waits for the link", () => {
    expect(page("application/json", URL_)).toContain(
      "Preview not built for application/json; download it",
    );
    expect(page("image/png", null)).toContain(PREVIEW_LOADING);
  });
});

describe("the run strip", () => {
  const rows = parseArtifacts(allGolden);
  const current = rows[1]?.artifact_id ?? "";

  it("sorts by name, marks the current file and keeps a thumbnail only for a linked image", () => {
    const items = stripModel(rows, current, new Map([[current, URL_]])) ?? [];
    expect(items.map((i) => i.name)).toEqual([
      "baseline-rates.csv",
      "spike-rates.csv",
      "tuning-curves.csv",
    ]);
    expect(items.filter((i) => i.current).map((i) => i.artifact_id)).toEqual([current]);
    expect(items.map((i) => i.thumb)).toEqual([null, URL_, null]);
    const html = renderToStaticMarkup(createElement(RunStrip, { items }));
    expect(html).toContain("From the same run · 3");
    expect(html).toMatch(new RegExp(`href="/results/${current}"[^>]*aria-current="page"`));
    expect(html.match(/aria-current=/g)).toHaveLength(1);
    expect(html.match(/<img /g)).toHaveLength(1);
  });

  it("is null when the run saved nothing else", () => {
    expect(stripModel(rows.slice(1, 2), current, new Map())).toBeNull();
  });
});

describe("the fetchers", () => {
  it("lists one run's files of every author, the id quoted", async () => {
    const f = fetchOf(allGolden);
    const r = await loadRun(CFG, "run/1 a", tokens(), f);
    expect(f.mock.calls[0]?.[0]).toBe(
      "https://api.example.test/artifacts?scope=all&run=run%2F1%20a",
    );
    expect(r.kind === "loaded" && r.files.map((a) => a.run_id)).toEqual(["r-1", "r-1", "r-1"]);
    // The golden rows carry no link: each is a name chip.
    expect(r.kind === "loaded" && r.files.map((a) => a.url)).toEqual([null, null, null]);
  });

  it("keeps each run row's https link, and drops anything else", async () => {
    const [first, second] = allGolden.artifacts;
    const f = fetchOf({
      artifacts: [
        { ...first, url: URL_ },
        { ...second, url: "http://bucket.s3.test/x.png" },
      ],
    });
    const r = await loadRun(CFG, "r-1", tokens(), f);
    expect(r.kind === "loaded" && r.files.map((a) => a.url)).toEqual([URL_, null]);
    expect(f).toHaveBeenCalledTimes(1);
  });

  it("names the HTTP status when the run listing fails", async () => {
    const f = vi
      .fn<(url: string, init: RequestInit) => Promise<Response>>()
      .mockResolvedValue(json(503, { message: "Service Unavailable" }));
    const r = await loadRun(CFG, "r-1", tokens(), f);
    expect(r).toEqual({ kind: "error", message: "The platform API answered HTTP 503." });
    const bad = await loadRun(
      CFG,
      "",
      tokens(),
      vi
        .fn<(url: string, init: RequestInit) => Promise<Response>>()
        .mockResolvedValue(json(400, { error: "invalid_run", detail: "run must be an id" })),
    );
    expect(bad).toEqual({
      kind: "error",
      message: "The platform API answered HTTP 400: run must be an id.",
    });
  });

  it("asks for an attachment link to download", async () => {
    const f = fetchOf(recordGolden);
    const r = await loadDownload(CFG, "a 1", tokens(), f);
    expect(f.mock.calls[0]?.[0]).toBe("https://api.example.test/artifacts/a%201?download=1");
    expect(r.kind === "loaded" && r.record.run_id).toBe("r-1");
  });
});
