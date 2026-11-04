// The signals view on the file page's LFP tab (it replaced the earlier LFP tab):
// every channel's band power over the whole recording from the presigned binary, grouped by region; a
// drag that asks for a window; the window spectrogram averaged into the overview's log bands in the
// browser and the trace; wheel zoom, drag pan and the arrow keys; the deep link; the not-built answers
// and the refusals. Runs against the sign-in env build (port WEB_PORT + 1).
import { readFileSync } from "node:fs";
import { expect, type Page } from "@playwright/test";
import { parseSpectrogram } from "../src/lfp/lfp";
import { parseOverviewBinary } from "../src/lfp/overview";
import { bandIndices, bandPowerDb, logBinned } from "../src/signals/model";
import { API, APP, CORS, fixture, stubBase, stubEmptyUploads, test } from "./support";

test.skip(
  process.env.E2E_LIVE === "1",
  "needs the fixture-mode auth build (no live server has the auth env)",
);

const OVERVIEW_URL = "https://raw.asteroidea.test/derived/overview.bin?X-Amz-Signature=e2e";
const NOT_BUILT = "overview not built for this file version";
const OVERVIEW_BIN = readFileSync(new URL("./fixtures/overview.bin", import.meta.url));
const EXPECTED = JSON.parse(fixture("overview-expected")) as { db: number[][][]; edges: number[] };
const SPEC = JSON.parse(fixture("spectrogram")) as {
  power_db: number[][];
  trace: { values: number[] };
};
const OV = parseOverviewBinary(
  OVERVIEW_BIN.buffer.slice(OVERVIEW_BIN.byteOffset, OVERVIEW_BIN.byteOffset + OVERVIEW_BIN.length),
);
/** The overview rows the view should draw for band [lo, hi]: channel 0 then 1 (two regions). */
function bandRows(lo: number, hi: number): number[][] {
  const v = bandPowerDb(OV, bandIndices(OV.edges, lo, hi));
  return [0, 1].map((c) => Array.from(v.subarray(c * OV.nTimes, (c + 1) * OV.nTimes)));
}
const BINNED = logBinned(parseSpectrogram(SPEC), OV.edges);

/** Every drawn value within 1e-4 dB of the wanted one (f32 on the canvas, f64 here). */
function expectRows(got: readonly number[][], want: readonly number[][]): void {
  expect(got.map((r) => r.length)).toEqual(want.map((r) => r.length));
  for (const [i, row] of got.entries()) {
    for (const [t, v] of row.entries()) expect(v).toBeCloseTo(want[i]?.[t] ?? Number.NaN, 4);
  }
}

interface Reply {
  status: number;
  body: string;
}

interface Stub {
  overview: Reply;
  spectrogram: Reply;
  overviewSeen: URL[];
  spectrogramSeen: URL[];
  downloads: { authorization: string | undefined }[];
  /** The electrodes table body; null serves the fixture's. */
  electrodes: unknown;
}

const OK_OVERVIEW: Reply = {
  status: 200,
  body: JSON.stringify({
    url: OVERVIEW_URL,
    expires_in: 900,
    format: "ASTLFPOV v1",
    bytes: OVERVIEW_BIN.length,
    shape: [2, 20, 8],
    rate_hz: 1000,
    starting_time_s: 12.5,
  }),
};

