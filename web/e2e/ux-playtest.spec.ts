// The manual walk-through: the acceptance flow, by user navigation only (clicks on links; a `goto`
// only for the first page and, labelled `via: "goto"`, for a page whose link the current build does not
// draw). It MEASURES: per page the wait from the action to the shell's ready mark (`<main data-ready="<view>">`) and
// each API request's time, printed against ~5 s as "miss" lines by `ux_playtest.py` (never FAIL).
// Two modes. Fixture (under `make web-e2e`, the sign-in env build, the API served by page.route):
// prints each page's wait and asserts the pages reach their ready marks. Live (`LIVE=1`, the live
// config, the hosted app): managed login is not driven; test tokens for the sandbox are placed in
// sessionStorage before the page loads.
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { type Page, test as plain } from "@playwright/test";
import { CATALOG_FIXTURE as F, RATES_CS62, UNITS_CS62 } from "../src/artboard/fixture";
import { EVENTS_CS62, TUNING_CS62 } from "../src/artboard/surfaces/session";
import {
  API,
  APP,
  CORS,
  fixture,
  test as guarded,
  stubBase,
  stubEmptyLabels,
  stubEmptyUploads,
} from "./support";

const live = process.env.LIVE === "1";
const BUDGET_S = 5;
const READY_TIMEOUT_MS = live ? 90_000 : 20_000;
const TOKENS_KEY = "asteroidea.auth.tokens/1"; // web/src/auth/store.ts

const VIEWPORTS = [
  { name: "desktop", width: 1440, height: 900 },
  { name: "phone", width: 375, height: 812 },
] as const;

interface Targets {
  /** The study route segment, its title, and the studies the route answers with. */
  study: string;
  title: string;
  titles: string[];
  dataset: string;
  subject: string;
  /** The session segment of the URL (`_` for none). */
  session: string;
  asset: string;
}

interface Request {
  method: string;
  path: string;
  status: number;
  ms: number;
}

interface Check {
  name: string;
  ok: boolean;
  detail: string;
}

interface PageRecord {
  viewport: string;
  page: string;
  ready: string;
  reached: boolean;
  via: "click" | "goto" | "start";
  wait_s: number;
  screenshot: string;
  requests: Request[];
  checks: Check[];
}

/** The fixture-mode targets: the live-catalog fixture the home, study and session specs share. */
const S = F.session;
const FIXTURE: Targets = {
  study: S.study,
  title: F.studies.studies.find((st) => st.study === S.study)?.title ?? S.study,
  titles: F.studies.studies.map((st) => st.title),
  dataset: S.dataset,
  subject: S.subject,
  session: S.session,
  asset: "asset-0101",
};

const json = (body: string | null, status = 200) => ({
  status: body === null ? 404 : status,
  contentType: "application/json",
  headers: CORS,
  body: body ?? '{"error":"not_found","detail":"no route"}',
});

async function stubFixtureApi(page: Page): Promise<void> {
  await stubBase(page);
  await stubEmptyUploads(page);
  await stubEmptyLabels(page, "asset-0101");
  await page.route(`${API}/**`, (route) => {
    const req = route.request();
    if (req.method() === "OPTIONS") return route.fulfill({ status: 204, headers: CORS });
    const url = new URL(req.url());
    const p = url.pathname;
    let body: string | null = null;
    if (p === "/catalog/studies") body = JSON.stringify(F.studies);
    else if (p === "/catalog/links") body = JSON.stringify(F.links);
    else if (p === `/catalog/assets/${S.asset_id}/tables/units`) body = JSON.stringify(UNITS_CS62);
    else if (p === "/compute/events") body = JSON.stringify(EVENTS_CS62);
    else if (p === "/catalog/session-summary") body = JSON.stringify({ status: "missing" });
    else if (p === "/compute/rates") body = JSON.stringify(RATES_CS62);
    else if (p === "/compute/tuning") body = JSON.stringify(TUNING_CS62);
    else if (p === "/catalog/datasets") body = fixture("catalog-datasets");
    else if (p === "/catalog/assets/asset-0101") body = fixture("catalog-file-nwb");
    else if (p === "/catalog/assets/asset-0101/events") body = fixture("events");
    else if (p.startsWith("/catalog/assets/asset-0101/tables/")) {
      const tables = JSON.parse(fixture("catalog-file-tables")) as Record<string, unknown>;
      const table = p.split("/").pop() ?? "";
      if (table in tables) body = JSON.stringify(tables[table]);
    } else if (p === "/artifacts") body = JSON.stringify({ artifacts: [] });
    else if (p === "/get-started") body = fixture("get-started");
    else if (p === "/uploads") return route.fallback();
    return route.fulfill(json(body));
  });
}

