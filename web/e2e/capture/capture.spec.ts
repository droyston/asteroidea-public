// Full-page PNG captures of the app at 1440x900 (and four at 375 wide), written to a local
// output directory. Not a test of behaviour and never part of
// `make web-e2e` (the browse project ignores this directory); run it with `playwright test -c playwright.capture.config.ts`.
// The routing is copied from the existing specs (research, synthesis, lfp, mri, labels, results,
// imports, start, browse, catalog) and the fixtures are the ones those specs use.
import { existsSync, mkdirSync, readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { gunzipSync } from "node:zlib";
import { expect, type Page, type Route } from "@playwright/test";
import {
  API,
  APP,
  AUTH,
  CORS,
  fixture,
  stubBase,
  stubEmptyLabels,
  stubEmptyUploads,
  test,
} from "../support";

test.skip(
  process.env.E2E_LIVE === "1",
  "the capture run needs the fixture-mode auth build (WEB_PORT + 1)",
);

const AGENTS = fileURLToPath(new URL("../../../.agents/", import.meta.url));
const OUT = fileURLToPath(new URL("../../../.agents/design/web-ux/baseline/", import.meta.url));
test.skip(!existsSync(AGENTS), "the capture output directory (.agents/) is absent");
if (existsSync(AGENTS)) mkdirSync(OUT, { recursive: true });

const SESSION = `${APP}data/dandi-000623/sub-CS62/ses-1`;
const FILE = `${APP}files/asset-0101`;
const RUN_ID = "0123456789abcdef0123456789abcdef";
const SOURCE = "dandi:000576@0.231010.1811";
const OVERVIEW_URL = "https://raw.asteroidea.test/derived/overview.bin?X-Amz-Signature=e2e";
const OVERVIEW_BIN = readFileSync(new URL("../fixtures/overview.bin", import.meta.url));
const NII = readFileSync(new URL("../fixtures/t1-oblique.nii.gz", import.meta.url));
const NII_URL = "https://raw.asteroidea.test/raw/sub-1_T1w.nii.gz?X-Amz-Signature=x";
const API_CORS = { ...CORS, "access-control-allow-methods": "GET, POST, PATCH, DELETE, OPTIONS" };
const DANDI_ASSETS =
  "https://api.dandiarchive.org/api/dandisets/000576/versions/0.231010.1811/assets/**";
const TYPES = ["scene_cut", "recognition_onset", "movie_onset"] as const;

const UPLOADS = [
  {
    session_id: "s2",
    dataset: "sample-20261023-b",
    relpath: "sub-01/anat/sub-01_T1w.nii",
    state: "COMMITTED",
    created_at: 1_792_000_100,
    asset_id: "asset-2",
  },
  {
    session_id: "s1",
    dataset: "sample-20261023-a",
    relpath: "sub-01/anat/sub-01_T1w.nii",
    state: "INCOMPLETE",
    created_at: 1_792_000_000,
    asset_id: null,
  },
];

const NII_ASSET = {
  asset: {
    asset_id: "asset-nii",
    dataset: "ds004473",
    s3_key: "raw/ds004473/sub-1/anat/sub-1_T1w.nii.gz",
    subject: "sub-1",
    format: "nifti",
    size_bytes: NII.length,
  },
  headers: [{ asset_id: "asset-nii", nifti_shape: [16, 16, 16], nifti_datatype: "int16" }],
};

const running = {
  run_id: RUN_ID,
  source: SOURCE,
  dataset: "dandi-000576",
  state: "RUNNING",
  files: [
    { relpath: "sub-1/a.nwb", outcome: "PASS" },
    { relpath: "sub-1/b.nwb", outcome: null },
    { relpath: "sub-2/c.nwb", outcome: "FAIL extraction bad header" },
  ],
};
const done = {
  ...running,
  state: "DONE",
  files: [
    { relpath: "sub-1/a.nwb", outcome: "PASS" },
    { relpath: "sub-1/b.nwb", outcome: "PASS" },
    { relpath: "sub-2/c.nwb", outcome: "FAIL extraction bad header" },
    { relpath: "sub-3/d.nwb", outcome: "PASS", already_imported: true },
  ],
};

const ART = (id: string, created_at: number, author: string) => ({
  artifact_id: id,
  name: `${id}.csv`,
  size: 2048,
  content_type: "text/csv",
  snapshot_id: null,
  state: "COMMITTED",
  created_at,
  finalized_at: created_at + 5,
  author_email: author,
});
const USED: Record<string, unknown[]> = {
  "asset-0201": [
    ART("art-a", 1792000100, "ada@example.test"),
    ART("art-b", 1792000300, "grace@example.test"),
  ],
  "asset-0202": [
    ART("art-b", 1792000300, "grace@example.test"),
    ART("art-c", 1792000200, "ada@example.test"),
  ],
};

function jwt(claims: Record<string, unknown>): string {
  const part = (v: unknown) => Buffer.from(JSON.stringify(v)).toString("base64url");
  return `${part({ alg: "none" })}.${part(claims)}.sig`;
}

interface Reply {
  status: number;
  body: string;
}

interface Opts {
  /** Units in asset-0201's units table: 44 (CS62 R2) or 160. */
  units?: 44 | 160;
  sub?: string;
  groups?: string[];
  /** Replaces the /catalog/datasets reply. */
  datasets?: Reply;
  /** Serve an empty /artifacts listing. */
  noArtifacts?: boolean;
  importList?: unknown[];
  importRun?: unknown;
  /** Uploads for the panel every signed-in page mounts. */
  uploads?: unknown[];
}

type RatesBody = {
  units: { unit_index: number; rate_hz: number[] }[];
  responsive: { unit_index: number; location: string; p: number; q: number; screened: boolean }[];
};

/** The 44-unit rates body for `type`, re-indexed onto the units asked for (copied from synthesis.spec). */
function ratesFor(type: string, units: string, count: number): string {
  const body = JSON.parse(fixture(`synthesis-rates-${type}`)) as RatesBody;
  const [lo, hi] = units === "all" ? [0, count - 1] : units.split("-").map(Number);
  const wanted: number[] = [];
  for (let u = lo ?? 0; u <= (hi ?? -1) && u < count; u += 1) wanted.push(u);
  const pick = <T>(list: T[], i: number): T => list[i % list.length] as T;
  return JSON.stringify({
    ...body,
    units: wanted.map((u) => ({ ...pick(body.units, u), unit_index: u })),
    responsive: wanted.map((u) => ({ ...pick(body.responsive, u), unit_index: u })),
  });
}

function reply(route: Route, status: number, body: string, headers = CORS): Promise<void> {
  return route.fulfill({ status, contentType: "application/json", headers, body });
}

/** Sign-in whose access token carries `sub` and `groups`, and every API route the pages use. */
async function stub(page: Page, o: Opts = {}): Promise<void> {
  const count = o.units ?? 44;
  await stubBase(page);
  await page.route(`${AUTH}/oauth2/token`, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      headers: CORS,
      body: JSON.stringify({
        access_token: jwt({
          sub: o.sub ?? "sub-grace",
          "cognito:groups": o.groups ?? ["researchers"],
        }),
        refresh_token: "refresh-1",
        id_token: "id-1",
      }),
    }),
  );
  await stubEmptyUploads(page);
  await page.route(`${API}/uploads`, (route) => {
    if (route.request().method() === "OPTIONS")
      return route.fulfill({ status: 204, headers: CORS });
    return reply(route, 200, JSON.stringify({ uploads: o.uploads ?? [] }));
  });
  await stubEmptyLabels(page, "asset-0201");
  await page.route(`${API}/files/asset-0101/labels**`, (route) => {
    if (route.request().method() === "OPTIONS")
      return route.fulfill({ status: 204, headers: API_CORS });
    return reply(route, 200, fixture("labels"), API_CORS);
  });

  await page.route(`${API}/catalog/**`, (route) => {
    const req = route.request();
    if (req.method() === "OPTIONS") return route.fulfill({ status: 204, headers: CORS });
    const url = new URL(req.url());
    const path = url.pathname;
    const q = url.searchParams;
    let body: string | null = null;
    if (path === "/catalog/datasets") {
      const r = o.datasets ?? { status: 200, body: fixture("catalog-datasets") };
      return reply(route, r.status, r.body);
    }
    if (path === "/catalog/datasets/dandi-000623") body = fixture("catalog-dataset");
    else if (path === "/catalog/datasets/dandi-000623/assets") {
      if (q.get("subject") === "sub-CS41") body = fixture("catalog-subject");
      else if (q.get("subject") === "sub-CS62") body = fixture("synthesis-files");
      else if (q.get("format") === "csv")
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
      else body = fixture("catalog-assets-p0");
    } else if (path === "/catalog/assets/asset-0101") body = fixture("catalog-file-nwb");
    else if (path === "/catalog/assets/asset-0101/events") body = fixture("events");
    else if (path.startsWith("/catalog/assets/asset-0101/tables/")) {
      const table = path.split("/").pop() ?? "";
      const tables = JSON.parse(fixture("catalog-file-tables")) as Record<string, unknown>;
      if (table in tables) body = JSON.stringify(tables[table]);
    } else if (path === "/catalog/assets/asset-0101/download")
      body = JSON.stringify({
        url: "https://raw.asteroidea.test/raw/sub-CS41_ses-1_ecephys.nwb?X-Amz-Signature=e2e",
        expires_in: 300,
        size_bytes: 2000000000,
        s3_version_id: "3HL4kqtJlcpXroDTDmJ.rmSpXd3dIbrHY",
        format: "nwb",
      });
    else if (path === "/catalog/assets/asset-0201/tables/units")
      body = fixture(`synthesis-units-${count}`);
    else if (path === "/catalog/assets/asset-nii/download")
      body = JSON.stringify({ url: NII_URL, expires_in: 900, size_bytes: NII.length });
    else if (path === "/catalog/assets/asset-nii") body = JSON.stringify(NII_ASSET);
    return reply(route, body === null ? 404 : 200, body ?? '{"error":"no route"}');
  });

  await page.route(`${API}/compute/**`, (route) => {
    const req = route.request();
    if (req.method() === "OPTIONS") return route.fulfill({ status: 204, headers: CORS });
    const url = new URL(req.url());
    const q = url.searchParams;
    const asset = q.get("asset_id");
    if (url.pathname === "/compute/events" && asset === "asset-0201")
      return reply(route, 200, fixture("synthesis-events"));
    if (url.pathname === "/compute/tuning") {
      const key = `${q.get("event")}/${q.get("attribute")}`;
      if (key === "scene_cut/cut_kind")
        return reply(route, 200, fixture("synthesis-tuning-scene_cut-cut_kind"));
      return reply(route, 400, '{"error":"not_found","detail":"no route"}');
    }
    if (url.pathname === "/compute/rates" && asset === "asset-0201") {
      const type = (q.get("align") ?? "").replace(/^event:/, "");
      if (!(TYPES as readonly string[]).includes(type))
        return reply(route, 404, '{"error":"not_found","detail":"no route"}');
      return reply(route, 200, ratesFor(type, q.get("units") ?? "", count));
    }
    if (url.pathname === "/compute/rates" && asset === "asset-0101") {
      const frames = (q.get("align") ?? "").startsWith("frames:");
      return reply(route, 200, fixture(frames ? "rates-scenecuts" : "rates"));
    }
    if (url.pathname === "/compute/overview")
      return reply(
        route,
        200,
        JSON.stringify({
          url: OVERVIEW_URL,
          expires_in: 900,
          format: "ASTLFPOV v1",
          bytes: OVERVIEW_BIN.length,
          shape: [2, 20, 8],
          rate_hz: 1000,
          starting_time_s: 12.5,
        }),
      );
    if (url.pathname === "/compute/spectrogram") return reply(route, 200, fixture("spectrogram"));
    return reply(route, 404, '{"error":"not_found","detail":"no route"}');
  });
  await page.route("https://raw.asteroidea.test/derived/overview.bin*", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/octet-stream",
      headers: CORS,
      body: OVERVIEW_BIN,
    }),
  );
  await page.route("https://raw.asteroidea.test/raw/**", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/octet-stream",
      headers: { ...CORS, "access-control-expose-headers": "content-length" },
      body: NII,
    }),
  );

  await page.route(`${API}/artifacts**`, (route) => {
    const req = route.request();
    if (req.method() === "OPTIONS") return route.fulfill({ status: 204, headers: API_CORS });
    const url = new URL(req.url());
    const send = (body: string) => reply(route, 200, body, API_CORS);
    if (url.pathname === "/artifacts/art-2") return send(fixture("artifact-record"));
    if (url.pathname !== "/artifacts")
      return reply(route, 404, '{"error":"not_found","detail":"no"}', API_CORS);
    if (o.noArtifacts === true) return send(JSON.stringify({ artifacts: [] }));
    const input = url.searchParams.get("input");
    if (input !== null && input in USED)
      return send(JSON.stringify({ artifacts: USED[input] ?? [] }));
    if (input === "art-2" || input === "asset-0101") return send(fixture("artifact-used-by"));
    return send(fixture(url.searchParams.get("scope") === "all" ? "artifacts-all" : "artifacts"));
  });
  await page.route("https://artifacts.asteroidea.test/**", (route) =>
    route.fulfill({
      status: 200,
      headers: { "content-disposition": 'attachment; filename="spike-rates.csv"' },
      contentType: "text/csv",
      body: "a,b\n1,2\n",
    }),
  );

  await page.route(`${API}/get-started`, (route) => {
    if (route.request().method() === "OPTIONS")
      return route.fulfill({ status: 204, headers: CORS });
    return reply(route, 200, fixture("get-started"));
  });

  await page.route(`${API}/imports**`, (route) => {
    const req = route.request();
    if (req.method() === "OPTIONS") return route.fulfill({ status: 204, headers: CORS });
    const path = new URL(req.url()).pathname;
    if (path === "/imports") return reply(route, 200, JSON.stringify({ runs: o.importList ?? [] }));
    return reply(route, 200, JSON.stringify(o.importRun ?? done));
  });
}

