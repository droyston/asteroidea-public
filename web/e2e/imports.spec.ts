// Web-started imports for curators: the Find
// rail turns the picks into imports and shows the run as a grid of squares, one sentence per failed
// file and "Open the new study". Per-file picks under a dataset list the archive through
// `sources/*`. Runs against the sign-in env build (port WEB_PORT + 1); managed login, the token
// endpoint, the API and the DANDI listing are served by page.route. The fixtures are in the shape
// ingest/src/asteroidea_ingest/imports_api.py returns.
import { readFileSync } from "node:fs";
import { expect, type Page } from "@playwright/test";
import { API, APP, AUTH, CORS, stubBase, stubEmptyUploads, test } from "./support";

test.skip(
  process.env.E2E_LIVE === "1",
  "needs the fixture-mode auth build (no live server has the auth env)",
);

const RUN_ID = "0123456789abcdef0123456789abcdef";
const SOURCE = "dandi:000576@0.231010.1811";
const DS = { source: "dandi", dataset_id: "000576", version: "0.231010.1811" } as const;
const DANDI_ASSETS =
  "https://api.dandiarchive.org/api/dandisets/000576/versions/0.231010.1811/assets/**";

function jwt(claims: Record<string, unknown>): string {
  const part = (v: unknown) => Buffer.from(JSON.stringify(v)).toString("base64url");
  return `${part({ alg: "none" })}.${part(claims)}.sig`;
}

/** Sign-in whose access token carries `groups` (the app reads the claim for display only). */
async function signIn(page: Page, groups: string[]): Promise<void> {
  await stubBase(page);
  await stubEmptyUploads(page);
  const token = jwt({ sub: "e2e-user", "cognito:groups": groups });
  await page.route(`${AUTH}/oauth2/token`, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      headers: CORS,
      body: JSON.stringify({ access_token: token, refresh_token: "refresh-1", id_token: "id-1" }),
    }),
  );
}

interface Api {
  posts: unknown[];
  gets: string[];
}

interface ApiOptions {
  /** Replies to successive GET /imports/{id} calls; the last one repeats. */
  runs?: unknown[];
  post?: { status: number; body: unknown };
  list?: unknown[];
}

async function stubImports(page: Page, opts: ApiOptions = {}): Promise<Api> {
  const seen: Api = { posts: [], gets: [] };
  let polls = 0;
  await page.route(`${API}/imports**`, (route) => {
    const req = route.request();
    const reply = (status: number, body: unknown) =>
      route.fulfill({
        status,
        contentType: "application/json",
        headers: CORS,
        body: JSON.stringify(body),
      });
    if (req.method() === "OPTIONS") return route.fulfill({ status: 204, headers: CORS });
    const path = new URL(req.url()).pathname;
    if (req.method() === "POST") {
      seen.posts.push(req.postDataJSON());
      const post = opts.post ?? { status: 202, body: { run_id: RUN_ID } };
      return reply(post.status, post.body);
    }
    seen.gets.push(path);
    if (path === "/imports") return reply(200, { runs: opts.list ?? [] });
    const runs = opts.runs ?? [];
    const run = runs[Math.min(polls, runs.length - 1)];
    polls += 1;
    return reply(200, run);
  });
  return seen;
}

/** The DANDI asset listing of 000576: 23 files on one archive page. */
async function stubDandi(page: Page): Promise<string[]> {
  const urls: string[] = [];
  const results = [
    ...Array.from({ length: 20 }, (_, i) => ({
      path: `sub-${i + 1}/sub-${i + 1}_ecephys.nwb`,
      size: 1_000_000 * (i + 1),
    })),
    { path: "dataset_description.json", size: 512 },
    { path: "README.md", size: 900 },
    { path: "sub-1/sub-1_scans.tsv", size: 300 },
  ];
  await page.route(DANDI_ASSETS, (route) => {
    urls.push(route.request().url());
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      headers: { "access-control-allow-origin": "*" },
      body: JSON.stringify({ count: results.length, next: null, results }),
    });
  });
  return urls;
}

type Pick = {
  path: string | null;
  note?: string;
  source?: string;
  dataset_id?: string;
  version?: string;
};

async function seedPicks(page: Page, picks: Pick[]): Promise<void> {
  const targets = picks.map((p) => ({ ...DS, note: "", ...p }));
  await page.addInitScript(
    (doc) => {
      window.localStorage.setItem("asteroidea.targets/1", JSON.stringify(doc));
    },
    { schema: "asteroidea.targets/1", exported_at: "2026-10-25T00:00:00.000Z", targets },
  );
}

