// The Results place: the dot timeline (mine darker), the tree by the data the
// results used (through `GET /catalog/assets?ids=`), one result as a chain with "current", the readable
// run, the preview (an image, an HTML page in a sandboxed frame, or the box with a sentence), the
// "From the same run" strip and "used by"; rename, note, withdraw and delete, with Delete
// disabled and its reason while another result uses it; and the file page's "Results that used
// this file". Runs against the sign-in env build (port WEB_PORT + 1); the API and the bucket are
// page.route stubs over the golden bodies the real handlers answer (`e2e/fixtures/ux/*.golden.json`,
// written by ingest/tests/test_artifacts_golden.py and test_catalog_batch.py). The access token is a
// JWT whose `sub` decides whether the page shows the author controls; the ID token carries the email
// that marks a dot as mine.
import { expect, type Page } from "@playwright/test";
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
} from "./support";

test.skip(
  process.env.E2E_LIVE === "1",
  "needs the fixture-mode auth build (no live server has the auth env)",
);

const BASELINE = "0b6f3e52-5a1d-4c3e-9f10-1a2b3c4d5e01";
const RATES = "0b6f3e52-5a1d-4c3e-9f10-1a2b3c4d5e02";
const TUNING = "0b6f3e52-5a1d-4c3e-9f10-1a2b3c4d5e03";
const ADA = "sub-ada";
const GRACE = "sub-grace";
const API_CORS = { ...CORS, "access-control-allow-methods": "GET, POST, PATCH, DELETE, OPTIONS" };
const NOT_AUTHOR = {
  error: "not_author",
  detail: "Only the author of this result can change, withdraw or delete it.",
};

function jwt(claims: object): string {
  const part = (o: object) => Buffer.from(JSON.stringify(o)).toString("base64url");
  return `${part({ alg: "none" })}.${part(claims)}.sig`;
}

const ALL = JSON.parse(fixture("ux/artifacts-all.golden")) as {
  artifacts: { artifact_id: string; author_email: string }[];
};
const RECORD = JSON.parse(fixture("ux/artifact-record.golden")) as {
  record: Record<string, unknown>;
  url: string;
};

/** The tuning result's record (grace's; nothing used it), built from the rates record. */
const TUNING_RECORD = {
  record: {
    ...RECORD.record,
    artifact_id: TUNING,
    author_sub: GRACE,
    author_email: "grace@example.test",
    name: "tuning-curves.csv",
    note: null,
    inputs: [{ kind: "artifact", id: RATES, role: "rates" }],
    run: { run_id: "r-1", function: "tuning", parameters: { attribute: "face" } },
  },
  url: "https://artifacts.asteroidea.test/artifacts/sub-grace/tuning/data/tuning-curves.csv",
};

/**
 * A second run's four files: a figure, a figure whose bucket link fails
 * (503), the HTML report and a table. The `?run=` rows carry each file's link.
 */
const RUN2 = "run-2";
const PNG_ID = "0b6f3e52-5a1d-4c3e-9f10-1a2b3c4d5e11";
const HTML_ID = "0b6f3e52-5a1d-4c3e-9f10-1a2b3c4d5e12";
const CSV_ID = "0b6f3e52-5a1d-4c3e-9f10-1a2b3c4d5e13";
const BROKEN_ID = "0b6f3e52-5a1d-4c3e-9f10-1a2b3c4d5e14";
const BUCKET = "https://artifacts.asteroidea.test";
const RUN2_FILES = [
  { id: PNG_ID, name: "cs62-figure.png", type: "image/png" },
  { id: BROKEN_ID, name: "cs62-broken.png", type: "image/png" },
  { id: HTML_ID, name: "cs62-report.html", type: "text/html" },
  { id: CSV_ID, name: "cs62-table.csv", type: "text/csv" },
] as const;
const linkOf = (name: string) => `${BUCKET}/${RUN2}/${name}?X-Amz-Signature=1`;
const RUN2_RECORDS = new Map(
  RUN2_FILES.map((f) => [
    `/artifacts/${f.id}`,
    {
      record: {
        ...RECORD.record,
        artifact_id: f.id,
        name: f.name,
        content_type: f.type,
        run: { ...(RECORD.record.run as object), run_id: RUN2 },
      },
      url: linkOf(f.name),
    },
  ]),
);
const RUN2_ROWS = RUN2_FILES.map((f) => ({
  artifact_id: f.id,
  name: f.name,
  size: 2048,
  content_type: f.type,
  snapshot_id: null,
  state: "COMMITTED",
  created_at: 1_791_972_800,
  finalized_at: 1_791_972_800,
  source_asset_ids: [],
  source_asset_count: 0,
  run_id: RUN2,
  author_email: "ada@example.test",
  url: linkOf(f.name),
}));
// A 1x1 PNG.
const PNG = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=",
  "base64",
);