/** Wait for the page to be quiet, then write a full-page PNG named `NN-<area>-<state>.png`. */
async function shot(page: Page, name: string): Promise<void> {
  await expect(page.getByTestId("catalog-loading")).toHaveCount(0);
  await page.waitForTimeout(500);
  const path = `${OUT}${name}.png`;
  if ((await page.locator("canvas").count()) === 0) {
    await page.screenshot({ path, fullPage: true });
    return;
  }
  // A full-page screenshot resizes the viewport, which clears the canvases and leaves them blank. Grow
  // the viewport to the page's height first, let them redraw, and take a plain viewport screenshot.
  const size = page.viewportSize();
  const height = await page.evaluate(() => document.documentElement.scrollHeight);
  if (size === null) throw new Error("no viewport");
  await page.setViewportSize({ width: size.width, height });
  await page.waitForTimeout(1500);
  await page.screenshot({ path });
  await page.setViewportSize(size);
}

/** Wait until the LFP overview canvas has drawn a pixel (its centre is opaque). */
async function overviewDrawn(page: Page): Promise<void> {
  await expect
    .poll(
      () =>
        page.getByTestId("lfp-overview").evaluate((el) => {
          const c = el as HTMLCanvasElement;
          return (
            c
              .getContext("2d")
              ?.getImageData(Math.floor(c.width / 2), Math.floor(c.height / 2), 1, 1).data[3] ?? -1
          );
        }),
      { timeout: 60_000 },
    )
    .toBe(255);
}