async function stub(page: Page): Promise<Stub> {
  await stubBase(page);
  await stubEmptyUploads(page);
  const state: Stub = {
    overview: OK_OVERVIEW,
    spectrogram: { status: 200, body: fixture("spectrogram") },
    overviewSeen: [],
    spectrogramSeen: [],
    downloads: [],
    electrodes: null,
  };
  await page.route(`${API}/catalog/**`, (route) => {
    const req = route.request();
    if (req.method() === "OPTIONS") return route.fulfill({ status: 204, headers: CORS });
    const url = new URL(req.url());
    // The anatomy panel's studies and links: the defaults stubBase serves.
    if (url.pathname === "/catalog/studies" || url.pathname === "/catalog/links") {
      return route.fallback();
    }
    let body: string | null = null;
    if (url.pathname === "/catalog/assets/asset-0101") body = fixture("catalog-file-nwb");
    else if (url.pathname.startsWith("/catalog/assets/asset-0101/tables/")) {
      const table = url.pathname.split("/").pop() ?? "";
      const tables = JSON.parse(fixture("catalog-file-tables")) as Record<string, unknown>;
      if (table === "electrodes" && state.electrodes !== null)
        body = JSON.stringify(state.electrodes);
      else if (table in tables) body = JSON.stringify(tables[table]);
    }
    return route.fulfill({
      status: body === null ? 404 : 200,
      contentType: "application/json",
      headers: CORS,
      body: body ?? '{"error":"no route"}',
    });
  });
  await page.route(`${API}/compute/overview*`, (route) => {
    if (route.request().method() === "OPTIONS")
      return route.fulfill({ status: 204, headers: CORS });
    state.overviewSeen.push(new URL(route.request().url()));
    return route.fulfill({
      status: state.overview.status,
      contentType: "application/json",
      headers: CORS,
      body: state.overview.body,
    });
  });
  await page.route(`${API}/compute/spectrogram*`, (route) => {
    if (route.request().method() === "OPTIONS")
      return route.fulfill({ status: 204, headers: CORS });
    state.spectrogramSeen.push(new URL(route.request().url()));
    return route.fulfill({
      status: state.spectrogram.status,
      contentType: "application/json",
      headers: CORS,
      body: state.spectrogram.body,
    });
  });
  await page.route("https://raw.asteroidea.test/derived/overview.bin*", (route) => {
    state.downloads.push({ authorization: route.request().headers().authorization });
    return route.fulfill({
      status: 200,
      contentType: "application/octet-stream",
      headers: CORS,
      body: OVERVIEW_BIN,
    });
  });
  return state;
}

/** The numbers a canvas drew, from its `astData` hook. */
async function drawn(page: Page, testId: string): Promise<unknown> {
  return page.getByTestId(testId).evaluate((el) => (el as unknown as { astData: unknown }).astData);
}

/** The alpha of the canvas pixel at fraction (fx, fy) of its size. */
async function alphaAt(page: Page, testId: string, fx: number, fy: number): Promise<number> {
  return page.getByTestId(testId).evaluate(
    (el, [x, y]) => {
      const canvas = el as HTMLCanvasElement;
      const data = canvas
        .getContext("2d")
        ?.getImageData(
          Math.floor((x ?? 0) * canvas.width),
          Math.floor((y ?? 0) * canvas.height),
          1,
          1,
        ).data;
      return data?.[3] ?? -1;
    },
    [fx, fy],
  );
}

async function drag(page: Page, from: number, to: number): Promise<void> {
  const box = await page.getByTestId("lfp-overview").boundingBox();
  if (box === null) throw new Error("no overview canvas");
  const y = box.y + box.height / 2;
  await page.mouse.move(box.x + box.width * from, y);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width * ((from + to) / 2), y, { steps: 4 });
  await page.mouse.move(box.x + box.width * to, y, { steps: 4 });
  await page.mouse.up();
}

test("the overview draws every channel's band power from the presigned binary, grouped by region", async ({
  page,
}) => {
  const s = await stub(page);
  await page.goto(`${APP}files/asset-0101?tab=lfp`);
  const canvas = page.getByTestId("lfp-overview");
  await expect(canvas).toBeAttached();
  await expect(page.getByTestId("lfp-hint")).toBeVisible();
  // The default band is 4-30 Hz: each channel's bands with centres in it, averaged in linear power.
  await expect(page.getByTestId("signals-band")).toHaveValue("4-30");
  const drawnOverview = (await drawn(page, "lfp-overview")) as {
    order: number[];
    rows: number[][];
  };
  expect(drawnOverview.order).toEqual([0, 1]);
  const want = bandRows(4, 30);
  expectRows(drawnOverview.rows, want);
  // The value really is a mix of bands: not equal to any one band of the fixture.
  expect(drawnOverview.rows[0]?.[0]).not.toBeCloseTo(EXPECTED.db[0]?.[0]?.[3] ?? 0, 2);
  expect(await alphaAt(page, "lfp-overview", 0.5, 0.5)).toBe(255);
  expect(await alphaAt(page, "lfp-overview", 0.01, 0.99)).toBe(255);
  expect(s.overviewSeen).toHaveLength(1);
  expect(s.overviewSeen[0]?.searchParams.get("asset_id")).toBe("asset-0101");
  expect(s.overviewSeen[0]?.searchParams.get("series")).toBe("/acquisition/LFP");
  expect(s.downloads).toEqual([{ authorization: undefined }]);
  await expect(page.getByTestId("lfp-overview-colourbar")).toContainText("dB, 4–30 Hz");
  // One region per electrode, named as the electrodes table names it, with its channel count.
  await expect(page.getByTestId("signals-region")).toHaveText([
    "amygdala left · 1",
    "hippocampus right · 1",
  ]);
  await expect(page.getByTestId("signals-regions-unknown")).toHaveCount(0);
  await expect(page.getByTestId("lfp-series")).toHaveValue("/acquisition/LFP");
  await expect(page.getByTestId("lfp-channel")).toHaveValue("0");
  await expect(page.getByTestId("lfp-channel").locator("option")).toHaveText([
    "Channel 0 · amygdala left",
    "Channel 1 · hippocampus right",
  ]);
});