interface Seen {
  calls: string[];
  bodies: string[];
  bucket: string[];
}

async function stub(
  page: Page,
  opts: {
    sub: string;
    email: string;
    refuseChanges?: boolean;
    runStatus?: number;
    /** The All listing's rows; the golden listing when absent. */
    all?: readonly unknown[];
  },
): Promise<Seen> {
  await stubBase(page);
  await stubEmptyUploads(page);
  await stubEmptyLabels(page);
  await page.route(`${AUTH}/oauth2/token`, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      headers: CORS,
      body: JSON.stringify({
        access_token: jwt({ sub: opts.sub }),
        refresh_token: "refresh-1",
        id_token: jwt({ sub: opts.sub, email: opts.email }),
      }),
    }),
  );
  const seen: Seen = { calls: [], bodies: [], bucket: [] };
  await page.route(`${API}/artifacts**`, (route) => {
    const req = route.request();
    if (req.method() === "OPTIONS") return route.fulfill({ status: 204, headers: API_CORS });
    const url = new URL(req.url());
    seen.calls.push(`${req.method()} ${url.pathname}${url.search}`);
    if (req.postData() !== null) seen.bodies.push(req.postData() ?? "");
    const reply = (status: number, body: unknown) =>
      route.fulfill({
        status,
        contentType: "application/json",
        headers: API_CORS,
        body: JSON.stringify(body),
      });
    const id = url.pathname.split("/")[2] ?? "";
    if (req.method() !== "GET") {
      if (opts.refuseChanges === true) return reply(403, NOT_AUTHOR);
      if (url.pathname.endsWith("/withdraw")) {
        return reply(200, { state: "WITHDRAWN", artifact_id: id });
      }
      if (req.method() === "DELETE") return reply(200, { state: "DELETED", artifact_id: id });
      const patch = JSON.parse(req.postData() ?? "{}") as Record<string, unknown>;
      return reply(200, { artifact: { ...RECORD.record, ...patch } });
    }
    if (url.pathname === `/artifacts/${RATES}`) return reply(200, RECORD);
    if (url.pathname === `/artifacts/${TUNING}`) return reply(200, TUNING_RECORD);
    const second = RUN2_RECORDS.get(url.pathname);
    if (second !== undefined) return reply(200, second);
    if (url.pathname !== "/artifacts") return reply(404, { error: "not_found", detail: "no" });
    // Every golden result is run r-1.
    const run = url.searchParams.get("run");
    // API Gateway's own 503 body, as measured live.
    if (run !== null && opts.runStatus !== undefined) {
      return reply(opts.runStatus, { message: "Service Unavailable" });
    }
    if (run === RUN2) return reply(200, { artifacts: RUN2_ROWS });
    if (run !== null) return reply(200, ALL);
    const input = url.searchParams.get("input");
    if (input === RATES) return reply(200, JSON.parse(fixture("ux/artifacts-used-by.golden")));
    if (input !== null) return reply(200, { artifacts: [] });
    if (url.searchParams.get("scope") === "all") {
      return reply(200, opts.all === undefined ? ALL : { artifacts: opts.all });
    }
    return reply(200, {
      artifacts: ALL.artifacts.filter((a) => a.author_email === opts.email),
    });
  });
  // The golden record's presigned link is the handler test's fake (`https://signed.example/...`).
  for (const bucket of ["https://signed.example/**", "https://artifacts.asteroidea.test/**"]) {
    await page.route(bucket, (route) => {
      seen.bucket.push(route.request().url());
      const path = new URL(route.request().url()).pathname;
      if (path.endsWith("broken.png")) return route.fulfill({ status: 503, body: "" });
      if (path.endsWith(".png")) {
        return route.fulfill({ status: 200, contentType: "image/png", body: PNG });
      }
      if (path.endsWith(".html")) {
        return route.fulfill({
          status: 200,
          contentType: "text/html",
          body: "<!doctype html><title>Report</title><h1>CS62 report</h1>",
        });
      }
      return route.fulfill({
        status: 200,
        headers: { "content-disposition": 'attachment; filename="spike-rates.csv"' },
        contentType: "text/csv",
        body: "a,b\n1,2\n",
      });
    });
  }
  return seen;
}

