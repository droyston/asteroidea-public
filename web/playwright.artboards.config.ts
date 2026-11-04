import { defineConfig, devices } from "@playwright/test";

// The artboard screenshots (`make web-artboards`): every registered surface at its
// viewports, light and dark, written beside the reference PNG names under ARTBOARD_OUT (default
// web/artboards/). Fixture mode only, on the sign-in build (every surface is a signed-in page), served
// on WEB_PORT; the API, managed login and the index are served by page.route. Not part of web-e2e.
const port = process.env.WEB_PORT ?? "5193";
const url = `http://localhost:${port}`;
const build = "node_modules/.e2e-artboards";

export default defineConfig({
  testDir: "e2e",
  testMatch: /artboards\.spec\.ts/,
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
  projects: [{ name: "artboards" }],
  webServer: {
    url,
    reuseExistingServer: false,
    command: `npx vite build --outDir ${build} --emptyOutDir && npx vite preview --outDir ${build} --port ${port} --strictPort`,
    env: {
      VITE_COGNITO_DOMAIN: "auth.asteroidea.test",
      VITE_COGNITO_CLIENT_ID: "e2e-client",
      VITE_API_URL: "https://api.asteroidea.test",
    },
    timeout: 120_000,
  },
});
