// The MRI viewer: the NiiVue chunk loads only from the Image tab; the volume at a
// presigned-style URL (with a query string) is requested exactly once and handed to NiiVue as bytes; a click
// on a known voxel reads that voxel's sform world mm (not the qform's); a 4D file is refused before any
// download; a deep link (`mm=`) restores the crosshair. Runs against the sign-in env build (WEB_PORT + 1).
import { readFileSync } from "node:fs";
import { gunzipSync } from "node:zlib";
import { expect, type Page } from "@playwright/test";
import { API, APP, CORS, stubBase, stubEmptyUploads, test } from "./support";

test.skip(
  process.env.E2E_LIVE === "1",
  "needs the fixture-mode auth build (no live server has the auth env)",
);

const NII = readFileSync(new URL("./fixtures/t1-oblique.nii.gz", import.meta.url));
const NII_URL = "https://raw.asteroidea.test/raw/sub-1_T1w.nii.gz?X-Amz-Signature=x";
const ASSET = {
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
const ASSET_4D = {
  asset: {
    ...ASSET.asset,
    asset_id: "asset-4d",
    s3_key: "raw/ds004473/sub-1/func/sub-1_bold.nii.gz",
  },
  headers: [{ asset_id: "asset-4d", nifti_shape: [16, 16, 16, 120], nifti_datatype: "int16" }],
};
const IMAGE = `${APP}files/asset-nii?tab=image`;

/** The fixture's own header: srow_x/y/z (floats at byte 280) and the qform offsets (byte 268). */
function headerAffines(): { sform: number[][]; qOffset: number[] } {
  const raw = gunzipSync(NII);
  const f = (at: number) => raw.readFloatLE(at);
  const sform = [0, 1, 2].map((r) => [0, 1, 2, 3].map((c) => f(280 + 16 * r + 4 * c)));
  return { sform, qOffset: [f(268), f(272), f(276)] };
}

function applySform(sform: number[][], v: number[]): number[] {
  return sform.map(
    (row) =>
      (row[0] ?? 0) * (v[0] ?? 0) +
      (row[1] ?? 0) * (v[1] ?? 0) +
      (row[2] ?? 0) * (v[2] ?? 0) +
      (row[3] ?? 0),
  );
}

function nums(text: string | null): number[] {
  return (text ?? "").split(",").map((t) => Number(t.trim()));
}

function expectClose(got: number[], want: number[]): void {
  for (const i of [0, 1, 2]) {
    expect(Math.abs((got[i] ?? Number.NaN) - (want[i] ?? 0))).toBeLessThanOrEqual(0.01);
  }
}

interface Seen {
  volume: string[];
  downloads: number;
  scripts: string[];
}

async function stub(page: Page): Promise<Seen> {
  const seen: Seen = { volume: [], downloads: 0, scripts: [] };
  await stubBase(page);
  await stubEmptyUploads(page);
  page.on("request", (req) => {
    if (req.url().endsWith(".js")) seen.scripts.push(new URL(req.url()).pathname);
  });
  await page.route(`${API}/catalog/assets/asset-**`, (route) => {
    if (route.request().method() === "OPTIONS")
      return route.fulfill({ status: 204, headers: CORS });
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/download")) seen.downloads += 1;
    const body = path.endsWith("/download")
      ? { url: NII_URL, expires_in: 900, size_bytes: NII.length }
      : path.includes("asset-4d")
        ? ASSET_4D
        : ASSET;
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      headers: CORS,
      body: JSON.stringify(body),
    });
  });
  await page.route("https://raw.asteroidea.test/raw/**", (route) => {
    seen.volume.push(`${route.request().method()} ${route.request().url()}`);
    return route.fulfill({
      status: 200,
      contentType: "application/octet-stream",
      headers: { ...CORS, "access-control-expose-headers": "content-length" },
      body: NII,
    });
  });
  return seen;
}

interface Tile {
  axCorSag: number;
  leftTopMM: number[];
  fovMM: number[];
  leftTopWidthHeight: number[];
  AxyzMxy: number[];
}

