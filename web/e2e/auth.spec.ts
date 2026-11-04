// Opt-in sign-in and "My uploads". Runs only in fixture mode, against the second
// build that carries the VITE_* auth values (playwright.config.ts, project "auth"). Managed login,
// the token endpoint and GET /uploads are all served by page.route; nothing leaves the machine.
import { createHash } from "node:crypto";
import { expect, type Page } from "@playwright/test";
import { API, AUTH, CORS, fixture, golden, stubBase, test } from "./support";

test.skip(
  process.env.E2E_LIVE === "1",
  "needs the fixture-mode auth build (no live server has the auth env)",
);

const ROWS = [
  {
    session_id: "s2",
    dataset: "sample-20261023-b",
    relpath: "sub-01/anat/sub-01_T1w.nii",
    state: "COMMITTED",
    created_at: 1_792_000_100,
    asset_id: "asset-2",
  },
  {
    session_id: "s1",
    dataset: "sample-20261023-a",
    relpath: "sub-01/anat/sub-01_T1w.nii",
    state: "INCOMPLETE",
    created_at: 1_792_000_000,
    asset_id: null,
  },
];

interface Seen {
  authorize: URL[];
  tokens: URLSearchParams[];
  uploadAuth: (string | undefined)[];
}

type UploadsReply = { status: number; body: unknown };

/** Stub managed login, the token endpoint and /uploads. `uploads(n)` answers the n-th GET (0-based). */
async function stub(
  page: Page,
  uploads: (n: number) => UploadsReply,
  opts: { state?: (real: string) => string } = {},
): Promise<Seen> {
  const base = await stubBase(page, opts);
  const seen: Seen = { authorize: base.authorize, tokens: base.tokens, uploadAuth: [] };
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
  await page.route(`${API}/uploads`, (route) => {
    if (route.request().method() === "OPTIONS")
      return route.fulfill({ status: 204, headers: CORS });
    const reply = uploads(seen.uploadAuth.length);
    seen.uploadAuth.push(route.request().headers().authorization);
    return route.fulfill({
      status: reply.status,
      contentType: "application/json",
      headers: CORS,
      body: JSON.stringify(reply.body),
    });
  });
  return seen;
}

const ok = (): UploadsReply => ({ status: 200, body: { uploads: ROWS } });

// Sign-in first: opening the app with no token goes to managed login by itself.
// "My uploads" lives at /uploads; the return path after managed login is kept.
async function signIn(page: Page): Promise<void> {
  await page.goto("/uploads");
}

test("sign in with PKCE, return to /uploads, list my uploads newest first", async ({ page }) => {
  const seen = await stub(page, ok);
  await signIn(page);

  const table = page.getByTestId("uploads-table");
  await expect(table).toBeVisible();
  expect(new URL(page.url()).pathname).toBe("/uploads");
  await expect(page.getByText("Signed in")).toBeVisible();
  const rows = table.locator("tbody tr");
  await expect(rows).toHaveCount(2);
  await expect(rows.nth(0)).toContainText("sample-20261023-b");
  await expect(rows.nth(0)).toContainText("In the catalog");
  await expect(rows.nth(0)).toContainText("2026-10-14");
  await expect(rows.nth(1)).toContainText("Did not finish");

  // The authorize request is PKCE S256 with a state; the token request carries the matching verifier.
  const authorize = seen.authorize[0];
  expect(authorize?.searchParams.get("code_challenge_method")).toBe("S256");
  expect(authorize?.searchParams.get("client_id")).toBe("e2e-client");
  expect(authorize?.searchParams.get("state")).toBeTruthy();
  const token = seen.tokens[0];
  expect(token?.get("grant_type")).toBe("authorization_code");
  expect(token?.get("code")).toBe("e2e-code");
  const verifier = token?.get("code_verifier") ?? "";
  expect(createHash("sha256").update(verifier).digest("base64url")).toBe(
    authorize?.searchParams.get("code_challenge"),
  );
  expect(seen.uploadAuth).toEqual(["Bearer access-1"]);

  // The dataset browser is still there, under Find.
  await page.getByRole("link", { name: "Find", exact: true }).click();
  await expect(page.getByTestId("match-count")).toBeVisible();
});

test("no token: redirected to managed login before the index is requested", async ({ page }) => {
  let indexRequests = 0;
  const seen = await stub(page, ok);
  await page.route("**/index.json", (route) => {
    indexRequests += 1;
    return route.fulfill({ status: 200, contentType: "application/json", body: golden });
  });
  const authorize = page.waitForRequest(`${AUTH}/oauth2/authorize*`);
  await page.goto("/discover");
  await authorize;
  expect(indexRequests).toBe(0);
  await expect(page.getByTestId("match-count")).toBeVisible();
  expect(seen.authorize).toHaveLength(1);
  expect(indexRequests).toBe(1);
});

test("tokens stay out of localStorage", async ({ page }) => {
  await stub(page, ok);
  await signIn(page);
  await expect(page.getByTestId("uploads-table")).toBeVisible();
  const stored = await page.evaluate(() => ({
    local: Object.keys(localStorage).filter((k) => localStorage.getItem(k)?.includes("access-1")),
    session: Object.keys(sessionStorage).length,
  }));
  expect(stored.local).toEqual([]);
  expect(stored.session).toBeGreaterThan(0);
});