const ada = { sub: ADA, email: "ada@example.test" };
const grace = { sub: GRACE, email: "grace@example.test" };

test("the timeline draws every result, mine darker, and the tree groups by the data used", async ({
  page,
}) => {
  const seen = await stub(page, ada);
  await page.goto(APP);
  // The home's Latest results card lists on its own; let it settle so the calls are ordered.
  await expect(page.getByTestId("home-latest-link")).toBeVisible();
  await page.getByRole("link", { name: "Results", exact: true }).click();
  await expect(page.locator('main[data-ready="results"]')).toBeVisible();

  await expect(page.getByRole("heading", { name: "3 results" })).toBeVisible();
  await expect(page.getByRole("tab", { name: "Mine 2" })).toBeVisible();
  await expect(page.getByRole("tab", { name: "Everyone's" })).toHaveAttribute(
    "aria-selected",
    "true",
  );
  const timeline = page.getByTestId("results-timeline");
  await expect(timeline.locator("circle")).toHaveCount(3);
  await expect(timeline.locator("circle.is-mine")).toHaveCount(2);

  const tree = page.getByTestId("results-tree");
  await expect(tree.getByRole("button", { name: /movie watching\s*2/ })).toBeVisible();
  await expect(tree.getByRole("button", { name: /From other results\s*1/ })).toBeVisible();
  await expect(tree.getByRole("button", { name: /CS62 · ses-1\s*2/ })).toBeVisible();
  const list = page.getByTestId("results-list");
  await expect(list.getByRole("link")).toHaveText([
    /spike-rates\.csv.*ada@example\.test/,
    /baseline-rates\.csv.*ada@example\.test/,
  ]);

  await tree.getByRole("button", { name: /From other results/ }).click();
  await expect(list.getByRole("link")).toHaveText([/tuning-curves\.csv.*grace@example\.test/]);

  await page.getByRole("tab", { name: "Mine 2" }).click();
  await expect(page.getByRole("heading", { name: "2 results" })).toBeVisible();
  await expect(timeline.locator("circle")).toHaveCount(2);

  // The home's Latest results card asks the All listing first; then the Results place.
  expect(seen.calls).toEqual([
    "GET /artifacts?scope=all",
    "GET /artifacts?scope=mine",
    "GET /artifacts?scope=all",
  ]);
});

/** The second run's files as the All listing lists them: newer than the golden run, named, CS62's. */
const RUN2_LISTED = RUN2_ROWS.map(({ url: _url, ...r }) => ({
  ...r,
  created_at: 1_792_000_000,
  finalized_at: 1_792_000_000,
  run_function: "showcase-cs62",
  source_asset_ids: ["6b1f0c2e-6f43-5b0e-9d2a-8a3c1f5e7d10"],
  source_asset_count: 1,
}));
/** A version 1 result: no run, so it stays in the tree and out of the Runs box. */
const NO_RUN = {
  ...RUN2_LISTED[3],
  artifact_id: "0b6f3e52-5a1d-4c3e-9f10-1a2b3c4d5e21",
  name: "old-batch.csv",
  created_at: 1_792_100_000,
  run_id: null,
  run_function: null,
};