function slug(text: string): string {
  return text.toLowerCase().replace(/[^a-z0-9]+/g, "-");
}

/** Walk the pages at the page's current viewport; return one record per page. */
async function walk(
  page: Page,
  vp: (typeof VIEWPORTS)[number],
  targets: Targets,
  app: string,
  api: string,
  out: string,
): Promise<PageRecord[]> {
  const records: PageRecord[] = [];
  const started = new Map<string, number>();
  let buffer: Request[] = [];
  page.on("request", (r) => {
    if (r.url().startsWith(api)) started.set(r.url() + r.method(), performance.now());
  });
  const finish = (r: { url(): string; method(): string }, status: number) => {
    const t0 = started.get(r.url() + r.method());
    if (t0 === undefined) return;
    const u = new URL(r.url());
    buffer.push({
      method: r.method(),
      path: u.pathname + u.search,
      status,
      ms: Math.round(performance.now() - t0),
    });
  };
  page.on("response", (r) => finish(r.request(), r.status()));
  page.on("requestfailed", (r) => finish(r, 0));

  // The home is judged at 375; the other pages are a reading, never a failure.
  const noteFor = async (name: string): Promise<Check[]> => {
    const sideways = await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth + 1,
    );
    if (name === "home")
      return [{ name: "no sideways scroll", ok: sideways, detail: `at ${vp.name}` }];
    return [
      {
        name: "reading: no sideways scroll",
        ok: true,
        detail: `${sideways ? "none" : "sideways"} at ${vp.name}`,
      },
    ];
  };
  const mainText = async () =>
    (await page
      .locator("main")
      .innerText()
      .catch(() => "")) || "";

  async function visit(
    name: string,
    ready: string,
    action: () => Promise<"click" | "goto" | "start">,
    extra?: () => Promise<Check[]>,
  ): Promise<void> {
    buffer = [];
    const t0 = performance.now();
    let via: PageRecord["via"] = "start";
    let reached = true;
    try {
      via = await action();
      await page
        .locator(`main[data-ready="${ready}"]`)
        .waitFor({ state: "attached", timeout: READY_TIMEOUT_MS });
    } catch {
      reached = false;
    }
    const wait = Math.round(((performance.now() - t0) / 1000) * 1000) / 1000;
    const checks = reached ? [...(await noteFor(name)), ...((await extra?.()) ?? [])] : [];
    const screenshot = `${out}/${vp.name}-${String(records.length).padStart(2, "0")}-${slug(name)}.png`;
    await page.screenshot({ path: screenshot }).catch(() => undefined);
    records.push({
      viewport: vp.name,
      page: name,
      ready,
      reached,
      via,
      wait_s: wait,
      screenshot,
      requests: buffer,
      checks,
    });
    console.log(
      `ux-playtest ${vp.name} ${name}: ${reached ? `${wait.toFixed(2)} s to data-ready="${ready}"` : `NOT REACHED (${READY_TIMEOUT_MS / 1000} s)`} via ${via}`,
    );
  }

  /** Click a link by href when the page draws one; else open the path (labelled). */
  const go = (hrefStart: string, path: string) => async (): Promise<"click" | "goto"> => {
    const link = page.locator(`main a[href^="${hrefStart}"], nav a[href^="${hrefStart}"]`).first();
    if ((await link.count()) > 0 && (await link.isVisible().catch(() => false))) {
      await link.click();
      return "click";
    }
    await page.goto(new URL(path, app).toString());
    return "goto";
  };
  const nav = (label: string, path: string) => async (): Promise<"click" | "goto"> => {
    const link = page
      .getByRole("navigation")
      .getByRole("link", { name: label, exact: true })
      .first();
    if ((await link.count()) > 0 && (await link.isVisible().catch(() => false))) {
      await link.click();
      return "click";
    }
    await page.goto(new URL(path, app).toString());
    return "goto";
  };

  await visit(
    "home",
    "home",
    async () => {
      await page.goto(app);
      return "start";
    },
    async () => {
      const text = await mainText();
      const missing = targets.titles.filter((t) => !text.includes(t));
      const checks: Check[] = [
        {
          name: "every study title is drawn",
          ok: missing.length === 0,
          detail: missing.join("; "),
        },
      ];
      if (vp.name === "desktop") {
        const fits = await page.evaluate(
          () => document.documentElement.scrollHeight <= window.innerHeight + 1,
        );
        checks.push({ name: "no vertical scroll", ok: fits, detail: "at 1440x900" });
      }
      return checks;
    },
  );
  await visit("study", "study", go(`/s/${targets.study}`, `/s/${targets.study}`), async () => {
    const ok = (await mainText()).includes(targets.title);
    return [{ name: "the study title is drawn", ok, detail: targets.title }];
  });
  const sessionPath = `/s/${targets.study}/${targets.dataset}/${targets.subject}/${targets.session}`;
  await visit("session", "session", go(sessionPath, sessionPath), async () => {
    const ok = /baseline/i.test(await mainText());
    return [{ name: "the baseline is named", ok, detail: "text matches /baseline/" }];
  });
  await visit("find", "find", nav("Find", "/discover"));
  await visit("results", "results", nav("Results", "/results"));
  await visit("setup", "setup", nav("Setup", "/start"));
  await visit("file", "file", go(`/files/${targets.asset}`, `/files/${targets.asset}`));
  await visit("uploads", "uploads", go("/uploads", "/uploads"));
  return records;
}

