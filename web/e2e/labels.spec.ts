// Shared researcher labels: the Labels lane of the
// signals view (here a file whose series table holds no LFP, so the lanes span the labels), drafting by
// drag with the naming popover and Esc, rename and edge drag of your own label, the read-only view of
// another researcher's, the 403 and 409 refusals, and the label options in the Units align picker. The
// API is served by page.route with the real response shapes (fixtures/labels.json is the labels
// Lambda's `_label` output: `mine`, never the author).
import { expect, type Page } from "@playwright/test";
import { API, APP, CORS, fixture, stubBase, stubEmptyUploads, test } from "./support";

test.skip(
  process.env.E2E_LIVE === "1",
  "needs the fixture-mode auth build (no live server has the auth env)",
);

const MINE = "3f1c0a9e5b7d4c2e8a6f1d0b9c8e7a65";
const THEIRS = "a91d4e7c20b64f1d9e3a58c7b6d20f14";

interface Labels {
  calls: { method: string; path: string; body: unknown }[];
  /** When set, the next PATCH or DELETE answers this status and text. */
  refuse: { status: number; error: string } | null;
  gets: number;
}

interface Stored {
  label_id: string;
  version: number;
  mine: boolean;
  [key: string]: unknown;
}

async function stub(page: Page): Promise<Labels> {
  await stubBase(page);
  await stubEmptyUploads(page);
  await page.route(`${API}/catalog/**`, (route) => {
    const req = route.request();
    if (req.method() === "OPTIONS") return route.fulfill({ status: 204, headers: CORS });
    const url = new URL(req.url());
    // The anatomy panel's studies and links: the defaults stubBase serves.
    if (url.pathname === "/catalog/studies" || url.pathname === "/catalog/links") {
      return route.fallback();
    }
    let body: string | null = null;
    if (/^\/catalog\/datasets\/[^/]+\/assets$/.test(url.pathname)) {
      body = JSON.stringify({ assets: [], page: 0, more: false, query_ms: 1 });
    } else if (url.pathname === "/catalog/assets/asset-0101") body = fixture("catalog-file-nwb");
    else if (url.pathname === "/catalog/assets/asset-0101/events") body = fixture("events");
    else if (url.pathname === "/catalog/assets/asset-0101/tables/series") {
      // No LFP series: the signals view draws the lanes and labels over the labels' span.
      body = JSON.stringify({ rows: [], page: 0, more: false });
    } else if (url.pathname.startsWith("/catalog/assets/asset-0101/tables/")) {
      const table = url.pathname.split("/").pop() ?? "";
      const tables = JSON.parse(fixture("catalog-file-tables")) as Record<string, unknown>;
      if (table in tables) body = JSON.stringify(tables[table]);
    }
    return route.fulfill({
      status: body === null ? 404 : 200,
      contentType: "application/json",
      headers: CORS,
      body: body ?? '{"error":"no route"}',
    });
  });
  await page.route(`${API}/compute/rates*`, (route) => {
    if (route.request().method() === "OPTIONS")
      return route.fulfill({ status: 204, headers: CORS });
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      headers: CORS,
      body: fixture("rates"),
    });
  });

  const state: Labels = { calls: [], refuse: null, gets: 0 };
  let stored = (JSON.parse(fixture("labels")) as { labels: Stored[] }).labels;
  await page.route(`${API}/files/asset-0101/labels**`, (route) => {
    const req = route.request();
    const headers = {
      ...CORS,
      "access-control-allow-methods": "GET, POST, PATCH, DELETE, OPTIONS",
    };
    if (req.method() === "OPTIONS") return route.fulfill({ status: 204, headers });
    const path = new URL(req.url()).pathname;
    const id = path.split("/labels/")[1] ?? null;
    const body: unknown = req.postData() === null ? null : JSON.parse(req.postData() ?? "null");
    state.calls.push({ method: req.method(), path, body });
    const reply = (status: number, payload: unknown) =>
      route.fulfill({
        status,
        contentType: "application/json",
        headers,
        body: payload === null ? "" : JSON.stringify(payload),
      });
    if (req.method() === "GET") {
      state.gets += 1;
      return reply(200, { labels: stored });
    }
    if (req.method() === "POST") {
      const made: Stored = {
        asset_id: "asset-0101",
        label_id: "c0ffee00c0ffee00c0ffee00c0ffee00",
        ...(body as object),
        dataset: "dandi-000623",
        s3_version_id: "3HL4kqtJlcpXroDTDmJ.rmSpXd3dIbrHY",
        snapshots: null,
        created_at: 1793000200,
        updated_at: 1793000200,
        version: 1,
        mine: true,
      };
      stored = [...stored, made];
      return reply(201, made);
    }
    const found = stored.find((l) => l.label_id === id);
    if (state.refuse !== null) {
      const { status, error } = state.refuse;
      state.refuse = null;
      return reply(status, { error });
    }
    if (found === undefined) return reply(404, { error: "no such label" });
    if (!found.mine) {
      return reply(403, { error: "only the author can change or delete a label" });
    }
    if (req.method() === "DELETE") {
      stored = stored.filter((l) => l.label_id !== id);
      return reply(204, null);
    }
    const next = { ...found, ...(body as object), version: found.version + 1 };
    stored = stored.map((l) => (l.label_id === id ? next : l));
    return reply(200, next);
  });
  return state;
}

