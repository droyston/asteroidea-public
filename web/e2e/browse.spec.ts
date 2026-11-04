// Browse flow (live mode) plus fixture-only load-state checks.
import { readFileSync } from "node:fs";
import { expect, type Page } from "@playwright/test";
import { test } from "./support";

const live = process.env.E2E_LIVE === "1";
const golden = readFileSync(
  new URL("../../services/tests/fixtures/harvest/expected-index.json", import.meta.url),
  "utf8",
);

async function serveIndex(page: Page, status: number, body: string): Promise<void> {
  await page.route("**/index.json", (route) =>
    route.fulfill({ status, contentType: "application/json", body }),
  );
}

function matchCount(page: Page) {
  return page.getByTestId("match-count");
}

async function shownCount(page: Page): Promise<number> {
  const text = (await matchCount(page).textContent()) ?? "";
  const m = /^([\d,]+) of ([\d,]+) datasets$/.exec(text.trim());
  if (m === null) throw new Error(`match count text not understood: ${JSON.stringify(text)}`);
  return Number((m[1] ?? "").replaceAll(",", ""));
}

test("rows 3-5: open, choose human species, tick Units and LFP", async ({ page }) => {
  if (!live) await serveIndex(page, 200, golden);

  // Open the URL; the page shows the harvest summary, the whole-index matrix and at most four fits.
  await page.goto("/discover");
  const summary = page.getByTestId("index-summary");
  await expect(summary).toContainText("Index harvested");
  await expect(summary).toContainText(/dandi: \d+ ok of \d+ listed, \d+ failed/);
  await expect(summary).toContainText(/openneuro: \d+ ok of \d+ listed, \d+ failed/);
  const total = await shownCount(page);
  expect(total).toBeGreaterThan(0);
  await expect(matchCount(page)).toHaveText(
    `${total.toLocaleString("en-US")} of ${total.toLocaleString("en-US")} datasets`,
  );
  await expect(page.locator('[data-ab="matrix-cell"]').first()).toBeVisible();
  await expect(page.getByTestId("best-fit").locator("li")).toHaveCount(Math.min(total, 4));
  await expect(page.getByTestId("find-more")).toContainText("more fit");

  // The Human species facet; the count and the URL follow.
  const species = page.getByRole("group", { name: "Species" });
  const human = species.getByRole("checkbox", { name: /^Human \d+$/ });
  const label = (await human.locator("xpath=..").textContent()) ?? "";
  const expected = Number(/(\d+)\s*$/.exec(label.replace(/,/g, ""))?.[1]);
  await human.check();
  await expect(page).toHaveURL(/[?&]sp=human\b/);
  await expect(matchCount(page)).toHaveText(
    `${expected.toLocaleString("en-US")} of ${total.toLocaleString("en-US")} datasets`,
  );

  // Tick Units and LFP; a short list that includes 000576.
  const contains = page.getByRole("group", { name: "Contains" });
  await contains.getByRole("checkbox", { name: /^Units \d+$/ }).check();
  await contains.getByRole("checkbox", { name: /^LFP \d+$/ }).check();
  await expect(page).toHaveURL(/has=units%2Clfp/);
  await expect(page.getByTestId("row-dandi-000576")).toBeVisible();
  const short = await shownCount(page);
  expect(short).toBeGreaterThanOrEqual(1);
  expect(short).toBeLessThan(expected);
});

test("a matrix cell filters, and its count is the match count", async ({ page }) => {
  if (!live) await serveIndex(page, 200, golden);
  await page.goto("/discover");
  const cell = page.getByRole("button", { name: /^Human, Units: \d+ datasets$/ });
  const n = Number(/: (\d+) datasets$/.exec((await cell.getAttribute("aria-label")) ?? "")?.[1]);
  await cell.click();
  await expect(cell).toHaveAttribute("aria-pressed", "true");
  await expect(page).toHaveURL(/sp=human&has=units/);
  expect(await shownCount(page)).toBe(n);
  // A second click clears both.
  await cell.click();
  await expect(page).not.toHaveURL(/sp=human/);
});

test("filters in the URL: a copied link draws the same list, and a reload keeps it", async ({
  page,
}) => {
  if (!live) await serveIndex(page, 200, golden);
  await page.goto("/discover?sp=human&has=units,lfp");
  await expect(page.getByRole("checkbox", { name: /^Human \d+$/ })).toBeChecked();
  await expect(page.getByRole("checkbox", { name: /^LFP \d+$/ })).toBeChecked();
  const n = await shownCount(page);
  await page.reload();
  expect(await shownCount(page)).toBe(n);
  await page.getByRole("button", { name: "Clear filters" }).click();
  await expect(page).toHaveURL(/\/discover$/);
});

test.describe("load states (fixture mode only)", () => {
  test.skip(live, "fixture mode routes index.json; live mode reads the harvested file");

  test("a missing index says to run make harvest", async ({ page, expectError }) => {
    expectError("404 index");
    await page.route("**/index.json", (route) => route.fulfill({ status: 404, body: "" }));
    await page.goto("/discover");
    await expect(page.getByRole("alert")).toContainText("No index: run make harvest");
  });

  test("a schema mismatch names the value found", async ({ page, expectError }) => {
    expectError("the index is refused with an error Notice");
    const doc = JSON.parse(golden) as Record<string, unknown>;
    doc.schema = "asteroidea.dataset-index/3";
    await serveIndex(page, 200, JSON.stringify(doc));
    await page.goto("/discover");
    await expect(page.getByRole("alert")).toContainText(
      'unsupported schema string "asteroidea.dataset-index/3"',
    );
  });

  test("a server error names the status", async ({ page, expectError }) => {
    expectError("server error status");
    await serveIndex(page, 500, "{}");
    await page.goto("/discover");
    await expect(page.getByRole("alert")).toContainText("HTTP 500");
  });
});

// Layout: the page never scrolls sideways at a phone width or at the desktop width.
for (const [name, width, height] of [
  ["375 px", 375, 812],
  ["1440 x 900", 1440, 900],
] as const) {
  test(`layout at ${name}: no horizontal scroll on /discover`, async ({ page }) => {
    if (!live) await serveIndex(page, 200, golden);
    await page.setViewportSize({ width, height });
    await page.goto("/discover");
    await expect(page.getByTestId("index-summary")).toContainText("Index harvested");
    await expect(page.getByRole("heading", { name: "asteroidea" })).toBeVisible();
    const widths = await page.evaluate(() => ({
      scroll: document.documentElement.scrollWidth,
      client: document.documentElement.clientWidth,
    }));
    expect(widths.scroll).toBeLessThanOrEqual(widths.client);
    const shots = process.env.E2E_SHOTS;
    if (shots !== undefined) await page.screenshot({ path: `${shots}/datasets-${width}.png` });
  });
}

test("keyboard focus is visible on a Button", async ({ page }) => {
  if (!live) await serveIndex(page, 200, golden);
  await page.goto("/discover");
  const clear = page.getByRole("button", { name: "Clear filters" });
  await expect(clear).toBeVisible();
  await clear.focus();
  await page.keyboard.press("Shift+Tab");
  await page.keyboard.press("Tab");
  await expect(clear).toBeFocused();
  const ring = await clear.evaluate((el) => {
    const s = getComputedStyle(el);
    return { style: s.outlineStyle, width: s.outlineWidth };
  });
  expect(ring.style).not.toBe("none");
  expect(ring.width).toBe("2px");
});
