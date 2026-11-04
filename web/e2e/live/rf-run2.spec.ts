// The research-frontend pass 2 (`make rf-run PASS=2` starts it). It drives the
// HOSTED app headless at 1440 x 900 with the page's own controls: clicks, typing and a drag. The only direct
// URL use is opening the app at the start of a row.
// Managed login is not driven: test tokens for two researchers are placed in
// sessionStorage before the page loads (one browser context each; the second is R11's researcher).
// Every name and id (datasets, subject labels, the T1w files, the first unit) comes from the API, never from a
// literal form. This file only measures; `rf_run.py` judges each row against the row budgets.
// R13 (the SDK in a fresh venv) is not here: `rf_run.py` runs it after this file and deletes the label.
import { mkdirSync, writeFileSync } from "node:fs";
import { type Browser, type BrowserContext, expect, type Page, test } from "@playwright/test";

const env = (name: string): string => {
  const v = process.env[name];
  if (v === undefined || v === "")
    throw new Error(`${name} is not set; start this with make rf-run PASS=2`);
  return v;
};

const FAST_MS = 30_000; // a missing element fails its row after this, with the locator named
const TOKENS_KEY = "asteroidea.auth.tokens/1"; // web/src/auth/store.ts
const DATASET = "dandi-000623";
const R2_MARK = "P62CSR2"; // analysis_worker.CS62_R2_MARK: the S3 key of the R2 NWB file carries it
const T1W = /(^|\/)anat\/[^/]*_T1w\.nii(\.gz)?$/; // the BIDS path of an anatomical T1w
const MAX_PAGES = 10;
const SETTLE_MS = 500; // after the readout shows: a second volume request would land in this time
const GUTTER = 120; // Timeline.tsx: the plot's left gutter on a wide page
const RIGHT_PAD = 20;
const LANE_H = 28; // Timeline.tsx
const FORBIDDEN = 403;

interface Row {
  row: string;
  ok: boolean;
  seconds: number;
  detail: string;
  parts: Record<string, number>;
  skipped?: boolean;
  notReached?: boolean;
}

/** A row that needs something an earlier row failed to produce: reported "not reached", never run. */
class NotReached extends Error {}

type Json = Record<string, unknown>;
type Part = (name: string, since: number) => void;
type Setter = (name: string, value: number) => void;

interface Image {
  dataset: string;
  subject: string;
  asset: string;
  basename: string;
}