async function unitsReady(page: Page, last: number): Promise<void> {
  for (const t of TYPES)
    await expect(page.getByTestId(`psth-${t}-${last}`)).toBeVisible({ timeout: 60_000 });
}

test("01 Data with the uploads panel", async ({ page }) => {
  await stub(page, { uploads: UPLOADS });
  await page.goto(APP);
  await expect(page.getByTestId("catalog-datasets")).toContainText("dandi-000623");
  await expect(page.getByTestId("uploads-table")).toBeVisible();
  await shot(page, "01-data-uploads");
});

test("02-05 dataset page tabs", async ({ page }) => {
  await stub(page);
  const base = `${APP}data/dandi-000623`;
  await page.goto(base);
  await expect(page.getByTestId("dataset-subjects")).toContainText("sub-CS62");
  await shot(page, "02-dataset-subjects");
  await page.goto(`${base}?tab=sessions`);
  await expect(page.getByTestId("dataset-sessions")).toContainText("Movie watching");
  await shot(page, "03-dataset-sessions");
  await page.goto(`${base}?tab=files`);
  await expect(page.getByTestId("dataset-files")).toBeVisible();
  await shot(page, "04-dataset-files");
  await page.goto(`${base}?tab=formats`);
  await expect(page.getByTestId("dataset-formats")).toContainText("nwb");
  await shot(page, "05-dataset-formats");
});

