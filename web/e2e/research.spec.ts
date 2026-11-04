// The research views: the dataset address (the study page in one-dataset mode), a
// a participant's page, the file page, and the deep links. Runs against the sign-in env build (port WEB_PORT + 1); the API is served by page.route.
import { expect, type Page } from "@playwright/test";
import {
  API,
  APP,
  CORS,
  fixture,
  kvValue,
  stubBase,
  stubEmptyLabels,
  stubEmptyUploads,
  test,
} from "./support";

test.skip(
  process.env.E2E_LIVE === "1",
  "needs the fixture-mode auth build (no live server has the auth env)",
);

const DOWNLOAD_URL =
  "https://raw.asteroidea.test/raw/sub-CS41_ses-1_ecephys.nwb?X-Amz-Signature=e2e";

/** The studies body for the dataset address: dandi-000623 in no study (one-dataset mode). */
const STUDIES = {
  studies: [],
  datasets: [
    {
      dataset: "dandi-000623",
      title: "Brain movie recordings",
      tier: "curated",
      ingest_method: "import",
      mine: false,
      source_archive: "dandi",
      source_dataset: "000623",
      source_version: "0.230625.0237",
      license: "CC-BY-4.0",
      doi: null,
      asset_count: 3,
      subject_count: 2,
      total_bytes: 3500000000,
      created_at: "2026-10-01 12:00:00.000",
      subjects: [
        {
          subject: "sub-CS41",
          species: "Homo sapiens",
          modalities: ["units", "task"],
          sessions: [
            {
              session: "ses-1",
              asset_ids: ["asset-0101"],
              nwb: [{ asset_id: "asset-0101", n_units: 3 }],
            },
          ],
          units: 3,
          trials: 41,
          lfp: [],
        },
        {
          subject: "sub-CS62",
          species: "Homo sapiens",
          modalities: ["units"],
          sessions: [],
          units: 5,
          trials: null,
          lfp: [],
        },
      ],
      regions: [{ region: "Right amygdala", units: 8, channels: 0 }],
    },
  ],
  query_ms: 233,
};

/** `studies` replaces the golden studies body for the pages that read one dataset. */
async function stub(page: Page, studies?: string): Promise<string[]> {
  await stubBase(page);
  await stubEmptyUploads(page);
  await stubEmptyLabels(page);
  const seen: string[] = [];
  await page.route(`${API}/catalog/**`, (route) => {
    const req = route.request();
    if (req.method() === "OPTIONS") return route.fulfill({ status: 204, headers: CORS });
    const url = new URL(req.url());
    // The participant page's own read: the summary keeps stubBase's answer.
    if (
      url.pathname.startsWith("/catalog/previews/") ||
      url.pathname === "/catalog/session-summary"
    )
      return route.fallback();
    seen.push(`${url.pathname}${url.search}`);
    let body: string | null = null;
    if (url.pathname === "/catalog/datasets") body = fixture("catalog-datasets");
    else if (url.pathname === "/catalog/studies")
      body = studies ?? fixture("ux/catalog-studies.golden");
    else if (url.pathname === "/catalog/links") body = fixture("ux/catalog-links.golden");
    else if (url.pathname === "/catalog/datasets/dandi-000623") body = fixture("catalog-dataset");
    else if (
      url.pathname === "/catalog/datasets/dandi-000623/assets" &&
      url.search === "?subject=sub-CS41"
    )
      body = fixture("catalog-subject");
    else if (
      url.pathname === "/catalog/datasets/dandi-000623/assets" &&
      url.search === "?format=csv"
    )
      body = JSON.stringify({
        assets: [
          {
            asset_id: "asset-0900",
            path: "stimuli/scenecut_info.csv",
            subject: null,
            bids_session: null,
            datatype: null,
            format: "csv",
            size_bytes: 4399,
            committed_at: "2026-10-01T12:00:00Z",
          },
        ],
        page: 0,
        more: false,
        query_ms: 12,
      });
    else if (/^\/catalog\/datasets\/[^/]+$/.test(url.pathname) && body === null)
      body = JSON.stringify({
        dataset: url.pathname.split("/").pop(),
        facts: { dataset: url.pathname.split("/").pop() },
        subjects: [],
        sessions: [],
        formats: [],
        query_ms: 1,
      });
    else if (/^\/catalog\/assets\/(?!asset-0101\/)[^/]+\/tables\/units$/.test(url.pathname))
      body = JSON.stringify({ rows: [], page: 0, more: false });
    else if (url.pathname === "/catalog/assets/asset-0101") body = fixture("catalog-file-nwb");
    else if (url.pathname === "/catalog/assets/asset-0101/events") body = fixture("events");
    else if (url.pathname.startsWith("/catalog/assets/asset-0101/tables/")) {
      const table = url.pathname.split("/").pop() ?? "";
      const tables = JSON.parse(fixture("catalog-file-tables")) as Record<string, unknown>;
      if (table in tables) body = JSON.stringify(tables[table]);
    } else if (url.pathname === "/catalog/assets/asset-0101/download") {
      body = JSON.stringify({
        url: DOWNLOAD_URL,
        expires_in: 300,
        size_bytes: 2000000000,
        s3_version_id: "3HL4kqtJlcpXroDTDmJ.rmSpXd3dIbrHY",
        format: "nwb",
      });
    }
    return route.fulfill({
      status: body === null ? 404 : 200,
      contentType: "application/json",
      headers: CORS,
      body: body ?? '{"error":"no route"}',
    });
  });
  return seen;
}

