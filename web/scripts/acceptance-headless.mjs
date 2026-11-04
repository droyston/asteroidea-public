// Headless stand-in for the guided web step of `make acceptance` (HEADLESS=1).
//
// Substitutions, both labelled by the caller: the access token is injected into sessionStorage
// instead of signing in through managed login, and GET /uploads is forwarded from Node with the
// page's origin allowed, because the API's CORS list holds only http://localhost:5173 and this run
// uses a free port. Prints one JSON line. Env: WEB_PORT, VITE_API_URL, ACCEPTANCE_TOKENS (path to
// the SDK's tokens.json), ACCEPTANCE_SHOT (png path).
import { readFileSync } from "node:fs";
import { chromium } from "@playwright/test";

const { WEB_PORT, VITE_API_URL, ACCEPTANCE_TOKENS, ACCEPTANCE_SHOT } = process.env;
const origin = `http://localhost:${WEB_PORT}`;
const { tokens } = JSON.parse(readFileSync(ACCEPTANCE_TOKENS, "utf8"));
const cors = {
  "access-control-allow-origin": origin,
  "access-control-allow-headers": "authorization,content-type",
  "access-control-allow-methods": "GET,POST,OPTIONS",
};

async function forward(route) {
  if (route.request().method() === "OPTIONS") {
    await route.fulfill({ status: 204, headers: cors });
    return;
  }
  try {
    const response = await route.fetch();
    await route.fulfill({ response, headers: { ...response.headers(), ...cors } });
  } catch {
    // The context closed while a request was in flight; nothing to answer.
  }
}

const browser = await chromium.launch({ headless: true });
try {
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  await context.addInitScript(
    (t) => {
      sessionStorage.setItem(
        "asteroidea.auth.tokens/1",
        JSON.stringify({ access_token: t.access, refresh_token: t.refresh }),
      );
    },
    { access: tokens.access_token, refresh: tokens.refresh_token },
  );
  const page = await context.newPage();
  await page.route(`${VITE_API_URL}/**`, forward);
  const started = Date.now();
  await page.goto(`${origin}/uploads`);
  await page.getByText("Signed in").first().waitFor({ timeout: 30000 });
  await page.getByText("My uploads").first().waitFor({ timeout: 30000 });
  await page.getByTestId("uploads-table").waitFor({ timeout: 30000 });
  const seconds = (Date.now() - started) / 1000;
  const rows = await page.locator("[data-testid=uploads-table] tbody tr").allInnerTexts();
  if (ACCEPTANCE_SHOT) await page.screenshot({ path: ACCEPTANCE_SHOT });
  await page.waitForTimeout(1000);
  console.log(JSON.stringify({ seconds, rows }));
} finally {
  await browser.close();
}