test("06-07 subject page", async ({ page }) => {
  await stub(page);
  await page.goto(`${APP}data/dandi-000623/sub-CS41`);
  await expect(page.getByTestId("subject-sessions")).toContainText("ses-1");
  await shot(page, "06-subject-sessions");
  await page.goto(`${APP}data/dandi-000623/sub-CS41?tab=files`);
  await expect(page.getByTestId("subject-files")).toContainText("sub-CS41_ses-1_ecephys.nwb");
  await shot(page, "07-subject-files");
});

test("08 session Units, 44 units", async ({ page }) => {
  await stub(page, { units: 44 });
  await page.goto(SESSION);
  await expect(page.getByTestId("session-units").locator("tbody tr")).toHaveCount(44);
  await unitsReady(page, 43);
  await shot(page, "08-session-units-44");
});

test("09 session Units, 160 units", async ({ page }) => {
  await stub(page, { units: 160 });
  await page.goto(SESSION);
  await expect(page.getByTestId("units-page")).toContainText("Units 0-151 (page 1 of 2)");
  await unitsReady(page, 151);
  await shot(page, "09-session-units-160");
});

test("10-13 session Tuning, Events, Files, Results", async ({ page }) => {
  await stub(page);
  await page.goto(`${SESSION}?tab=tuning`);
  await expect(page.getByTestId("tuning-event")).toHaveValue("scene_cut");
  await page.getByRole("button", { name: "Show tuning" }).click();
  await expect(page.getByTestId("tuning-table").locator("tbody tr")).toHaveCount(3);
  await shot(page, "10-session-tuning");
  await page.goto(`${SESSION}?tab=events`);
  await expect(page.getByTestId("session-event-types")).toBeVisible();
  await expect(page.getByTestId("session-events")).toBeVisible();
  await shot(page, "11-session-events");
  await page.goto(`${SESSION}?tab=files`);
  await expect(page.getByTestId("session-files")).toBeVisible();
  await shot(page, "12-session-files");
  await page.goto(`${SESSION}?tab=results`);
  await expect(page.getByTestId("session-results")).toBeVisible();
  await shot(page, "13-session-results");
});

