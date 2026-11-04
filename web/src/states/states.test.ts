import { readdirSync, readFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { CuratorOnly, curatorSentence } from "./CuratorOnly";
import { CODE_COPY, codeOf, copyFor, NETWORK, STATUS_CODES } from "./codes";
import { EmptyResults, SAVE_CALL } from "./EmptyResults";
import { DEFAULT_EXPECTED, EXPECTED, expectedFor, progress, routeKey, seconds } from "./expected";
import { Loading, loadingSentence } from "./Loading";
import { isCuratorRefusal, Refusal, supportText } from "./Refusal";
import { endedSentence, returnLabel } from "./SessionEnded";
import { fitFraction, MAX_UNITS, smaller } from "./smaller";

const INGEST = resolve(
  new URL(".", import.meta.url).pathname,
  "../../../ingest/src/asteroidea_ingest",
);

/** Every code the API source can answer: `code="..."`, the `*_CODE`/`UNREADABLE`/`UPLOAD_*` constants,
 * the not-built codes and the status defaults. */
function apiCodes(): Set<string> {
  const codes = new Set<string>();
  for (const name of readdirSync(INGEST).filter((n) => n.endsWith(".py"))) {
    const text = readFileSync(join(INGEST, name), "utf8");
    for (const m of text.matchAll(/code="([a-z_]+)"/g)) codes.add(m[1] ?? "");
    for (const m of text.matchAll(
      /^[A-Z_]*(?:_CODE|UNREADABLE|UPLOAD_[A-Z]+): Final = "([a-z_]+)"/gm,
    ))
      codes.add(m[1] ?? "");
    for (const m of text.matchAll(/: "([a-z_]+_not_built)"/g)) codes.add(m[1] ?? "");
    const defaults = /^CODES: Final = \{([^}]*)\}/m.exec(text);
    if (defaults !== null) {
      for (const m of (defaults[1] ?? "").matchAll(/: "([a-z_]+)"/g)) codes.add(m[1] ?? "");
    }
  }
  codes.delete("");
  return codes;
}

describe("the code map", () => {
  it("reads the API source (a known code is found, so an empty scan cannot pass)", () => {
    const codes = apiCodes();
    expect(codes).toContain("slow");
    expect(codes).toContain("body_estimate_too_large");
    expect(codes).toContain("upstream_failed");
    expect(codes).toContain("overview_not_built");
    expect(codes.size).toBeGreaterThan(30);
  });

  it("has a browser sentence and a next step for every code the API answers", () => {
    const missing = [...apiCodes()].filter((c) => CODE_COPY[c] === undefined).sort();
    expect(missing).toEqual([]);
  });

  it("covers every status default and the no-answer case", () => {
    for (const code of Object.values(STATUS_CODES)) expect(CODE_COPY[code]).toBeDefined();
    expect(CODE_COPY[NETWORK]?.next).toBe("retry");
  });

  it("takes the body's code, else the status's, else network", () => {
    expect(codeOf({ status: 503, code: "slow" })).toBe("slow");
    expect(codeOf({ status: 502, code: null })).toBe("upstream_failed");
    expect(codeOf({ status: null, code: null })).toBe("network");
    expect(codeOf({ status: 418, code: null })).toBe("invalid_request");
  });

  it("an unknown code keeps itself and borrows its status's copy; session_<state> has its own", () => {
    const c = copyFor({ status: 502, code: "brand_new" });
    expect(c.code).toBe("brand_new");
    expect(c.title).toBe(CODE_COPY.upstream_failed?.title);
    expect(copyFor({ status: 409, code: "session_incomplete" }).title).toMatch(
      /cannot be finished/,
    );
  });

  it("size refusals offer a smaller request; failures retry; request problems show the detail", () => {
    for (const c of ["slow", "too_large", "body_estimate_too_large", "too_many_units"]) {
      expect(CODE_COPY[c]?.next).toBe("smaller");
    }
    for (const c of ["upstream_failed", "internal_error", "unavailable"]) {
      expect(CODE_COPY[c]?.next).toBe("retry");
      expect(CODE_COPY[c]?.detail).toBe("fold");
    }
    expect(CODE_COPY.malformed_align?.detail).toBe("inline");
  });
});

