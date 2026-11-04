// The study page and `/d/:dataset`, over CATALOG_FIXTURE served by page.route.
// Runs against the sign-in env build (port WEB_PORT + 1); the API is served by page.route.
import { expect, type Page } from "@playwright/test";
import { datasetsListing, CATALOG_FIXTURE as F } from "../src/artboard/fixture";
import { API, APP, CORS, stubBase, stubEmptyUploads, test } from "./support";

test.skip(
  process.env.E2E_LIVE === "1",
  "needs the fixture-mode auth build (no live server has the auth env)",
);

interface Served {
  /** Every API request as `path?query`, in order. */
  seen: string[];
}

const PAGE = 200;

/** One 200-row page of `total` files, as the assets route pages them (`more` while rows remain). */
function assetsPage(dataset: string, page: number, total: number) {
  const from = page * PAGE;
  const n = Math.max(0, Math.min(PAGE, total - from));
  return {
    assets: Array.from({ length: n }, (_, i) => {
      const k = from + i;
      return {
        asset_id: `${dataset}-a${k}`,
        path: `sub-${String(k % 25).padStart(2, "0")}/ses-1/${k % 2 === 0 ? "func" : "anat"}/f${k}.nii.gz`,
        subject: `sub-${String(k % 25).padStart(2, "0")}`,
        bids_session: "ses-1",
        datatype: k % 2 === 0 ? "func" : "anat",
        format: "nii.gz",
        size_bytes: 1000,
        committed_at: null,
      };
    }),
    page,
    more: from + n < total,
    query_ms: 1,
  };
}

async function serve(
  page: Page,
  opts: { studies?: unknown; status?: number; refusal?: string; assetsTotal?: number } = {},
): Promise<Served> {
  await stubBase(page);
  await stubEmptyUploads(page);
  const served: Served = { seen: [] };
  await page.route(`${API}/catalog/**`, (route) => {
    const req = route.request();
    if (req.method() === "OPTIONS") return route.fulfill({ status: 204, headers: CORS });
    const url = new URL(req.url());
    served.seen.push(`${url.pathname}${url.search}`);
    const json = (status: number, body: unknown) =>
      route.fulfill({
        status,
        contentType: "application/json",
        headers: CORS,
        body: JSON.stringify(body),
      });
    if (url.pathname === "/catalog/studies") {
      if (opts.status !== undefined) return json(opts.status, { error: opts.refusal });
      return json(200, opts.studies ?? F.studies);
    }
    if (url.pathname === "/catalog/links") return json(200, F.links);
    const assets = url.pathname.match(/^\/catalog\/datasets\/([^/]+)\/assets$/);
    if (assets !== null) {
      const at = Number(url.searchParams.get("page") ?? "0");
      return json(200, assetsPage(decodeURIComponent(assets[1] ?? ""), at, opts.assetsTotal ?? 0));
    }
    if (url.pathname === "/catalog/datasets") return json(200, datasetsListing(F.studies.datasets));
    return json(404, { error: "not in the study fixture" });
  });
  return served;
}

const MOVIE = "/s/movie-watching";

test("the study page: paragraph, releases with licence and version, the matrix and its links", async ({
  page,
}) => {
  const served = await serve(page);
  await page.goto(`${APP}${MOVIE.slice(1)}`);
  await expect(page.locator("main")).toHaveAttribute("data-ready", "study");
  await expect(page.getByRole("heading", { level: 2 })).toContainText("movie watching");
  await expect(page.getByTestId("study-paragraph")).toContainText("2 releases");
  await expect(page.getByTestId("study-paragraph")).toContainText("single units");

  const dandi = page.getByTestId("release-dandi-000623");
  await expect(dandi.getByTestId("release-meta")).toContainText("CC-BY-4.0");
  await expect(dandi.getByTestId("release-meta")).toContainText("version 0.240227.2023");
  const open = page.getByTestId("release-openneuro-ds004798");
  await expect(open.getByTestId("release-meta")).toContainText("CC0");
  await expect(open.getByTestId("release-meta")).toContainText("version 1.0.5");

  // One row per participant record: 2 in DANDI, 15 in OpenNeuro.
  await expect(dandi.locator('[data-ab="participant-row"]')).toHaveCount(2);
  await expect(open.locator('[data-ab="participant-row"]')).toHaveCount(15);
  await expect(dandi.getByRole("columnheader")).toHaveText([
    "Participant",
    "Units",
    "LFP",
    "Task",
    "Sessions",
    "Same person across releases",
  ]);

  // The four cross-archive links are pending: dashed, never solid.
  await expect(page.locator('[data-ab="link-cell"][data-ab-line="dashed"]')).toHaveCount(4);
  await expect(page.locator('[data-ab="link-cell"][data-ab-line="solid"]')).toHaveCount(0);
  await expect(dandi.getByTestId("link-cell").first()).toContainText("pending");

  // The participant links to the participant page, by the person's subject key.
  await expect(dandi.getByRole("link", { name: "sub-CS62" })).toHaveAttribute(
    "href",
    "/s/movie-watching/p/bmovie-patient-62",
  );
  const spine = page.getByRole("navigation", { name: "Breadcrumb" });
  await expect(spine.getByRole("link", { name: "Studies" })).toHaveAttribute("href", "/");

  expect(served.seen.sort()).toEqual(["/catalog/links?study=movie-watching", "/catalog/studies"]);
});