test("14-19 NWB file page tabs", async ({ page }) => {
  await stub(page);
  await page.goto(FILE);
  await expect(page.getByTestId("file-overview")).toContainText("Spike sorted units");
  await shot(page, "14-file-nwb-overview");

  await page.goto(`${FILE}?tab=units&unit=0&align=intervals:trials`);
  await expect(page.getByTestId("unit-plot-0")).toBeVisible();
  await shot(page, "15-file-nwb-units");

  await page.goto(`${FILE}?tab=lfp`);
  const overview = page.getByTestId("lfp-overview");
  await expect(overview).toBeAttached();
  const box = await overview.boundingBox();
  if (box === null) throw new Error("no overview canvas");
  const y = box.y + box.height / 2;
  await page.mouse.move(box.x + box.width * 0.25, y);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width * 0.5, y, { steps: 4 });
  await page.mouse.move(box.x + box.width * 0.75, y, { steps: 4 });
  await page.mouse.up();
  await expect(page.getByTestId("lfp-spectrogram")).toBeAttached();
  await expect(page.getByTestId("lfp-trace")).toBeAttached();
  await expect(page.getByText("Loading the window")).toHaveCount(0);
  await overviewDrawn(page);
  await shot(page, "16-file-nwb-lfp-window");

  await page.goto(`${FILE}?tab=electrodes`);
  await expect(page.getByTestId("file-electrodes")).toContainText("amygdala left");
  await shot(page, "17-file-nwb-electrodes");

  await page.goto(`${FILE}?tab=series`);
  await expect(page.getByTestId("file-series")).toContainText("ElectricalSeries");
  await shot(page, "18-file-nwb-series");

  await page.goto(`${FILE}?tab=events`);
  await expect(page.getByTestId("lane-trials").locator("rect.bar")).toHaveCount(41);
  const label = page.locator('[data-testid^="label-item-"]').first();
  await expect(label).toBeVisible();
  await label.click();
  await expect(page.getByTestId("label-editor")).toBeVisible();
  await shot(page, "19-file-nwb-events-label");
});

