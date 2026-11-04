// Sign-in gate and the Get started page. Runs against the sign-in env build (port
// WEB_PORT + 1, the one auth.spec.ts uses); managed login, the token endpoint and the API are all
// served by page.route, so nothing leaves the machine.
import { expect, type Page } from "@playwright/test";
import { API, APP, CORS, fixture, stubBase, stubEmptyUploads, test } from "./support";

test.skip(
  process.env.E2E_LIVE === "1",
  "needs the fixture-mode auth build (no live server has the auth env)",
);

const startBody = fixture("get-started");

interface Seen {
  start: (string | undefined)[];
}

async function stub(
  page: Page,
  start: { status: number; body: string } = { status: 200, body: startBody },
): Promise<Seen> {
  await stubBase(page);
  const seen: Seen = { start: [] };
  await stubEmptyUploads(page);
  // Data is the landing page; it asks for the datasets list.
  await page.route(`${API}/catalog/datasets`, (route) =>
    route.request().method() === "OPTIONS"
      ? route.fulfill({ status: 204, headers: CORS })
      : route.fulfill({
          status: 200,
          contentType: "application/json",
          headers: CORS,
          body: fixture("catalog-datasets"),
        }),
  );
  await page.route(`${API}/get-started`, (route) => {
    if (route.request().method() === "OPTIONS")
      return route.fulfill({ status: 204, headers: CORS });
    seen.start.push(route.request().headers().authorization);
    return route.fulfill({
      status: start.status,
      contentType: "application/json",
      headers: CORS,
      body: start.body,
    });
  });
  return seen;
}

test("Get started shows the setup command, and what it does", async ({ page }) => {
  const seen = await stub(page);
  await page.goto(APP);
  await page.getByRole("link", { name: "Setup", exact: true }).click();
  const install = page.getByTestId("start-install");
  await expect(install).toContainText("Install uv first if it is missing");
  await expect(install).toContainText("uvx --from 'https://app.asteroidea.test/sdk/");
  await expect(install).toContainText("asteroidea-setup https://app.asteroidea.test");
  await expect(page.getByRole("button", { name: "Copy" })).toBeVisible();
  await expect(page.getByTestId("start-steps")).toBeVisible();
  await expect(page.getByTestId("start-steps").getByRole("listitem")).toHaveCount(4);
  await expect(page.getByText("lasts 1 hour")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Reload command" })).toHaveCount(0);
  await expect(install).not.toContainText("X-Amz-");
  await expect(page.getByTestId("start-platform")).toHaveCount(0);
  expect(seen.start).toEqual(["Bearer access-1"]);
});

test("a 403 shows the server's text verbatim", async ({ page, expectError }) => {
  expectError("403 refusal");
  await stub(page, {
    status: 403,
    body: JSON.stringify({ error: "caller is not in the researchers group" }),
  });
  await page.goto(APP);
  await page.getByRole("link", { name: "Setup", exact: true }).click();
  await expect(page.getByTestId("start-error")).toContainText(
    "caller is not in the researchers group",
  );
});

test("a 503 shows the server's text verbatim", async ({ page, expectError }) => {
  expectError("503 refusal");
  const text =
    "the SDK download is not published yet; ask the administrator to run make web-publish";
  await stub(page, { status: 503, body: JSON.stringify({ error: text }) });
  await page.goto(APP);
  await page.getByRole("link", { name: "Setup", exact: true }).click();
  await expect(page.getByTestId("start-error")).toContainText(text);
});

test("a network failure shows a specific message", async ({ page, expectError }) => {
  expectError("network failure");
  await stub(page);
  await page.route(`${API}/get-started`, (route) => route.abort("connectionrefused"));
  await page.goto(APP);
  await page.getByRole("link", { name: "Setup", exact: true }).click();
  await expect(page.getByTestId("start-error")).toContainText("Could not reach the platform API");
});