test("a session cell links to the session page only with an NWB of units; any other to its file", async ({
  page,
}) => {
  await serve(page);
  await page.goto(`${APP}${MOVIE.slice(1)}`);
  await expect(page.locator("main")).toHaveAttribute("data-ready", "study");
  const dandi = page.getByTestId("release-dandi-000623");
  await expect(dandi.locator('a[data-ab-to="session"]')).toHaveCount(4);
  await expect(dandi.getByRole("link", { name: "ses-2" }).nth(1)).toHaveAttribute(
    "href",
    "/s/movie-watching/dandi-000623/sub-CS62/ses-2",
  );
  const mri = page.getByTestId("release-openneuro-ds004798");
  // The MRI-only session has no NWB, so it never reaches the session page (422 not_nwb there).
  await expect(mri.locator('a[data-ab-to="session"]')).toHaveCount(0);
  await expect(mri.getByRole("link", { name: "ses-mri" })).toHaveAttribute(
    "href",
    "/files/asset-p62cs-t1w",
  );
});

test("/d/:dataset for a dataset in a study draws the dataset alone and names its study", async ({
  page,
}) => {
  const served = await serve(page);
  await page.goto(`${APP}d/dandi-001775`);
  await expect(page.locator("main")).toHaveAttribute("data-ready", "study");
  await expect(page.getByTestId("study-page")).toHaveAttribute("data-mode", "dataset");
  await expect(page.locator('[data-ab="release"]')).toHaveCount(1);
  await expect(page.getByTestId("study-part")).toContainText("Part of the study");
  await expect(page.getByTestId("study-part").getByRole("link")).toHaveAttribute(
    "href",
    "/s/spatial-working-memory",
  );
  await expect(page.getByRole("navigation", { name: "Breadcrumb" })).toContainText(
    "Studies / Mouse hippocampal",
  );
  expect(served.seen.sort()).toEqual([
    "/catalog/links?study=spatial-working-memory",
    "/catalog/studies",
  ]);
});

test("/d/:dataset for a dataset in no study is the one-dataset view and asks for no links", async ({
  page,
}) => {
  const body = { ...F.studies, datasets: [...F.studies.datasets, ...F.variants.mine_two] };
  const served = await serve(page, { studies: body });
  await page.goto(`${APP}d/upload-mine-a`);
  await expect(page.locator("main")).toHaveAttribute("data-ready", "study");
  await expect(page.getByTestId("study-part")).toHaveText("This dataset is in no study.");
  await expect(page.locator('[data-ab="release"]')).toHaveCount(1);
  // The fixture's upload lists no subjects: the release says so rather than drawing an empty table.
  await expect(page.locator('[data-ab="participant-row"]')).toHaveCount(0);
  await expect(page.getByText("This release lists no participants.")).toBeVisible();
  expect(served.seen).toEqual(["/catalog/studies"]);
});

test("a study or dataset the catalog does not hold says so, and the page is still ready", async ({
  page,
}) => {
  await serve(page);
  await page.goto(`${APP}d/no-such-dataset`);
  await expect(page.locator("main")).toHaveAttribute("data-ready", "study");
  await expect(page.getByTestId("study-missing")).toContainText(
    "There is no dataset no-such-dataset",
  );
  await page.goto(`${APP}s/no-such-study`);
  await expect(page.getByTestId("study-missing")).toContainText("There is no study no-such-study");
});