test("a dataset deep link shows its release meta and participants; a participant link opens the participant page", async ({
  page,
}) => {
  const seen = await stub(page, JSON.stringify(STUDIES));
  await page.goto(`${APP}data/dandi-000623`);
  await expect(page).toHaveURL(/\/d\/dandi-000623$/);
  await expect(page.getByRole("heading", { name: "Brain movie recordings" })).toBeVisible();
  const release = page.getByTestId("release-dandi-000623");
  await expect(release.getByTestId("release-meta")).toContainText("CC-BY-4.0");
  await expect(release.getByTestId("release-meta")).toContainText("version 0.230625.0237");
  await expect(release.locator('[data-ab="participant-row"]')).toHaveCount(2);
  await expect(release).toContainText("sub-CS62");
  await expect(page.getByTestId("catalog-query-ms")).toHaveText("233 ms");
  await expect(page).toHaveTitle("Studies - asteroidea");

  await page.getByRole("link", { name: "sub-CS41", exact: true }).click();
  await expect(page).toHaveURL(/\/s\/_\/p\/dandi-000623%3Asub-CS41$/);
  await expect(page.locator("main")).toHaveAttribute("data-ready", "participant");
  await expect(page.getByTestId("participant-page")).toContainText("sub-CS41");
  expect(seen).toContain("/catalog/datasets/dandi-000623");
});

test("a file deep link shows its facts and header", async ({ page }) => {
  await stub(page);
  await page.goto(`${APP}files/asset-0101`);
  await expect(page.getByTestId("file-facts")).toContainText("dandi-000623");
  await expect(page.getByTestId("file-overview")).toContainText("Spike sorted units");
  // Each header value is read from its own term / definition pair.
  const overview = page.getByTestId("file-overview");
  expect(await kvValue(overview, "n_electrodes")).toBe("2");
  expect(await kvValue(overview, "n_units")).toBe("3");
  expect(await kvValue(overview, "n_trials")).toBe("41");
});

test("the dataset page takes its facts from the studies route, not the datasets list or the dataset route", async ({
  page,
}) => {
  const seen = await stub(page, JSON.stringify(STUDIES));
  await page.goto(`${APP}data/dandi-000623`);
  const meta = page.getByTestId("release-dandi-000623").getByTestId("release-meta");
  await expect(meta).toContainText("CC-BY-4.0");
  await expect(meta).toContainText("3.5 GB");
  // In no study, so the links route is not asked.
  expect(seen).toEqual(["/catalog/studies"]);
});

