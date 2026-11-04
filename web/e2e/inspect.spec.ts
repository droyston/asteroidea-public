// Flexible inspection in the fixture-mode sign-in build: `?view=inspect`
// on the session page asks `/compute/rates?...&raster=1` once, then every control (normalisation,
// baseline and response windows, bin, scales, axes) recomputes in the browser with no request and
// is kept in the URL. Bodies: CATALOG_FIXTURE, the CS62 units table, the resolver's CS62
// events, and the raster body from `fixtures/ux/gen_inspect.py`.
import { expect, type Page } from "@playwright/test";
import { CATALOG_FIXTURE as F, UNITS_CS62 } from "../src/artboard/fixture";
import { RASTER_CS62 } from "../src/artboard/surfaces/inspect";
import { EVENTS_CS62, TUNING_CS62 } from "../src/artboard/surfaces/session";
import { API, APP, CORS, stubBase, stubEmptyUploads, test } from "./support";

test.skip(
  process.env.E2E_LIVE === "1",
  "needs the fixture-mode auth build (no live server has the auth env)",
);

const S = F.session;
const R2 = `${APP}s/${S.study}/${S.dataset}/${S.subject}/${S.session}`;
const INSPECT = `${R2}?view=inspect&align=scene_cut`;

type Reply = { status: number; body: unknown };

