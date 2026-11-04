// The data home (it replaces the Data page's spec, catalog.spec.ts): studies, the
// modality matrix, the MRI-and-ephys rail with pending links dashed, the sandbox fold, and the server's
// refusal text. Runs against the sign-in env build (port WEB_PORT + 1); the API is served by
// page.route, so nothing leaves the machine.
import { expect, type Page } from "@playwright/test";
import { CATALOG_FIXTURE as F } from "../src/artboard/fixture";
import { API, APP, CORS, stubBase, stubEmptyUploads, test } from "./support";

test.skip(
  process.env.E2E_LIVE === "1",
  "needs the fixture-mode auth build (no live server has the auth env)",
);

interface Seen {
  urls: string[];
  auth: (string | undefined)[];
}

type Override = { status: number; body: string } | null;

async function stub(page: Page, studies: Override = null): Promise<Seen> {
  await stubBase(page);
  const seen: Seen = { urls: [], auth: [] };
  await stubEmptyUploads(page);
  await page.route(`${API}/catalog/**`, (route) => {
    const req = route.request();
    if (req.method() === "OPTIONS") return route.fulfill({ status: 204, headers: CORS });
    const url = new URL(req.url());
    seen.urls.push(`${url.pathname}${url.search}`);
    seen.auth.push(req.headers().authorization);
    let reply: { status: number; body: string } = { status: 404, body: '{"error":"no route"}' };
    if (url.pathname === "/catalog/studies") {
      reply = studies ?? { status: 200, body: JSON.stringify(F.studies) };
    } else if (url.pathname === "/catalog/links") {
      reply = { status: 200, body: JSON.stringify(F.links) };
    }
    return route.fulfill({
      status: reply.status,
      contentType: "application/json",
      headers: CORS,
      body: reply.body,
    });
  });
  return seen;
}

test("the home lists a row per study, a column per modality and the rail, from the two routes", async ({
  page,
}) => {
  const seen = await stub(page);
  await page.goto(APP);
  await expect(page.locator('main[data-ready="home"]')).toBeVisible();

  await expect(page.getByTestId("study-row")).toHaveCount(F.studies.studies.length);
  for (const s of F.studies.studies) {
    await expect(page.getByRole("link", { name: s.title, exact: true })).toBeVisible();
  }
  const columns = page.locator('[data-ab="matrix-col"]');
  await expect(columns.first()).toHaveText("Units");
  await expect(columns).toHaveCount(8);
  // A release badge carries the full version string and links to its dataset.
  await expect(
    page.getByRole("link", { name: /^DANDI 000623 · 0\.240227\.2023 · ephys$/ }),
  ).toHaveAttribute("href", "/d/dandi-000623");

  expect([...seen.urls].sort()).toEqual(["/catalog/links", "/catalog/studies"]);
  expect(new Set(seen.auth)).toEqual(new Set(["Bearer access-1"]));
});

test("the theme toggle cycles to Dark, recolours the page and survives a reload", async ({
  page,
}) => {
  await stub(page);
  await page.goto(APP);
  await expect(page.locator('main[data-ready="home"]')).toBeVisible();
  const html = page.locator("html");
  const surface = () => page.evaluate(() => getComputedStyle(document.body).backgroundColor);
  await expect(html).not.toHaveAttribute("data-theme", /.*/);

  const toggle = page.getByRole("button", { name: /^Theme: System/ });
  await toggle.focus();
  await page.keyboard.press("Enter"); // keyboard-operable: System -> Light
  await expect(html).toHaveAttribute("data-theme", "light");
  expect(await surface()).toBe("rgb(255, 255, 255)");
  await page.getByRole("button", { name: /^Theme: Light/ }).click();
  await expect(html).toHaveAttribute("data-theme", "dark");
  expect(await surface()).toBe("rgb(13, 17, 23)");
  await expect(page.getByRole("button", { name: "Theme: Dark. Switch to System" })).toBeVisible();

  await page.reload();
  await expect(page.locator('main[data-ready="home"]')).toBeVisible();
  await expect(html).toHaveAttribute("data-theme", "dark");
  expect(await surface()).toBe("rgb(13, 17, 23)");
});

test("pending links are dashed and a confirmed one is solid, each with its words", async ({
  page,
}) => {
  await stub(page);
  await page.goto(APP);
  await expect(page.locator('main[data-ready="home"]')).toBeVisible();
  const entries = page.locator('[data-ab="rail-entry"]');
  await expect(entries).toHaveCount(3);
  const pending = page.locator('[data-ab-line="dashed"] .home-link');
  const confirmed = page.locator('[data-ab-line="solid"] .home-link');
  await expect(pending).toHaveText(["link pending", "link pending"]);
  await expect(confirmed).toHaveText(["link confirmed"]);
  const style = (loc: typeof pending) =>
    loc.first().evaluate((el) => getComputedStyle(el).borderTopStyle);
  expect(await style(pending)).toBe("dashed");
  expect(await style(confirmed)).toBe("solid");
});

