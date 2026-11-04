// Acceptance flow on the Find page: live mode uses the `make web`
// dev server and web/public/index.json from `make harvest`; fixture mode uses the production build under
// `vite preview` with the index served by page.route. The remaining acceptance steps run from the shell. The whole-dataset
// pick replaces the old per-file pick (the file list went with the detail pane).
import { readFileSync } from "node:fs";
import { expect, type Page, test } from "@playwright/test";
import { parseTargets } from "../src/targets/schema";

const live = process.env.E2E_LIVE === "1";
const golden = readFileSync(
  new URL("../../services/tests/fixtures/harvest/expected-index.json", import.meta.url),
  "utf8",
);

async function routeIndex(page: Page): Promise<void> {
  await page.route("**/index.json", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: golden }),
  );
}

const pickCount = (page: Page) => page.getByTestId("pick-count");
const pick = (page: Page) => page.getByRole("button", { name: "Pick dataset 000576", exact: true });

/** Open, human species, Units and LFP; leaves 000576 among the best fits. */
async function filterToExemplars(page: Page): Promise<void> {
  await page.goto("/discover");
  await expect(page.getByTestId("index-summary")).toContainText("Index harvested");
  await expect(pickCount(page)).toHaveText("0");
  await page.getByRole("checkbox", { name: /^Human \d+$/ }).check();
  await page.getByRole("checkbox", { name: /^Units \d+$/ }).check();
  await page.getByRole("checkbox", { name: /^LFP \d+$/ }).check();
  await expect(page.getByTestId("row-dandi-000576")).toBeVisible();
}

test("rows 3-12: filter, pick 000576, note, export, clear, import, reload", async ({ page }) => {
  if (!live) await routeIndex(page);
  await filterToExemplars(page);

  // Pick the dataset; the entry shows the source, id, version and an empty note.
  await pick(page).click();
  await expect(pickCount(page)).toHaveText("1");
  const entry = page.getByTestId("pick-entry");
  await expect(entry).toContainText("DANDI 000576 · whole dataset");
  const note = entry.getByRole("textbox");
  await expect(note).toHaveValue("");

  // Type the note.
  await note.fill("exemplar candidate");
  await expect(note).toHaveValue("exemplar candidate");

  // Export list downloads targets.json with exactly the five keys.
  const [download] = await Promise.all([
    page.waitForEvent("download"),
    page.getByRole("button", { name: "Export list" }).click(),
  ]);
  expect(download.suggestedFilename()).toBe("targets.json");
  const exported = readFileSync(await download.path(), "utf8");
  expect(parseTargets(JSON.parse(exported)).targets).toHaveLength(1);
  const first = JSON.parse(exported).targets[0] as Record<string, unknown>;
  expect(Object.keys(first).sort()).toEqual(["dataset_id", "note", "path", "source", "version"]);
  expect(first).toMatchObject({
    source: "dandi",
    dataset_id: "000576",
    path: null,
    note: "exemplar candidate",
  });

  // Clear, then confirm in the dialog.
  await page.getByRole("button", { name: "Clear", exact: true }).click();
  const dialog = page.getByTestId("picks-dialog");
  await expect(dialog).toContainText("Clear all 1 picks?");
  await dialog.getByRole("button", { name: "Clear", exact: true }).click();
  await expect(pickCount(page)).toHaveText("0");

  // Import the exported file and confirm "Replace 0 picks with 1".
  await page.getByLabel("Import").setInputFiles({
    name: "targets.json",
    mimeType: "application/json",
    buffer: Buffer.from(exported),
  });
  await expect(dialog).toContainText("Replace 0 picks with 1 from targets.json?");
  await dialog.getByRole("button", { name: "Replace" }).click();
  await expect(pickCount(page)).toHaveText("1");
  await expect(page.getByTestId("pick-entry").getByRole("textbox")).toHaveValue(
    "exemplar candidate",
  );

  // Reload; the pick and its note survive (localStorage).
  await page.reload();
  await expect(pickCount(page)).toHaveText("1");
  await expect(page.getByTestId("pick-entry").getByRole("textbox")).toHaveValue(
    "exemplar candidate",
  );
});

test.describe("pick rail states (fixture mode only)", () => {
  test.skip(live, "fixture mode serves the index from the recorded response");

  test.beforeEach(async ({ page }) => {
    await routeIndex(page);
  });

  test("refuses a duplicate pick and removes", async ({ page }) => {
    await page.goto("/discover?q=000576");
    await pick(page).click();
    await expect(page.getByTestId("row-dandi-000576")).toContainText("Picked");
    await expect(pickCount(page)).toHaveText("1");
    await page.getByRole("button", { name: /^Remove 000576/ }).click();
    await expect(pickCount(page)).toHaveText("0");
  });

  test("clear asks first and keeps the list when cancelled", async ({ page }) => {
    await page.goto("/discover?q=000576");
    await pick(page).click();
    await page.getByRole("button", { name: "Clear", exact: true }).click();
    await page.getByTestId("picks-dialog").getByRole("button", { name: "Cancel" }).click();
    await expect(pickCount(page)).toHaveText("1");
  });

  test("a non-curator can ask a curator: the button exports the list", async ({ page }) => {
    await page.goto("/discover?q=000576");
    const ask = page.getByRole("button", { name: "Ask a curator to import" });
    await expect(ask).toBeDisabled();
    await pick(page).click();
    const [download] = await Promise.all([page.waitForEvent("download"), ask.click()]);
    expect(download.suggestedFilename()).toBe("targets.json");
  });

  test("import rejects an invalid file whole and leaves the list alone", async ({ page }) => {
    await page.goto("/discover?q=000576");
    await pick(page).click();
    const bad = {
      schema: "asteroidea.targets/1",
      exported_at: "2026-10-23T13:00:00Z",
      targets: [
        { source: "dandi", dataset_id: "000003", version: "0.1", path: null, note: "", size: 5 },
      ],
    };
    await page.getByLabel("Import").setInputFiles({
      name: "bad.json",
      mimeType: "application/json",
      buffer: Buffer.from(JSON.stringify(bad)),
    });
    await expect(page.getByRole("alert")).toContainText(
      'bad.json: targets[0]: unexpected key "size"',
    );
    await expect(pickCount(page)).toHaveText("1");
  });
});
