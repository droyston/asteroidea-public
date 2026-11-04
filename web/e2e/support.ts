// Shared by auth.spec.ts and start.spec.ts: the stub hosts, the CORS headers, the golden index and
// the routes both specs need (the index, managed login and the token endpoint).
import { readFileSync } from "node:fs";
import { test as base, type Locator, type Page, type Route } from "@playwright/test";

export const AUTH = "https://auth.asteroidea.test";
export const API = "https://api.asteroidea.test";
export const golden = readFileSync(
  new URL("../../services/tests/fixtures/harvest/expected-index.json", import.meta.url),
  "utf8",
);
/** The sign-in env build (WEB_PORT + 1) that start.spec.ts and catalog.spec.ts open (auth.spec.ts gets it as its baseURL). */
export const APP = `http://localhost:${Number(process.env.WEB_PORT ?? "5173") + 1}/`;
export const CORS = {
  "access-control-allow-origin": "*",
  "access-control-allow-headers": "authorization, content-type",
  "access-control-allow-methods": "GET, POST, OPTIONS",
};

/** A JSON fixture from `e2e/fixtures/<name>.json`, as text. */
export function fixture(name: string): string {
  return readFileSync(new URL(`./fixtures/${name}.json`, import.meta.url), "utf8");
}

/** Stub GET /uploads with an empty list (the panel every signed-in page mounts). */
export async function stubEmptyUploads(page: Page): Promise<void> {
  await page.route(`${API}/uploads`, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      headers: CORS,
      body: JSON.stringify({ uploads: [] }),
    }),
  );
}

export interface BaseSeen {
  /** Number of `index.json` requests. */
  index: number;
  authorize: URL[];
  tokens: URLSearchParams[];
}

/**
 * Stub the index, managed login and the token endpoint, recording each request. `state` rewrites
 * the state managed login returns (a forged one for the mismatch test).
 */
export async function stubBase(
  page: Page,
  opts: { state?: (real: string) => string } = {},
): Promise<BaseSeen> {
  const seen: BaseSeen = { index: 0, authorize: [], tokens: [] };
  // The home draws `/catalog/studies` and `/catalog/links`; the golden bodies serve every spec that
  // opens `/` (a spec that cares registers its own route after this, which wins). The signals view's
  // anatomy panel asks the links route with a query (`?dataset=&subject=`), so the pattern takes one.
  for (const [path, name] of [
    ["/catalog/studies", "ux/catalog-studies.golden"],
    ["/catalog/links", "ux/catalog-links.golden"],
  ] as const) {
    await page.route(new RegExp(`^${API.replaceAll(".", "\\.")}${path}(\\?.*)?$`), (route) =>
      route.request().method() === "OPTIONS"
        ? route.fulfill({ status: 204, headers: CORS })
        : route.fulfill({
            status: 200,
            contentType: "application/json",
            headers: CORS,
            body: fixture(name),
          }),
    );
  }
  // The Results place names the files results used with `/catalog/assets?ids=`; the golden body
  // serves every spec (the ids it does not know are simply not named).
  await page.route(
    (url) => url.origin === API && url.pathname === "/catalog/assets",
    (route) =>
      route.request().method() === "OPTIONS"
        ? route.fulfill({ status: 204, headers: CORS })
        : route.fulfill({
            status: 200,
            contentType: "application/json",
            headers: CORS,
            body: fixture("ux/catalog-assets.golden"),
          }),
  );
  // The home asks `/artifacts?scope=all` for its Latest results card; every spec that opens `/`
  // sees no results, so no card (a spec that cares registers its own route after this, which wins).
  await page.route(
    (url) => url.origin === API && url.pathname === "/artifacts",
    (route) =>
      route.request().method() === "OPTIONS"
        ? route.fulfill({ status: 204, headers: CORS })
        : route.fulfill({
            status: 200,
            contentType: "application/json",
            headers: CORS,
            body: JSON.stringify({ artifacts: [] }),
          }),
  );
  // The session page asks `/catalog/session-summary` first; every spec but its own sees `missing`.
  await page.route(`${API}/catalog/session-summary*`, (route) =>
    route.request().method() === "OPTIONS"
      ? route.fulfill({ status: 204, headers: CORS })
      : route.fulfill({
          status: 200,
          contentType: "application/json",
          headers: CORS,
          body: JSON.stringify({ status: "missing" }),
        }),
  );
  await page.route(`${API}/catalog/datasets`, (route) =>
    route.request().method() === "OPTIONS"
      ? route.fulfill({ status: 204, headers: CORS })
      : route.fulfill({
          status: 200,
          contentType: "application/json",
          headers: CORS,
          body: JSON.stringify({ datasets: [], query_ms: 0 }),
        }),
  );
  // The participant page asks the events route for a session it has no stored summary for; every
  // spec but participant.spec.ts sees "no events".
  await page.route(`${API}/compute/events*`, (route) =>
    route.request().method() === "OPTIONS"
      ? route.fulfill({ status: 204, headers: CORS })
      : route.fulfill({
          status: 200,
          contentType: "application/json",
          headers: CORS,
          body: JSON.stringify({ asset_id: "none", dataset: "none", event_types: [] }),
        }),
  );
  // The signals view and a BIDS events file read the file's labels; every spec but the ones that
  // register their own labels route sees none.
  await page.route(new RegExp(`^${API.replaceAll(".", "\\.")}/files/[^/]+/labels$`), (route) =>
    route.request().method() === "OPTIONS"
      ? route.fulfill({ status: 204, headers: CORS })
      : route.fulfill({
          status: 200,
          contentType: "application/json",
          headers: CORS,
          body: JSON.stringify({ labels: [] }),
        }),
  );
  await page.route("**/index.json", (route) => {
    seen.index += 1;
    return route.fulfill({ status: 200, contentType: "application/json", body: golden });
  });
  await page.route(`${AUTH}/oauth2/authorize*`, (route) => {
    const url = new URL(route.request().url());
    seen.authorize.push(url);
    const state = url.searchParams.get("state") ?? "";
    const back = new URL(url.searchParams.get("redirect_uri") ?? "");
    back.searchParams.set("code", "e2e-code");
    back.searchParams.set("state", opts.state?.(state) ?? state);
    return route.fulfill({ status: 302, headers: { location: back.toString() } });
  });
  await page.route(`${AUTH}/oauth2/token`, (route: Route) => {
    const form = new URLSearchParams(route.request().postData() ?? "");
    seen.tokens.push(form);
    const body =
      form.get("grant_type") === "refresh_token"
        ? { access_token: "access-2" }
        : { access_token: "access-1", refresh_token: "refresh-1", id_token: "id-1" };
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      headers: CORS,
      body: JSON.stringify(body),
    });
  });
  return seen;
}

