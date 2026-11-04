// The hosted-app smoke run (`make web-smoke`): every page and tab a researcher can reach, by user
// navigation from `/`, at 1440 x 900 and 375 x 812. It only MEASURES: for each step it records the
// console errors, uncaught page errors, failed requests, HTTP statuses >= 400, error Notices and
// empty data views it saw, plus a screenshot. `web_smoke.py` judges them (which 404s a step may
// expect, which steps must exist) and writes the report.
// Managed login is not driven: test tokens for the sandbox are placed in
// sessionStorage before the page loads (web/src/auth/store.ts).
// Empty views are found against the API: this file asks the API directly (the same bearer token)
// how many rows a table has, then requires the page to draw at least one.
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import {
  type BrowserContext,
  expect as baseExpect,
  type Page,
  type Response,
  test,
} from "@playwright/test";
import { allInView, dragPath } from "./smoke-drag";

// A failing step costs this long per assertion (the live config's 90 s is for timed rows); a
// cold LFP overview is the slowest legitimate wait.
const expect = baseExpect.configure({ timeout: 45_000 });

const env = (name: string): string => {
  const v = process.env[name];
  if (v === undefined || v === "")
    throw new Error(`${name} is not set; start this with make web-smoke`);
  return v;
};

const TOKENS_KEY = "asteroidea.auth.tokens/1"; // web/src/auth/store.ts
const SETTLE_MS = 5_000;
const TEST_TIMEOUT_MS = 1_500_000;
const WINDOW_S = 60;
const WINDOW_AT_S = 300;

type Kind = "console" | "pageerror" | "requestfailed" | "http" | "notice" | "empty";

interface Issue {
  kind: Kind;
  text: string;
  url: string;
  status: number;
}

interface StepRecord {
  viewport: string;
  step: string;
  ok: boolean;
  seconds: number;
  detail: string;
  screenshot: string;
  issues: Issue[];
}

interface Target {
  asset: string;
  dataset: string;
  subject: string;
}

interface Targets {
  datasets: string[];
  nwb: Target;
  nifti: Target | null;
}

const VIEWPORTS = [
  { name: "desktop", width: 1440, height: 900 },
  { name: "phone", width: 375, height: 812 },
] as const;

type Row = { [key: string]: unknown };

function slug(text: string): string {
  return text
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-|-$/g, "");
}