test("the study with 24 participants is a strip: every record a column, two links confirmed (solid), one screen", async ({
  page,
}) => {
  await serve(page);
  await page.goto(`${APP}s/synthetic-24-participants`);
  await expect(page.locator("main")).toHaveAttribute("data-ready", "study");
  await expect(page.locator('[data-ab="participant-row"]')).toHaveCount(25);
  await expect(page.locator('[data-ab="strip"]')).toHaveCount(1);
  // The 24-participant release has no table; the one-participant release keeps its own.
  await expect(
    page.getByRole("table", { name: "Participants of synthetic-24p-ephys" }),
  ).toHaveCount(0);
  await expect(page.getByRole("table", { name: "Participants of synthetic-24p-mri" })).toHaveCount(
    1,
  );
  const column = await page.locator(".strip-hit").first().boundingBox();
  expect(column?.width).toBe(6);
  await expect(page.locator('[data-ab="link-cell"][data-ab-line="solid"]')).toHaveCount(2);
  await expect(page.locator('[data-ab="link-cell"][data-ab-line="dashed"]')).toHaveCount(0);
  const m = await page.evaluate(() => ({
    sh: document.documentElement.scrollHeight,
    ih: window.innerHeight,
  }));
  expect(m.sh).toBeLessThanOrEqual(m.ih);
});

test("How the recordings relate counts each release; no file is listed until Source files opens", async ({
  page,
}) => {
  const served = await serve(page);
  await page.goto(`${APP}s/synthetic-24-participants`);
  await expect(page.locator("main")).toHaveAttribute("data-ready", "study");
  const ephys = page.getByTestId("relate-synthetic-24p-ephys");
  await expect(ephys).toContainText("24 participants");
  await expect(ephys).toContainText("24 sessions");
  await expect(ephys).toContainText("48 recording groups");
  await expect(page.getByTestId("sources-drawer")).toBeHidden();
  expect(served.seen.filter((u) => u.includes("/assets"))).toEqual([]);
});

test("Source files opens a drawer of recording groups and pages 200 files at a time", async ({
  page,
}) => {
  const patched = {
    ...F.studies,
    datasets: F.studies.datasets.map((d) =>
      d.dataset === "synthetic-24p-ephys" ? { ...d, asset_count: 450 } : d,
    ),
  };
  const served = await serve(page, { studies: patched, assetsTotal: 450 });
  await page.goto(`${APP}s/synthetic-24-participants`);
  await expect(page.locator("main")).toHaveAttribute("data-ready", "study");
  const open = page
    .getByTestId("relate-synthetic-24p-ephys")
    .getByRole("button", { name: "Source files" });
  await open.click();
  const drawer = page.getByTestId("sources-drawer");
  await expect(drawer).toBeVisible();
  await expect(page.getByTestId("sources-count")).toContainText("200 files read of 450");
  expect(served.seen.filter((u) => u.includes("/assets"))).toEqual([
    "/catalog/datasets/synthetic-24p-ephys/assets",
  ]);
  // 25 participants x 2 datatypes in one session, so fifty groups, each folded.
  await expect(drawer.locator('[data-ab="recording-group"]')).toHaveCount(50);
  await expect(drawer.locator(".sources-files").first()).toBeHidden();
  await page.getByTestId("sources-more").click();
  await expect(page.getByTestId("sources-count")).toContainText("400 files read of 450");
  await page.getByTestId("sources-more").click();
  await expect(page.getByTestId("sources-count")).toContainText("450 files read of 450");
  await expect(page.getByTestId("sources-more")).toHaveCount(0);
  expect(served.seen.filter((u) => u.includes("/assets"))).toEqual([
    "/catalog/datasets/synthetic-24p-ephys/assets",
    "/catalog/datasets/synthetic-24p-ephys/assets?page=1",
    "/catalog/datasets/synthetic-24p-ephys/assets?page=2",
  ]);
  await page.keyboard.press("Escape");
  await expect(drawer).toBeHidden();
  await expect(open).toBeFocused();
});

test("375 px wide: the study page does not scroll sideways", async ({ page }) => {
  await serve(page);
  await page.setViewportSize({ width: 375, height: 812 });
  await page.goto(`${APP}${MOVIE.slice(1)}`);
  await expect(page.locator("main")).toHaveAttribute("data-ready", "study");
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
  expect(overflow).toBeLessThanOrEqual(0);
});

test("a 503 from the studies route shows the server's sentence and a retry", async ({
  page,
  expectError,
}) => {
  expectError("503 slow text");
  const text = "the catalog is slow to answer; try again";
  const served = await serve(page, { status: 503, refusal: text });
  await page.goto(`${APP}${MOVIE.slice(1)}`);
  await expect(page.getByTestId("catalog-error")).toContainText(text);
  const before = served.seen.length;
  await page.getByRole("button", { name: "Try again" }).click();
  await expect.poll(() => served.seen.length).toBeGreaterThan(before);
});