test("the Runs box lists runs newest first and opens a run's first figure with its strip", async ({
  page,
  expectError,
}) => {
  expectError("the broken sibling's thumbnail answers 503");
  const all = [NO_RUN, ...RUN2_LISTED, ...ALL.artifacts];
  const seen = await stub(page, { ...ada, all });
  await page.goto(APP);
  // The home's Latest results card links the newest run, labelled as the box labels it.
  const latest = page.getByTestId("home-latest");
  await expect(latest).toContainText("Latest results");
  await expect(latest.getByRole("link")).toHaveText(/showcase-cs62\s*4 files · 14 Oct · you/);
  await expect(latest.getByRole("link")).toHaveAttribute("href", `/results/${BROKEN_ID}`);

  await page.getByRole("link", { name: "Results", exact: true }).click();
  await expect(page.locator('main[data-ready="results"]')).toBeVisible();
  const runs = page.getByTestId("results-runs");
  await expect(runs.getByRole("heading", { name: "Runs" })).toBeVisible();
  const rows = runs.getByRole("link");
  await expect(rows).toHaveText([
    /showcase-cs62\s*CS62 · 4 files · 14 Oct · you/,
    /tuning\s*CS62 · 3 files · \d+ Oct · grace@example\.test/,
  ]);
  // The version 1 result is no run: the timeline still draws it.
  await expect(page.getByTestId("results-timeline").locator("circle")).toHaveCount(all.length);
  await expect(runs).not.toContainText("old-batch.csv");
  // The box draws from the listing the page already loaded: no request beyond the place's own.
  expect(seen.calls.filter((c) => c.startsWith("GET /artifacts?"))).toEqual([
    "GET /artifacts?scope=all",
    "GET /artifacts?scope=mine",
    "GET /artifacts?scope=all",
  ]);

  // One link: the run's first image by name, with the "From the same run" strip.
  await rows.first().click();
  await expect(page).toHaveURL(new RegExp(`/results/${BROKEN_ID}$`));
  const strip = page.getByTestId("result-run-strip");
  await expect(strip).toContainText("From the same run · 4");
  await expect(strip.locator('[aria-current="page"]')).toHaveText("cs62-broken.png");
});

test("one result is a chain: inputs with role and current, the readable run, a preview box, used by", async ({
  page,
}) => {
  const seen = await stub(page, ada);
  await page.goto(`${APP}results/${RATES}`);
  const chain = page.getByTestId("result-page");
  await expect(chain.getByRole("heading", { name: /spike-rates\.csv/ })).toBeVisible();
  await expect(page.getByTestId("result-by")).toContainText("By ada@example.test (you)");
  await expect(chain).toContainText("Rates around scene cuts.");

  const inputs = page.getByTestId("result-inputs").locator("li");
  await expect(inputs).toHaveCount(3);
  await expect(inputs.nth(0)).toContainText("RECORDING · role: spikes");
  await expect(inputs.nth(0)).toContainText("CS62 · ses-1 · NWB ephys");
  await expect(inputs.nth(0)).toContainText("current ✓");
  await expect(inputs.nth(1)).toContainText("EVENTS · role: alignment");
  await expect(inputs.nth(2)).toContainText("RESULT · role: baseline");
  await expect(inputs.nth(2).getByRole("link")).toHaveAttribute("href", `/results/${BASELINE}`);

  const run = page.getByTestId("result-run");
  await expect(run).toContainText("Firing rates around scene cut");
  await expect(run).toContainText("analysis.rates");
  await expect(run).toContainText("100 ms");
  await expect(run).toContainText("Study defaults 2026.10");

  await expect(page.getByTestId("result-preview")).toHaveText(
    "Preview not built for application/octet-stream; download it",
  );
  // Every golden result is run r-1: the strip lists the three, this one marked.
  const strip = page.getByTestId("result-run-strip");
  await expect(strip.getByRole("link")).toHaveCount(3);
  await expect(strip.locator('[aria-current="page"]')).toHaveText("spike-rates.csv");
  const usedBy = page.getByTestId("result-used-by");
  await expect(usedBy.getByRole("link", { name: "tuning-curves.csv" })).toHaveAttribute(
    "href",
    `/results/${TUNING}`,
  );
  await expect(usedBy).toContainText("grace@example.test");

  // The open result's branch lists it, selected.
  await expect(page.getByTestId("results-list").locator('[aria-current="page"]')).toContainText(
    "spike-rates.csv",
  );

  // Delete is visibly disabled with the reason while another result uses it.
  await expect(page.getByTestId("result-delete")).toBeDisabled();
  await expect(page.getByTestId("result-delete-reason")).toContainText(
    "Delete is refused while another result uses it",
  );

  await page.getByTestId("result-download").click();
  await expect.poll(() => seen.bucket.length).toBe(1);
  expect(seen.calls).toContain(`GET /artifacts?scope=all&input=${RATES}`);
  expect(seen.calls).toContain("GET /artifacts?scope=all&run=r-1");
  // Download asks for a fresh attachment link.
  expect(seen.calls).toContain(`GET /artifacts/${RATES}?download=1`);
});