describe("a smaller request", () => {
  it("slow halves the units", () => {
    expect(smaller({ units: 44 }, "slow", null)).toEqual({ units: 22, label: "Use 22 units" });
  });

  it("one unit (or none) shrinks the window instead", () => {
    expect(smaller({ windowS: 1200 }, "slow", null)).toEqual({
      windowS: 600,
      label: "Use 600 s window",
    });
    expect(smaller({ units: 1, windowS: 3 }, "slow", null)?.windowS).toBe(1);
  });

  it("too_large scales by the sizes the detail names, with room to spare", () => {
    const detail = "the response is 10485760 bytes, over the 5242880 limit; use fewer units";
    expect(fitFraction(detail)).toBeCloseTo(0.4);
    expect(smaller({ units: 100 }, "too_large", detail)?.units).toBe(40);
    expect(
      smaller(
        { units: 10 },
        "body_estimate_too_large",
        "the raster would be about 20 bytes, over the 5 limit",
      )?.units,
    ).toBe(2);
  });

  it("too_many_units cuts to the bound the detail names", () => {
    const detail = "units resolve to 300 units, over the bound of 152; request a range";
    expect(smaller({ units: 300 }, "too_many_units", detail)).toEqual({
      units: MAX_UNITS,
      label: "Use 152 units",
    });
    expect(smaller({ units: 100 }, "too_many_units", detail)).toBeNull();
  });

  it("is null for other codes and when nothing can shrink", () => {
    expect(smaller({ units: 44 }, "upstream_failed", null)).toBeNull();
    expect(smaller({ units: 1 }, "slow", null)).toBeNull();
    expect(smaller({ windowS: 0.1 }, "slow", null)).toBeNull();
  });
});

describe("expected times", () => {
  it("lists the measured routes and marks each source", () => {
    expect(EXPECTED["/catalog/studies"]?.ms).toBe(4500);
    expect(EXPECTED["/compute/rates"]?.source).toMatch(/typical warm p50/);
    expect(EXPECTED["/catalog/session-summary"]?.source).toMatch(/estimate/);
    for (const e of Object.values(EXPECTED)) expect(e.source).toMatch(/typical warm p50|estimate/);
  });

  it("folds a query and an asset id into the table key", () => {
    expect(routeKey("/catalog/links?study=x")).toBe("/catalog/links");
    expect(routeKey("/catalog/assets/abc-123/tables/units")).toBe(
      "/catalog/assets/{id}/tables/units",
    );
    expect(expectedFor("/catalog/assets/abc/tables/units").ms).toBe(2000);
    expect(expectedFor("/nowhere")).toBe(DEFAULT_EXPECTED);
  });

  it("the bar reaches 90 % at the usual time and never fills", () => {
    expect(progress(0, 4000)).toBe(0);
    expect(progress(2000, 4000)).toBeCloseTo(0.45);
    expect(progress(4000, 4000)).toBeCloseTo(0.9);
    expect(progress(400_000, 4000)).toBeLessThan(0.99);
    expect(progress(400_000, 4000)).toBeGreaterThan(0.98);
  });

  it("states elapsed against usual, and the limit", () => {
    expect(seconds(2400)).toBe("2.4 s");
    expect(seconds(30_000)).toBe("30 s");
    expect(loadingSentence(2400, "/catalog/links")).toBe(
      "2.4 s so far · usually 1.9 s. If it passes 30 s we stop and say why.",
    );
  });
});