test("the band selector re-averages the overview in the browser and keeps the band in the URL", async ({
  page,
}) => {
  const s = await stub(page);
  await page.goto(`${APP}files/asset-0101?tab=lfp`);
  await expect(page.getByTestId("lfp-overview")).toBeAttached();
  await page.getByTestId("signals-band").selectOption("80-250");
  await expect(page).toHaveURL(/band=80-250/);
  await expect(page.getByTestId("lfp-overview-colourbar")).toContainText("High gamma");
  const got = (await drawn(page, "lfp-overview")) as { rows: number[][] };
  const want = bandRows(80, 250);
  expectRows(got.rows, want);
  expect(s.overviewSeen).toHaveLength(1);
  await page.goto(`${APP}files/asset-0101?tab=lfp&band=1-4`);
  await expect(page.getByTestId("signals-band")).toHaveValue("1-4");
});

test("channels stay in recording order with no regions when the electrodes do not match the series", async ({
  page,
}) => {
  const s = await stub(page);
  s.electrodes = {
    rows: [{ electrode_index: 0, location: "amygdala left" }],
    page: 0,
    more: false,
  };
  await page.goto(`${APP}files/asset-0101?tab=lfp`);
  await expect(page.getByTestId("lfp-overview")).toBeAttached();
  await expect(page.getByTestId("signals-regions-unknown")).toBeVisible();
  await expect(page.getByTestId("signals-region")).toHaveText(["region not known · 2"]);
  await expect(page.getByTestId("lfp-channel").locator("option")).toHaveText([
    "Channel 0",
    "Channel 1",
  ]);
});