test("R9-R12 on the hosted app", async ({ browser }) => {
  const app = env("RF_APP_URL");
  const api = env("RF_API_URL").replace(/\/$/, "");
  const token = env("RF_ACCESS_TOKEN");
  const token2 = env("RF_ACCESS_TOKEN_2");
  const asset = env("RF_ASSET");
  const name = env("RF_LABEL");
  const out = env("RF_OUT");
  mkdirSync(out, { recursive: true });

  const signedIn = async (b: Browser, access: string, refresh: string): Promise<BrowserContext> => {
    const made = await b.newContext({ viewport: { width: 1440, height: 900 } });
    await made.addInitScript(
      ({ key, a, r }) => {
        try {
          if (window.sessionStorage.getItem(key) === null) {
            window.sessionStorage.setItem(
              key,
              JSON.stringify({ access_token: a, refresh_token: r }),
            );
          }
        } catch {
          // storage blocked: the run will show the signed-out page
        }
      },
      { key: TOKENS_KEY, a: access, r: refresh },
    );
    made.setDefaultTimeout(FAST_MS);
    return made;
  };
  const first = await signedIn(browser, token, env("RF_REFRESH_TOKEN"));
  const second = await signedIn(browser, token2, env("RF_REFRESH_TOKEN_2"));
  const page1 = await first.newPage();
  const page2 = await second.newPage();
  let shown: Page = page1;

  const apiGet = async (path: string): Promise<Json> => {
    const reply = await first.request.get(`${api}${path}`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (!reply.ok()) throw new Error(`API GET ${path} answered ${reply.status()}`);
    return (await reply.json()) as Json;
  };
  const list = (body: Json, key: string): Json[] => {
    const found = body[key];
    return Array.isArray(found) ? (found as Json[]) : [];
  };
  const rows: Row[] = [];
  const now = () => performance.now() / 1000;
  const save = () => writeFileSync(`${out}/rows.json`, JSON.stringify(rows, null, 2));

  async function run(
    id: string,
    body: (part: Part, set: Setter) => Promise<string>,
    page: Page,
  ): Promise<void> {
    shown = page;
    const parts: Record<string, number> = {};
    const started = now();
    let ok = true;
    let notReached = false;
    let detail = "";
    try {
      detail = await body(
        (n, since) => {
          parts[n] = Math.round((now() - since) * 1000) / 1000;
        },
        (n, value) => {
          parts[n] = value;
        },
      );
    } catch (err) {
      ok = false;
      notReached = err instanceof NotReached;
      detail = String(err).split("\n")[0] ?? "failed";
    }
    const seconds = Math.round((now() - started) * 1000) / 1000;
    await shown.screenshot({ path: `${out}/${id}.png` }).catch(() => undefined);
    // A failing row is recorded and the next row starts from its own start state.
    rows.push({ row: id, ok, seconds, detail, parts, ...(notReached ? { notReached } : {}) });
    save();
  }
  const skip = (id: string, detail: string): void => {
    rows.push({ row: id, ok: true, seconds: 0, detail, parts: {}, skipped: true });
    save();
  };

  const tab = (p: Page, n: string | RegExp) => p.getByRole("tab", { name: n });

  /** Catalog, the dataset, the Subjects tab, the subject, the Files tab, the file: the page's own links. */
  async function openFile(p: Page, dataset: string, subject: string, id: string): Promise<void> {
    await p.goto(app);
    await p.getByRole("link", { name: "Catalog", exact: true }).first().click();
    await p.getByRole("link", { name: dataset, exact: true }).click();
    await tab(p, /Subjects/).click();
    await p.getByRole("link", { name: subject, exact: true }).click();
    await tab(p, "Files").click();
    await p.locator(`a[href$="/files/${id}"]`).click();
    await expect(p.getByTestId("file-page")).toBeVisible();
  }

  // --- what the API holds ---------------------------------------------------------------
  const datasets = list(await apiGet("/catalog/datasets"), "datasets").map((d) =>
    String(d.dataset),
  );
  const subjectLabels = async (dataset: string): Promise<string[]> =>
    list(await apiGet(`/catalog/datasets/${dataset}`), "subjects").map((s) => String(s.subject));
  const labelIn = async (dataset: string, id: string): Promise<string | null> =>
    (await subjectLabels(dataset)).find((l) => l === id || l.replace(/^sub-/, "") === id) ?? null;

  /** The first 3D anatomical T1w of the subject, or null when the dataset, subject or file is absent. */
  const findT1w = async (suffix: string, id: string): Promise<Image | null> => {
    const dataset = datasets.find((d) => d === suffix || d.endsWith(`-${suffix}`));
    if (dataset === undefined) return null;
    const subject = await labelIn(dataset, id);
    if (subject === null) return null;
    for (let n = 0; n < MAX_PAGES; n += 1) {
      const found = await apiGet(
        `/catalog/datasets/${dataset}/assets?subject=${encodeURIComponent(subject)}&format=nifti&page=${n}`,
      );
      for (const a of list(found, "assets")) {
        const path = String(a.path ?? "");
        if (!T1W.test(path)) continue;
        const detail = await apiGet(`/catalog/assets/${String(a.asset_id)}`);
        const shape = list(detail, "headers")[0]?.nifti_shape;
        if (Array.isArray(shape) && shape.length === 3) {
          return {
            dataset,
            subject,
            asset: String(a.asset_id),
            basename: path.split("/").pop() ?? path,
          };
        }
      }
      if (found.more !== true) break;
    }
    return null;
  };

  /** The Image tab of `target`: three planes, a crosshair readout, the volume URL fetched once. */
  async function imageTab(target: Image, part: Part): Promise<string> {
    const p = page1;
    const volume: string[] = [];
    const appHost = new URL(app).host;
    const watch = (req: { url(): string; method(): string }) => {
      const u = new URL(req.url());
      if (u.host !== appHost && u.pathname.endsWith(target.basename)) volume.push(req.method());
    };
    p.on("request", watch);
    try {
      await openFile(p, target.dataset, target.subject, target.asset);
      let t = now();
      const download = p.waitForResponse((r) =>
        r.url().includes(`/catalog/assets/${target.asset}/download`),
      );
      await tab(p, "Image").click();
      const reply = await download;
      if (!reply.ok()) throw new Error(`the presign route answered ${reply.status()}`);
      part("presign", t);
      const size = Number(((await reply.json()) as Json).size_bytes);
      t = now();
      await expect(p.getByTestId("mri-mm")).toBeVisible();
      part("volume", t);
      await p.waitForTimeout(SETTLE_MS);
      await expect
        .poll(async () =>
          p.getByTestId("mri-canvas").evaluate((el) => {
            const nv = (el as unknown as { astMri?: { screenSlices?: { axCorSag: number }[] } })
              .astMri;
            return [...new Set((nv?.screenSlices ?? []).map((s) => s.axCorSag))]
              .filter((k) => k <= 2)
              .sort();
          }),
        )
        .toEqual([0, 1, 2]);
      const mm = ((await p.getByTestId("mri-mm").textContent()) ?? "")
        .split(",")
        .map((v) => Number(v.trim()));
      if (mm.length !== 3 || mm.some((v) => !Number.isFinite(v)))
        throw new Error(`the crosshair readout is not three numbers in mm: ${mm.join(",")}`);
      const gets = volume.filter((m) => m === "GET").length;
      if (gets !== 1)
        throw new Error(`the volume ${target.basename} was requested ${gets} times, not 1`);
      return `${target.dataset} ${target.subject}: 3 planes, crosshair ${mm.join(", ")} mm, volume fetched once (${size} B)`;
    } finally {
      p.off("request", watch);
    }
  }

  // --- the CS62 R2 NWB file ---------------------------------------------------------------
  const cs62 = await labelIn(DATASET, "CS62");
  if (cs62 === null) throw new Error(`the API lists no subject CS62 in ${DATASET}`);
  const detail = (await apiGet(`/catalog/assets/${asset}`)).asset as Json;
  if (!String(detail.s3_key ?? "").includes(R2_MARK))
    throw new Error(`asset ${asset} is not the R2 file: its key lacks ${R2_MARK}`);
  const unitRows = list(await apiGet(`/catalog/assets/${asset}/tables/units`), "rows");
  const firstUnit = Math.min(...unitRows.map((u) => Number(u.unit_index)));

  // --- R9 -----------------------------------------------------------------------------------
  const brain = await findT1w("ds004473", "1");
  if (brain === null)
    throw new Error(`no 3D anatomical T1w for sub 1 of ds004473 among: ${datasets.join(", ")}`);
  await run("R9", (part) => imageTab(brain, part), page1);

  const other = await findT1w("ds004798", "p62cs");
  if (other === null) {
    skip("R9b", "no anatomical T1w for sub p62cs of ds004798 in the catalog");
  } else {
    await run("R9b", (part) => imageTab(other, part), page1);
  }

  // --- R10: the author drags a label, saves it, reloads ------------------------------------
  let labelId = "";
  let span = { start: 0, stop: 0 };
  await run(
    "R10",
    async (part) => {
      const p = page1;
      await openFile(p, DATASET, cs62, asset);
      await tab(p, "Events").click();
      await expect(p.getByTestId("lane-labels")).toBeAttached();
      const svg = p.locator(".timeline-box svg");
      const box = await svg.boundingBox();
      if (box === null) throw new Error("no timeline box");
      const lanes = await p
        .locator('.timeline-box g[data-testid^="lane-"]:not([data-testid="lane-labels"])')
        .count();
      const width = box.width - GUTTER - RIGHT_PAD;
      const y = box.y + lanes * LANE_H + LANE_H / 2;
      await p.mouse.move(box.x + GUTTER + 0.3 * width, y);
      await p.mouse.down();
      await p.mouse.move(box.x + GUTTER + 0.35 * width, y, { steps: 4 });
      await p.mouse.move(box.x + GUTTER + 0.4 * width, y, { steps: 4 });
      await p.mouse.up();
      await expect(p.getByTestId("label-editor")).toContainText("New label");
      span = {
        start: Number(await p.getByTestId("label-start").inputValue()),
        stop: Number(await p.getByTestId("label-stop").inputValue()),
      };
      if (!(span.stop > span.start)) throw new Error(`the drag gave ${span.start} to ${span.stop}`);
      await p.getByTestId("label-name").fill(name);
      const t = now();
      const post = p.waitForResponse(
        (r) => r.request().method() === "POST" && r.url().endsWith(`/files/${asset}/labels`),
      );
      await p.getByRole("button", { name: "Create label" }).click();
      const reply = await post;
      part("post", t);
      if (reply.status() !== 201) throw new Error(`POST answered ${reply.status()}`);
      labelId = String(((await reply.json()) as Json).label_id);
      await expect(p.getByTestId("label-list")).toContainText(name);
      await p.reload();
      await expect(p.getByTestId(`label-item-${labelId}`)).toContainText(name);
      await expect(p.getByTestId(`label-item-${labelId}`)).toContainText("you");
      await expect(p.getByTestId(`label-bar-${labelId}`)).toBeVisible();
      return `label ${name} ${span.start} to ${span.stop} s created, persisted after a reload, marked yours`;
    },
    page1,
  );

  // --- R11: a second researcher sees it and may not edit it --------------------------------
  await run(
    "R11",
    async (part) => {
      if (labelId === "") throw new NotReached("R10 created no label for R11 to read");
      const p = page2;
      await openFile(p, DATASET, cs62, asset);
      await tab(p, "Events").click();
      const item = p.getByTestId(`label-item-${labelId}`);
      await expect(item).toContainText(name);
      await expect(item).toContainText("another researcher");
      await item.click();
      await expect(p.getByTestId("label-readonly")).toBeVisible();
      await expect(p.getByTestId("label-readonly-reason")).toHaveText(/only the author/i);
      await expect(p.getByTestId("label-editor")).toHaveCount(0);
      await expect(p.getByRole("button", { name: "Save changes" })).toHaveCount(0);
      await expect(p.getByRole("button", { name: "Delete" })).toHaveCount(0);
      const t = now();
      const refused = await second.request.patch(`${api}/files/${asset}/labels/${labelId}`, {
        headers: { Authorization: `Bearer ${token2}` },
        data: {
          name: `${name}-edited`,
          note: "",
          start_s: span.start,
          stop_s: span.stop,
          version: 1,
        },
      });
      part("patch", t);
      const text = await refused.text();
      if (refused.status() !== FORBIDDEN || !/only the author/i.test(text))
        throw new Error(`the second researcher's PATCH answered ${refused.status()}: ${text}`);
      const stored = list(await apiGet(`/files/${asset}/labels`), "labels").find(
        (l) => l.label_id === labelId,
      );
      if (stored?.name !== name)
        throw new Error(`the refused edit changed the label: ${String(stored?.name)}`);
      return `sees ${name} as another researcher's; read-only view and the PATCH both refuse: ${text.trim()}`;
    },
    page2,
  );

  // --- R12: the author aligns the Units tab to the label name -------------------------------
  await run(
    "R12",
    async (part, set) => {
      if (labelId === "") throw new NotReached("R10 created no label to align to");
      const p = page1;
      // R10 may have failed after creating the label, away from the file page: its own links lead back.
      if (!(await p.getByTestId("file-page").isVisible())) await openFile(p, DATASET, cs62, asset);
      await tab(p, "Units").click();
      await p.getByLabel(new RegExp(`^Unit ${firstUnit} ·`)).check();
      await p.getByTestId("units-pre").fill("2");
      await p.getByTestId("units-post").fill("2");
      await p.getByTestId("units-bin").fill("0.05");
      await p.getByRole("button", { name: "Update" }).click();
      const align = p.getByTestId("units-align");
      const option = align.locator("option", { hasText: `All labels named ${name}` });
      await expect(option).toHaveCount(1);
      const t = now();
      const reply = p.waitForResponse((r) => r.url().includes("/compute/rates"));
      await align.selectOption({ label: `All labels named ${name}` });
      const got = await reply;
      if (!got.ok()) throw new Error(`rates answered ${got.status()}`);
      const sent = new URL(got.url()).searchParams.get("align");
      if (sent !== `label_name:${name}`) throw new Error(`the page sent align=${String(sent)}`);
      const bytes = (await got.body()).length;
      await expect(p.getByTestId("rates-n-trials")).toHaveText(/^1 trials?$/);
      await expect(p.getByTestId(`curve-${firstUnit}`)).toBeAttached();
      part("row", t);
      set("body", bytes);
      return `align label_name:${name}: 1 trial for unit ${firstUnit}; response ${bytes} bytes`;
    },
    page1,
  );

  await first.close();
  await second.close();
});