/** NiiVue's slice types: 0 axial (screen x,y = world x,y), 1 coronal (x,z), 2 sagittal (y,z). */
const AXIAL = 0;
const CORONAL = 1;
const SAGITTAL = 2;
const SCREEN_AXES: Record<number, [number, number]> = {
  [AXIAL]: [0, 1],
  [CORONAL]: [0, 2],
  [SAGITTAL]: [1, 2],
};

/**
 * Click, with the mouse, the pixel in the `plane` tile where the in-plane world coordinates `uv` fall, and
 * return the 3D world point that pixel stands for: the tile's own plane (`AxyzMxy`, tilted for an oblique
 * volume) fixes the third coordinate.
 */
async function clickWorld(page: Page, uv: [number, number], plane: number): Promise<number[]> {
  const canvas = page.getByTestId("mri-canvas");
  const at = await canvas.evaluate(
    (el, arg) => {
      const nv = (el as unknown as { astMri: { screenSlices: Tile[] } }).astMri;
      const tile = nv.screenSlices.find((t) => t.axCorSag === arg.plane);
      if (tile === undefined) return null;
      const [u, v] = arg.uv;
      const fx = (u - (tile.leftTopMM[0] ?? 0)) / (tile.fovMM[0] ?? 1);
      const fy = (v - (tile.leftTopMM[1] ?? 0)) / (tile.fovMM[1] ?? 1);
      const [l = 0, t = 0, w = 1, h = 1] = tile.leftTopWidthHeight;
      const left = w < 0 ? l + w : l;
      const sx = left + (w < 0 ? 1 - fx : fx) * Math.abs(w);
      const sy = t + (1 - fy) * h;
      const [a0 = 0, a1 = 0, a2 = 0, a3 = 0, a4 = 0] = tile.AxyzMxy;
      const third = a2 + a4 * (v - a1) - a3 * (u - a0);
      const r = el.getBoundingClientRect();
      const c = el as HTMLCanvasElement;
      return {
        x: (sx * r.width) / c.width,
        y: (sy * r.height) / c.height,
        third,
      };
    },
    { uv, plane },
  );
  if (at === null) throw new Error(`no tile for plane ${plane}`);
  await canvas.click({ position: { x: at.x, y: at.y } });
  const [u, v] = uv;
  if (plane === AXIAL) return [u, v, at.third];
  if (plane === CORONAL) return [u, at.third, v];
  return [at.third, u, v];
}

/** The voxel index whose sform world position is nearest `mm` (the sform's 3 x 3 part is inverted). */
function voxelAt(sform: number[][], mm: number[]): number[] {
  const m = sform.map((r) => r.slice(0, 3));
  const d = (mm[0] ?? 0) - (sform[0]?.[3] ?? 0);
  const e = (mm[1] ?? 0) - (sform[1]?.[3] ?? 0);
  const f = (mm[2] ?? 0) - (sform[2]?.[3] ?? 0);
  const [[a = 0, b = 0, c = 0] = [], [g = 0, h = 0, i = 0] = [], [j = 0, k = 0, l = 0] = []] = m;
  const det = a * (h * l - i * k) - b * (g * l - i * j) + c * (g * k - h * j);
  const inv = [
    [(h * l - i * k) / det, (c * k - b * l) / det, (b * i - c * h) / det],
    [(i * j - g * l) / det, (a * l - c * j) / det, (c * g - a * i) / det],
    [(g * k - h * j) / det, (b * j - a * k) / det, (a * h - b * g) / det],
  ];
  return inv.map((row) => Math.round((row[0] ?? 0) * d + (row[1] ?? 0) * e + (row[2] ?? 0) * f));
}

async function readVox(page: Page): Promise<number[]> {
  return nums(await page.getByTestId("mri-vox").textContent());
}

async function readMm(page: Page): Promise<number[]> {
  return nums(await page.getByTestId("mri-mm").textContent());
}