test("a region is never drawn without its label", async ({ page }) => {
  await stub(page);
  await page.goto(APP);
  await expect(page.locator('main[data-ready="home"]')).toBeVisible();
  const segments = await page.locator('[data-ab="region-seg"]').count();
  const labels = await page.locator(".home-regions li").count();
  expect(segments).toBeGreaterThan(0);
  expect(labels).toBe(segments);
  const texts = await page.locator(".home-regions li").allTextContents();
  expect(texts.every((t) => t.trim().length > 0)).toBe(true);
  expect(texts.some((t) => t.includes("Left vmPFC"))).toBe(true);
});

test("the sandbox is one dashed line; its toggle shows the list", async ({ page }) => {
  await stub(page);
  await page.goto(APP);
  const fold = page.locator('[data-ab="sandbox-fold"]');
  const count = F.studies.datasets.filter((d) => d.tier === "sandbox").length;
  await expect(fold).toContainText(`${count} sandbox datasets hidden`);
  await expect(fold).toContainText("conc-… ");
  await expect(fold.locator("li").first()).toBeHidden();
  await fold.getByText("Show").click();
  await expect(fold.locator("li")).toHaveCount(count);
});

test("the tier switch shows Mine and the Sandbox list", async ({ page }) => {
  await stub(page);
  await page.goto(APP);
  await expect(page.locator('main[data-ready="home"]')).toBeVisible();
  await page.getByRole("button", { name: /^Sandbox/ }).click();
  await expect(page.getByRole("heading", { name: /sandbox datasets$/ })).toBeVisible();
  await expect(page.getByTestId("study-row")).toHaveCount(0);
  await page.getByRole("button", { name: /^Mine/ }).click();
  await expect(page.getByText("No uploads of yours yet")).toBeVisible();
  await page.getByRole("button", { name: /^Curated studies/ }).click();
  await expect(page.getByTestId("study-row")).toHaveCount(F.studies.studies.length);
});

/** Studies with at least one participant of `modality`, from the fixture. */
function studiesWith(modality: string): number {
  return F.studies.studies.filter((s) =>
    s.datasets.some((r) =>
      F.studies.datasets
        .find((d) => d.dataset === r.dataset)
        ?.subjects.some((x) => x.modalities.includes(modality)),
    ),
  ).length;
}

test("the tier and the facets are in the URL: a view is a link, Back undoes a choice, a reload keeps it", async ({
  page,
}) => {
  await stub(page);
  await page.goto(APP);
  await expect(page.locator('main[data-ready="home"]')).toBeVisible();
  const rows = page.getByTestId("study-row");
  await expect(rows).toHaveCount(F.studies.studies.length);

  await page.locator('[data-facet="modality"][data-value="anat"]').click();
  await expect(page).toHaveURL(/\/\?modality=anat$/);
  const anat = studiesWith("anat");
  expect(anat).toBeLessThan(F.studies.studies.length);
  await expect(rows).toHaveCount(anat);
  await expect(
    page.getByRole("heading", { level: 2, name: /of 5 curated studies$/ }),
  ).toContainText(`${anat} of`);
  await expect(page.locator('[data-facet="modality"][data-value="anat"]')).toHaveAttribute(
    "aria-pressed",
    "true",
  );

  await page.locator('[data-facet="species"][data-value="Homo sapiens"]').click();
  await expect(page).toHaveURL(/\/\?modality=anat&species=Homo\+sapiens$/);

  await page.goBack();
  await expect(page).toHaveURL(/\/\?modality=anat$/);
  await page.goBack();
  await expect(page).not.toHaveURL(/\?/);
  await expect(rows).toHaveCount(F.studies.studies.length);
  await page.goForward();
  await expect(rows).toHaveCount(anat);

  await page.reload();
  await expect(page.locator('main[data-ready="home"]')).toBeVisible();
  await expect(rows).toHaveCount(anat);

  await page.getByRole("button", { name: /^Mine/ }).click();
  await expect(page).toHaveURL(/\/\?modality=anat&tier=mine$|\/\?tier=mine&modality=anat$/);
  await expect(rows).toHaveCount(0);
  await page.goBack();
  await expect(rows).toHaveCount(anat);
});

test("a link to a faceted view opens it narrowed; a facet nothing has says so and clears", async ({
  page,
}) => {
  await stub(page);
  await page.goto(`${APP}?tier=curated&region=Nowhere`);
  await expect(page.locator('main[data-ready="home"]')).toBeVisible();
  await expect(page.getByTestId("home-no-match")).toContainText("No study matches these facets");
  await page.getByRole("button", { name: "Clear facets" }).click();
  await expect(page).not.toHaveURL(/region=/);
  await expect(page.getByTestId("study-row")).toHaveCount(F.studies.studies.length);
});