/** Stub GET /files/{asset_id}/labels with no labels, in the labels Lambda's response shape. */
export async function stubEmptyLabels(page: Page, assetId = "asset-0101"): Promise<void> {
  await page.route(`${API}/files/${assetId}/labels**`, (route) => {
    const preflight = route.request().method() === "OPTIONS";
    return route.fulfill({
      status: preflight ? 204 : 200,
      contentType: "application/json",
      headers: CORS,
      body: preflight ? "" : JSON.stringify({ labels: [] }),
    });
  });
}

const NOTICE_MARK = "__asteroidea_notice_error__";

/**
 * The fixture-e2e guard: a test fails on a `console.error`, an uncaught page
 * error, or a kit Notice of kind error that appears on the page, unless it calls
 * `expectError(reason)` to say why it legitimately shows one. Every fixture-mode spec imports this
 * `test` instead of `@playwright/test`'s.
 */
export const test = base.extend<{
  expectError: (reason: string) => void;
  errorReasons: string[];
  guard: undefined;
}>({
  errorReasons: async ({ page: _page }, use) => {
    await use([]);
  },
  expectError: async ({ errorReasons }, use) => {
    await use((reason) => {
      errorReasons.push(reason);
    });
  },
  guard: [
    async ({ page, errorReasons }, use) => {
      const seen: string[] = [];
      page.on("console", (m) => {
        const text = m.text();
        if (m.type() === "error") seen.push(`console.error: ${text}`);
        else if (text.startsWith(NOTICE_MARK)) {
          seen.push(`error Notice: ${text.slice(NOTICE_MARK.length)}`);
        }
      });
      page.on("pageerror", (e) => seen.push(`page error: ${e.message}`));
      await page.addInitScript((mark) => {
        const report = (el: Element) => {
          if (el.matches(".notice-error")) console.debug(`${mark}${el.textContent ?? ""}`);
        };
        new MutationObserver((records) => {
          for (const r of records) {
            for (const n of r.addedNodes) {
              if (n instanceof Element) {
                report(n);
                for (const c of n.querySelectorAll(".notice-error")) report(c);
              }
            }
          }
        }).observe(document, { childList: true, subtree: true });
      }, NOTICE_MARK);
      await use(undefined);
      if (errorReasons.length === 0 && seen.length > 0) {
        throw new Error(
          `unexpected errors on the page (a test that expects one calls expectError):\n${seen.join("\n")}`,
        );
      }
    },
    { auto: true },
  ],
});

/**
 * The text of one KeyValue pair, read from its own `dt` and the `dd` that follows it, never from the joined
 * text. Not `getByRole("term", { name })`: a term takes no accessible name from its content, so that
 * matches nothing.
 */
export async function kvValue(list: Locator, label: string): Promise<string> {
  const escaped = label.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const term = list.locator("dt").filter({ hasText: new RegExp(`^${escaped}$`) });
  return ((await term.locator("xpath=following-sibling::dd[1]").textContent()) ?? "").trim();
}
