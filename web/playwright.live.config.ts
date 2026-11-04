import { defineConfig, devices } from "@playwright/test";

// The research-frontend run (`make rf-run`): one spec against the hosted app. No web
// server is started and nothing is routed; the tokens, URL and output directory come from the
// environment `rf_run.py` sets. It is not part of `make web-e2e` (that config ignores e2e/live).
export default defineConfig({
  // The live smoke and research-frontend specs, and the manual walk-through (LIVE=1, `make live-ux-playtest`).
  testDir: "e2e",
  testMatch: [/\/live\/.*\.spec\.ts$/, /ux-playtest\.spec\.ts$/],
  forbidOnly: true,
  retries: 0,
  workers: 1,
  // The row budgets sum to under 200 s (rf_run.py judges each one); the test ceiling is well above
  // that, and a missing element fails its own step after 30 s with the locator named.
  timeout: 600_000,
  expect: { timeout: 30_000 },
  reporter: [["list"]],
  outputDir: process.env.RF_OUT ? `${process.env.RF_OUT}/pw-output` : "test-results-live",
  use: {
    ...devices["Desktop Chrome"],
    actionTimeout: 30_000,
    navigationTimeout: 60_000,
    headless: true,
    viewport: { width: 1440, height: 900 },
  },
});