test("375 px wide: the facets are chips, the page does not scroll sideways, a chip narrows", async ({
  page,
}) => {
  await stub(page);
  await page.setViewportSize({ width: 375, height: 812 });
  await page.goto(APP);
  await expect(page.locator('main[data-ready="home"]')).toBeVisible();
  const chip = page.locator('[data-facet="modality"][data-value="anat"]');
  await expect(chip).toBeVisible();
  const box = await chip.boundingBox();
  expect(box?.width ?? 0).toBeLessThan(200);
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
  expect(overflow).toBeLessThanOrEqual(0);
  await chip.click();
  await expect(page).toHaveURL(/modality=anat/);
  await expect(page.getByTestId("study-row")).toHaveCount(studiesWith("anat"));
});

test("a 502 says the catalog didn't answer, folds Athena's reason and offers a retry", async ({
  page,
  expectError,
}) => {
  expectError("502 refusal");
  const text = "the catalog query failed: SYNTAX_ERROR: line 1:8: Column 'x' cannot be resolved";
  const seen = await stub(page, { status: 502, body: JSON.stringify({ error: text }) });
  await page.goto(APP);
  const error = page.getByTestId("catalog-error");
  await expect(error.getByRole("heading")).toHaveText("The catalog didn't answer");
  await expect(error).toContainText("We couldn't load the studies list just now.");
  await expect(error.locator(".state-code")).toHaveText("code: upstream_failed");
  // The reason is kept, folded under "Technical detail".
  const detail = error.locator("details");
  await expect(detail.locator("code")).toBeHidden();
  await detail.locator("summary").click();
  await expect(detail.locator("code")).toHaveText(text);
  const first = seen.urls.filter((u) => u === "/catalog/studies").length;
  await error.getByRole("button", { name: "Try again" }).click();
  await expect.poll(() => seen.urls.filter((u) => u === "/catalog/studies").length).toBe(first + 1);
});

/** Answer `/artifacts` with `status`, or never answer it (`hang`). Registered after stubBase, so it wins. */
async function stubLatest(page: Page, answer: { status: number } | "hang"): Promise<string[]> {
  const asked: string[] = [];
  await page.route(
    (url) => url.origin === API && url.pathname === "/artifacts",
    (route) => {
      if (route.request().method() === "OPTIONS") {
        return route.fulfill({ status: 204, headers: CORS });
      }
      asked.push(route.request().url());
      if (answer === "hang") return undefined;
      return route.fulfill({
        status: answer.status,
        contentType: "application/json",
        headers: CORS,
        body: JSON.stringify({ message: "Service Unavailable" }),
      });
    },
  );
  return asked;
}

test("a failing results list leaves the home ready; the card names the HTTP status", async ({
  page,
  expectError,
}) => {
  expectError("the /artifacts listing answers 503");
  await stub(page);
  const asked = await stubLatest(page, { status: 503 });
  await page.goto(APP);
  await expect(page.locator('main[data-ready="home"]')).toBeVisible();
  await expect(page.getByTestId("study-row")).toHaveCount(F.studies.studies.length);
  await expect(page.getByTestId("home-latest-error")).toHaveText(
    "The latest results could not be listed: the platform API answered HTTP 503.",
  );
  expect(asked.map((u) => new URL(u).search)).toEqual(["?scope=all"]);
});

test("a results list that never answers leaves the home ready; the card says it is listing", async ({
  page,
}) => {
  await stub(page);
  const asked = await stubLatest(page, "hang");
  await page.goto(APP);
  await expect(page.locator('main[data-ready="home"]')).toBeVisible();
  await expect(page.getByTestId("study-row")).toHaveCount(F.studies.studies.length);
  const card = page.getByTestId("home-latest");
  await expect(card).toHaveAttribute("data-state", "loading");
  await expect(card).toContainText("Listing the latest results…");
  await expect.poll(() => asked.length).toBe(1);
});

test("with no runs the card says so", async ({ page }) => {
  await stub(page);
  await page.goto(APP);
  await expect(page.locator('main[data-ready="home"]')).toBeVisible();
  await expect(page.getByTestId("home-latest")).toContainText("No saved runs yet.");
});

test("a 503 shows the slow text and a retry that asks again", async ({ page, expectError }) => {
  expectError("503 slow text");
  const text = "the catalog is slow to answer; try again";
  const seen = await stub(page, { status: 503, body: JSON.stringify({ error: text }) });
  await page.goto(APP);
  await expect(page.getByTestId("catalog-error")).toContainText(text);
  const first = seen.urls.filter((u) => u === "/catalog/studies").length;
  await page.getByRole("button", { name: "Try again" }).click();
  await expect.poll(() => seen.urls.filter((u) => u === "/catalog/studies").length).toBe(first + 1);
});