// The lanes' plot frame: LEFT and RIGHT margins of signals/Frame.tsx; the Labels lane is the first row
// here (the default events stub resolves no event types). The axis spans the labels' own span, 1 s
// padded: [1, 10.25] s for the fixture's two labels.
const LEFT = 52;
const RIGHT = 12;
const DOMAIN = [1, 10.25] as const;

async function laneBox(page: Page): Promise<{ x: (t: number) => number; y: number }> {
  const box = await page.getByTestId("signals-lanes").locator("svg").boundingBox();
  if (box === null) throw new Error("no lanes box");
  const plotW = box.width - LEFT - RIGHT;
  return {
    x: (t: number) => box.x + LEFT + ((t - DOMAIN[0]) / (DOMAIN[1] - DOMAIN[0])) * plotW,
    y: box.y + 12,
  };
}

/** Drag across the Labels lane from `from` to `to` seconds. */
async function dragLabelsLane(page: Page, from: number, to: number): Promise<void> {
  const { x, y } = await laneBox(page);
  await page.mouse.move(x(from), y);
  await page.mouse.down();
  await page.mouse.move((x(from) + x(to)) / 2, y, { steps: 3 });
  await page.mouse.move(x(to), y, { steps: 3 });
  await page.mouse.up();
}

const FILE = `${APP}files/asset-0101?tab=lfp`;

test("the labels list shows each label with the author as you or another, and a count", async ({
  page,
}) => {
  await stub(page);
  await page.goto(FILE);
  await expect(page.getByTestId("lfp-none")).toBeVisible();
  await expect(page.getByTestId(`label-bar-${MINE}`)).toBeVisible();
  await expect(page.getByTestId(`label-bar-${THEIRS}`)).toBeVisible();
  await expect(page.getByTestId(`label-item-${MINE}`)).toContainText("you");
  await expect(page.getByTestId(`label-item-${THEIRS}`)).toContainText("another researcher");
  await expect(page.getByTestId("labels-count")).toHaveText("2 · 1 yours");
  await expect(page.getByTestId("labels-more")).toHaveCount(0);
});