async function openImage(page: Page, url = IMAGE): Promise<void> {
  await page.goto(url);
  await expect(page.getByTestId("mri-mm")).toBeVisible();
}

test("the NiiVue chunk is fetched only when the Image tab opens", async ({ page }) => {
  const seen = await stub(page);
  await page.goto(`${APP}files/asset-nii`);
  await expect(page.getByTestId("file-facts")).toBeVisible();
  expect(seen.scripts.filter((s) => s.includes("viewer-"))).toEqual([]);
  await page.goto(IMAGE);
  await expect(page.getByTestId("mri-canvas")).toBeAttached();
  await expect.poll(() => seen.scripts.filter((s) => s.includes("viewer-")).length).toBe(1);
});

test("the presign request starts before the viewer chunk is requested", async ({ page }) => {
  const order: string[] = [];
  page.on("request", (req) => {
    const path = new URL(req.url()).pathname;
    if (path.endsWith("/download")) order.push("download");
    else if (path.includes("viewer-") && path.endsWith(".js")) order.push("viewer-chunk");
  });
  await stub(page);
  await openImage(page);
  // The page never waits on the chunk to ask for the presigned URL: the request leaves first.
  expect(order.filter((o) => o === "download")).toHaveLength(1);
  expect(order.indexOf("download")).toBeGreaterThanOrEqual(0);
  expect(order.indexOf("download")).toBeLessThan(order.indexOf("viewer-chunk"));
});

test("headless Chromium has WebGL2, a canvas gets a webgl2 context, and no isolation is needed", async ({
  page,
}) => {
  await stub(page);
  await page.goto(`${APP}files/asset-nii`);
  const env = await page.evaluate(() => ({
    hasWebGL2: typeof WebGL2RenderingContext !== "undefined",
    context: document.createElement("canvas").getContext("webgl2") !== null,
    isolated: crossOriginIsolated,
  }));
  expect(env).toEqual({ hasWebGL2: true, context: true, isolated: false });
});

test("the volume at a query-string URL is requested exactly once and loads", async ({ page }) => {
  const seen = await stub(page);
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await openImage(page);
  const info = await page.getByTestId("mri-canvas").evaluate((el) => {
    const nv = (el as unknown as { astMri: { volumes: { dims?: number[] }[] } }).astMri;
    return { n: nv.volumes.length, dims: nv.volumes[0]?.dims };
  });
  expect(info.n).toBe(1);
  expect(info.dims?.slice(1, 4)).toEqual([16, 16, 16]);
  // The app asked for one presigned URL and fetched it once; NiiVue got bytes and made no request.
  expect(seen.downloads).toBe(1);
  expect(seen.volume).toEqual([`GET ${NII_URL}`]);
  expect(errors).toEqual([]);
});

test("clicks in the three planes read the sform world mm, not the qform's", async ({ page }) => {
  await stub(page);
  await openImage(page);
  const { sform, qOffset } = headerAffines();
  // The qform offset differs from the sform's, so a qform reader fails the check below.
  expect(Math.abs((qOffset[0] ?? 0) - (sform[0]?.[3] ?? 0))).toBeGreaterThan(1);
  await expect(page.getByTestId("mri-value")).toHaveText("100");
  // Each click aims at the world position of a known voxel in the plane's two axes; the third axis stays at
  // the crosshair. The voxel expected is the header's own inverse sform at the point actually aimed at.
  const aims: { plane: number; vox: number[] }[] = [
    { plane: AXIAL, vox: [6, 9, 0] },
    { plane: SAGITTAL, vox: [0, 4, 12] },
    { plane: CORONAL, vox: [10, 0, 3] },
  ];
  let last = await readVox(page);
  for (const aim of aims) {
    const target = applySform(sform, aim.vox);
    const [ax = 0, ay = 1] = SCREEN_AXES[aim.plane] ?? [];
    const point = await clickWorld(page, [target[ax] ?? 0, target[ay] ?? 0], aim.plane);
    await expect.poll(() => readVox(page)).not.toEqual(last);
    const got = await readVox(page);
    expect(got).toEqual(voxelAt(sform, point));
    expectClose(await readMm(page), applySform(sform, got));
    last = got;
  }
  // The intensity shown is the volume's own value at that voxel.
  const shown = Number(await page.getByTestId("mri-value").textContent());
  const raw = await page.getByTestId("mri-canvas").evaluate(
    (el, v) =>
      (
        el as unknown as {
          astMri: { volumes: { getValue(i: number, j: number, k: number): number }[] };
        }
      ).astMri.volumes[0]?.getValue(v[0] ?? 0, v[1] ?? 0, v[2] ?? 0),
    last,
  );
  expect(shown).toBe(raw);
});