test("the home links a release badge to its dataset page", async ({ page }) => {
  await stub(page);
  await page.goto(APP);
  await page.getByRole("link", { name: /^DANDI 000623 / }).click();
  await expect(page).toHaveURL(/\/d\/dandi-000623$/);
  await expect(page.getByTestId("release-dandi-000623")).toContainText("CS62");
});

test("tabs move with the arrow keys", async ({ page }) => {
  await stub(page);
  await page.goto(`${APP}files/asset-0101`);
  await page.getByRole("tab", { name: "Overview" }).focus();
  await page.keyboard.press("ArrowRight");
  await expect(page.getByRole("tab", { name: "Units" })).toHaveAttribute("aria-selected", "true");
});

test("375 px wide: no horizontal page scroll on the dataset and participant pages", async ({
  page,
}) => {
  await stub(page);
  await page.setViewportSize({ width: 375, height: 812 });
  const cases = [
    { path: "catalog/dandi-000623", ready: "study-page" },
    { path: "catalog/dandi-000623/subjects/CS41", ready: "participant-page" },
  ];
  for (const { path, ready } of cases) {
    await page.goto(`${APP}${path}`);
    await expect(page.getByTestId(ready)).toBeVisible();
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
    );
    expect(overflow).toBeLessThanOrEqual(0);
  }
});

test("a BIDS events file draws a lane per table with its count; an NWB file has no Events tab", async ({
  page,
}) => {
  // The NWB file's events are lanes in its Signals tab; a BIDS events file
  // keeps an Events tab, drawn as the same lanes over its whole span.
  await stub(page);
  await page.route(`${API}/catalog/assets/asset-ev**`, (route) => {
    if (route.request().method() === "OPTIONS")
      return route.fulfill({ status: 204, headers: CORS });
    const events = new URL(route.request().url()).pathname.endsWith("/events");
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      headers: CORS,
      body: events
        ? fixture("events")
        : JSON.stringify({
            asset: {
              asset_id: "asset-ev",
              dataset: "dandi-000623",
              subject: "sub-CS41",
              format: "events",
              s3_key: "raw/dandi-000623/sub-CS41/func/sub-CS41_task-movie_events.tsv",
              size_bytes: 4000,
            },
            headers: [],
          }),
    });
  });
  await page.goto(`${APP}files/asset-ev?tab=events`);
  await expect(page.getByRole("tab", { name: "Events" })).toHaveAttribute("aria-selected", "true");
  await expect(page.getByTestId("lane-trials").locator("text title")).toHaveText("trials (41)");
  await expect(page.getByTestId("lane-epochs").locator("text title")).toHaveText("epochs (2)");
  await expect(page.getByTestId("lane-labels")).toBeAttached();

  await page.goto(`${APP}files/asset-0101`);
  await expect(page.getByTestId("file-overview")).toBeVisible();
  await expect(page.getByRole("tab", { name: "Signals" })).toBeVisible();
  await expect(page.getByRole("tab", { name: "Events" })).toHaveCount(0);
});

test("file tabs update the URL and a deep link restores the tab", async ({ page }) => {
  await stub(page);
  await page.goto(`${APP}files/asset-0101`);
  await expect(page.getByTestId("file-overview")).toBeVisible();
  await page.getByRole("tab", { name: "Units" }).click();
  await expect(page).toHaveURL(/\/files\/asset-0101\?tab=units$/);
  await expect(page.getByTestId("unit-picker")).toContainText("hippocampus right");
  await page.getByRole("tab", { name: "Electrodes" }).click();
  await expect(page).toHaveURL(/\?tab=electrodes$/);
  await expect(page.getByTestId("file-electrodes")).toContainText("amygdala left");
  await page.getByRole("tab", { name: "Series" }).click();
  await expect(page.getByTestId("file-series")).toContainText("ElectricalSeries");
  await page.getByRole("tab", { name: "Overview" }).click();
  await expect(page).toHaveURL(/\/files\/asset-0101$/);
  await page.goto(`${APP}files/asset-0101?tab=electrodes`);
  await expect(page.getByRole("tab", { name: "Electrodes" })).toHaveAttribute(
    "aria-selected",
    "true",
  );
});