test("a drag in the Labels lane drafts a span; the popover names it and POSTs", async ({
  page,
}) => {
  const labels = await stub(page);
  await page.goto(FILE);
  await expect(page.getByTestId("lane-labels")).toBeAttached();
  await dragLabelsLane(page, 5, 7);
  const pop = page.getByTestId("label-popover");
  await expect(pop).toContainText("New label");
  await expect(page.getByTestId("label-draft")).toBeVisible();
  await expect(page.getByTestId("label-name")).toBeFocused();
  await page.getByTestId("label-name").fill("sharp wave");
  await pop.getByRole("button", { name: "Save" }).click();

  await expect(page.getByTestId("label-popover")).toHaveCount(0);
  await expect(page.getByTestId("labels-list")).toContainText("sharp wave");
  const post = labels.calls.find((c) => c.method === "POST");
  expect(post?.path).toBe("/files/asset-0101/labels");
  const body = post?.body as { name: string; note: string; start_s: number; stop_s: number };
  expect({ name: body.name, note: body.note }).toEqual({ name: "sharp wave", note: "" });
  // The drag's ends, to the lane's resolution (one pixel is about 0.01 s here).
  expect(body.start_s).toBeCloseTo(5, 1);
  expect(body.stop_s).toBeCloseTo(7, 1);
});

test("Esc cancels a drafted span with no request", async ({ page }) => {
  const labels = await stub(page);
  await page.goto(FILE);
  await expect(page.getByTestId("lane-labels")).toBeAttached();
  await dragLabelsLane(page, 5, 7);
  await expect(page.getByTestId("label-popover")).toBeVisible();
  await page.getByTestId("label-name").fill("never sent");
  await page.keyboard.press("Escape");
  await expect(page.getByTestId("label-popover")).toHaveCount(0);
  await expect(page.getByTestId("label-draft")).toHaveCount(0);
  expect(labels.calls.filter((c) => c.method !== "GET")).toHaveLength(0);
});

test("an empty name is refused before any request", async ({ page, expectError }) => {
  expectError("name validation notice");
  const labels = await stub(page);
  await page.goto(FILE);
  await expect(page.getByTestId("lane-labels")).toBeAttached();
  await dragLabelsLane(page, 5, 7);
  await page.getByTestId("label-name").fill("   ");
  await page.getByTestId("label-popover").getByRole("button", { name: "Save" }).click();
  await expect(page.getByTestId("label-refusal")).toContainText("1 to 64 characters");
  expect(labels.calls.filter((c) => c.method === "POST")).toHaveLength(0);
});

test("renaming your label sends PATCH with all four fields and the version", async ({ page }) => {
  const labels = await stub(page);
  await page.goto(FILE);
  await page.getByTestId(`label-item-${MINE}`).click();
  const pop = page.getByTestId("label-popover");
  await expect(pop).toContainText("Your label");
  await page.getByTestId("label-name").fill("burst 2");
  await pop.getByRole("button", { name: "Save" }).click();
  await expect(page.getByTestId(`label-item-${MINE}`)).toContainText("burst 2");
  const patch = labels.calls.find((c) => c.method === "PATCH");
  expect(patch?.path).toBe(`/files/asset-0101/labels/${MINE}`);
  expect(patch?.body).toEqual({
    name: "burst 2",
    note: "first burst after the cue",
    start_s: 2,
    stop_s: 4.5,
    version: 1,
  });
});

test("dragging an edge of your label moves it with PATCH", async ({ page }) => {
  const labels = await stub(page);
  await page.goto(FILE);
  await page.getByTestId(`label-bar-${MINE}`).click();
  await expect(page.getByTestId("label-popover")).toContainText("Your label");
  const { x, y } = await laneBox(page);
  await page.mouse.move(x(4.5), y);
  await page.mouse.down();
  await page.mouse.move(x(5.5), y, { steps: 4 });
  await page.mouse.move(x(6), y, { steps: 4 });
  await page.mouse.up();
  await expect.poll(() => labels.calls.filter((c) => c.method === "PATCH").length).toBe(1);
  const body = labels.calls.find((c) => c.method === "PATCH")?.body as Record<string, number>;
  expect(body.start_s).toBe(2);
  expect(body.stop_s).toBeCloseTo(6, 1);
  expect(body.version).toBe(1);
});

