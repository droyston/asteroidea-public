// The session heatmap in the fixture-mode sign-in build (port WEB_PORT + 1):
// the URL's session resolves through `/catalog/studies` to its NWB file with the most units; the page
// waits on `/compute/events`, the units table and `/compute/rates` at the display parameters, and
// marks `data-ready="session"` once the heatmap has drawn. Bodies: CATALOG_FIXTURE, the recorded
// rates body for CS62 R2 (one null z), the resolver's CS62 events, and the 152-unit surface.
import { expect, type Page } from "@playwright/test";
import {
  CATALOG_FIXTURE as F,
  RATES_CS62,
  rates152,
  UNITS_152,
  UNITS_CS62,
} from "../src/artboard/fixture";
import { EVENTS_CS62, TUNING_CS62, tuningBody } from "../src/artboard/surfaces/session";
import { API, APP, CORS, fixture, stubBase, stubEmptyUploads, test } from "./support";

test.skip(
  process.env.E2E_LIVE === "1",
  "needs the fixture-mode auth build (no live server has the auth env)",
);

const S = F.session;
const R2 = `${APP}s/${S.study}/${S.dataset}/${S.subject}/${S.session}`;
const UNITS_PATH = `/catalog/assets/${S.asset_id}/tables/units`;

type Reply = { status: number; body: unknown };

interface Options {
  units?: unknown;
  rates?: (q: URLSearchParams, n: number) => Reply | Promise<Reply>;
  events?: (assetId: string) => Reply;
  /** The `/catalog/session-summary` answer; `missing` when unset. */
  summary?: Reply | (() => Reply);
  /** The `/compute/tuning` answer; TUNING_CS62 when unset. */
  tuning?: (q: URLSearchParams) => Reply;
}

interface Seen {
  rates: URLSearchParams[];
  events: string[];
  units: string[];
  summary: string[];
  tuning: URLSearchParams[];
}

const ok = (body: unknown): Reply => ({ status: 200, body });

async function stub(page: Page, opts: Options = {}): Promise<Seen> {
  const seen: Seen = { rates: [], events: [], units: [], summary: [], tuning: [] };
  await stubBase(page);
  await stubEmptyUploads(page);
  await page.route(`${API}/**`, async (route) => {
    const req = route.request();
    if (req.method() === "OPTIONS") return route.fulfill({ status: 204, headers: CORS });
    const url = new URL(req.url());
    let reply: Reply;
    if (url.pathname === "/catalog/studies") reply = ok(F.studies);
    else if (url.pathname === "/catalog/session-summary") {
      seen.summary.push(url.search);
      const s = opts.summary ?? ok({ status: "missing" });
      reply = typeof s === "function" ? s() : s;
    } else if (url.pathname.endsWith("/tables/units")) {
      seen.units.push(url.pathname);
      reply = ok(opts.units ?? UNITS_CS62);
    } else if (url.pathname === "/compute/events") {
      const id = url.searchParams.get("asset_id") ?? "";
      seen.events.push(id);
      reply = opts.events?.(id) ?? ok({ ...(EVENTS_CS62 as object), asset_id: id });
    } else if (url.pathname === "/compute/rates") {
      seen.rates.push(url.searchParams);
      reply = await (opts.rates?.(url.searchParams, seen.rates.length) ?? ok(RATES_CS62));
    } else if (url.pathname === "/compute/tuning") {
      seen.tuning.push(url.searchParams);
      reply = opts.tuning?.(url.searchParams) ?? ok(TUNING_CS62);
    } else return route.fallback();
    return route.fulfill({
      status: reply.status,
      contentType: "application/json",
      headers: CORS,
      body: JSON.stringify(reply.body),
    });
  });
  return seen;
}

const ab = (page: Page, mark: string) => page.locator(`[data-ab="${mark}"]`);