test("Download asks /download and navigates to the presigned URL", async ({ page }) => {
  const seen = await stub(page);
  let fetchedBytes = 0;
  const navigated: string[] = [];
  await page.route("https://raw.asteroidea.test/**", (route) => {
    const req = route.request();
    if (req.isNavigationRequest()) navigated.push(req.url());
    else fetchedBytes += 1;
    return route.fulfill({ status: 200, contentType: "text/plain", body: "stub" });
  });
  await page.goto(`${APP}files/asset-0101`);
  const button = page.getByRole("button", { name: "Download (2.0 GB)" });
  await expect(button).toBeVisible();
  await button.click();
  await expect.poll(() => navigated).toEqual([DOWNLOAD_URL]);
  expect(seen).toContain("/catalog/assets/asset-0101/download");
  expect(fetchedBytes).toBe(0);
});

test("375 px wide: no horizontal page scroll on the file page", async ({ page }) => {
  await stub(page);
  await page.setViewportSize({ width: 375, height: 812 });
  for (const tab of ["", "?tab=events", "?tab=units"]) {
    await page.goto(`${APP}files/asset-0101${tab}`);
    await expect(page.getByTestId("file-page").locator("h2")).toBeVisible();
    await expect(page.getByTestId("catalog-loading")).toHaveCount(0);
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
    );
    expect(overflow).toBeLessThanOrEqual(0);
  }
});

interface RatesStub {
  seen: URL[];
  reply: { status: number; body: string };
}

async function stubRates(page: Page, name: string): Promise<RatesStub> {
  const stubbed: RatesStub = { seen: [], reply: { status: 200, body: fixture(name) } };
  await page.route(`${API}/compute/rates*`, (route) => {
    if (route.request().method() === "OPTIONS")
      return route.fulfill({ status: 204, headers: CORS });
    stubbed.seen.push(new URL(route.request().url()));
    return route.fulfill({
      status: stubbed.reply.status,
      contentType: "application/json",
      headers: CORS,
      body: stubbed.reply.body,
    });
  });
  return stubbed;
}

async function drawn(page: Page, unit: number): Promise<unknown> {
  const text = await page.getByTestId(`curve-${unit}`).getAttribute("data-values");
  return JSON.parse(text ?? "null");
}

test("a Units deep link draws the curve with exactly the returned rate_hz", async ({ page }) => {
  await stub(page);
  const rates = await stubRates(page, "rates");
  await page.goto(`${APP}files/asset-0101?tab=units&unit=0&align=intervals:trials`);
  const curve = page.getByTestId("curve-0");
  await expect(curve).toBeAttached();
  const body = JSON.parse(fixture("rates")) as { units: { rate_hz: number[] }[] };
  expect(await drawn(page, 0)).toEqual(body.units[0]?.rate_hz);
  await expect(curve).toHaveAttribute("data-series", "rate_hz");
  await expect(page.getByTestId("zero-0")).toBeAttached();
  await expect(page.getByTestId("raster-0")).toHaveAttribute("data-spikes", "8");
  await expect(page.getByTestId("rates-n-trials")).toHaveText("4 trials");
  await expect(page.getByTestId("rates-source")).toHaveText("source: catalog");
  await expect(page.getByTestId("rates-compute-ms")).toHaveText("compute 87 ms");
  await expect(page.getByTestId("rates-missing")).toHaveCount(0);
  const request = rates.seen[0];
  expect(request?.searchParams.get("align")).toBe("intervals:trials");
  expect(request?.searchParams.get("units")).toBe("0");
  expect(request?.searchParams.get("asset_id")).toBe("asset-0101");
  expect(request?.searchParams.get("zscore")).toBe("0");
});

