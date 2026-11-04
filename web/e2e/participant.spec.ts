// The participant page: one person across archives, over
// CATALOG_FIXTURE served by page.route. Runs against the sign-in env build (port WEB_PORT + 1).
import { expect, type Page } from "@playwright/test";
import { CATALOG_FIXTURE as F, RATES_CS62, UNITS_CS62 } from "../src/artboard/fixture";
import { datasetBody } from "../src/artboard/surfaces/participant";
import { EVENTS_CS62 } from "../src/artboard/surfaces/session";
import { API, APP, CORS, stubBase, stubEmptyUploads, test } from "./support";

test.skip(
  process.env.E2E_LIVE === "1",
  "needs the fixture-mode auth build (no live server has the auth env)",
);

const KEY = "bmovie-patient-62";
const PATH = `s/movie-watching/p/${KEY}`;
const ASSET_OF: Record<string, string> = { "ses-1": "asset-cs62-1", "ses-2": "asset-cs62-r2" };

interface Opts {
  links?: unknown;
  /** A stored summary per session, or the default `missing`. */
  stored?: boolean;
}

/** Every API request as `path?query`, in order. */
async function serve(page: Page, opts: Opts = {}): Promise<string[]> {
  await stubBase(page);
  await stubEmptyUploads(page);
  const seen: string[] = [];
  const reply = (status: number, body: unknown) => ({
    status,
    contentType: "application/json",
    headers: CORS,
    body: typeof body === "string" ? body : JSON.stringify(body),
  });
  await page.route(`${API}/**`, (route) => {
    const req = route.request();
    if (req.method() === "OPTIONS") return route.fulfill({ status: 204, headers: CORS });
    const url = new URL(req.url());
    const p = url.pathname;
    const q = url.searchParams;
    seen.push(`${p}${url.search}`);
    const links = (opts.links ?? F.links) as {
      links: { dataset: string; subject: string; subject_key: string }[];
    };
    if (p === "/catalog/studies") return route.fulfill(reply(200, F.studies));
    if (p === "/catalog/links") {
      const key = q.get("subject_key");
      const dataset = q.get("dataset");
      const rows = links.links.filter((l) => {
        if (key !== null) return l.subject_key === key;
        if (dataset !== null) return l.dataset === dataset && l.subject === q.get("subject");
        return true;
      });
      return route.fulfill(reply(200, { links: rows }));
    }
    const dataset = /^\/catalog\/datasets\/([^/]+)$/.exec(p)?.[1];
    if (dataset !== undefined) return route.fulfill(reply(200, datasetBody(dataset)));
    if (p === "/catalog/session-summary") {
      const session = q.get("session") ?? "";
      if (opts.stored !== true || ASSET_OF[session] === undefined) {
        return route.fulfill(reply(200, { status: "missing" }));
      }
      return route.fulfill(
        reply(200, {
          status: "built",
          delivery: "inline",
          manifest: {
            asset_id: ASSET_OF[session],
            built_at: "2026-10-26T10:00:00Z",
            display: { pre_s: 1, post_s: 2, bin_s: 0.05, z_baseline_s: [-1, 0] },
            units_table: { rows: UNITS_CS62.rows.length, more: false },
          },
          parts: {
            "rates-scene_cut": RATES_CS62,
            units: { rows: UNITS_CS62.rows, page: 0, more: false },
          },
        }),
      );
    }
    if (/^\/catalog\/assets\/[^/]+\/tables\/units$/.test(p)) {
      return route.fulfill(reply(200, { rows: UNITS_CS62.rows, page: 0, more: false }));
    }
    if (p === "/compute/events") return route.fulfill(reply(200, EVENTS_CS62));
    if (p === "/compute/rates") return route.fulfill(reply(200, RATES_CS62));
    return route.fulfill(reply(404, { error: "not in the participant fixture" }));
  });
  return seen;
}

const ready = (page: Page) =>
  expect(page.locator("main")).toHaveAttribute("data-ready", "participant");
const study = F.studies.studies.find((s) => s.study === "movie-watching");