async function stub(page: Page, rates?: () => Reply): Promise<URLSearchParams[]> {
  const seen: URLSearchParams[] = [];
  await stubBase(page);
  await stubEmptyUploads(page);
  await page.route(`${API}/**`, async (route) => {
    const req = route.request();
    if (req.method() === "OPTIONS") return route.fulfill({ status: 204, headers: CORS });
    const url = new URL(req.url());
    let reply: Reply;
    if (url.pathname === "/catalog/studies") reply = { status: 200, body: F.studies };
    else if (url.pathname === "/catalog/session-summary")
      reply = { status: 200, body: { status: "missing" } };
    else if (url.pathname.endsWith("/tables/units")) reply = { status: 200, body: UNITS_CS62 };
    else if (url.pathname === "/compute/events") reply = { status: 200, body: EVENTS_CS62 };
    else if (url.pathname === "/compute/tuning") reply = { status: 200, body: TUNING_CS62 };
    else if (url.pathname === "/compute/rates") {
      seen.push(url.searchParams);
      reply =
        rates?.() ??
        (url.searchParams.get("raster") === "1"
          ? { status: 200, body: RASTER_CS62 }
          : { status: 404, body: { error: "not_in_fixture" } });
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
  expect(m.sh, "the inspect view scrolls at 1440x900").toBeLessThanOrEqual(m.ih);
}

async function rankedText(page: Page): Promise<string[]> {
  return ab(page, "ranked-row").allTextContents();
}

test("the heatmap's Inspect link opens the inspect view, which asks for the raster once", async ({
  page,
}) => {
  // The heatmap's own request (raster=0) gets the same body; it carries z per unit too.
  const seen = await stub(page, () => ({ status: 200, body: RASTER_CS62 }));
  await page.goto(R2);
  await expect(page.locator('main[data-ready="session"]')).toBeVisible();
  const link = page.getByTestId("session-inspect-link");
  await expect(link).toHaveAttribute("href", /\?view=inspect&align=scene_cut$/);
  await link.click();
  await expect(page).toHaveURL(/\?view=inspect&align=scene_cut$/);
  await expect(page.getByTestId("inspect-page")).toBeVisible();
  const raster = seen.filter((q) => q.get("raster") === "1");
  expect(
    raster.map((q) => [q.get("align"), q.get("pre_s"), q.get("post_s"), q.get("bin_s")]),
  ).toEqual([["event:scene_cut", "1", "2", "0.05"]]);
});

test("inspect view: control row, 43 dots, 4 strips, the top 12, the local note and no Save", async ({
  page,
}) => {
  const seen = await stub(page);
  await page.goto(INSPECT);
  await expect(page.locator('main[data-ready="session"]')).toBeVisible();
  await expect(ab(page, "control-row")).toHaveCount(1);
  await expect(ab(page, "scatter-dot")).toHaveCount(43);
  await expect(ab(page, "strip")).toHaveCount(4);
  await expect(ab(page, "strip-dot")).toHaveCount(43);
  await expect(ab(page, "ranked-row")).toHaveCount(12);
  await expect(page.getByTestId("inspect-note")).toHaveText(
    "Computed in your browser · nothing is saved",
  );
  await expect(page.getByRole("button", { name: /save/i })).toHaveCount(0);
  await expect(page.getByTestId("inspect-undefined")).toContainText("unit 43");
  await expect(page.getByText("Inspect units", { exact: true })).toBeVisible();
  expect(seen).toHaveLength(1);
  await noScroll(page);
});

test("every control recomputes in the browser with no request and is kept in the URL", async ({
  page,
}) => {
  const seen = await stub(page);
  await page.goto(INSPECT);
  await expect(page.locator('main[data-ready="session"]')).toBeVisible();
  const before = await rankedText(page);

  // Drag the baseline start from -1.00 to -0.50 s.
  await page.getByTestId("baseline-start").fill("-0.5");
  await expect(page.getByTestId("baseline-text")).toHaveText("−0.50 to 0.00 s");
  await expect(page).toHaveURL(/base=-0\.5%2C0/);
  const moved = await rankedText(page);
  expect(moved).not.toEqual(before);

  await page.getByTestId("response-end").fill("0.5");
  await expect(page.getByTestId("response-text")).toHaveText("0.00 to 0.50 s");
  await page.getByTestId("bin-0.1").click();
  await page.getByTestId("norm-pct").click();
  await expect(page.locator(".in-kicker").first()).toContainText("% change");
  await page.getByTestId("scales-unit").click();
  await page.getByTestId("inspect-x").selectOption("spikes");
  await page.getByTestId("inspect-y").selectOption("response_hz");
  await expect(page).toHaveURL(/norm=pct/);
  await expect(page).toHaveURL(/scales=unit/);
  await expect(page).toHaveURL(/x=spikes/);
  await expect(ab(page, "scatter-dot")).toHaveCount(44);

  // Per-unit scales put every ranked value within ±1.
  for (const row of await rankedText(page)) {
    const v = Number(row.slice(-4).replace("−", "-"));
    expect(Math.abs(v)).toBeLessThanOrEqual(1);
  }

  // Reload: the state comes back from the URL, still from one request per load.
  const url = page.url();
  await page.reload();
  await expect(page.locator('main[data-ready="session"]')).toBeVisible();
  expect(page.url()).toBe(url);
  await expect(page.getByTestId("norm-pct")).toHaveAttribute("aria-pressed", "true");
  await expect(page.getByTestId("bin-0.1")).toHaveAttribute("aria-pressed", "true");
  expect(seen).toHaveLength(2);
});

test("back to the heatmap leaves the inspect view", async ({ page }) => {
  await stub(page, () => ({ status: 200, body: RASTER_CS62 }));
  await page.goto(INSPECT);
  await expect(page.getByTestId("inspect-page")).toBeVisible();
  await page.getByTestId("inspect-heatmap-link").click();
  await expect(page.getByTestId("session-heatmap")).toBeVisible();
  await expect(page).not.toHaveURL(/view=/);
});

test("a rates refusal is the server's sentence with Try again", async ({ page, expectError }) => {
  expectError("the stub answers the raster request with 413 too_large");
  await stub(page, () => ({
    status: 413,
    body: { error: "too_large", detail: "the raster would be about 6000000 bytes" },
  }));
  await page.goto(INSPECT);
  await expect(page.locator('main[data-ready="session"]')).toBeVisible();
  await expect(page.getByTestId("inspect-refusal")).toContainText("6000000 bytes");
});