async function noScroll(page: Page): Promise<void> {
  const m = await page.evaluate(() => ({
    sh: document.documentElement.scrollHeight,
    ih: window.innerHeight,
  }));
  expect(m.sh, "the session page scrolls at 1440x900").toBeLessThanOrEqual(m.ih);
}

test("CS62 R2: 44 rows in 4 side-kept regions, 43 Δ bars and 1 hatched null-z row, totals and the baseline", async ({
  page,
}) => {
  const seen = await stub(page);
  await page.goto(R2);
  await expect(page.locator('main[data-ready="session"]')).toBeVisible();
  await expect(ab(page, "unit-row")).toHaveCount(44);
  await expect(ab(page, "region-group")).toHaveCount(4);
  await expect(ab(page, "delta-bar")).toHaveCount(43);
  await expect(ab(page, "null-z-row")).toHaveCount(1);
  await expect(ab(page, "null-z-row")).toHaveText(`unit ${S.null_z_unit}: no baseline variance`);
  await expect(ab(page, "region-group").locator("strong")).toHaveText([
    "Right ACC",
    "Right amygdala",
    "Right preSMA",
    "Right vmPFC",
  ]);
  // Each region group carries its region colour on its edge.
  const edges = await ab(page, "region-group").evaluateAll((els) =>
    els.map((el) => {
      const region = el.querySelector(".sh-region");
      return region === null ? "" : getComputedStyle(region).borderLeftColor;
    }),
  );
  expect(new Set(edges).size).toBe(4);
  const canvasRows = await page
    .locator("canvas[data-ab-rows]")
    .evaluateAll((els) => els.reduce((n, el) => n + Number(el.getAttribute("data-ab-rows")), 0));
  expect(canvasRows).toBe(44);
  const heights = await page
    .locator("canvas[data-ab-rows]")
    .evaluateAll((els) => els.map((el) => el.getBoundingClientRect().height));
  expect(heights).toEqual([10, 350, 60, 20]);

  const box = ab(page, "baseline-box");
  await expect(box).toContainText("−1 to 0 s, z");
  await expect(box).toContainText(
    "Analysis default (dandi-000623 block): baseline none, response window 0 to 1 s.",
  );
  const totals = ab(page, "totals");
  await expect(totals).toContainText("Fire more: mean z ≥ +1 over 0 to 1 s after the scene cut");
  await expect(totals).toContainText(
    `no baseline variance, counted in neither direction: unit ${S.null_z_unit}`,
  );
  const counts = await totals.locator(".sh-big strong").allTextContents();
  expect(counts.map(Number).reduce((a, b) => a + b, 0)).toBe(43);

  // The three requests the wait spans, each once, for the most-units NWB file of the session.
  expect(seen.events).toEqual([S.asset_id]);
  expect(seen.units).toEqual([UNITS_PATH]);
  expect(seen.rates).toHaveLength(1);
  expect(Object.fromEntries(seen.rates[0] ?? [])).toEqual({
    asset_id: S.asset_id,
    units: "all",
    align: "event:scene_cut",
    pre_s: "1",
    post_s: "2",
    bin_s: "0.05",
    zscore: "1",
    raster: "0",
  });
  // The onset strip draws the chosen type's onsets only: one tick per scene cut.
  const ticks = await page
    .locator(".sh-strip-onsets")
    .getAttribute("d")
    .then((d) => (d ?? "").split("M").length - 1);
  expect(ticks).toBe(S.n_trials);
  await noScroll(page);
});

test("152 units: 3 px rows, six side-kept groups, no scroll at 1440x900", async ({ page }) => {
  await stub(page, {
    units: { rows: UNITS_152.rows, page: 0, more: false },
    rates: () => ok(rates152()),
  });
  await page.goto(R2);
  await expect(page.locator('main[data-ready="session"]')).toBeVisible();
  await expect(ab(page, "unit-row")).toHaveCount(152);
  await expect(ab(page, "region-group")).toHaveCount(6);
  const heights = await page
    .locator("canvas[data-ab-rows]")
    .evaluateAll((els) =>
      els.map((el) => el.getBoundingClientRect().height / Number(el.getAttribute("data-ab-rows"))),
    );
  expect(new Set(heights)).toEqual(new Set([3]));
  await noScroll(page);
});