test("an image and an HTML page preview in place, and the run strip links the run's files", async ({
  page,
  expectError,
}) => {
  expectError("the broken sibling's thumbnail answers 503");
  const seen = await stub(page, ada);
  await page.goto(`${APP}results/${PNG_ID}`);
  const img = page.getByTestId("result-preview").locator("img");
  await expect(img).toHaveAttribute("src", `${BUCKET}/${RUN2}/cs62-figure.png?X-Amz-Signature=1`);
  await expect.poll(() => img.evaluate((e: HTMLImageElement) => e.naturalWidth)).toBe(1);

  const strip = page.getByTestId("result-run-strip");
  await expect(strip).toContainText("From the same run · 4");
  await expect(strip.getByRole("link")).toHaveCount(4);
  await expect(strip.locator('[aria-current="page"]')).toHaveText("cs62-figure.png");
  // The broken figure's thumbnail fails (503): it drops to its name chip, the figure's stays.
  await expect(strip.locator("img")).toHaveCount(1);
  await expect(strip.getByRole("link", { name: "cs62-broken.png" })).toBeVisible();
  // One listing call and no GET per sibling: each row carried its own link.
  expect(seen.calls.filter((c) => c.startsWith("GET /artifacts/"))).toEqual([
    `GET /artifacts/${PNG_ID}`,
  ]);
  expect(seen.calls).toContain(`GET /artifacts?scope=all&run=${RUN2}`);
  expect(seen.bucket).toContain(linkOf("cs62-broken.png"));

  await strip.getByRole("link", { name: "cs62-report.html" }).click();
  await expect(page).toHaveURL(new RegExp(`/results/${HTML_ID}$`));
  const frame = page.getByTestId("result-preview").locator("iframe");
  await expect(frame).toHaveAttribute("sandbox", "allow-scripts");
  await expect(
    page.frameLocator('[data-testid="result-preview"] iframe').getByRole("heading", {
      name: "CS62 report",
    }),
  ).toBeVisible();

  await page.getByTestId("result-run-strip").getByRole("link", { name: "cs62-table.csv" }).click();
  await expect(page).toHaveURL(new RegExp(`/results/${CSV_ID}$`));
  await expect(page.getByTestId("result-preview")).toHaveText(
    "Preview not built for text/csv; download it",
  );
});

test("a failing run listing shows its HTTP status and never holds the page", async ({
  page,
  expectError,
}) => {
  expectError("the run listing answers 503");
  await stub(page, { ...ada, runStatus: 503 });
  await page.goto(`${APP}results/${PNG_ID}`);
  const img = page.getByTestId("result-preview").locator("img");
  await expect.poll(() => img.evaluate((e: HTMLImageElement) => e.naturalWidth)).toBe(1);
  await expect(page.getByTestId("result-run-strip-error")).toHaveText(
    "The other files of this run could not be listed: The platform API answered HTTP 503.",
  );
  await expect(page.getByTestId("result-run-strip-loading")).toHaveCount(0);
  await expect(page.getByTestId("result-download")).toBeEnabled();
});

test("the author renames, edits the note and withdraws through a confirm dialog", async ({
  page,
}) => {
  const seen = await stub(page, ada);
  await page.goto(`${APP}results/${RATES}`);
  await page.getByRole("button", { name: "Rename" }).click();
  await page.getByRole("textbox", { name: "Name" }).fill("rates-v2.csv");
  await page.getByRole("button", { name: "Save name" }).click();
  await expect(page.getByTestId("result-done")).toHaveText("Renamed.");
  await expect(page.getByRole("heading", { name: /rates-v2\.csv/ })).toBeVisible();

  await page.getByRole("button", { name: "Edit note" }).click();
  await page.getByRole("textbox", { name: "Note" }).fill("Second pass.");
  await page.getByRole("button", { name: "Save note" }).click();
  await expect(page.getByTestId("result-done")).toHaveText("Note saved.");

  await page.getByTestId("result-withdraw").click();
  const dialog = page.getByTestId("result-confirm");
  await expect(dialog).toBeVisible();
  await dialog.getByRole("button", { name: "Withdraw" }).click();
  await expect(page.getByTestId("result-done")).toContainText("Withdrawn.");
  await expect(page.getByRole("heading", { name: /Withdrawn/ })).toBeVisible();

  expect(seen.bodies.map((b) => JSON.parse(b) as unknown)).toEqual([
    { name: "rates-v2.csv", note: "Rates around scene cuts." },
    { name: "rates-v2.csv", note: "Second pass." },
  ]);
  expect(seen.calls).toContain(`POST /artifacts/${RATES}/withdraw`);
});