test("one person across two archives: a dashed bridge while pending, with status, who, when and basis", async ({
  page,
}) => {
  await serve(page);
  await page.goto(`${APP}${PATH}`);
  await ready(page);
  await expect(page.getByRole("heading", { level: 2 })).toHaveText("sub-CS62");
  await expect(page.getByTestId("participant-facts")).toContainText("2 datasets");
  await expect(page.locator('[data-ab="record"]')).toHaveCount(2);
  await expect(page.getByTestId("record-dandi-000623")).toContainText("sub-CS62");
  await expect(page.getByTestId("record-openneuro-ds004798")).toContainText("sub-p62cs");
  await expect(page.locator('[data-ab="bridge"][data-ab-line="dashed"]')).toHaveCount(1);
  await expect(page.locator('[data-ab="bridge"][data-ab-line="solid"]')).toHaveCount(0);
  await expect(page.getByTestId("bridge-status")).toHaveText(/pending\s+review/);
  await expect(page.getByTestId("bridge-text")).toContainText(
    "Linked by a curator (no email recorded)",
  );
  await expect(page.getByTestId("bridge-basis")).toContainText("Basis: not recorded");
  const spine = page.getByRole("navigation", { name: "Breadcrumb" });
  await expect(spine).toHaveText(`Studies / ${study?.title ?? ""} / sub-CS62`);
  await expect(page).toHaveTitle("Studies - asteroidea");
});

test("a confirmed link draws solid and names the linker's email, the date and the basis", async ({
  page,
}) => {
  const links = {
    links: F.links.links.map((l) =>
      l.subject_key === KEY
        ? {
            ...l,
            status: "confirmed",
            linked_by: "m.okafor@example.test",
            linked_at: "2026-10-20 12:00:00.000",
            basis: "same patient code, sex and age in both releases",
          }
        : l,
    ),
  };
  await serve(page, { links });
  await page.goto(`${APP}${PATH}`);
  await ready(page);
  await expect(page.locator('[data-ab="bridge"][data-ab-line="solid"]')).toHaveCount(1);
  await expect(page.locator('[data-ab="bridge"][data-ab-line="dashed"]')).toHaveCount(0);
  await expect(page.getByTestId("bridge-status")).toHaveText("confirmed");
  await expect(page.getByTestId("bridge-text")).toContainText(
    "m.okafor@example.test on 2026-10-20",
  );
  await expect(page.getByTestId("bridge-basis")).toContainText(
    "same patient code, sex and age in both releases",
  );
});

test("the timeline has a lane per modality and a tick per recording, on the sessions' dates", async ({
  page,
}) => {
  await serve(page);
  await page.goto(`${APP}${PATH}`);
  await ready(page);
  const lanes = page.locator('[data-ab="lane"]');
  await expect(lanes).toHaveCount(4);
  await expect(lanes.locator(".pp-lane-name")).toHaveText([
    "Ephys",
    "Anatomical MRI",
    "Functional MRI",
    "fmap",
  ]);
  await expect(page.locator('[data-ab="tick"]')).toHaveCount(5);
  await expect(page.getByTestId("timeline-axis")).toHaveText("2023-06-152023-06-25");
});

test("without a stored summary each ephys card computes its glyph live: z, no raster", async ({
  page,
}) => {
  const seen = await serve(page);
  await page.goto(`${APP}${PATH}`);
  await ready(page);
  await expect(page.getByTestId("ephys-card")).toHaveCount(2);
  await expect(page.locator('[data-ab="glyph"]')).toHaveCount(2);
  // One row per unit: the CS62 R2 body holds 44 units, for each of the two sessions.
  await expect(page.locator('[data-ab="glyph-row"]')).toHaveCount(88);
  await expect(page.getByTestId("glyph-caption").first()).toContainText("computed live");
  const rates = seen.filter((s) => s.startsWith("/compute/rates"));
  expect(rates).toHaveLength(2);
  for (const r of rates) {
    expect(r).toContain("zscore=1");
    expect(r).toContain("raster=0");
  }
  await expect(page.getByRole("link", { name: "Open session →" }).first()).toHaveAttribute(
    "href",
    /^\/s\/movie-watching\/dandi-000623\/sub-CS62\/ses-[12]$/,
  );
});