test("a drag on the overview asks for that window and draws exactly the returned power_db and trace", async ({
  page,
}) => {
  const s = await stub(page);
  await page.goto(`${APP}files/asset-0101?tab=lfp`);
  await expect(page.getByTestId("lfp-overview")).toBeAttached();
  await drag(page, 0.25, 0.75);
  await expect(page.getByTestId("lfp-spectrogram")).toBeAttached();
  const asked = s.spectrogramSeen.at(-1);
  expect(asked?.searchParams.get("asset_id")).toBe("asset-0101");
  expect(asked?.searchParams.get("series")).toBe("/acquisition/LFP");
  expect(asked?.searchParams.get("channel")).toBe("0");
  expect(Number(asked?.searchParams.get("start_s"))).toBeCloseTo(5, 0);
  expect(Number(asked?.searchParams.get("duration_s"))).toBeCloseTo(10, 0);
  const inUrl = () => new URL(page.url()).searchParams;
  expect(Number(inUrl().get("start"))).toBeCloseTo(5, 0);
  expect(Number(inUrl().get("dur"))).toBeCloseTo(10, 0);
  // The window's linear bins, averaged into the overview's log bands in the browser.
  expect(await drawn(page, "lfp-spectrogram")).toEqual(
    BINNED.map((row) => row.map((v) => (Number.isNaN(v) ? null : v))),
  );
  expect(await drawn(page, "lfp-trace")).toEqual(SPEC.trace.values);
  expect(await alphaAt(page, "lfp-spectrogram", 0.5, 0.5)).toBe(255);
  await expect(page.getByTestId("lfp-bytes")).toHaveText("123456 bytes read");
  await expect(page.getByTestId("lfp-requests")).toHaveText("7 requests");
  await expect(page.getByTestId("lfp-compute-ms")).toHaveText("compute 87 ms");
  await expect(page.getByTestId("lfp-window-mark")).toBeVisible();
  await expect(page.getByTestId("lfp-spectrogram-colourbar")).toContainText("dB");
  // The window spectrogram and the trace share the time axis: the same x tick marks (vertical lines),
  // labelled once, under the trace.
  const xMarks = async (testId: string): Promise<string[]> =>
    page
      .getByTestId("lfp-window-plots")
      .locator(".lfp-frame", { has: page.getByTestId(testId) })
      .locator("line.lfp-tickmark")
      .evaluateAll((ls) =>
        ls
          .filter((l) => l.getAttribute("x1") === l.getAttribute("x2"))
          .map((l) => l.getAttribute("x1") ?? ""),
      );
  const specX = await xMarks("lfp-spectrogram");
  expect(specX.length).toBeGreaterThanOrEqual(3);
  expect(specX).toEqual(await xMarks("lfp-trace"));
  const traceLabels = await page
    .getByTestId("lfp-window-plots")
    .locator(".lfp-frame", { has: page.getByTestId("lfp-trace") })
    .locator('text.timeline-tick[text-anchor="middle"]:not([transform])')
    .allTextContents();
  expect(traceLabels).toHaveLength(specX.length);
  // The y axis is the overview's log axis: 1 Hz at the bottom, 250 Hz at the top.
  await expect(page.getByTestId("signals-axis-note")).toContainText("Log axis 1–250 Hz");
  // The overview was not asked for again.
  expect(s.overviewSeen).toHaveLength(1);
});

test("a deep link restores the series, channel and window, and fetches them", async ({ page }) => {
  const s = await stub(page);
  await page.goto(
    `${APP}files/asset-0101?tab=lfp&series=%2Facquisition%2FLFP&channel=1&start=5&dur=10`,
  );
  await expect(page.getByTestId("lfp-spectrogram")).toBeAttached();
  const asked = s.spectrogramSeen.at(-1);
  expect(asked?.searchParams.get("channel")).toBe("1");
  expect(asked?.searchParams.get("start_s")).toBe("5");
  expect(asked?.searchParams.get("duration_s")).toBe("10");
  await expect(page.getByTestId("lfp-channel")).toHaveValue("1");
  await expect(page.getByTestId("signals-channel-mark")).toHaveAttribute("y", String(6 + 192 / 2));
  expect(await drawn(page, "lfp-spectrogram")).toEqual(BINNED);
  // Changing the channel keeps the window, refetches it and does not refetch the overview.
  await page.getByTestId("lfp-channel").selectOption("0");
  await expect(page).toHaveURL(/start=5&dur=10/);
  await expect.poll(() => s.spectrogramSeen.at(-1)?.searchParams.get("channel")).toBe("0");
  expect(s.overviewSeen).toHaveLength(1);
});

test("Zoom, Pan, Whole recording and the keyboard move the window", async ({ page }) => {
  const s = await stub(page);
  await page.goto(`${APP}files/asset-0101?tab=lfp&start=5&dur=10`);
  await expect(page.getByTestId("lfp-spectrogram")).toBeAttached();
  const last = () => s.spectrogramSeen.at(-1)?.searchParams;
  await page.getByRole("button", { name: "Zoom in" }).click();
  await expect.poll(() => last()?.get("duration_s")).toBe("5");
  expect(last()?.get("start_s")).toBe("7.5");
  await page.getByRole("button", { name: "Pan right" }).click();
  await expect.poll(() => last()?.get("start_s")).toBe("10");
  await page.getByRole("button", { name: "Pan left" }).click();
  await expect.poll(() => last()?.get("start_s")).toBe("7.5");
  await page.getByRole("button", { name: "Zoom out" }).click();
  await expect.poll(() => last()?.get("duration_s")).toBe("10");
  expect(last()?.get("start_s")).toBe("5");
  await page.getByTestId("lfp-overview-area").focus();
  await page.keyboard.press("ArrowRight");
  await expect.poll(() => last()?.get("start_s")).toBe("10");
  await page.keyboard.press("+");
  await expect.poll(() => last()?.get("duration_s")).toBe("5");
  await page.keyboard.press("Escape");
  await expect(page.getByTestId("lfp-hint")).toBeVisible();
  await expect(page).not.toHaveURL(/start=/);
  await expect(page.getByTestId("lfp-spectrogram")).toHaveCount(0);
  await page.keyboard.press("+");
  await expect.poll(() => last()?.get("duration_s")).toBe("20");
});