test("the wait shows the elapsed time while the rates compute", async ({ page }) => {
  let release: () => void = () => undefined;
  const held = new Promise<void>((r) => {
    release = r;
  });
  await stub(page, {
    rates: async () => {
      await held;
      return ok(RATES_CS62);
    },
  });
  await page.goto(R2);
  const wait = page.getByTestId("session-rates-loading");
  await expect(wait).toContainText("Computing the rates for 93 scene cut events");
  const first = await page.getByTestId("session-elapsed").textContent();
  await expect(page.getByTestId("session-elapsed")).not.toHaveText(first ?? "");
  await expect(page.locator("main[data-ready]")).toHaveCount(0);
  release();
  await expect(page.locator('main[data-ready="session"]')).toBeVisible();
  await expect(wait).toHaveCount(0);
});

test("the event-type picker asks for the chosen type's rates", async ({ page }) => {
  const seen = await stub(page);
  await page.goto(R2);
  await expect(page.locator('main[data-ready="session"]')).toBeVisible();
  await expect(page.getByTestId("event-scene_cut")).toHaveAttribute("aria-pressed", "true");
  await page.getByTestId("event-recognition_onset").click();
  await expect(page.getByTestId("event-recognition_onset")).toHaveAttribute("aria-pressed", "true");
  await expect(page.locator('main[data-ready="session"]')).toBeVisible();
  expect(seen.rates.map((q) => q.get("align"))).toEqual([
    "event:scene_cut",
    "event:recognition_onset",
  ]);
  await expect(page.locator(".sh-strip-label")).toHaveText("recognition onset · 40");
});

test("a dataset without an event recipe says so and offers the file page", async ({ page }) => {
  const seen = await stub(page, {
    units: JSON.parse(fixture("synthesis-units-44")),
    events: (id) =>
      ok({
        asset_id: id,
        dataset: "dandi-000576",
        recipe: { version: "default", default: true, lfp_clock_offset_s: 0 },
        event_types: [],
      }),
  });
  await page.goto(`${APP}s/_/dandi-000576/sub-01/ses-1`);
  await expect(page.locator('main[data-ready="session"]')).toBeVisible();
  const note = page.getByTestId("session-no-events");
  await expect(note).toContainText("dandi-000576 has no event recipe");
  await expect(note.getByRole("link", { name: "Open the file page" })).toHaveAttribute(
    "href",
    "/files/asset-000576-01",
  );
  expect(seen.events).toEqual(["asset-000576-01"]);
  expect(seen.rates).toHaveLength(0);
});

test("over 152 units the page asks for the first 152 and says so", async ({ page }) => {
  const units160 = JSON.parse(fixture("synthesis-units-160"));
  const seen = await stub(page, { units: units160, rates: () => ok(rates152()) });
  await page.goto(R2);
  await expect(page.locator('main[data-ready="session"]')).toBeVisible();
  expect(seen.rates[0]?.get("units")).toBe("0-151");
  await expect(page.getByTestId("session-capped")).toContainText(
    "showing the first 152 of 160 units",
  );
});

test("413 too_large: the sentence and the file page", async ({ page, expectError }) => {
  expectError("the stub answers the rates with 413 too_large");
  await stub(page, {
    rates: () => ({
      status: 413,
      body: { error: "too_large", detail: "the response is 6000000 bytes, over 5242880" },
    }),
  });
  await page.goto(R2);
  await expect(page.locator('main[data-ready="session"]')).toBeVisible();
  const refusal = page.getByTestId("session-refusal");
  await expect(refusal).toContainText("over the server's 5 MiB response cap");
  await expect(refusal.getByRole("link", { name: "Open the file page" })).toHaveAttribute(
    "href",
    `/files/${S.asset_id}`,
  );
  await expect(ab(page, "unit-row")).toHaveCount(0);
});