for (const vp of VIEWPORTS) {
  test(`smoke ${vp.name}`, async ({ browser }) => {
    test.setTimeout(TEST_TIMEOUT_MS);
    const app = env("SMOKE_APP_URL");
    const api = env("SMOKE_API_URL").replace(/\/$/, "");
    const out = env("SMOKE_OUT");
    const targets = JSON.parse(readFileSync(env("SMOKE_TARGETS_FILE"), "utf8")) as Targets;
    const token = env("SMOKE_ACCESS_TOKEN");
    mkdirSync(out, { recursive: true });
    const context: BrowserContext = await browser.newContext({
      viewport: { width: vp.width, height: vp.height },
    });
    await context.addInitScript(
      ({ key, access, refresh }) => {
        try {
          if (window.sessionStorage.getItem(key) === null) {
            window.sessionStorage.setItem(
              key,
              JSON.stringify({ access_token: access, refresh_token: refresh }),
            );
          }
        } catch {
          // storage blocked: the run shows the signed-out page and fails at the first step
        }
      },
      { key: TOKENS_KEY, access: token, refresh: env("SMOKE_REFRESH_TOKEN") },
    );
    context.setDefaultTimeout(45_000); // every action fails after this, its locator named
    const page: Page = await context.newPage();
    const records: StepRecord[] = [];
    const seen: Issue[] = [];
    const note = (kind: Kind, text: string, url = "", status = 0) => {
      seen.push({ kind, text, url, status });
    };
    page.on("console", (m) => {
      if (m.type() === "error") note("console", m.text(), m.location().url);
    });
    page.on("pageerror", (e) => note("pageerror", String(e.stack ?? e.message)));
    page.on("requestfailed", (r) =>
      note("requestfailed", r.failure()?.errorText ?? "failed", r.url()),
    );
    page.on("response", (r) => {
      if (r.status() >= 400) note("http", `${r.status()} ${r.statusText()}`, r.url(), r.status());
    });

    const apiGet = async (path: string): Promise<Row> => {
      const reply = await context.request.get(`${api}${path}`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!reply.ok()) throw new Error(`oracle GET ${path} answered ${reply.status()}`);
      return (await reply.json()) as Row;
    };
    const countOf = (body: Row, key: string): number => {
      const list = body[key];
      return Array.isArray(list) ? list.length : 0;
    };

    const started = performance.now();
    async function step(name: string, body: () => Promise<string | undefined>): Promise<void> {
      const t0 = performance.now();
      let ok = true;
      let detail = "";
      try {
        detail = (await body()) ?? "";
      } catch (err) {
        ok = false;
        detail = String(err).split("\n")[0] ?? "failed";
      }
      await page.waitForLoadState("networkidle", { timeout: SETTLE_MS }).catch(() => undefined);
      for (const el of await page.locator(".notice-error").all()) {
        note("notice", ((await el.textContent().catch(() => "")) ?? "").trim());
      }
      const screenshot = `${out}/${vp.name}-${String(records.length).padStart(2, "0")}-${slug(name)}.png`;
      await page.screenshot({ path: screenshot }).catch(() => undefined);
      const issues = seen.splice(0);
      records.push({
        viewport: vp.name,
        step: name,
        ok,
        seconds: Math.round(((performance.now() - t0) / 1000) * 1000) / 1000,
        detail,
        screenshot,
        issues,
      });
      writeFileSync(`${out}/smoke-${vp.name}.json`, JSON.stringify(records, null, 2));
    }

    function empty(text: string): never {
      note("empty", text);
      throw new Error(text);
    }
    const nav = (label: string) =>
      page
        .getByRole("navigation", { name: "Main" })
        .getByRole("link", { name: label, exact: true });
    const tab = (name: string | RegExp) => page.getByRole("tab", { name });
    const loaded = async () => {
      await expect(page.getByTestId("catalog-loading")).toHaveCount(0);
      await expect(page.getByTestId("research-loading")).toHaveCount(0);
      await expect(page.getByTestId("participant-loading")).toHaveCount(0);
      await expect(page.getByTestId("study-loading")).toHaveCount(0);
    };
    // A table drawn from `expected` API rows must show at least one row and not its empty note.
    const tableRows = async (testId: string, expected: number, what: string) => {
      const table = page.getByTestId(testId);
      await expect(table.or(page.getByTestId(`${testId}-empty`))).toBeVisible();
      const drawn = await table.locator("tbody tr").count();
      if (expected > 0 && drawn === 0)
        empty(`${what}: the API has ${expected} rows, the page draws none`);
      return drawn;
    };
    // The dataset address `/d/:dataset` is the study page in one-dataset mode.
    const openDataset = async (dataset: string) => {
      await page.goto(new URL(`/d/${encodeURIComponent(dataset)}`, app).href);
      await expect(page.getByTestId("study-page")).toBeVisible();
      await loaded();
    };
    const openSubject = async (t: Target) => {
      await openDataset(t.dataset);
      await page
        .getByTestId(`release-${t.dataset}`)
        .getByRole("link", { name: t.subject, exact: true })
        .click();
      await expect(page.getByTestId("participant-page")).toBeVisible();
      await expect(page.locator('main[data-ready="participant"]')).toBeVisible();
      await loaded();
    };
    // The participant page lists a person's recordings, not a subject's files: the file page is
    // reached by its address, as the study page's session cells do.
    const openFile = async (t: Target) => {
      await openSubject(t);
      await page.goto(new URL(`/files/${encodeURIComponent(t.asset)}`, app).href);
      await expect(page.getByTestId("file-page")).toBeVisible();
      await loaded();
    };

    // A failed step does not stop the run; each step first reaches its own start state by the
    // page's own links (these helpers do nothing when the previous step left the page there).
    const ensureFile = async (t: Target) => {
      if (!(await page.getByTestId("file-page").isVisible())) await openFile(t);
    };

    // 1. Find (the public-archive browser)
    await step("Discover", async () => {
      await page.goto(app);
      await nav("Find").click();
      await expect(page.getByTestId("index-summary")).toBeVisible();
      const rows = await page.getByTestId("best-fit").locator("li").count();
      if (rows === 0) empty("the Find page has no best-fit rows");
      const cells = await page.locator('[data-ab="matrix-cell"]').count();
      if (cells === 0) empty("the Find matrix has no cell");
      return `${rows} best fits; ${cells} matrix cells; ${(await page.getByTestId("match-count").textContent()) ?? ""}`;
    });
    await step("Dataset detail pane", async () => {
      if (!(await page.getByTestId("best-fit").isVisible())) await nav("Find").click();
      const first = page.getByTestId("best-fit").locator("li").first();
      await expect(first.getByRole("link").first()).toBeVisible();
      const picks = page
        .getByRole("button", { name: /^Pick dataset / })
        .or(first.getByText("Picked"));
      await expect(picks.first()).toBeVisible();
      return ((await first.locator(".find-fit-meta").textContent()) ?? "").trim();
    });

    // 2. The data home: one row per study, plus one per curated dataset outside every
    // study, equal to what the studies route answers.
    await step("Data list", async () => {
      await nav("Studies").click();
      await expect(page.locator('main[data-ready="home"]')).toBeVisible();
      const body = await apiGet("/catalog/studies");
      const studies = (body.studies as Row[] | undefined) ?? [];
      const inStudy = new Set(
        studies.flatMap((s) => ((s.datasets as Row[] | undefined) ?? []).map((d) => d.dataset)),
      );
      const loose = ((body.datasets as Row[] | undefined) ?? []).filter(
        (d) => d.tier === "curated" && !inStudy.has(d.dataset),
      );
      const expected = studies.length + loose.length;
      const rows = await page.getByTestId("study-row").count();
      if (expected > 0 && rows === 0) empty("the API has studies, the home draws no row");
      if (rows !== expected)
        throw new Error(`the home draws ${rows} rows; the API has ${expected}`);
      return `${rows} study rows`;
    });
    const studies = await apiGet("/catalog/studies");
    for (const dataset of targets.datasets) {
      const row = ((studies.datasets as Row[] | undefined) ?? []).find(
        (d) => d.dataset === dataset,
      );
      const expected = Array.isArray(row?.subjects) ? row.subjects.length : 0;
      await step(`Dataset page ${dataset}`, async () => {
        await openDataset(dataset);
        const drawn = await page
          .getByTestId(`release-${dataset}`)
          .locator('[data-ab="participant-row"]')
          .count();
        if (expected > 0 && drawn === 0)
          empty(`${dataset}: the API has ${expected} subjects, the page draws no participant row`);
        return `${drawn} participants`;
      });
    }

    // 3. One subject
    await step("Subject page", async () => {
      await openSubject(targets.nwb);
      // The page is ready once every card has its glyph or the reason it has none.
      const lanes = await page.locator('[data-ab="lane"]').count();
      if (lanes === 0) empty("the participant page draws no timeline lane");
      const cards = await page.getByTestId("ephys-card").count();
      const glyphs = await page.locator('[data-ab="glyph"]').count();
      const none = await page.getByTestId("glyph-none").count();
      if (glyphs + none !== cards)
        throw new Error(`${cards} ephys cards, ${glyphs} glyphs, ${none} refusals`);
      return `${lanes} lanes; ${cards} ephys cards; ${glyphs} glyphs`;
    });

    // 4. One NWB file
    const nwb = targets.nwb.asset;
    const series = await apiGet(`/catalog/assets/${nwb}/tables/series`);
    const electrical = ((series.rows as Row[] | undefined) ?? []).filter((r) =>
      /ElectricalSeries|LFP/.test(String(r.neurodata_type ?? "")),
    );
    const units = await apiGet(`/catalog/assets/${nwb}/tables/units`);
    const electrodes = await apiGet(`/catalog/assets/${nwb}/tables/electrodes`);
    const events = await apiGet(`/catalog/assets/${nwb}/events`);
    await step("File Overview", async () => {
      await openFile(targets.nwb);
      await expect(page.getByTestId("file-overview")).toBeVisible();
      return "header drawn";
    });
    await step("File Units", async () => {
      await ensureFile(targets.nwb);
      await tab("Units").click();
      await expect(page.getByTestId("unit-picker")).toBeVisible();
      await loaded();
      const boxes = page.getByTestId("unit-picker").locator("input[type=checkbox]");
      const n = await boxes.count();
      if (countOf(units, "rows") > 0 && n === 0)
        empty("the API lists units, the picker offers none");
      await boxes.first().check();
      const align = page.getByTestId("units-align");
      // The picker is disabled ("Loading alignments…") until the intervals have loaded.
      await expect(align).toBeEnabled();
      const options = await align
        .locator("option")
        .evaluateAll((os) => os.map((o) => (o as HTMLOptionElement).value).filter((v) => v !== ""));
      const wanted = countOf(events, "intervals") > 0;
      const first = options[0];
      if (first === undefined) {
        if (wanted) empty("the API lists intervals, the align picker offers none");
        return `${n} units; no alignment to request`;
      }
      await align.selectOption(first);
      await expect(page.getByTestId("rates-result")).toBeVisible();
      await expect(page.getByTestId("rates-summary")).toBeVisible();
      return `${n} units; default rates for ${first}`;
    });
    await step("File Electrodes", async () => {
      await ensureFile(targets.nwb);
      await tab("Electrodes").click();
      const drawn = await tableRows("file-electrodes", countOf(electrodes, "rows"), "electrodes");
      return `${drawn} rows`;
    });
    await step("File Series", async () => {
      await ensureFile(targets.nwb);
      await tab("Series").click();
      const drawn = await tableRows("file-series", countOf(series, "rows"), "series");
      return `${drawn} rows; ${electrical.length} electrical`;
    });
    await step("File Events", async () => {
      await ensureFile(targets.nwb);
      // The events are lanes in the Signals tab; the labels list is always drawn.
      await tab(/^Signals/i).click();
      await expect(page.getByTestId("labels-list")).toBeVisible();
      const has = countOf(events, "intervals") + countOf(events, "events") > 0;
      if ((await page.getByTestId("lanes-error").count()) > 0)
        empty("the events route answered an error on the Signals tab");
      return has ? "labels list drawn; events load as lanes" : "no events in this file";
    });
    let total = 0;
    await step("File LFP overview", async () => {
      await ensureFile(targets.nwb);
      await tab(/^Signals/i).click();
      await loaded();
      if (electrical.length > 0) {
        await expect(page.getByTestId("lfp-series")).toBeVisible();
        const n = await page.getByTestId("lfp-series").locator("option").count();
        if (n === 0)
          empty("the series table lists an ElectricalSeries, the LFP picker has no options");
        await expect(page.getByTestId("lfp-overview")).toBeVisible();
        await expect(page.getByTestId("lfp-loading")).toHaveCount(0);
        if ((await page.getByTestId("lfp-preparing").count()) > 0)
          empty("the overview is not built for this file");
        total = Number(await page.getByTestId("lfp-overview-area").getAttribute("aria-valuemax"));
        return `${n} series options; ${total.toFixed(0)} s recording`;
      }
      await expect(page.getByTestId("lfp-none")).toBeVisible();
      return "no electrical series in this file";
    });
    await step("File LFP window", async () => {
      if (electrical.length === 0) return "no electrical series in this file";
      await ensureFile(targets.nwb);
      if (!(await page.getByTestId("lfp-overview-area").isVisible())) {
        await tab(/^Signals/i).click();
        await loaded();
      }
      const area = page.getByTestId("lfp-overview-area");
      const viewport = { width: vp.width, height: vp.height };
      const spectrogram = () =>
        page.waitForResponse((r) => r.url().includes("/compute/spectrogram"));
      const settled = async (reply: Promise<Response>) => {
        const answered = await reply;
        if (!answered.ok()) throw new Error(`spectrogram answered ${answered.status()}`);
        await expect(page.getByTestId("lfp-window-result")).toBeVisible();
        await expect(page.getByTestId("lfp-spectrogram")).toBeVisible();
        await expect(page.getByTestId("lfp-trace")).toBeVisible();
      };
      // `page.mouse` does not scroll: bring the overview into view, then measure it.
      const drag = async (start: number) => {
        await area.scrollIntoViewIfNeeded();
        const box = await area.boundingBox();
        if (box === null) throw new Error("no overview box");
        const path = dragPath(box, total, start, WINDOW_S);
        if (!allInView(path, viewport))
          throw new Error(`the overview drag is outside the ${vp.width} x ${vp.height} viewport`);
        const reply = spectrogram();
        const [first, ...rest] = path;
        if (first === undefined) throw new Error("no drag path");
        await page.mouse.move(first.x, first.y);
        await page.mouse.down();
        for (const p of rest) await page.mouse.move(p.x, p.y, { steps: 6 });
        await page.mouse.up();
        await settled(reply);
      };
      if (!(total > WINDOW_S)) throw new Error(`no overview span (${total} s)`);
      const start = Math.min(WINDOW_AT_S, total - WINDOW_S);
      if (vp.name === "phone") {
        // Primary on a phone: the Zoom in button. The drag is a second check.
        await area.scrollIntoViewIfNeeded();
        const reply = spectrogram();
        await page.getByRole("button", { name: "Zoom in" }).click();
        await settled(reply);
        await drag(Math.min(start + 2 * WINDOW_S, total - WINDOW_S));
        return `zoom in, then a ${WINDOW_S} s drag at t=${start.toFixed(0)} s`;
      }
      await drag(start);
      return `window ${WINDOW_S} s at t=${start.toFixed(0)} s`;
    });

    // 5. One NIfTI file
    await step("NIfTI Overview", async () => {
      if (targets.nifti === null) throw new Error("the catalog has no NIfTI file to visit");
      await openFile(targets.nifti);
      await expect(page.getByTestId("file-overview")).toBeVisible();
      return "header drawn";
    });
    await step("NIfTI Image", async () => {
      if (targets.nifti === null) throw new Error("the catalog has no NIfTI file to visit");
      await ensureFile(targets.nifti);
      const volumeRequests: string[] = [];
      page.on("request", (r) => {
        if (/\.nii(\.gz)?$/.test(new URL(r.url()).pathname)) volumeRequests.push(r.url());
      });
      await tab("Image").click();
      await expect(page.getByTestId("mri-canvas")).toBeVisible();
      const mm = page.getByTestId("mri-mm");
      await expect(mm).toBeVisible();
      await expect
        .poll(async () => ((await mm.textContent()) ?? "").match(/-?\d+(\.\d+)?/g)?.length ?? 0)
        .toBe(3);
      if (volumeRequests.length !== 1) {
        throw new Error(`the volume was requested ${volumeRequests.length} times`);
      }
      return "viewer canvas drawn, world mm shows 3 numbers, volume requested once";
    });

    // 5b. The session heatmap. Live-only; the locators are the fixture build's
    // (web/e2e/session.spec.ts): `main[data-ready="session"]`, `data-ab` marks unit-row, region-group,
    // delta-bar, null-z-row, totals and baseline-box; test ids session-page, session-no-events,
    // session-no-units, session-refusal, event-<type>; link per session: session-link-<session>, "_"
    // for none. The page waits on the events, the units table and the rates (usually 6 to 9 s).
    const sessionOf = async (assetId: string): Promise<string | null> => {
      const body = await apiGet(`/catalog/assets/${assetId}`);
      const s = (body.asset as Row | undefined)?.bids_session;
      return typeof s === "string" && s !== "" ? s : null;
    };
    const openSession = async (t: Target) => {
      const session = await sessionOf(t.asset);
      await openSubject(t);
      const t0 = performance.now();
      await page
        .locator(
          `a[href$="/${encodeURIComponent(t.subject)}/${encodeURIComponent(session ?? "_")}"]`,
        )
        .click();
      await expect(page.locator('main[data-ready="session"]')).toBeVisible({ timeout: 60_000 });
      return { session, waited: Math.round(performance.now() - t0) / 1000 };
    };
    const ab = (mark: string) => page.locator(`[data-ab="${mark}"]`);
    // What the session page drew: the heatmap's counts, or the sentence standing in its place.
    const drawnSession = async (): Promise<string> => {
      for (const id of ["session-no-events", "session-no-units", "session-refusal"]) {
        const el = page.getByTestId(id);
        if ((await el.count()) > 0) return `${id}: ${((await el.textContent()) ?? "").trim()}`;
      }
      const rows = await ab("unit-row").count();
      const groups = await ab("region-group").count();
      const bars = await ab("delta-bar").count();
      const nulls = await ab("null-z-row").count();
      const totals = ((await ab("totals").textContent()) ?? "").replace(/\s+/g, " ").trim();
      return `${rows} unit rows, ${groups} region groups, ${bars} delta bars, ${nulls} null-z rows; totals: ${totals}`;
    };
    const sessionUnits = (await apiGet(`/catalog/assets/${nwb}/tables/units`).catch(
      () => ({}),
    )) as Row;
    // The step names are `web_smoke.STEPS`'s; each now reads the heatmap page.
    const ensureSession = async (t: Target) => {
      if ((await page.locator('main[data-ready="session"]').count()) === 0) await openSession(t);
    };
    await step("Session page", async () => {
      const { session, waited } = await openSession(targets.nwb);
      if ((await page.getByTestId("session-page").count()) > 0) {
        await expect(ab("baseline-box")).toContainText("−1 to 0 s, z");
        await expect(ab("totals")).toBeVisible();
      }
      return `session ${session ?? "(none)"}, ready after ${waited} s; ${await drawnSession()}`;
    });
    await step("Session Units", async () => {
      await ensureSession(targets.nwb);
      const expected = Math.min(countOf(sessionUnits, "rows"), 152);
      const drawn = await ab("unit-row").count();
      if ((await page.getByTestId("session-page").count()) > 0 && expected > 0) {
        if (drawn !== expected)
          empty(
            `the units table has ${countOf(sessionUnits, "rows")} rows, the heatmap draws ${drawn}`,
          );
        const bars = await ab("delta-bar").count();
        const nulls = await ab("null-z-row").count();
        if (bars + nulls !== drawn)
          empty(`${drawn} unit rows but ${bars} delta bars and ${nulls} null-z rows`);
        const groups = await ab("region-group").count();
        return `${drawn} unit rows in ${groups} region groups, ${bars} delta bars, ${nulls} null-z rows`;
      }
      return await drawnSession();
    });
    await step("Session Tuning", async () => {
      await ensureSession(targets.nwb);
      // No tuning marks on the session page (tuning is deferred); the step checks none show.
      const marks = await page.getByTestId("tuning-table").count();
      if (marks > 0) empty("the session page shows a tuning table; tuning is deferred");
      return "no tuning on the session page";
    });
    await step("Session Events", async () => {
      await ensureSession(targets.nwb);
      const body = await apiGet(`/compute/events?asset_id=${nwb}`);
      const types = ((body.event_types as Row[] | undefined) ?? []).filter(
        (t) => typeof t.n_events === "number" && t.n_events > 0,
      );
      const drawn = await page.locator('[data-testid^="event-"]').count();
      if (drawn !== types.length)
        empty(
          `/compute/events has ${types.length} event types with events, the picker offers ${drawn}`,
        );
      return `${drawn} event types: ${types.map((t) => String(t.name)).join(", ")}`;
    });
    await step("Session Files", async () => {
      await ensureSession(targets.nwb);
      // The heatmap names its NWB file and links its file page (the session's other files are there).
      const link = page.getByTestId("session-page").locator(".sh-head a");
      if ((await link.count()) === 0) return await drawnSession();
      const href = (await link.first().getAttribute("href")) ?? "";
      if (!href.startsWith("/files/")) empty(`the session's file link is "${href}"`);
      return `file link ${href}`;
    });
    await step("Session Results", async () => {
      await ensureSession(targets.nwb);
      // The session page has no Results tab; results stay on /results and each file page.
      const tabs = await page.getByRole("tab", { name: "Results" }).count();
      if (tabs > 0) empty("the session page still shows a Results tab");
      return "no Results on the session page; results are on /results and the file page";
    });
    // A second dataset's session page (dandi-001775: the default defaults block).
    await step("Second dataset session", async () => {
      // The second dandiset; no fallback to another.
      const wanted = "dandi-001775";
      if (!targets.datasets.includes(wanted))
        throw new Error(`${wanted} is absent from the Data list; this step needs it`);
      const found = await apiGet(
        `/catalog/datasets/${encodeURIComponent(wanted)}/assets?format=nwb`,
      );
      const row = ((found.assets as Row[] | undefined) ?? [])[0];
      if (row === undefined || typeof row.subject !== "string")
        throw new Error(`${wanted} has no NWB file with a subject to open a session of`);
      const second: Target = { asset: String(row.asset_id), dataset: wanted, subject: row.subject };
      const { waited } = await openSession(second);
      return `${second.dataset}: ready after ${waited} s; ${await drawnSession()}`;
    });

    // 6. Results, Setup (the steps keep their names)
    await step("My results", async () => {
      await nav("Results").click();
      await expect(page.getByRole("heading", { name: /results?$/ })).toBeVisible();
      await expect(tab(/^Mine \d+$/)).toBeVisible();
      await expect(tab("Everyone's")).toBeVisible();
      // nobody has one yet: the empty-state text, else the dot timeline
      await expect(
        page.getByTestId("results-empty").or(page.getByTestId("results-timeline")).first(),
      ).toBeVisible();
      return "results panel drawn";
    });
    await step("Get started", async () => {
      await nav("Setup").click();
      await expect(page.getByTestId("start-loading")).toHaveCount(0);
      await expect(page.getByTestId("start-install")).toBeVisible();
      return "install command shown";
    });

    // 7. Result lineage: only with `make web-smoke RS_RESULT_FILE=<file>`, the id file that
    // tests/live/test_research_synthesis.py writes ({"x": <result id>, "y": <result id>}; Y's only
    // input is X). Another researcher made both, so they show under All. Locators as above:
    // result-page, result-inputs, result-used-by, file-used-by-show, file-used-by-table.
    const resultFile = process.env.SMOKE_RS_RESULT_FILE;
    if (resultFile !== undefined && resultFile !== "") {
      const ids = JSON.parse(readFileSync(resultFile, "utf8")) as { x: string; y: string };
      await step("Result page", async () => {
        await nav("Results").click();
        await tab("Everyone's").click();
        // the dot in the timeline (the tree may list it too)
        await page
          .locator(`a[href="/results/${encodeURIComponent(ids.y)}"]`)
          .first()
          .click();
        await expect(page.getByTestId("result-page")).toBeVisible();
        await expect(page.getByTestId("result-loading")).toHaveCount(0);
        const inputs = page.getByTestId("result-inputs");
        const link = inputs.locator(`a[href="/results/${encodeURIComponent(ids.x)}"]`);
        await expect(link).toBeVisible();
        await link.click();
        await expect(page.getByRole("heading", { level: 3, name: /^Used by/ })).toBeVisible();
        const usedBy = page.getByTestId("result-used-by");
        await expect(
          usedBy.locator(`a[href="/results/${encodeURIComponent(ids.y)}"]`),
        ).toBeVisible();
        return `Y ${ids.y} lists X ${ids.x} in Inputs; X's Used by lists Y`;
      });
      await step("Result file page", async () => {
        await openFile(targets.nwb);
        await page.getByTestId("file-used-by-show").click();
        const table = page.getByTestId("file-used-by-table");
        await expect(
          table.locator(`a[href="/results/${encodeURIComponent(ids.x)}"]`),
        ).toBeVisible();
        return `the file page lists X ${ids.x}`;
      });
    }

    writeFileSync(
      `${out}/smoke-${vp.name}.wall`,
      String(Math.round((performance.now() - started) / 1000)),
    );
    await context.close();
  });
}
