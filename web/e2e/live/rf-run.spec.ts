// The research-frontend run (`make rf-run` starts it). It drives the
// HOSTED app headless at 1440 x 900 with the page's own controls: clicks, selects, typing and drags. The only
// direct URL use is R0 (opening the app) and R8 (the copied URL in a new tab).
// Managed login is not driven: test tokens for the sandbox are placed in sessionStorage
// before the page loads (in every tab of the context, as managed login would leave a second tab signed in).
// This file only measures. `rf_run.py` judges each row's wall time against the row budgets.
import { mkdirSync, writeFileSync } from "node:fs";
import { expect, type Page, test } from "@playwright/test";
import { kvValue } from "../support";

const env = (name: string): string => {
  const v = process.env[name];
  if (v === undefined || v === "")
    throw new Error(`${name} is not set; start this with make rf-run`);
  return v;
};

const FAST_MS = 30_000; // a missing element fails its row after this, with the locator named
const TOKENS_KEY = "asteroidea.auth.tokens/1"; // web/src/auth/store.ts
const WINDOW_S = 60;
const WINDOW_AT_S = 300;
const DATASET = "dandi-000623";
const PATIENTS = ["CS41", "CS62"]; // the subject ids; the catalog's label form comes from the API
const R2_MARK = "P62CSR2"; // analysis_worker.CS62_R2_MARK: the S3 key of the R2 NWB file carries it
const LEFT = 52; // LfpTab.tsx plot margins
const RIGHT = 12;
const TOP = 6;
const PLOT_H = 150;

interface Row {
  row: string;
  ok: boolean;
  seconds: number;
  detail: string;
  parts: Record<string, number>;
  notReached?: boolean;
}

/** A row that needs something an earlier row failed to produce: reported "not reached", never run. */
class NotReached extends Error {}