test("503 slow: the sentence and Try again, which draws the heatmap", async ({
  page,
  expectError,
}) => {
  expectError("the stub answers the first rates request with 503 slow");
  const seen = await stub(page, {
    rates: (_q, n) =>
      n === 1
        ? { status: 503, body: { error: "slow", detail: "the request took too long" } }
        : ok(RATES_CS62),
  });
  await page.goto(R2);
  const refusal = page.getByTestId("session-refusal");
  await expect(refusal).toContainText("did not finish the rates within its time limit");
  await refusal.getByRole("button", { name: "Try again" }).click();
  await expect(ab(page, "unit-row")).toHaveCount(44);
  expect(seen.rates).toHaveLength(2);
});

test("422 not_nwb: the sentence names it", async ({ page, expectError }) => {
  expectError("the stub answers the events with 422 not_nwb");
  await stub(page, {
    events: () => ({
      status: 422,
      body: { error: "not_nwb", detail: "the asset is not an NWB file" },
    }),
  });
  await page.goto(R2);
  await expect(page.locator('main[data-ready="session"]')).toBeVisible();
  await expect(page.getByTestId("session-refusal")).toContainText(
    "This file is not an NWB file, so it has no units or events to show.",
  );
});

const SUMMARY = JSON.parse(fixture("ux/session-summary.golden")) as {
  manifest: { built_at: string; units_table: unknown };
  parts: Record<string, unknown>;
};

/** Every request the page sends to a compute route or a catalog table route. */
function liveRequests(page: Page): string[] {
  const live: string[] = [];
  page.on("request", (req) => {
    if (req.method() === "OPTIONS") return;
    const url = new URL(req.url());
    if (!req.url().startsWith(API)) return;
    if (
      url.pathname.startsWith("/compute/") ||
      /^\/catalog\/assets\/[^/]+\/tables\//.test(url.pathname)
    )
      live.push(url.pathname);
  });
  return live;
}

test("a built summary with every part draws the heatmap with no live request", async ({ page }) => {
  expect(Object.keys(SUMMARY.parts)).toEqual(["events", "units", "rates-scene_cut"]);
  const parts = { ...SUMMARY.parts, "tuning-scene_cut-cut_kind": TUNING_CS62 };
  const seen = await stub(page, { summary: ok({ ...SUMMARY, parts }) });
  const live = liveRequests(page);
  await page.goto(R2);
  await expect(page.locator('main[data-ready="session"]')).toBeVisible();
  await expect(ab(page, "unit-row")).toHaveCount(44);
  await expect(ab(page, "region-group")).toHaveCount(4);
  await expect(page.getByTestId("session-from-summary")).toContainText(
    `From the stored summary built ${SUMMARY.manifest.built_at}`,
  );
  expect(seen.summary).toHaveLength(1);
  expect(live).toEqual([]);
  expect([seen.events, seen.units, seen.rates.length, seen.tuning.length]).toEqual([[], [], 0, 0]);
  await expect(ab(page, "tuning-mark")).toHaveCount(5);
  await expect(page.getByTestId("tuning-from-summary")).toContainText(SUMMARY.manifest.built_at);
});

test("a summary stored without the events and units parts asks for them live", async ({ page }) => {
  const { events: _e, units: _u, ...rates } = SUMMARY.parts;
  const { units_table: _t, ...manifest } = SUMMARY.manifest;
  const seen = await stub(page, { summary: ok({ ...SUMMARY, manifest, parts: rates }) });
  await page.goto(R2);
  await expect(page.locator('main[data-ready="session"]')).toBeVisible();
  await expect(ab(page, "unit-row")).toHaveCount(44);
  expect(seen.events).toEqual([S.asset_id]);
  expect(seen.units).toEqual([UNITS_PATH]);
  expect(seen.rates).toHaveLength(0);
});