test("deleting asks first, then sends DELETE", async ({ page }) => {
  const labels = await stub(page);
  await page.goto(FILE);
  await page.getByTestId(`label-item-${MINE}`).click();
  await page.getByRole("button", { name: "Delete" }).click();
  await expect(page.getByTestId("label-confirm")).toContainText("Delete this label");
  await page.getByRole("button", { name: "Keep it" }).click();
  expect(labels.calls.filter((c) => c.method === "DELETE")).toHaveLength(0);
  await page.getByRole("button", { name: "Delete" }).click();
  await page.getByRole("button", { name: "Confirm delete" }).click();
  await expect(page.getByTestId(`label-item-${MINE}`)).toHaveCount(0);
  const del = labels.calls.find((c) => c.method === "DELETE");
  expect(del?.path).toBe(`/files/asset-0101/labels/${MINE}`);
  expect(del?.body).toBeNull();
});

test("another researcher's label is read-only and says why", async ({ page }) => {
  const labels = await stub(page);
  await page.goto(FILE);
  await page.getByTestId(`label-item-${THEIRS}`).click();
  const pop = page.getByTestId("label-popover");
  await expect(pop).toContainText("artifact");
  await expect(page.getByTestId("label-readonly")).toContainText("another researcher");
  await expect(page.getByTestId("label-readonly-reason")).toHaveText(
    "Only the author can change or delete a label.",
  );
  await expect(page.getByTestId("label-name")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Delete" })).toHaveCount(0);
  await expect(pop.getByRole("button", { name: "Save" })).toHaveCount(0);
  // Its edges do not move either.
  const { x, y } = await laneBox(page);
  await page.mouse.move(x(8), y);
  await page.mouse.down();
  await page.mouse.move(x(7), y, { steps: 4 });
  await page.mouse.up();
  expect(labels.calls.filter((c) => c.method !== "GET")).toHaveLength(0);
});

test("a 409 shows the server text and Reload fetches the labels again", async ({
  page,
  expectError,
}) => {
  expectError("409 stale label");
  const labels = await stub(page);
  await page.goto(FILE);
  await page.getByTestId(`label-item-${MINE}`).click();
  labels.refuse = { status: 409, error: "the label changed; reload it and try again" };
  await page.getByTestId("label-name").fill("stale edit");
  await page.getByTestId("label-popover").getByRole("button", { name: "Save" }).click();
  const notice = page.getByTestId("label-refusal");
  await expect(notice).toContainText("the label changed; reload it and try again");
  expect(labels.gets).toBe(1);
  await notice.getByRole("button", { name: "Reload" }).click();
  await expect.poll(() => labels.gets).toBe(2);
  await expect(page.getByTestId("label-refusal")).toHaveCount(0);
});

test("a 403 shows the server text and no Reload", async ({ page, expectError }) => {
  expectError("403 not the author");
  const labels = await stub(page);
  await page.goto(FILE);
  await page.getByTestId(`label-item-${MINE}`).click();
  labels.refuse = { status: 403, error: "only the author can change or delete a label" };
  await page.getByTestId("label-popover").getByRole("button", { name: "Save" }).click();
  const notice = page.getByTestId("label-refusal");
  await expect(notice).toContainText("only the author can change or delete a label");
  await expect(notice.getByRole("button")).toHaveCount(0);
});

test("the Units align picker offers label options and sends the right align", async ({ page }) => {
  await stub(page);
  const rates: URL[] = [];
  await page.route(`${API}/compute/rates*`, (route) => {
    if (route.request().method() === "OPTIONS")
      return route.fulfill({ status: 204, headers: CORS });
    rates.push(new URL(route.request().url()));
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      headers: CORS,
      body: fixture("rates"),
    });
  });
  await page.goto(`${APP}files/asset-0101?tab=units&unit=0`);
  const align = page.getByTestId("units-align");
  await expect(align.locator("option", { hasText: "Label: burst" })).toHaveCount(1);
  await expect(align.locator("option", { hasText: "All labels named artifact" })).toHaveCount(1);
  await align.selectOption({ label: "Label: burst" });
  await expect(page.getByTestId("rates-result")).toBeVisible();
  expect(rates.at(-1)?.searchParams.get("align")).toBe(`label:${MINE}`);
  await align.selectOption({ label: "All labels named artifact" });
  await expect.poll(() => rates.at(-1)?.searchParams.get("align")).toBe("label_name:artifact");
});