test("the align picker offers scene cuts and sends the frames: align", async ({ page }) => {
  await stub(page);
  const rates = await stubRates(page, "rates-scenecuts");
  await page.goto(`${APP}files/asset-0101?tab=units&unit=0,1&z=1`);
  const align = page.getByTestId("units-align");
  await expect(align.locator("option", { hasText: "Movie scene cuts" })).toHaveCount(1);
  await expect(align.locator("option", { hasText: "Intervals: trials" }).first()).toHaveCount(1);
  await expect(align.locator("option", { hasText: "Events file" })).toHaveCount(1);
  await align.selectOption({ label: "Movie scene cuts" });
  await expect(page).toHaveURL(
    /align=frames%3Aasset-0900%3Ashot_start_fr%3A%2Fstimulus%2Fmovieframe_time/,
  );
  await expect(page.getByTestId("rates-result")).toBeVisible();
  const request = rates.seen.at(-1);
  expect(request?.searchParams.get("align")).toBe(
    "frames:asset-0900:shot_start_fr:/stimulus/movieframe_time",
  );
  expect(request?.searchParams.get("zscore")).toBe("1");
  expect(await drawn(page, 0)).toEqual([-0.4, -0.1, 3.2, 1.6, 0.2, -0.3]);
  await expect(page.getByTestId("curve-0")).toHaveAttribute("data-series", "z");
  await expect(page.getByTestId("unit-noz-1")).toHaveText("no z-score: flat baseline");
  await expect(page.getByTestId("rates-missing")).toContainText("2 frames");
  await expect(page.getByTestId("rates-source")).toHaveText("source: file");
});

test("a 422 shows its sentence and the server message; a 503 offers Try again", async ({
  page,
  expectError,
}) => {
  expectError("422 and 503 refusals");
  await stub(page);
  const rates = await stubRates(page, "rates");
  rates.reply = {
    status: 422,
    body: '{"error":"the events file does not belong to this recording"}',
  };
  await page.goto(`${APP}files/asset-0101?tab=units&unit=0&align=events:asset-0102`);
  const refusal = page.getByTestId("catalog-error");
  await expect(refusal.getByRole("heading")).toHaveText("The server could not use that file");
  await expect(refusal.locator(".state-server")).toHaveText(
    "the events file does not belong to this recording",
  );
  await expect(refusal.locator(".state-code")).toHaveText("code: unprocessable");
  await expect(page.getByRole("button", { name: "Try again" })).toHaveCount(0);
  rates.reply = { status: 503, body: '{"error":"compute timed out"}' };
  await page.goto(`${APP}files/asset-0101?tab=units&unit=0&align=intervals:trials`);
  await expect(page.getByTestId("catalog-error")).toContainText("compute timed out");
  rates.reply = { status: 200, body: fixture("rates") };
  await page.getByRole("button", { name: "Try again" }).click();
  await expect(page.getByTestId("unit-plot-0")).toBeVisible();
});

test("375 px wide: no horizontal scroll on the Units tab with plots", async ({ page }) => {
  await stub(page);
  await stubRates(page, "rates-scenecuts");
  await page.setViewportSize({ width: 375, height: 812 });
  await page.goto(`${APP}files/asset-0101?tab=units&unit=0,1&z=1&align=intervals:trials`);
  await expect(page.getByTestId("unit-plot-1")).toBeVisible();
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
  expect(overflow).toBeLessThanOrEqual(0);
});

