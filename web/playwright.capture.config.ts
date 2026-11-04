import { defineConfig, devices } from "@playwright/test";

// Baseline captures of the app (spec: e2e/capture/capture.spec.ts). Never part of `make web-e2e`:
// the browse project of playwright.config.ts ignores e2e/capture. Fixture mode only, both builds:
// the plain build on WEB_PORT and the sign-in build on WEB_PORT + 1, which every signed-in page
// (e2e/support.ts :: APP) uses.
const port = process.env.WEB_PORT ?? "5183";
const url = `http://localhost:${port}`;
const authPort = String(Number(port) + 1);
const authUrl = `http://localhost:${authPort}`;
const authBuild = "node_modules/.e2e-auth";

export default defineConfig({
  testDir: "e2e/capture",
  forbidOnly: true,
  retries: 0,
  workers: 1,
  timeout: 60_000,
  reporter: [["list"]],
  use: {
    ...devices["Desktop Chrome"],
    baseURL: url,
    headless: true,
    viewport: { width: 1440, height: 900 },
    trace: "off",
  },
  webServer: [
    {
      url,
      reuseExistingServer: false,
      command: `npm run build && npx vite preview --port ${port} --strictPort`,
      timeout: 120_000,
    },
    {
      url: authUrl,
      reuseExistingServer: false,
      command: `npx vite build --outDir ${authBuild} --emptyOutDir && npx vite preview --outDir ${authBuild} --port ${authPort} --strictPort`,
      env: {
        VITE_COGNITO_DOMAIN: "auth.asteroidea.test",
        VITE_COGNITO_CLIENT_ID: "e2e-client",
        VITE_API_URL: "https://api.asteroidea.test",
      },
      timeout: 120_000,
    },
  ],
});