test("with a stored summary the glyph is drawn from its rates part and no compute route is asked", async ({
  page,
}) => {
  const seen = await serve(page, { stored: true });
  await page.goto(`${APP}${PATH}`);
  await ready(page);
  await expect(page.locator('[data-ab="glyph-row"]')).toHaveCount(88);
  await expect(page.getByTestId("glyph-caption").first()).toContainText("stored summary");
  expect(seen.filter((s) => s.startsWith("/compute/"))).toEqual([]);
  expect(seen.filter((s) => s.includes("/tables/units"))).toEqual([]);
});

test("the regions panel counts this person's units per region, label beside the colour", async ({
  page,
}) => {
  await serve(page);
  await page.goto(`${APP}${PATH}`);
  await ready(page);
  const lines = page.locator('[data-ab="region-line"]');
  await expect(lines).toHaveCount(4);
  const labels = [...new Set(UNITS_CS62.rows.map((r) => r.location))];
  for (const label of labels) {
    await expect(page.getByTestId("participant-regions")).toContainText(label);
  }
  await expect(lines.first().locator(".region-dot")).toHaveCSS("background-color", /rgb/);
});

test("the T1w card says 'Preview not built yet' and opens the file's MRI tab in the viewer; the preview route is never asked", async ({
  page,
}) => {
  const seen = await serve(page);
  await page.goto(`${APP}${PATH}`);
  await ready(page);
  const card = page.getByTestId("t1w-card");
  await expect(card).toContainText("Preview not built yet");
  await expect(card.locator('[data-ab="t1w-placeholder"]')).toHaveCount(3);
  await expect(card.getByRole("link", { name: "Open in viewer →" })).toHaveAttribute(
    "href",
    "/files/asset-p62cs-t1w?tab=image",
  );
  expect(seen.filter((s) => s.includes("previews"))).toEqual([]);
});

test("the study matrix links a participant to this page, and a subject path redirects here", async ({
  page,
}) => {
  await serve(page);
  await page.goto(`${APP}s/movie-watching`);
  await expect(page.locator("main")).toHaveAttribute("data-ready", "study");
  await page
    .getByTestId("release-dandi-000623")
    .getByRole("link", { name: "sub-CS62", exact: true })
    .click();
  await expect(page).toHaveURL(new RegExp(`/s/movie-watching/p/${KEY}$`));
  await ready(page);
  await expect(page.locator('[data-ab="record"]')).toHaveCount(2);

  await page.goto(`${APP}data/dandi-000623/sub-CS62`);
  await expect(page).toHaveURL(/\/s\/_\/p\/dandi-000623%3Asub-CS62$/);
  await ready(page);
  // A linked subject is its person: both records, the bridge.
  await expect(page.locator('[data-ab="record"]')).toHaveCount(2);
  await expect(page.locator('[data-ab="bridge"]')).toHaveCount(1);
});

test("a subject no link names is a person of one record, with no bridge", async ({ page }) => {
  await serve(page, { links: { links: [] } });
  await page.goto(`${APP}s/_/p/dandi-000623%3Asub-CS62`);
  await ready(page);
  await expect(page.locator('[data-ab="record"]')).toHaveCount(1);
  await expect(page.locator('[data-ab="bridge"]')).toHaveCount(0);
  await expect(page.getByTestId("bridge-text")).toHaveText(
    "Not linked to a record in another archive.",
  );
  await expect(page.getByTestId("t1w-card")).toHaveCount(0);
});

test("a participant that is not in the catalog says so", async ({ page }) => {
  await serve(page);
  await page.goto(`${APP}s/_/p/nobody`);
  await ready(page);
  await expect(page.getByTestId("participant-missing")).toContainText(
    "There is no participant nobody",
  );
});

test("375 px wide: no horizontal page scroll", async ({ page }) => {
  await serve(page);
  await page.setViewportSize({ width: 375, height: 812 });
  await page.goto(`${APP}${PATH}`);
  await ready(page);
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
  expect(overflow).toBeLessThanOrEqual(0);
});