test("20 NIfTI Image tab", async ({ page }) => {
  await stub(page);
  // A point inside the fixture's 1000-valued cube (voxel 6, 6, 6), through the sform in its header.
  const raw = gunzipSync(NII);
  const mm = [0, 1, 2]
    .map((r) => [0, 1, 2, 3].map((c) => raw.readFloatLE(280 + 16 * r + 4 * c)))
    .map((row) => (row[0] ?? 0) * 6 + (row[1] ?? 0) * 6 + (row[2] ?? 0) * 6 + (row[3] ?? 0))
    .map((n) => n.toFixed(2));
  await page.goto(`${APP}files/asset-nii?tab=image&mm=${mm.join(",")}`);
  await expect(page.getByTestId("mri-canvas")).toBeAttached({ timeout: 60_000 });
  await expect(page.getByTestId("mri-mm")).toBeVisible({ timeout: 60_000 });
  await page.waitForTimeout(4000);
  await shot(page, "20-file-nifti-image");
});

test("21 Discover with filters and the pick dock", async ({ page }) => {
  await stub(page, { groups: ["researchers", "curators"] });
  await page.route(DANDI_ASSETS, (route) =>
    route.request().method() === "OPTIONS"
      ? route.fulfill({ status: 204, headers: CORS })
      : route.fulfill({
          status: 200,
          contentType: "application/json",
          headers: CORS,
          body: readFileSync(
            new URL("../fixtures/live/dandi-000576-assets-p1.json", import.meta.url),
            "utf8",
          ),
        }),
  );
  const targets = [
    {
      source: "dandi",
      dataset_id: "000576",
      version: "0.231010.1811",
      path: "sub-1/a.nwb",
      note: "exemplar candidate",
    },
    {
      source: "dandi",
      dataset_id: "000576",
      version: "0.231010.1811",
      path: "sub-1/b.nwb",
      note: "",
    },
    { source: "openneuro", dataset_id: "ds000001", version: "1.0.0", path: null, note: "" },
  ];
  await page.addInitScript(
    (doc) => {
      window.localStorage.setItem("asteroidea.targets/1", JSON.stringify(doc));
    },
    { schema: "asteroidea.targets/1", exported_at: "2026-10-25T00:00:00.000Z", targets },
  );
  await page.goto(`${APP}discover`);
  await expect(page.getByTestId("index-summary")).toContainText("Index harvested");
  await page.getByRole("checkbox", { name: /^Homo sapiens - Human \(\d+\)$/ }).check();
  await page.getByRole("checkbox", { name: "has Units" }).check();
  await page.getByRole("checkbox", { name: "has LFP" }).check();
  await expect(page.getByTestId("row-dandi-000576")).toBeVisible();
  await page.getByTestId("row-dandi-000576").locator("td.col-title").click();
  await expect(page.getByTestId("file-count")).toBeVisible();
  await expect(page.getByTestId("pick-count")).toHaveText("3");
  await shot(page, "21-discover-filters-dock");
});

test("22-23 Imports (curator) and one import run", async ({ page }) => {
  await stub(page, {
    groups: ["researchers", "curators"],
    importList: [
      {
        run_id: "f".repeat(32),
        source: "openneuro:ds000001@1.0.0",
        include: [],
        state: "REFUSED",
        created_at: 1_792_000_000,
        refusal: "50-file cap",
      },
      {
        run_id: RUN_ID,
        source: SOURCE,
        include: ["sub-1/*.nwb"],
        state: "DONE",
        created_at: 1_792_000_500,
      },
    ],
    importRun: done,
  });
  await page.goto(`${APP}discover/imports`);
  await expect(page.getByTestId("imports-table")).toContainText("openneuro:ds000001@1.0.0");
  await shot(page, "22-imports-curator");
  await page.goto(`${APP}discover/imports/${RUN_ID}`);
  await expect(page.getByTestId("import-run-state")).toContainText("DONE");
  await expect(page.getByTestId("import-files")).toContainText("already imported");
  await shot(page, "23-import-run");
});