test("a 404 'overview not built' shows the preparing message and Try again", async ({
  page,
  expectError,
}) => {
  expectError("404 overview not built");
  const s = await stub(page);
  s.overview = { status: 404, body: JSON.stringify({ error: NOT_BUILT }) };
  await page.goto(`${APP}files/asset-0101?tab=lfp`);
  await expect(page.getByTestId("lfp-preparing")).toHaveText(
    /This recording's overview is being prepared\.\s*Try again/,
  );
  await expect(page.getByTestId("lfp-overview")).toHaveCount(0);
  s.overview = OK_OVERVIEW;
  await page.getByRole("button", { name: "Try again" }).click();
  await expect(page.getByTestId("lfp-overview")).toBeAttached();
  await expect(page.getByTestId("lfp-preparing")).toHaveCount(0);
});

test("an overview the builder skipped says so with the builder's reason", async ({
  page,
  expectError,
}) => {
  expectError("404 series not indexed");
  const s = await stub(page);
  const reason = "LfpIndexError: /acquisition/LFP is not chunked";
  s.overview = {
    status: 404,
    body: JSON.stringify({ error: "not_found", detail: `series not indexed: ${reason}` }),
  };
  await page.goto(`${APP}files/asset-0101?tab=lfp`);
  await expect(page.getByTestId("signals-not-built")).toHaveText(
    `Overview not built for this series. The builder skipped it: ${reason}`,
  );
  await expect(page.getByTestId("lfp-overview")).toHaveCount(0);
  await expect(page.getByTestId("lfp-preparing")).toHaveCount(0);
});

test("wheel zoom, drag pan and the arrow keys on the window ask once each, after they settle", async ({
  page,
}) => {
  const s = await stub(page);
  await page.goto(`${APP}files/asset-0101?tab=lfp&start=5&dur=10`);
  await expect(page.getByTestId("lfp-window-result")).toBeVisible();
  expect(s.spectrogramSeen).toHaveLength(1);
  const last = () => s.spectrogramSeen.at(-1)?.searchParams;
  const area = page.getByTestId("signals-window");
  // page.mouse does not scroll: bring the window plots into view first.
  await page.getByTestId("lfp-trace").scrollIntoViewIfNeeded();
  const box = await page.getByTestId("lfp-spectrogram").boundingBox();
  if (box === null) throw new Error("no spectrogram box");
  // Three wheel ticks in (each 1/1.25 of the window) settle into one request.
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  for (let i = 0; i < 3; i += 1) await page.mouse.wheel(0, -100);
  await expect.poll(() => s.spectrogramSeen.length).toBe(2);
  expect(Number(last()?.get("duration_s"))).toBeCloseTo(10 / 1.25 ** 3, 0);
  // A drag to the right moves the window earlier, one request on release.
  const y = box.y + box.height / 2;
  const startBefore = Number(last()?.get("start_s"));
  await page.mouse.move(box.x + box.width * 0.3, y);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width * 0.5, y, { steps: 5 });
  await page.mouse.move(box.x + box.width * 0.7, y, { steps: 5 });
  await page.mouse.up();
  await expect.poll(() => s.spectrogramSeen.length).toBe(3);
  expect(Number(last()?.get("start_s"))).toBeLessThan(startBefore);
  // The arrow keys step half a window.
  const before = Number(last()?.get("start_s"));
  const dur = Number(last()?.get("duration_s"));
  await area.focus();
  await page.keyboard.press("ArrowRight");
  await expect.poll(() => s.spectrogramSeen.length).toBe(4);
  expect(Number(last()?.get("start_s"))).toBeCloseTo(before + dur / 2, 0);
  expect(s.overviewSeen).toHaveLength(1);
});