const running = {
  run_id: RUN_ID,
  source: SOURCE,
  dataset: "dandi-000623",
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

test("a non-curator has no Imports tab and no Imports page", async ({ page }) => {
  await signIn(page, ["researchers"]);
  await stubImports(page);
  await page.goto(`${APP}discover`);
  await expect(page.getByRole("link", { name: "Setup", exact: true })).toBeVisible();
  await expect(page.getByRole("link", { name: "Imports" })).toHaveCount(0);
  await page.goto(`${APP}discover/imports`);
  await expect(page.getByRole("heading", { name: "Page not found" })).toBeVisible();
  await expect(page.getByTestId("imports-recent")).toHaveCount(0);
});

test("a curator picks files under a dataset, imports them, and the run polls to DONE in the rail", async ({
  page,
}, testInfo) => {
  test.setTimeout(40_000);
  await signIn(page, ["researchers", "curators"]);
  const listed = await stubDandi(page);
  const api = await stubImports(page, { runs: [running, done] });
  await seedPicks(page, [{ path: null, note: "exemplar" }]);
  await page.goto(`${APP}discover?species=human`);
  await expect(page.getByTestId("pick-count")).toHaveText("1");
  await expect(page.getByTestId("pick-scope")).toHaveText("whole dataset");

  // Expand the dataset: its archive listing, ten rows at a time, narrowed by path and type.
  await page.getByRole("button", { name: "Files of 000576" }).click();
  await expect(page.getByTestId("pick-files-count")).toHaveText(
    "1–10 of 23 matching · 23 listed here",
  );
  expect(listed).toHaveLength(1);
  await page.getByLabel("Narrow DANDI 000576 files by type").selectOption(".nwb");
  await expect(page.getByTestId("pick-files-count")).toHaveText(
    "1–10 of 20 matching · 23 listed here",
  );
  await page.getByRole("button", { name: "Next 10" }).click();
  await expect(page.getByTestId("pick-files-count")).toHaveText(
    "11–20 of 20 matching · 23 listed here",
  );
  await page.getByLabel("Narrow DANDI 000576 files by path").fill("sub-1/");
  await page.getByLabel("Pick sub-1/sub-1_ecephys.nwb").check();
  await page.getByLabel("Narrow DANDI 000576 files by path").fill("sub-12");
  await page.getByLabel("Pick sub-12/sub-12_ecephys.nwb").check();
  await expect(page.getByTestId("pick-file")).toHaveCount(2);
  await expect(page.getByTestId("pick-scope")).toHaveText("2 files");
  await page.screenshot({ path: testInfo.outputPath("find-per-file-picks.png") });

  await page.getByRole("button", { name: /^Import 1 pick · ≤ 50 files per run$/ }).click();
  await expect(page).toHaveURL(new RegExp(`/discover/imports/${RUN_ID}\\?species=human$`));
  expect(api.posts).toEqual([
    { source: SOURCE, include: ["sub-1/sub-1_ecephys.nwb", "sub-12/sub-12_ecephys.nwb"] },
  ]);

  // The run in the rail: one square per file, state in colour and in words.
  const grid = page.getByTestId("import-grid");
  await expect(grid.getByRole("listitem")).toHaveCount(3);
  await expect(grid.getByRole("listitem", { name: "sub-1/b.nwb: waiting" })).toBeVisible();
  await expect(page.getByTestId("import-run-progress")).toContainText("2 of 3 files done");
  await expect(page.getByTestId("import-failures")).toHaveText(
    "sub-2/c.nwb failed: Extraction bad header.",
  );
  await expect(page.getByTestId("import-run-state")).toHaveText("RUNNING");
  await page.screenshot({ path: testInfo.outputPath("find-run-running.png") });

  // The second poll (5 s later) returns DONE; the polling stops and the new study opens from here.
  await expect(page.getByTestId("import-run-state")).toHaveText("DONE", { timeout: 15_000 });
  await expect(page.getByTestId("import-legend")).toContainText("imported 2");
  await expect(page.getByTestId("import-legend")).toContainText("already here 1");
  await expect(page.getByTestId("import-legend")).toContainText("failed 1");
  const open = page.getByTestId("open-new-study");
  await expect(open).toHaveAttribute("href", "/s/bmovie");
  await expect(open).toContainText("Open the new study");
  await page.screenshot({ path: testInfo.outputPath("find-run-done.png") });
  const polls = api.gets.filter((p) => p.startsWith("/imports/")).length;
  await page.waitForTimeout(6_000);
  expect(api.gets.filter((p) => p.startsWith("/imports/")).length).toBe(polls);
});

test("a run whose dataset no study lists opens the dataset page", async ({ page }) => {
  await signIn(page, ["curators"]);
  await stubImports(page, { runs: [{ ...done, dataset: "dandi-000576" }] });
  await page.goto(`${APP}discover/imports/${RUN_ID}`);
  await expect(page.getByTestId("open-new-study")).toHaveAttribute("href", "/d/dandi-000576");
});

test("a REFUSED run shows the refusal text and stops polling (deep link)", async ({
  page,
  expectError,
}) => {
  expectError("a REFUSED run shows its reason as an error Notice");
  await signIn(page, ["curators"]);
  const reason = "the selection holds 73 files; a web import takes at most 50";
  const api = await stubImports(page, {
    runs: [{ run_id: RUN_ID, source: SOURCE, state: "REFUSED", refusal: reason, files: [] }],
  });
  await page.goto(`${APP}discover/imports/${RUN_ID}`);
  await expect(page.getByTestId("import-refusal")).toContainText(reason);
  await expect(page.getByTestId("import-run-state")).toContainText("REFUSED");
  expect(api.gets).toEqual([`/imports/${RUN_ID}`]);
});

test("a 403 on start shows the server's text", async ({ page, expectError }) => {
  expectError("403 refusal");
  await signIn(page, ["curators"]);
  await stubImports(page, {
    post: { status: 403, body: { error: "caller is not in the curators group" } },
  });
  await seedPicks(page, [{ path: null }]);
  await page.goto(`${APP}discover`);
  await page.getByRole("button", { name: /^Import 1 pick/ }).click();
  await expect(page.getByTestId("import-picked-problem")).toContainText(
    "caller is not in the curators group",
  );
  await expect(page).toHaveURL(/\/discover$/);
});

test("the older per-file list imports one run per dataset, files as the subset", async ({
  page,
}) => {
  await signIn(page, ["curators"]);
  const api = await stubImports(page, { runs: [done], list: [] });
  await seedPicks(page, [
    { path: "sub-1/a.nwb" },
    { path: "sub-1/b.nwb" },
    { source: "openneuro", dataset_id: "ds001021", version: "1.0.0", path: null },
  ]);
  await page.goto(`${APP}discover`);
  await expect(page.getByTestId("pick-count")).toHaveText("2");
  await page.getByRole("button", { name: /^Import 2 picks/ }).click();
  await expect(page.getByTestId("import-started")).toContainText("2 imports started");
  await expect(page).toHaveURL(new RegExp(`/discover/imports/${RUN_ID}$`));
  expect(api.posts).toEqual([
    { source: SOURCE, include: ["sub-1/a.nwb", "sub-1/b.nwb"] },
    { source: "openneuro:ds001021@1.0.0", include: [] },
  ]);
});

test("a whole dataset over the 50-file cap is stopped with a sentence before any request", async ({
  page,
}) => {
  await signIn(page, ["curators"]);
  const api = await stubImports(page);
  await seedPicks(page, [
    { source: "openneuro", dataset_id: "ds000001", version: "1.0.0", path: null },
  ]);
  await page.goto(`${APP}discover`);
  await expect(page.getByTestId("import-cap")).toHaveText(
    "OpenNeuro ds000001 has 133 files, and one import takes at most 50. Open it in the picks and pick the files to import.",
  );
  await expect(page.getByRole("button", { name: /^Import 1 pick/ })).toBeDisabled();
  expect(api.posts).toEqual([]);
});

test("a researcher picks files under a dataset and exports both levels", async ({ page }) => {
  await signIn(page, ["researchers"]);
  await stubDandi(page);
  await seedPicks(page, [{ path: null }]);
  await page.goto(`${APP}discover`);
  await expect(page.getByRole("button", { name: /^Import \d/ })).toHaveCount(0);
  await page.getByRole("button", { name: "Files of 000576" }).click();
  await page.getByLabel("Pick sub-2/sub-2_ecephys.nwb").check();
  await expect(page.getByTestId("pick-scope")).toHaveText("1 file");
  const [download] = await Promise.all([
    page.waitForEvent("download"),
    page.getByRole("button", { name: "Ask a curator to import" }).click(),
  ]);
  const doc = JSON.parse(readFileSync((await download.path()) ?? "", "utf8"));
  expect(doc.targets).toEqual([
    { ...DS, path: null, note: "" },
    { ...DS, path: "sub-2/sub-2_ecephys.nwb", note: "" },
  ]);
  // Unpicking the last file leaves the whole dataset picked.
  await page.getByRole("button", { name: "Remove 000576 sub-2/sub-2_ecephys.nwb" }).click();
  await expect(page.getByTestId("pick-scope")).toHaveText("whole dataset");
});

// The retired /imports paths land under Find's Imports tab.
test("/imports redirects to /discover/imports, which lists the curator's recent imports", async ({
  page,
}) => {
  await signIn(page, ["curators"]);
  await stubImports(page, {
    list: [
      {
        run_id: "f".repeat(32),
        source: "openneuro:ds000001@1.0.0",
        include: [],
        state: "REFUSED",
        created_at: 1_792_000_000,
        refusal: "50-file cap",
      },
    ],
  });
  await page.goto(`${APP}imports`);
  await expect(page).toHaveURL(/\/discover\/imports$/);
  await expect(page.getByTestId("imports-recent")).toContainText("openneuro:ds000001@1.0.0");
  await page.getByRole("link", { name: "openneuro:ds000001@1.0.0" }).click();
  await expect(page).toHaveURL(new RegExp(`/discover/imports/${"f".repeat(32)}$`));
});

test("/imports/:run_id redirects to /discover/imports/:run_id", async ({ page }) => {
  await signIn(page, ["curators"]);
  await stubImports(page, { runs: [done] });
  await page.goto(`${APP}imports/${RUN_ID}`);
  await expect(page).toHaveURL(new RegExp(`/discover/imports/${RUN_ID}$`));
  await expect(page.getByTestId("import-run-state")).toContainText("DONE");
});