test("another researcher sees no author control", async ({ page }) => {
  await stub(page, grace);
  await page.goto(`${APP}results/${RATES}`);
  await expect(page.getByTestId("result-by")).toContainText("By ada@example.test");
  await expect(page.getByTestId("result-download")).toBeVisible();
  await expect(page.getByRole("button", { name: "Rename" })).toHaveCount(0);
  await expect(page.getByTestId("result-withdraw")).toHaveCount(0);
  await expect(page.getByTestId("result-delete")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Edit note" })).toHaveCount(0);
});

test("the author deletes a result nothing used, then returns to the list", async ({ page }) => {
  const seen = await stub(page, grace);
  await page.goto(`${APP}results/${TUNING}`);
  await expect(page.getByTestId("result-delete")).toBeEnabled();
  await page.getByTestId("result-delete").click();
  await page.getByTestId("result-confirm").getByRole("button", { name: "Delete" }).click();
  await expect(page).toHaveURL(/\/results$/);
  await expect(page.getByTestId("results-pick")).toBeVisible();
  expect(seen.calls).toContain(`DELETE /artifacts/${TUNING}`);
});

test("a not_author refusal is shown as a Notice with the server's sentence", async ({
  page,
  expectError,
}) => {
  expectError("not_author refusal");
  await stub(page, { ...ada, refuseChanges: true });
  await page.goto(`${APP}results/${RATES}`);
  await page.getByTestId("result-withdraw").click();
  await page.getByTestId("result-confirm").getByRole("button", { name: "Withdraw" }).click();
  const problem = page.getByTestId("result-problem");
  await expect(problem).toHaveText(NOT_AUTHOR.detail);
  await expect(problem).toHaveAttribute("role", "alert");
  await expect(page.getByRole("heading", { name: /Saved/ })).toBeVisible();
});

test("a failed listing shows an error Notice with Try again", async ({ page, expectError }) => {
  expectError("failed listing");
  await stubBase(page);
  await stubEmptyUploads(page);
  let calls = 0;
  await page.route(`${API}/artifacts**`, (route) => {
    if (route.request().method() === "OPTIONS")
      return route.fulfill({ status: 204, headers: CORS });
    calls += 1;
    const failed = calls === 1;
    return route.fulfill({
      status: failed ? 403 : 200,
      contentType: "application/json",
      headers: CORS,
      body: failed
        ? JSON.stringify({ error: "forbidden", detail: "caller is not in the researchers group" })
        : JSON.stringify(ALL),
    });
  });
  await page.goto(`${APP}results`);
  const error = page.getByTestId("results-error");
  await expect(error).toContainText("Your account is not in the researchers group");
  await expect(error).toHaveClass(/notice-error/);
  await error.getByRole("button", { name: "Try again" }).click();
  await expect(page.getByTestId("results-timeline")).toBeVisible();
});

test("the file page lists the results that used the file", async ({ page }) => {
  const seen = await stub(page, ada);
  for (const [path, name] of [
    ["/catalog/assets/asset-0101", "catalog-file-nwb"],
    ["/catalog/datasets", "catalog-datasets"],
  ] as const) {
    await page.route(`${API}${path}`, (route) =>
      route.request().method() === "OPTIONS"
        ? route.fulfill({ status: 204, headers: CORS })
        : route.fulfill({
            status: 200,
            contentType: "application/json",
            headers: CORS,
            body: fixture(name),
          }),
    );
  }
  await page.route(`${API}/artifacts?scope=all&input=asset-0101`, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      headers: API_CORS,
      body: fixture("artifact-used-by"),
    }),
  );
  await page.goto(`${APP}files/asset-0101`);
  await expect(page.getByTestId("file-used-by")).toBeVisible();
  await page.getByTestId("file-used-by-show").click();
  const rows = page.getByTestId("file-used-by-table").locator("tbody tr");
  await expect(rows).toHaveCount(1);
  await expect(rows.nth(0).getByRole("link", { name: "tuning-curves.csv" })).toHaveAttribute(
    "href",
    "/results/art-9",
  );
  await expect(rows.nth(0)).toContainText("grace@example.test");
  expect(seen.calls).toEqual([]);
});