test("a click on an overview row picks that row's channel", async ({ page }) => {
  const s = await stub(page);
  await page.goto(`${APP}files/asset-0101?tab=lfp&start=5&dur=10`);
  await expect(page.getByTestId("lfp-window-result")).toBeVisible();
  const box = await page.getByTestId("lfp-overview").boundingBox();
  if (box === null) throw new Error("no overview canvas");
  await page.mouse.click(box.x + box.width / 2, box.y + box.height * 0.75);
  await expect(page.getByTestId("lfp-channel")).toHaveValue("1");
  await expect.poll(() => s.spectrogramSeen.at(-1)?.searchParams.get("channel")).toBe("1");
  await expect(page).toHaveURL(/start=5&dur=10/);
});

test("the server's 400, 413 and 422 texts show; a 503 offers Try again", async ({
  page,
  expectError,
}) => {
  expectError("400, 413, 422, 503 refusals");
  const s = await stub(page);
  const limit = "duration_s must be from 1 to 1200 seconds";
  s.spectrogram = { status: 400, body: JSON.stringify({ error: limit }) };
  await page.goto(`${APP}files/asset-0101?tab=lfp&start=0&dur=2000`);
  await expect(page.getByTestId("catalog-error")).toHaveText(limit);
  expect(s.spectrogramSeen.at(-1)?.searchParams.get("duration_s")).toBe("2000");
  await expect(page.getByRole("button", { name: "Try again" })).toHaveCount(0);
  for (const [status, text] of [
    [413, "the response is 6000000 bytes, over the 5242880 limit; ask for a shorter window"],
    [422, "the series has no uniform sampling rate"],
  ] as const) {
    s.spectrogram = { status, body: JSON.stringify({ error: text }) };
    await page.goto(`${APP}files/asset-0101?tab=lfp&start=0&dur=10`);
    await expect(page.getByTestId("catalog-error")).toHaveText(text);
    await expect(page.getByRole("button", { name: "Try again" })).toHaveCount(0);
  }
  s.spectrogram = { status: 503, body: '{"error":"compute timed out"}' };
  await page.goto(`${APP}files/asset-0101?tab=lfp&start=0&dur=10`);
  await expect(page.getByTestId("catalog-error")).toContainText("compute timed out");
  s.spectrogram = { status: 200, body: fixture("spectrogram") };
  await page.getByRole("button", { name: "Try again" }).click();
  await expect(page.getByTestId("lfp-spectrogram")).toBeAttached();
});

test("the overview and the window repaint when the page width changes", async ({ page }) => {
  await stub(page);
  await page.goto(`${APP}files/asset-0101?tab=lfp&start=5&dur=10`);
  await expect(page.getByTestId("lfp-window-result")).toBeVisible();
  const before = await page
    .getByTestId("lfp-overview")
    .evaluate((el) => (el as HTMLCanvasElement).width);
  // A resize changes the canvas size, which clears it: each plot must paint again.
  await page.setViewportSize({ width: 1000, height: 900 });
  await expect
    .poll(() => page.getByTestId("lfp-overview").evaluate((el) => (el as HTMLCanvasElement).width))
    .not.toBe(before);
  for (const id of ["lfp-overview", "lfp-spectrogram"]) {
    await expect.poll(() => alphaAt(page, id, 0.5, 0.5)).toBe(255);
  }
});

test("375 px wide: no horizontal scroll on the LFP tab with a window", async ({ page }) => {
  await stub(page);
  await page.setViewportSize({ width: 375, height: 812 });
  await page.goto(`${APP}files/asset-0101?tab=lfp&start=5&dur=10`);
  await expect(page.getByTestId("lfp-trace")).toBeAttached();
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
  expect(overflow).toBeLessThanOrEqual(0);
});