test("a stored units part that is only the first page reads the units live", async ({ page }) => {
  const manifest = { ...SUMMARY.manifest, units_table: { rows: 200, more: true } };
  const seen = await stub(page, { summary: ok({ ...SUMMARY, manifest }) });
  await page.goto(R2);
  await expect(page.locator('main[data-ready="session"]')).toBeVisible();
  await expect(ab(page, "unit-row")).toHaveCount(44);
  expect(seen.events).toEqual([]);
  expect(seen.units).toEqual([UNITS_PATH]);
});

test("a built summary draws the heatmap with no /compute/rates request", async ({ page }) => {
  const seen = await stub(page, { summary: ok(SUMMARY) });
  await page.goto(R2);
  await expect(page.locator('main[data-ready="session"]')).toBeVisible();
  await expect(ab(page, "unit-row")).toHaveCount(44);
  await expect(page.getByTestId("session-from-summary")).toContainText(
    `From the stored summary built ${SUMMARY.manifest.built_at}`,
  );
  expect(seen.summary).toEqual([`?dataset=${S.dataset}&subject=${S.subject}&session=${S.session}`]);
  expect(seen.rates).toHaveLength(0);
});

test("a built summary without the chosen type's part computes that type live", async ({ page }) => {
  const seen = await stub(page, { summary: ok(SUMMARY) });
  await page.goto(R2);
  await expect(page.locator('main[data-ready="session"]')).toBeVisible();
  expect(seen.rates).toHaveLength(0);
  await page.getByTestId("event-recognition_onset").click();
  await expect(page.getByTestId("event-recognition_onset")).toHaveAttribute("aria-pressed", "true");
  await expect(page.locator('main[data-ready="session"]')).toBeVisible();
  await expect(page.getByTestId("session-from-summary")).toHaveCount(0);
  expect(seen.rates.map((q) => q.get("align"))).toEqual(["event:recognition_onset"]);
});

test("a missing summary computes live and says nothing of it", async ({ page }) => {
  const seen = await stub(page, { summary: ok({ status: "missing" }) });
  await page.goto(R2);
  await expect(page.locator('main[data-ready="session"]')).toBeVisible();
  expect(seen.rates).toHaveLength(1);
  await expect(page.getByTestId("session-from-summary")).toHaveCount(0);
});

test("a summary error never blocks the page", async ({ page, expectError }) => {
  expectError("the stub answers the summary with 502 summary_unreadable");
  const seen = await stub(page, {
    summary: { status: 502, body: { error: "summary_unreadable", detail: "AccessDenied" } },
  });
  await page.goto(R2);
  await expect(page.locator('main[data-ready="session"]')).toBeVisible();
  await expect(ab(page, "unit-row")).toHaveCount(44);
  await expect(page.getByTestId("session-refusal")).toHaveCount(0);
  expect(seen.rates).toHaveLength(1);
});

// ---- the exploratory tuning screen and the small multiples ----

/** The CS62 units the tuning fixture marks: every ninth row from the fourth. */
const SCREENED = UNITS_CS62.rows.filter((_, i) => i % 9 === 3).map((r) => r.unit_index);