if (live) {
  for (const vp of VIEWPORTS) {
    plain(`ux-playtest ${vp.name}`, async ({ browser }) => {
      plain.setTimeout(1_200_000);
      const env = (name: string): string => {
        const v = process.env[name];
        if (v === undefined || v === "")
          throw new Error(`${name} is not set; start this with make live-ux-playtest`);
        return v;
      };
      const out = env("UX_OUT");
      mkdirSync(out, { recursive: true });
      const targets = JSON.parse(readFileSync(env("UX_TARGETS_FILE"), "utf8")) as Targets;
      const context = await browser.newContext({
        viewport: { width: vp.width, height: vp.height },
      });
      await context.addInitScript(
        ({ key, access, refresh }) => {
          try {
            if (window.sessionStorage.getItem(key) === null)
              window.sessionStorage.setItem(
                key,
                JSON.stringify({ access_token: access, refresh_token: refresh }),
              );
          } catch {
            // storage blocked: the run shows the signed-out page and the first page is not reached
          }
        },
        { key: TOKENS_KEY, access: env("UX_ACCESS_TOKEN"), refresh: env("UX_REFRESH_TOKEN") },
      );
      const page = await context.newPage();
      const records = await walk(
        page,
        vp,
        targets,
        env("UX_APP_URL"),
        env("UX_API_URL").replace(/\/$/, ""),
        out,
      );
      writeFileSync(`${out}/ux-${vp.name}.json`, JSON.stringify(records, null, 2));
      await context.close();
    });
  }
} else {
  guarded.skip(
    process.env.E2E_LIVE === "1",
    "needs the fixture-mode auth build (no live server has the auth env)",
  );
  guarded("the acceptance walk reaches every ready mark at both sizes", async ({ page }, info) => {
    guarded.setTimeout(240_000);
    await stubFixtureApi(page);
    const all: PageRecord[] = [];
    for (const vp of VIEWPORTS) {
      await page.setViewportSize({ width: vp.width, height: vp.height });
      all.push(...(await walk(page, vp, FIXTURE, APP, API, info.outputPath())));
    }
    const failed = all.filter((r) => !r.reached || r.checks.some((c) => !c.ok));
    if (failed.length > 0) {
      const lines = failed.map(
        (r) =>
          `${r.viewport} ${r.page}: ${r.reached ? "" : "not reached; "}${r.checks
            .filter((c) => !c.ok)
            .map((c) => `${c.name} (${c.detail})`)
            .join("; ")}`,
      );
      throw new Error(`pages that failed:\n${lines.join("\n")}`);
    }
    console.log(`ux-playtest budget ${BUDGET_S} s is applied by make live-ux-playtest`);
  });
}