describe("state components", () => {
  const html = (el: Parameters<typeof renderToStaticMarkup>[0]) => renderToStaticMarkup(el);

  it("loading draws a progress bar, the elapsed time and a -loading testid", () => {
    const h = html(
      createElement(Loading, { path: "/compute/rates", what: "Computing", initialElapsedMs: 4200 }),
    );
    expect(h).toContain('data-testid="catalog-loading"');
    expect(h).toContain('role="progressbar"');
    expect(h).toContain('aria-valuenow="45"');
    expect(h).toContain("4.2 s so far · usually 8.4 s.");
  });

  it("a slow refusal with a request offers the smaller one first and keeps the code", () => {
    const h = html(
      createElement(Refusal, {
        status: 503,
        code: "slow",
        detail: "the computation ran out of time",
        request: { units: 44 },
        onSmaller: () => undefined,
        onRetry: () => undefined,
        notebookCode: "x",
      }),
    );
    expect(h).toMatch(
      /<div class="state-actions"><button type="button" class="btn btn-primary">Use 22 units<\/button>/,
    );
    expect(h).toContain("Copy notebook code");
    expect(h).toContain("code: slow");
    expect(h).toContain("<summary>Technical detail</summary>");
    expect(h).not.toContain("Try again");
  });

  it("a slow refusal with nothing to cut retries", () => {
    const h = html(
      createElement(Refusal, { status: 503, code: "slow", detail: null, onRetry: () => undefined }),
    );
    expect(h).toContain('class="btn btn-primary">Try again');
  });

  it("a too_large refusal with nothing to cut does not retry the same request", () => {
    const h = html(
      createElement(Refusal, {
        status: 413,
        code: "too_large",
        detail: "x",
        onRetry: () => undefined,
      }),
    );
    expect(h).not.toContain("Try again");
    expect(h).toContain("Copy details for support");
  });

  it("a catalog failure: retry first, the detail folded, the code small", () => {
    const h = html(
      createElement(Refusal, {
        status: 502,
        code: null,
        detail: "the catalog query failed",
        noun: "the studies list",
        onRetry: () => undefined,
        testId: "catalog-error",
      }),
    );
    expect(h).toContain("The catalog didn&#x27;t answer");
    expect(h).toContain("We couldn&#x27;t load the studies list just now.");
    expect(h).toMatch(
      /class="btn btn-primary">Try again<\/button><button type="button" class="btn btn-secondary">Copy details for support/,
    );
    expect(h).toMatch(
      /<details[^>]*><summary>Technical detail<\/summary><code class="state-detail">the catalog query failed<\/code>/,
    );
    expect(h).toContain("code: upstream_failed");
    expect(h).toContain('role="alert"');
  });

  it("a request problem shows the server's detail in the open", () => {
    const h = html(
      createElement(Refusal, {
        status: 422,
        code: "not_nwb",
        detail: "the asset is not an NWB file",
      }),
    );
    expect(h).toContain('<p class="state-server">the asset is not an NWB file</p>');
    expect(h).not.toContain("<details");
  });

  it("a 403 naming the curators group is the curator-only state, with no names", () => {
    expect(isCuratorRefusal(403, "caller is not in the curators group")).toBe(true);
    expect(isCuratorRefusal(403, "caller is not in the researchers group")).toBe(false);
    const h = html(
      createElement(Refusal, {
        status: 403,
        code: "forbidden",
        detail: "caller is not in the curators group",
      }),
    );
    expect(h).toContain("Imports are started by curators");
    expect(h).toContain("code: forbidden");
    expect(h).not.toContain("Who are the curators");
  });

  it("curator-only with picks offers the export first", () => {
    expect(curatorSentence(3)).toContain("Export your 3 picks and send them to a curator");
    expect(curatorSentence(1)).toContain("your 1 pick and");
    const h = html(createElement(CuratorOnly, { picks: 3, onExport: () => undefined }));
    expect(h).toContain('class="btn btn-primary">Export pick list');
  });

  it("support details carry the code, status, request and detail", () => {
    const t = supportText({ status: 502, code: null, detail: "boom", path: "/catalog/studies" });
    expect(t).toMatch(
      /^code: upstream_failed\nstatus: 502\nrequest: \/catalog\/studies\ndetail: boom\n/,
    );
  });

  it("empty results show the example and the one save call", () => {
    const h = html(createElement(EmptyResults, { example: "CS62 · ses-1 · you" }));
    expect(h).toContain("No results yet");
    expect(h).toContain("your-result.csv");
    expect(h).toContain("asteroidea.save_artifact(");
    expect(SAVE_CALL).toContain("snapshot_id=");
    expect(h).toContain('href="/start"');
  });

  it("session ended names where the reader comes back to", () => {
    expect(returnLabel([{ label: "Studies" }, { label: "CS62" }, { label: "ses-1" }], "/x")).toBe(
      "CS62 · ses-1",
    );
    expect(returnLabel(null, "/results")).toBe("/results");
    expect(returnLabel(null, "/")).toBe("the Studies list");
    expect(endedSentence("CS62 · ses-1")).toBe(
      "You'll come back to CS62 · ses-1 with the same view. Your tray is kept.",
    );
  });
});