test("24-26 Results Mine, All and a result page", async ({ page }) => {
  await stub(page, { sub: "sub-grace" });
  await page.goto(`${APP}results`);
  await expect(page.getByTestId("results-table").locator("tbody tr")).toHaveCount(2);
  await shot(page, "24-results-mine");
  await page.getByRole("tab", { name: "All" }).click();
  await expect(page).toHaveURL(/\/results\?tab=all$/);
  await expect(page.getByRole("columnheader", { name: "Author" })).toBeVisible();
  await shot(page, "25-results-all");
  await page.goto(`${APP}results/art-2`);
  await expect(page.getByTestId("result-facts")).toBeVisible();
  await expect(page.getByTestId("result-inputs").locator("tbody tr")).toHaveCount(5);
  await expect(page.getByTestId("result-used-by").locator("tbody tr")).toHaveCount(1);
  await shot(page, "26-result-page");
});

test("27 Get started", async ({ page }) => {
  await stub(page);
  await page.goto(`${APP}start`);
  await expect(page.getByTestId("start-install")).toContainText("asteroidea-setup");
  await shot(page, "27-get-started");
});

test("28 signed-out gate (session ended)", async ({ page, expectError }) => {
  expectError("a 401 after the refresh ends the session");
  await stub(page);
  await page.route(`${API}/uploads`, (route) =>
    route.request().method() === "OPTIONS"
      ? route.fulfill({ status: 204, headers: CORS })
      : reply(route, 401, "{}"),
  );
  await page.goto(APP);
  await expect(page.getByRole("dialog", { name: "Sign in again to carry on" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Sign in" })).toBeVisible();
  await shot(page, "28-signed-out-gate");
});

test("29 load-error state (catalog 502)", async ({ page, expectError }) => {
  expectError("502 refusal");
  await stub(page, {
    datasets: {
      status: 502,
      body: JSON.stringify({
        error: "the catalog query failed: SYNTAX_ERROR: line 1:8: Column 'x' cannot be resolved",
      }),
    },
  });
  await page.goto(APP);
  await expect(page.getByTestId("catalog-error")).toContainText("the catalog query failed");
  await shot(page, "29-error-catalog-502");
});

test("30 empty state (no results yet)", async ({ page }) => {
  await stub(page, { noArtifacts: true });
  await page.goto(`${APP}results`);
  await expect(page.getByTestId("results-page")).toBeVisible();
  await expect(page.getByTestId("results-table")).toHaveCount(0);
  await shot(page, "30-empty-results");
});

test.describe("375 wide", () => {
  test.use({ viewport: { width: 375, height: 812 } });

  test("31 Data", async ({ page }) => {
    await stub(page, { uploads: UPLOADS });
    await page.goto(APP);
    await expect(page.getByTestId("catalog-datasets")).toContainText("dandi-000623");
    await shot(page, "31-m375-data");
  });

  test("32 session Units", async ({ page }) => {
    await stub(page, { units: 44 });
    await page.goto(SESSION);
    await expect(page.getByTestId("session-units").locator("tbody tr")).toHaveCount(44);
    await unitsReady(page, 43);
    await shot(page, "32-m375-session-units");
  });

  test("33 file LFP", async ({ page }) => {
    await stub(page);
    await page.goto(`${FILE}?tab=lfp&start=5&dur=10`);
    await expect(page.getByTestId("lfp-overview")).toBeAttached();
    await expect(page.getByTestId("lfp-spectrogram")).toBeAttached();
    await expect(page.getByText("Loading the window")).toHaveCount(0);
    await overviewDrawn(page);
    await shot(page, "33-m375-file-lfp");
  });

  test("34 Discover", async ({ page }) => {
    await stub(page);
    await page.goto(`${APP}discover`);
    await expect(page.getByTestId("index-summary")).toContainText("Index harvested");
    await shot(page, "34-m375-discover");
  });
});