test("R0-R8 on the hosted app", async ({ browser }) => {
  const app = env("RF_APP_URL");
  const api = env("RF_API_URL").replace(/\/$/, "");
  const token = env("RF_ACCESS_TOKEN");
  const series = env("RF_SERIES");
  const out = env("RF_OUT");
  mkdirSync(out, { recursive: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
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
        // storage blocked: the run will show the signed-out page
      }
    },
    { key: TOKENS_KEY, access: env("RF_ACCESS_TOKEN"), refresh: env("RF_REFRESH_TOKEN") },
  );
  context.setDefaultTimeout(FAST_MS);
  let page: Page = await context.newPage();
  type Json = Record<string, unknown>;
  const apiGet = async (path: string): Promise<Json> => {
    const reply = await context.request.get(`${api}${path}`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (!reply.ok()) throw new Error(`API GET ${path} answered ${reply.status()}`);
    return (await reply.json()) as Json;
  };
  const list = (body: Json, key: string): Json[] => {
    const rows = body[key];
    return Array.isArray(rows) ? (rows as Json[]) : [];
  };
  // The labels, the R2 file and the first unit all come from the API, never from a literal form.
  const labels = list(await apiGet(`/catalog/datasets/${DATASET}`), "subjects").map((s) =>
    String(s.subject),
  );
  const labelOf = (id: string): string => {
    const hit = labels.find((l) => l === id || l.replace(/^sub-/, "") === id);
    if (hit === undefined) throw new Error(`the API lists no subject ${id}: ${labels.join(", ")}`);
    return hit;
  };
  const cs62 = labelOf("CS62");
  const nwbFiles = list(
    await apiGet(`/catalog/datasets/${DATASET}/assets?subject=${encodeURIComponent(cs62)}`),
    "assets",
  ).filter((a) => a.format === "nwb");
  let asset = "";
  for (const a of nwbFiles) {
    const detail = await apiGet(`/catalog/assets/${String(a.asset_id)}`);
    const row = (detail.asset ?? {}) as Json;
    if (String(row.s3_key ?? "").includes(R2_MARK)) asset = String(a.asset_id);
  }
  if (asset === "") throw new Error(`${cs62} has no NWB file whose key carries ${R2_MARK}`);
  if (asset !== process.env.RF_ASSET)
    throw new Error(`the API locates ${asset}; the runner warmed ${process.env.RF_ASSET ?? ""}`);
  const unitRows = list(await apiGet(`/catalog/assets/${asset}/tables/units`), "rows");
  const electrodeRows = list(await apiGet(`/catalog/assets/${asset}/tables/electrodes`), "rows");
  if (unitRows.length !== 44 || electrodeRows.length !== 96)
    throw new Error(
      `API: ${electrodeRows.length} electrodes, ${unitRows.length} units; plan: 96, 44`,
    );
  const firstUnit = Math.min(...unitRows.map((u) => Number(u.unit_index)));
  const rows: Row[] = [];
  const now = () => performance.now() / 1000;

  // A failing row is recorded and the next row starts from its own start state, reached by the
  // page's own links. The one deep link is `page.goto(app)` when the app shell is not on screen at all;
  // a row that used it says so in its detail.
  let restored = false;
  const catalogLink = () => page.getByRole("link", { name: "Catalog", exact: true }).first();
  const datasetLink = () => page.getByRole("link", { name: DATASET });
  async function atShell(): Promise<void> {
    if (await catalogLink().isVisible()) return;
    restored = true;
    await page.goto(app);
    await expect(catalogLink()).toBeVisible();
  }
  async function atCatalog(): Promise<void> {
    await atShell();
    if (!(await datasetLink().isVisible())) await catalogLink().click();
  }
  /** The CS62 R2 file page with its header, by the page's own links unless it is already there. */
  async function atFile(): Promise<void> {
    if (await page.getByTestId("file-overview").isVisible()) return;
    await atShell();
    await catalogLink().click();
    await datasetLink().click();
    await tab(page, /Subjects/).click();
    await page.getByRole("link", { name: cs62, exact: true }).click();
    await tab(page, "Files").click();
    await page.locator(`a[href$="/files/${asset}"]`).click();
    await expect(page.getByTestId("file-overview")).toBeVisible();
  }
  /** The dataset page (overview tab), by the page's own links unless it is already there. */
  async function atDataset(): Promise<void> {
    if (await page.getByTestId("dataset-subjects").isVisible()) return;
    await atCatalog();
    await datasetLink().click();
    await expect(page.getByTestId("dataset-subjects")).toBeVisible();
  }
  /** The Units tab with the unit and the windows R4 sets, as R5 needs them. */
  async function atUnits(): Promise<void> {
    const align = page.getByTestId("units-align");
    if ((await align.isVisible()) && (await page.getByTestId(`curve-${firstUnit}`).count()) > 0)
      return;
    await atFile();
    await tab(page, "Units").click();
    await page.getByLabel(new RegExp(`^Unit ${firstUnit} ·`)).check();
    await page.getByTestId("units-pre").fill("2");
    await page.getByTestId("units-post").fill("2");
    await page.getByTestId("units-bin").fill("0.05");
    await page.getByRole("button", { name: "Update" }).click();
    await expect(page.getByTestId(`curve-${firstUnit}`)).toBeAttached();
  }

  async function run(
    id: string,
    body: (
      part: (name: string, since: number) => void,
      set: (name: string, value: number) => void,
    ) => Promise<string>,
  ): Promise<void> {
    const parts: Record<string, number> = {};
    const started = now();
    let ok = true;
    let notReached = false;
    let detail = "";
    restored = false;
    try {
      detail = await body(
        (name, since) => {
          parts[name] = Math.round((now() - since) * 1000) / 1000;
        },
        (name, value) => {
          parts[name] = value;
        },
      );
    } catch (err) {
      ok = false;
      notReached = err instanceof NotReached;
      detail = String(err).split("\n")[0] ?? "failed";
    }
    if (restored) detail += " (start state restored by deep link)";
    const seconds = Math.round((now() - started) * 1000) / 1000;
    await page.screenshot({ path: `${out}/${id}.png` }).catch(() => undefined);
    rows.push({ row: id, ok, seconds, detail, parts, ...(notReached ? { notReached } : {}) });
    writeFileSync(`${out}/rows.json`, JSON.stringify(rows, null, 2));
  }

  const tab = (p: Page, name: string | RegExp) => p.getByRole("tab", { name });

  await run("R0", async () => {
    await page.goto(app);
    await expect(page.getByRole("link", { name: "Catalog", exact: true }).first()).toBeVisible();
    return "the app shell rendered with the tokens in sessionStorage";
  });

  await run("R1", async () => {
    await atShell();
    await catalogLink().click();
    await expect(page.getByRole("link", { name: "dandi-000623" })).toBeVisible();
    return "datasets listed, dandi-000623 among them";
  });

  await run("R2", async () => {
    await atCatalog();
    await datasetLink().click();
    const subjects = page.getByTestId("dataset-subjects");
    for (const id of PATIENTS) {
      await expect(subjects.getByRole("link", { name: labelOf(id), exact: true })).toBeVisible();
    }
    return `subjects ${PATIENTS.map(labelOf).join(" and ")} listed`;
  });

  await run("R3", async (part) => {
    await atDataset();
    let t = now();
    await tab(page, /Subjects/).click();
    await page.getByRole("link", { name: cs62, exact: true }).click();
    await expect(page.getByTestId("subject-sessions")).toBeVisible();
    part("subject", t);
    t = now();
    await tab(page, "Files").click();
    const link = page.locator(`a[href$="/files/${asset}"]`);
    await expect(link).toBeVisible();
    part("files", t);
    t = now();
    await link.click();
    const overview = page.getByTestId("file-overview");
    await expect(overview).toBeVisible();
    part("file", t);
    // Each value is read from its own term / definition pair, never from the joined text.
    for (const [label, want] of [
      ["n_electrodes", "96"],
      ["n_units", "44"],
      ["n_trials", "41"],
    ] as const) {
      const got = await kvValue(overview, label);
      if (got !== want) throw new Error(`the header shows ${label} = "${got}", not ${want}`);
    }
    return "header shows 96 electrodes, 44 units, 41 trials";
  });

  const rates = () => page.waitForResponse((r) => r.url().includes("/compute/rates"));

  await run("R4", async (part, set) => {
    await atFile();
    await tab(page, "Units").click();
    await page.getByLabel(new RegExp(`^Unit ${firstUnit} ·`)).check();
    await page.getByTestId("units-pre").fill("2");
    await page.getByTestId("units-post").fill("2");
    await page.getByTestId("units-bin").fill("0.05");
    await page.getByRole("button", { name: "Update" }).click();
    await page.getByTestId("units-z").check();
    const t = now();
    const reply = rates();
    await page.getByTestId("units-align").selectOption("intervals:trials");
    const bytes = (await (await reply).body()).length;
    await expect(page.getByTestId(`curve-${firstUnit}`)).toBeAttached();
    await expect(page.getByTestId("rates-n-trials")).toHaveText("41 trials");
    await expect(page.getByTestId("rates-source")).toHaveText("source: catalog");
    await expect(page.getByTestId(`raster-${firstUnit}`)).toBeAttached();
    part("row", t);
    set("body", bytes);
    return `41 trials, source catalog; response ${bytes} bytes`;
  });

  await run("R5", async (part) => {
    await atUnits();
    const cut = page.getByTestId("units-align").locator("option", { hasText: /^Movie scene cuts/ });
    const value = await cut.first().getAttribute("value");
    if (value === null) throw new Error("the align picker offers no Movie scene cuts");
    const t = now();
    const reply = rates();
    await page.getByTestId("units-align").selectOption(value);
    const body = await (await reply).body();
    await expect(page.getByTestId("rates-n-trials")).toHaveText("93 trials");
    await expect(page.getByTestId(`curve-${firstUnit}`)).toBeAttached();
    part("row", t);
    writeFileSync(`${out}/R5-rates.json`, body);
    return `93 trials for ${value}`;
  });

  await run("R6", async () => {
    await atFile();
    await tab(page, "Events").click();
    await expect(page.getByTestId("lane-trials").locator("rect.bar")).toHaveCount(41);
    return "41 trials on the timeline";
  });

  let lfpUrl = "";
  await run("R7", async (part) => {
    const spectrogram = () => page.waitForResponse((r) => r.url().includes("/compute/spectrogram"));
    await atFile();
    let t = now();
    await tab(page, /^LFP/i).click();
    const picker = page.getByTestId("lfp-series");
    await expect(picker).toBeVisible();
    if ((await picker.inputValue()) !== series) await picker.selectOption(series);
    const area = page.getByTestId("lfp-overview-area");
    await expect(page.getByTestId("lfp-overview")).toBeVisible();
    await expect(page.getByTestId("lfp-loading")).toHaveCount(0);
    part("overview", t);
    const total = Number(await area.getAttribute("aria-valuemax"));
    const box = await area.boundingBox();
    if (box === null || !(total > WINDOW_S))
      throw new Error(`no overview box or span (${total} s)`);
    const at = (s: number) => box.x + LEFT + (s / total) * (box.width - LEFT - RIGHT);
    const y = box.y + TOP + PLOT_H / 2;
    const start = Math.min(WINDOW_AT_S, total - WINDOW_S);
    t = now();
    const first = spectrogram();
    await page.mouse.move(at(start), y);
    await page.mouse.down();
    await page.mouse.move(at(start + WINDOW_S / 2), y, { steps: 6 });
    await page.mouse.move(at(start + WINDOW_S), y, { steps: 6 });
    await page.mouse.up();
    const reply = await first;
    if (!reply.ok()) throw new Error(`spectrogram answered ${reply.status()}`);
    await expect(page.getByTestId("lfp-window-result")).toBeVisible();
    await expect(page.getByTestId("lfp-spectrogram")).toBeVisible();
    await expect(page.getByTestId("lfp-trace")).toBeVisible();
    part("window", t);
    t = now();
    const second = spectrogram();
    await page.getByRole("button", { name: "Pan right" }).click();
    const panned = await second;
    if (!panned.ok()) throw new Error(`spectrogram (pan) answered ${panned.status()}`);
    await expect(page.getByTestId("lfp-window-result")).toBeVisible();
    part("pan", t);
    lfpUrl = page.url();
    return `${total.toFixed(0)} s recording; window near t=${start.toFixed(0)} s; panned once`;
  });

  await run("R8", async () => {
    if (lfpUrl === "") throw new NotReached("R7 did not produce the URL R8 copies");
    const copy = new URL(lfpUrl);
    page = await context.newPage();
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto(copy.toString());
    await expect(page.getByTestId("lfp-window-result")).toBeVisible();
    await expect(tab(page, /^LFP/i)).toHaveAttribute("aria-selected", "true");
    const now2 = new URL(page.url());
    for (const key of ["tab", "series", "channel", "start", "dur"]) {
      if (now2.searchParams.get(key) !== copy.searchParams.get(key)) {
        throw new Error(
          `${key} differs: ${now2.searchParams.get(key)} vs ${copy.searchParams.get(key)}`,
        );
      }
    }
    return "same tab, series, channel and window in the new tab";
  });
});