test("401 refreshes once, then retries with the new token", async ({ page, expectError }) => {
  expectError("401 before the refresh");
  const seen = await stub(page, (n) => (n === 0 ? { status: 401, body: {} } : ok()));
  await signIn(page);
  await expect(page.getByTestId("uploads-table")).toBeVisible();
  expect(seen.tokens.map((t) => t.get("grant_type"))).toEqual([
    "authorization_code",
    "refresh_token",
  ]);
  expect(seen.tokens[1]?.get("refresh_token")).toBe("refresh-1");
  expect(seen.uploadAuth).toEqual(["Bearer access-1", "Bearer access-2"]);
});

test("a 401 after the refresh ends the session in a dialog over the same page", async ({
  page,
  expectError,
}) => {
  expectError("401 after the refresh");
  const seen = await stub(page, () => ({ status: 401, body: {} }));
  await signIn(page);
  const dialog = page.getByRole("dialog", { name: "Sign in again to carry on" });
  await expect(dialog).toBeVisible();
  await expect(dialog).toContainText("Session ended");
  await expect(dialog).toContainText("with the same view. Your tray is kept.");
  // The page stays under the dialog, at the same address; the dialog's Sign in is the one control.
  const url = page.url();
  await expect(page.getByTestId("uploads-panel")).toHaveCount(1);
  await page.keyboard.press("Escape");
  await expect(dialog).toBeVisible();
  expect(page.url()).toBe(url);
  await expect(page.getByRole("button", { name: "Sign in" })).toHaveCount(1);
  expect(seen.tokens.filter((t) => t.get("grant_type") === "refresh_token")).toHaveLength(1);
  expect(seen.uploadAuth).toHaveLength(2);
  // Sign in goes through managed login and comes back to the same address.
  const before = seen.authorize.length;
  await dialog.getByRole("button", { name: "Sign in" }).click();
  await expect.poll(() => seen.authorize.length).toBe(before + 1);
  await expect(page).toHaveURL(url);
});

test("an account outside the researchers group gets a message", async ({ page, expectError }) => {
  expectError("403 outside the group");
  await stub(page, () => ({ status: 403, body: { message: "Forbidden" } }));
  await signIn(page);
  await expect(page.getByTestId("uploads-error")).toContainText("not in the researchers group");
});

test("an API failure shows a message and Try again recovers", async ({ page, expectError }) => {
  expectError("API failure then retry");
  await stub(page, (n) => (n === 0 ? { status: 500, body: {} } : ok()));
  await signIn(page);
  const error = page.getByTestId("uploads-error");
  await expect(error).toContainText("HTTP 500");
  await error.getByRole("button", { name: "Try again" }).click();
  await expect(page.getByTestId("uploads-table")).toBeVisible();
});

test("no uploads shows the empty state", async ({ page }) => {
  await stub(page, () => ({ status: 200, body: { uploads: [] } }));
  await signIn(page);
  await expect(page.getByTestId("uploads-empty")).toContainText("No uploads of yours yet");
});

test("a state mismatch is refused before any token request", async ({ page, expectError }) => {
  expectError("state mismatch refusal");
  const seen = await stub(page, ok, { state: () => "forged" });
  await signIn(page);
  await expect(page.getByTestId("auth-notice")).toContainText("state mismatch");
  expect(seen.tokens).toHaveLength(0);
  expect(seen.uploadAuth).toHaveLength(0);
  await expect(page.getByTestId("uploads-panel")).toHaveCount(0);
});

test("a build without the auth values shows no Sign in control", async ({ page }) => {
  // The default fixture build (WEB_PORT) has no VITE_* values.
  await page.route("**/index.json", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: golden }),
  );
  await page.goto(`http://localhost:${process.env.WEB_PORT ?? "5173"}/discover`);
  await expect(page.getByTestId("match-count")).toBeVisible();
  await expect(page.getByRole("button", { name: "Sign in" })).toHaveCount(0);
});

test("a fresh load of /uploads sets the ready mark once the panel has drawn", async ({ page }) => {
  await stub(page, ok);
  await signIn(page);
  await expect(page.getByTestId("uploads-table")).toBeVisible();
  await expect(page.locator('main[data-ready="uploads"]')).toBeVisible();
});

test("a signed-in tab that opens /uploads directly gets the ready mark", async ({ page }) => {
  await stub(page, ok);
  await page.addInitScript(() => {
    sessionStorage.setItem(
      "asteroidea.auth.tokens/1",
      JSON.stringify({ access_token: "access-1", refresh_token: "refresh-1" }),
    );
  });
  await page.goto("/uploads");
  await expect(page.getByTestId("uploads-table")).toBeVisible();
  await expect(page.locator('main[data-ready="uploads"]')).toBeVisible();
});

test("the home no longer carries the uploads panel", async ({ page }) => {
  await stub(page, ok);
  await page.goto("/");
  await expect(page.locator('main[data-ready="home"]')).toBeVisible();
  await expect(page.getByTestId("uploads-panel")).toHaveCount(0);
});