test("no stored tuning part: one live /compute/tuning on open marks the passed units", async ({
  page,
}) => {
  const seen = await stub(page);
  await page.goto(R2);
  await expect(page.locator('main[data-ready="session"]')).toBeVisible();
  expect(seen.tuning.map((q) => [q.get("event"), q.get("attribute"), q.get("units")])).toEqual([
    ["scene_cut", "cut_kind", "all"],
  ]);
  const marks = ab(page, "tuning-mark");
  await expect(marks).toHaveCount(SCREENED.length);
  const marked = await marks.evaluateAll((els) =>
    els.map((el) => Number(el.getAttribute("data-unit"))),
  );
  expect(marked.sort((a, b) => a - b)).toEqual([...SCREENED].sort((a, b) => a - b));
  const summary = ab(page, "tuning-summary");
  await expect(summary).toContainText("Exploratory");
  await expect(summary).toContainText("change (13) and continuity (80)");
  await expect(summary).toContainText("not a confirmed result");
  await expect(ab(page, "tuning-region")).toHaveCount(4);
  const counts = await ab(page, "tuning-region").locator(".sh-tuning-count").allTextContents();
  const passed = counts.map((c) => Number(c.split("/")[0])).reduce((a, b) => a + b, 0);
  const total = counts.map((c) => Number(c.split("/")[1])).reduce((a, b) => a + b, 0);
  expect([passed, total]).toEqual([SCREENED.length, 44]);
  await noScroll(page);
});

test("a tuning refusal is the server's sentence in the rail and never blocks the heatmap", async ({
  page,
  expectError,
}) => {
  expectError("the stub refuses the tuning screen with 422 too_few_events_per_level");
  await stub(page, {
    tuning: () => ({
      status: 422,
      body: {
        error: "too_few_events_per_level",
        detail: "change has 3 events; the screen needs 12 per level",
      },
    }),
  });
  await page.goto(R2);
  await expect(page.locator('main[data-ready="session"]')).toBeVisible();
  await expect(ab(page, "unit-row")).toHaveCount(44);
  await expect(ab(page, "tuning-mark")).toHaveCount(0);
  await expect(page.getByTestId("tuning-none")).toContainText("change has 3 events");
});

test("Small multiples: one PSTH per unit, held in the URL, back to the heatmap", async ({
  page,
}) => {
  await stub(page);
  await page.goto(R2);
  await expect(page.locator('main[data-ready="session"]')).toBeVisible();
  await page.getByTestId("view-multiples").click();
  await expect(page).toHaveURL(/\?view=multiples$/);
  await expect(ab(page, "multiple")).toHaveCount(44);
  await expect(ab(page, "unit-row")).toHaveCount(0);
  await noScroll(page);
  await page.reload();
  await expect(page.locator('main[data-ready="session"]')).toBeVisible();
  await expect(page.getByTestId("view-multiples")).toHaveAttribute("aria-pressed", "true");
  await expect(ab(page, "multiple")).toHaveCount(44);
  await page.getByTestId("view-heatmap").click();
  await expect(page).not.toHaveURL(/view=/);
  await expect(ab(page, "unit-row")).toHaveCount(44);
});

test("152 units in small multiples fit 1440x900 with no scroll", async ({ page }) => {
  await stub(page, {
    units: { rows: UNITS_152.rows, page: 0, more: false },
    rates: () => ok(rates152()),
    tuning: () => ok(tuningBody(UNITS_152.rows)),
  });
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto(`${R2}?view=multiples`);
  await expect(page.locator('main[data-ready="session"]')).toBeVisible();
  await expect(ab(page, "multiple")).toHaveCount(152);
  await noScroll(page);
});

test("Copy code on the session page copies the notebook snippet for this session", async ({
  page,
  context,
}) => {
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  await stub(page);
  await page.goto(R2);
  await expect(page.locator('main[data-ready="session"]')).toBeVisible();
  const button = page.getByRole("button", { name: "Copy code" });
  await button.focus();
  await expect(page.getByRole("tooltip")).toContainText("session's stored summary");
  await page.keyboard.press("Enter");
  await expect(page.getByTestId("toast")).toContainText("Copied — paste into a notebook cell");
  const text = await page.evaluate(() => navigator.clipboard.readText());
  expect(text).toContain("import asteroidea");
  expect(text).toContain("asteroidea.sign_in()");
  expect(text).toContain(
    `asteroidea.session_summary("${S.dataset}", "${S.subject}", "${S.session}")`,
  );
});
