import { defineConfig, devices } from "@playwright/test";

// Two server modes. Each run says which server it covers.
// - Live (E2E_LIVE=1): reuse the `make web` Vite dev server at WEB_PORT, the server a developer
//   opens. Playwright 1.64.1 requires `command`; it only runs when that server is down, and then
//   it fails at once instead of starting a different server.
// - Fixture (default): build, then serve web/dist with `vite preview`; specs route index.json
//   to the golden index. Covers the production build, not the dev server; not acceptance by hand.
const port = process.env.WEB_PORT ?? "5173";
const url = `http://localhost:${port}`;
const live = process.env.E2E_LIVE === "1";
// Sign-in is opt-in at build time. The sign-in spec needs a build that carries the
// VITE_* values, so fixture mode serves a second build on WEB_PORT + 1; every other spec runs on
// the build without them, exactly as before. The domain and API are `.test` names that the spec
// intercepts; nothing leaves the machine.
const authPort = String(Number(port) + 1);
const authUrl = `http://localhost:${authPort}`;
const authBuild = "node_modules/.e2e-auth";

export default defineConfig({
  testDir: "e2e",
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
    trace: "retain-on-failure",
  },
  projects: [
    { name: "browse", testIgnore: [/auth\.spec\.ts/, /\/live\//, /\/capture\//] },
    { name: "auth", testMatch: /auth\.spec\.ts/, use: { baseURL: authUrl } },
  ],
  webServer: live
    ? {
        url,
        reuseExistingServer: true,
        command: 'echo "start the server with make web first" >&2; exit 1',
        timeout: 10_000,
      }
    : [
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