test.describe("phone width", () => {
  test.use({ viewport: { width: 375, height: 812 }, hasTouch: true, isMobile: true });

  test("Units: the align picker says it is loading, then offers the intervals the events route lists", async ({
    page,
  }) => {
    await stub(page);
    await stubRates(page, "rates");
    // The events route answers slowly, as it does for a large file on a phone connection.
    await page.route(`${API}/catalog/assets/asset-0101/events`, async (route) => {
      if (route.request().method() === "OPTIONS") return route.fallback();
      await new Promise((resolve) => setTimeout(resolve, 1500));
      return route.fallback();
    });
    await page.goto(`${APP}files/asset-0101?tab=units`);
    await expect(page.getByTestId("unit-picker")).toBeVisible();
    await page.getByTestId("unit-picker").locator("input[type=checkbox]").first().check();
    const align = page.getByTestId("units-align");
    // Until the alignments have loaded the control says so and cannot be used.
    await expect(align).toBeDisabled();
    await expect(align).toHaveAttribute("aria-busy", "true");
    await expect(align).toBeEnabled();
    await expect(align).toHaveAttribute("aria-busy", "false");
    await expect(align.locator("option", { hasText: "Intervals: trials" }).first()).toHaveCount(1);
  });
});

// The retired /catalog paths land on their Data addresses, the query kept.
test("/catalog redirects to the home", async ({ page }) => {
  await stub(page);
  await page.goto(`${APP}catalog`);
  await expect(page).toHaveURL(/\/$/);
  await expect(page.getByTestId("home-page")).toBeVisible();
});

test("/catalog/:dataset redirects to /d/:dataset, keeping the query", async ({ page }) => {
  await stub(page);
  await page.goto(`${APP}catalog/dandi-000623?tab=formats`);
  await expect(page).toHaveURL(/\/d\/dandi-000623\?tab=formats$/);
  await expect(page.getByTestId("study-page")).toBeVisible();
});

test("/catalog/:dataset/subjects/:subject and /data/:dataset/:subject redirect to the participant page", async ({
  page,
}) => {
  await stub(page);
  await page.goto(`${APP}catalog/dandi-000623/subjects/CS41`);
  await expect(page).toHaveURL(/\/s\/_\/p\/dandi-000623%3ACS41$/);
  await expect(page.getByTestId("participant-page")).toContainText("CS41");
  await page.goto(`${APP}data/dandi-000623/CS62`);
  await expect(page).toHaveURL(/\/s\/_\/p\/dandi-000623%3ACS62$/);
  await expect(page.getByTestId("participant-page")).toContainText("CS62");
});

test("/data/:dataset redirects to /d/:dataset", async ({ page }) => {
  await stub(page);
  await page.goto(`${APP}data/dandi-000623?tab=formats`);
  await expect(page).toHaveURL(/\/d\/dandi-000623\?tab=formats$/);
});

test("the shell: top bar, user label, spine and the ready mark", async ({ page }) => {
  await stub(page);
  await page.goto(APP);
  const main = page.locator("main");
  await expect(main).toHaveAttribute("data-ready", "home");
  const bar = page.getByRole("navigation", { name: "Main" });
  await expect(bar.getByRole("link")).toHaveText(["Studies", "Find", "Results", "Setup"]);
  await expect(bar.getByRole("link", { name: "Studies" })).toHaveAttribute("aria-current", "page");
  // The test tokens carry no email claim, so the label is the role.
  await expect(page.getByTestId("user-label")).toHaveText("researcher");

  await page.getByRole("link", { name: /^DANDI 000623 / }).click();
  await expect(main).toHaveAttribute("data-ready", "study");
  const spine = page.getByRole("navigation", { name: "Breadcrumb" });
  await expect(spine).toHaveText(
    "Studies / Human single neurons and fMRI during movie watching / dandi-000623",
  );
  await expect(spine.getByRole("link", { name: "Studies" })).toHaveAttribute("href", "/");

  await page.getByRole("link", { name: "CS41", exact: true }).first().click();
  await expect(main).toHaveAttribute("data-ready", "participant");
  await expect(spine).toHaveText(/^Studies \/ .+ \/ CS41$/);
});