test.describe("phone width, touch", () => {
  test.use({ viewport: { width: 375, height: 812 }, hasTouch: true, isMobile: true });

  test("the Zoom in button and a touch drag each ask for a window at 375 px", async ({ page }) => {
    const s = await stub(page);
    await page.goto(`${APP}files/asset-0101?tab=lfp`);
    const area = page.getByTestId("lfp-overview-area");
    await expect(page.getByTestId("lfp-overview")).toBeAttached();
    // The overview fits the screen: its box is not wider than the viewport.
    const fit = await area.boundingBox();
    expect((fit?.x ?? 0) + (fit?.width ?? 9999)).toBeLessThanOrEqual(375);

    await page.getByRole("button", { name: "Zoom in" }).click();
    await expect.poll(() => s.spectrogramSeen.length).toBe(1);
    await page.getByRole("button", { name: "Whole recording" }).click();
    await expect(page.getByTestId("lfp-hint")).toBeVisible();

    // A finger drag, sent as touch events through the protocol (pointerType touch).
    await area.scrollIntoViewIfNeeded();
    const box = await area.boundingBox();
    if (box === null) throw new Error("no overview box");
    const y = box.y + 6 + 75;
    const at = (f: number) => ({ x: box.x + 52 + f * (box.width - 52 - 12), y });
    const cdp = await page.context().newCDPSession(page);
    const touch = (type: "touchStart" | "touchMove" | "touchEnd", f: number) =>
      cdp.send("Input.dispatchTouchEvent", {
        type,
        touchPoints: type === "touchEnd" ? [] : [{ ...at(f), id: 1 }],
      });
    await touch("touchStart", 0.25);
    for (const f of [0.35, 0.5, 0.65, 0.75]) await touch("touchMove", f);
    await touch("touchEnd", 0.75);
    await expect.poll(() => s.spectrogramSeen.length).toBe(2);
    const asked = s.spectrogramSeen.at(-1);
    expect(Number(asked?.searchParams.get("start_s"))).toBeCloseTo(5, -1);
    expect(Number(asked?.searchParams.get("duration_s"))).toBeGreaterThan(5);
  });
});

// --- event lanes and labels on the window's axis --------------------------------------------

/**
 * The recipe's LFP clock runs 10 s ahead of the trials clock, and the series starts 12.5 s into the LFP
 * clock (the fixture overview's `starting_time`): a trials time t sits at t - 2.5 on the view's axis.
 */
const OFFSET_EVENTS = {
  asset_id: "asset-0101",
  dataset: "dandi-000623",
  recipe: { version: "e2e", default: false, lfp_clock_offset_s: 10 },
  event_types: [
    {
      name: "scene_cut",
      averaging: "average",
      n_events: 3,
      events: [5.5, 7.5, 9.5].map((t, row) => ({
        onset_s: t,
        stop_s: null,
        attributes: {},
        source: { kind: "e2e", ref: "cuts", row },
      })),
    },
    {
      name: "frame",
      averaging: "single",
      n_events: 20_000,
      events: Array.from({ length: 20_000 }, (_v, row) => ({
        onset_s: 2.5 + row / 2000,
        stop_s: null,
        attributes: {},
        source: { kind: "e2e", ref: "frames", row },
      })),
    },
  ],
};

async function stubLanes(page: Page): Promise<{ posts: unknown[] }> {
  const seen = { posts: [] as unknown[] };
  await page.route(`${API}/compute/events*`, (route) =>
    route.request().method() === "OPTIONS"
      ? route.fulfill({ status: 204, headers: CORS })
      : route.fulfill({
          status: 200,
          contentType: "application/json",
          headers: CORS,
          body: JSON.stringify(OFFSET_EVENTS),
        }),
  );
  await page.route(`${API}/files/asset-0101/labels`, (route) => {
    const req = route.request();
    const headers = { ...CORS, "access-control-allow-methods": "GET, POST, OPTIONS" };
    if (req.method() === "OPTIONS") return route.fulfill({ status: 204, headers });
    if (req.method() === "POST") {
      const body = JSON.parse(req.postData() ?? "{}") as Record<string, unknown>;
      seen.posts.push(body);
      return route.fulfill({
        status: 201,
        contentType: "application/json",
        headers,
        body: JSON.stringify({ ...body, label_id: "made", mine: true, version: 1 }),
      });
    }
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      headers,
      body: JSON.stringify({
        labels: [
          {
            label_id: "stored",
            name: "stored",
            note: "",
            start_s: 4.5,
            stop_s: 6.5,
            mine: false,
            version: 1,
          },
        ],
      }),
    });
  });
  return seen;
}

