// The participant page's link into the session page. The session page is
// the session heatmap; its own spec is session.spec.ts. Runs against the
// sign-in env build (port WEB_PORT + 1); the API is served by page.route: the participant
// page's dataset fixture, then the session page's studies body, events, units table and rates.
import { expect, type Page } from "@playwright/test";
import { CATALOG_FIXTURE as F, RATES_CS62 } from "../src/artboard/fixture";
import { EVENTS_CS62, TUNING_CS62 } from "../src/artboard/surfaces/session";
import {
  API,
  APP,
  CORS,
  fixture,
  stubBase,
  stubEmptyLabels,
  stubEmptyUploads,
  test,
} from "./support";

test.skip(
  process.env.E2E_LIVE === "1",
  "needs the fixture-mode auth build (no live server has the auth env)",
);

async function stub(page: Page): Promise<string[]> {
  await stubBase(page);
  await stubEmptyUploads(page);
  await stubEmptyLabels(page, "asset-0201");
  const assets: string[] = [];
  await page.route(`${API}/**`, (route) => {
    const req = route.request();
    if (req.method() === "OPTIONS") return route.fulfill({ status: 204, headers: CORS });
    const url = new URL(req.url());
    let body: unknown = null;
    if (url.pathname === "/catalog/datasets/dandi-000623")
      body = JSON.parse(fixture("catalog-dataset"));
    else if (
      url.pathname === "/catalog/datasets/dandi-000623/assets" &&
      url.searchParams.get("subject") === "sub-CS62"
    )
      body = JSON.parse(fixture("synthesis-files"));
    else if (url.pathname === "/catalog/studies") body = F.studies;
    else if (url.pathname === "/catalog/links") body = { links: [] };
    else if (url.pathname.endsWith("/tables/units"))
      body = JSON.parse(fixture("synthesis-units-44"));
    else if (url.pathname === "/compute/events") {
      assets.push(url.searchParams.get("asset_id") ?? "");
      body = EVENTS_CS62;
    } else if (url.pathname === "/compute/rates") body = RATES_CS62;
    else if (url.pathname === "/compute/tuning") body = TUNING_CS62;
    else return route.fallback();
    return route.fulfill({
      status: body === null ? 404 : 200,
      contentType: "application/json",
      headers: CORS,
      body: JSON.stringify(body ?? { error: "not_found", detail: "no route" }),
    });
  });
  return assets;
}

test("the participant page links each session to the session page, which opens the session's heatmap", async ({
  page,
}) => {
  const assets = await stub(page);
  await page.goto(`${APP}data/dandi-000623/sub-CS62`);
  await expect(page.locator('main[data-ready="participant"]')).toBeVisible();
  // The participant page's own glyph reads are not the session page's.
  assets.length = 0;
  await page.locator('a[href$="/dandi-000623/sub-CS62/ses-1"]').click();
  await expect(page).toHaveURL(/\/s\/movie-watching\/dandi-000623\/sub-CS62\/ses-1$/);
  await expect(page.locator('main[data-ready="session"]')).toBeVisible();
  await expect(page.getByTestId("session-page")).toBeVisible();
  // ses-1 of CS62 in the studies body: its NWB file with the most units.
  expect(assets).toEqual(["asset-cs62-1"]);
  await expect(page.locator('[data-ab="unit-row"]')).toHaveCount(44);
  await expect(page).toHaveTitle("Studies - asteroidea");
});