test("the crosshair is in the URL and a deep link restores it", async ({ page }) => {
  await stub(page);
  await openImage(page);
  const { sform } = headerAffines();
  const aimed = applySform(sform, [5, 7, 8]);
  const point = await clickWorld(page, [aimed[0] ?? 0, aimed[1] ?? 0], AXIAL);
  await expect.poll(() => new URL(page.url()).searchParams.get("mm")).not.toBeNull();
  const url = page.url();
  const clicked = await readVox(page);
  expect(clicked).toEqual(voxelAt(sform, point));
  expectClose(nums(new URL(url).searchParams.get("mm")), applySform(sform, clicked));
  await page.goto("about:blank");
  await openImage(page, url);
  await expect.poll(() => readVox(page)).toEqual(clicked);
  expectClose(await readMm(page), applySform(sform, clicked));
});

test("a deep link to a point inside the fixture's cube reads the cube's intensity", async ({
  page,
}) => {
  await stub(page);
  const mm = applySform(headerAffines().sform, [6, 6, 6]).map((n) => n.toFixed(2));
  await openImage(page, `${IMAGE}&mm=${mm.join(",")}`);
  await expect(page.getByTestId("mri-vox")).toHaveText("6, 6, 6");
  await expect(page.getByTestId("mri-value")).toHaveText("1000");
});

test("arrow keys move the crosshair a voxel and Reset view returns to the centre", async ({
  page,
}) => {
  await stub(page);
  await openImage(page);
  await page.getByTestId("mri-canvas").focus();
  const start = nums(await page.getByTestId("mri-vox").textContent());
  await page.keyboard.press("ArrowRight");
  await expect(page.getByTestId("mri-vox")).toHaveText(
    [(start[0] ?? 0) + 1, start[1], start[2]].join(", "),
  );
  await page.keyboard.press("PageUp");
  await expect(page.getByTestId("mri-vox")).toHaveText(
    [(start[0] ?? 0) + 1, start[1], (start[2] ?? 0) + 1].join(", "),
  );
  await page.getByTestId("mri-reset").click();
  await expect(page.getByTestId("mri-vox")).toHaveText(start.join(", "));
});

test("the layout toggle and the window control change the view and Reset restores it", async ({
  page,
}) => {
  await stub(page);
  await openImage(page);
  const state = () =>
    page.getByTestId("mri-canvas").evaluate((el) => {
      const nv = (
        el as unknown as {
          astMri: {
            opts: { multiplanarLayout?: number };
            volumes: { cal_min?: number; cal_max?: number }[];
          };
        }
      ).astMri;
      return {
        layout: nv.opts.multiplanarLayout,
        min: nv.volumes[0]?.cal_min,
        max: nv.volumes[0]?.cal_max,
      };
    });
  const before = await state();
  await page.getByTestId("mri-layout-row").click();
  expect((await state()).layout).not.toBe(before.layout);
  await page.getByTestId("mri-window").focus();
  await page.keyboard.press("ArrowRight");
  await expect.poll(async () => (await state()).max).not.toBe(before.max);
  await page.getByTestId("mri-reset").click();
  await expect.poll(async () => (await state()).max).toBe(before.max);
  expect((await state()).layout).toBe(before.layout);
});