test("event lanes sit on the window's axis through the recipe's clock offset", async ({ page }) => {
  await stub(page);
  await stubLanes(page);
  await page.goto(`${APP}files/asset-0101?tab=lfp&start=0&dur=10`);
  const cuts = page.getByTestId("lane-Scene cut");
  await expect(cuts.locator("line.lane-tick")).toHaveCount(3);
  await expect(cuts.locator("line.lane-tick title")).toHaveText([
    "Scene cut at 3 s",
    "Scene cut at 5 s",
    "Scene cut at 7 s",
  ]);
  // The stored label (trials clock 4.5-6.5 s) is drawn at 2-4 s, and ticks under the overview.
  const bar = await page.getByTestId("label-bar-stored").boundingBox();
  const svg = await page.getByTestId("signals-lanes").locator("svg").boundingBox();
  if (bar === null || svg === null) throw new Error("no label bar");
  const plotW = svg.width - 52 - 12;
  expect(Math.abs(bar.x - svg.x - 52 - 0.2 * plotW)).toBeLessThan(2);
  expect(Math.abs(bar.width - 0.2 * plotW)).toBeLessThan(2);
  await expect(page.locator("rect.signals-label-tick")).toHaveCount(1);
  await expect(page.getByTestId("labels-count")).toHaveText("1 · 0 yours");
});

test("20,000 events closer than a pixel bin into density, one mark per pixel at most", async ({
  page,
}) => {
  await stub(page);
  await stubLanes(page);
  await page.goto(`${APP}files/asset-0101?tab=lfp&start=0&dur=10`);
  const frames = page.getByTestId("lane-Frame");
  await expect(frames.locator("rect.lane-bin").first()).toBeAttached();
  const svg = await page.getByTestId("signals-lanes").locator("svg").boundingBox();
  const plotW = (svg?.width ?? 0) - 52 - 12;
  const marks = await frames.locator("rect.lane-bin, line.lane-tick").count();
  expect(marks).toBeGreaterThan(0);
  expect(marks).toBeLessThanOrEqual(Math.ceil(plotW));
  // The marks hold every event in the window (all 20,000 are at 0-10 s on the axis).
  const binned = await frames
    .locator("rect.lane-bin title")
    .evaluateAll((ts) => ts.reduce((n, t) => n + Number.parseInt(t.textContent ?? "0", 10), 0));
  const ticks = await frames.locator("line.lane-tick").count();
  expect(binned + ticks).toBe(20_000);
});

test("a span drafted under the window is stored on the trials clock", async ({ page }) => {
  await stub(page);
  const seen = await stubLanes(page);
  await page.goto(`${APP}files/asset-0101?tab=lfp&start=0&dur=10`);
  // Drafting waits for the events: they carry the clock that places the label.
  await expect(page.getByTestId("lane-Scene cut")).toBeAttached();
  await expect(page.getByTestId("lfp-window-result")).toBeVisible();
  // `page.mouse` does not scroll: bring the lanes into view, then measure them.
  await page.getByTestId("signals-lanes").scrollIntoViewIfNeeded();
  const svg = await page.getByTestId("signals-lanes").locator("svg").boundingBox();
  if (svg === null) throw new Error("no lanes");
  const plotW = svg.width - 52 - 12;
  const x = (t: number) => svg.x + 52 + (t / 10) * plotW;
  const y = svg.y + 2 * 18 + 12;
  await page.mouse.move(x(6), y);
  await page.mouse.down();
  await page.mouse.move(x(7), y, { steps: 3 });
  await page.mouse.move(x(8), y, { steps: 3 });
  await page.mouse.up();
  const pop = page.getByTestId("label-popover");
  await expect(pop).toContainText("New label · 6");
  await page.getByTestId("label-name").fill("theta burst");
  // Arrow keys in the name field edit the text; they do not move the window.
  await page.keyboard.press("ArrowLeft");
  await pop.getByRole("button", { name: "Save" }).click();
  await expect.poll(() => seen.posts.length).toBe(1);
  const body = seen.posts[0] as Record<string, number | string>;
  expect(body.name).toBe("theta burst");
  expect(Number(body.start_s)).toBeCloseTo(8.5, 1);
  expect(Number(body.stop_s)).toBeCloseTo(10.5, 1);
  await expect(page).toHaveURL(/start=0&dur=10/);
});