test("a 4D file is refused with its shape, before any download", async ({ page }) => {
  const seen = await stub(page);
  await page.goto(`${APP}files/asset-4d?tab=image`);
  const note = page.getByTestId("file-image-refused");
  await expect(note).toContainText("16 x 16 x 16 x 120");
  await expect(note).toContainText("Functional runs are not viewable yet");
  expect(seen.downloads).toBe(0);
  expect(seen.volume).toEqual([]);
  expect(seen.scripts.filter((s) => s.includes("viewer-"))).toEqual([]);
});

test("at 375 px wide the Image tab does not scroll the page sideways", async ({ page }) => {
  await stub(page);
  await page.setViewportSize({ width: 375, height: 800 });
  await openImage(page);
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
  expect(overflow).toBeLessThanOrEqual(0);
});

// --- the signals view's anatomy panel ---------------------------------------

/**
 * An NWB file of dandi-000623 CS41. The golden links tie CS41 to openneuro-ds004798 CS41 (pending); the
 * golden studies give that record one non-NWB session with `m41-scans` then `m41-t1`: the panel asks the
 * catalog for each in turn and opens the first NIfTI.
 */
async function stubAnatomy(page: Page): Promise<{ assets: string[] }> {
  const asked = { assets: [] as string[] };
  const reply = (body: unknown) => ({
    status: 200,
    contentType: "application/json",
    headers: CORS,
    body: JSON.stringify(body),
  });
  await page.route(`${API}/catalog/assets/asset-ephys**`, (route) => {
    if (route.request().method() === "OPTIONS")
      return route.fulfill({ status: 204, headers: CORS });
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/tables/series")) {
      return route.fulfill(reply({ rows: [], page: 0, more: false }));
    }
    return route.fulfill(
      reply({
        asset: {
          asset_id: "asset-ephys",
          dataset: "dandi-000623",
          subject: "CS41",
          format: "nwb",
          s3_key: "raw/dandi-000623/sub-CS41/sub-CS41_ecephys.nwb",
          size_bytes: 1000,
        },
        headers: [],
      }),
    );
  });
  await page.route(`${API}/catalog/assets/m41-**`, (route) => {
    if (route.request().method() === "OPTIONS")
      return route.fulfill({ status: 204, headers: CORS });
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/download")) {
      return route.fulfill(reply({ url: NII_URL, expires_in: 900, size_bytes: NII.length }));
    }
    const id = path.split("/").pop() ?? "";
    asked.assets.push(id);
    return route.fulfill(
      reply(
        id === "m41-t1"
          ? { ...ASSET, asset: { ...ASSET.asset, asset_id: id, dataset: "openneuro-ds004798" } }
          : { asset: { asset_id: id, format: "other", s3_key: "raw/x/scans.tsv" }, headers: [] },
      ),
    );
  });
  return asked;
}

test("the anatomy panel finds this person's T1w through the pending link and loads it on demand", async ({
  page,
}) => {
  const seen = await stub(page);
  const asked = await stubAnatomy(page);
  await page.goto(`${APP}files/asset-ephys?tab=lfp`);
  await expect(page.getByTestId("anatomy-source")).toContainText("T1w from openneuro-ds004798");
  await expect(page.getByTestId("anatomy-link-pending")).toHaveText("link pending");
  expect(asked.assets).toEqual(["m41-scans", "m41-t1"]);
  // Nothing of the viewer or the volume until asked.
  await expect(page.getByTestId("anatomy-open")).toBeVisible();
  expect(seen.scripts.filter((s) => s.includes("viewer-"))).toEqual([]);
  expect(seen.volume).toEqual([]);
  await page.getByTestId("anatomy-open").click();
  await expect(page.getByTestId("mri-mm")).toBeVisible();
  expect(seen.volume).toHaveLength(1);
  await expect.poll(() => seen.scripts.filter((s) => s.includes("viewer-")).length).toBe(1);
  await expect(page.getByTestId("mri-layout-grid")).toHaveCount(0);
});
