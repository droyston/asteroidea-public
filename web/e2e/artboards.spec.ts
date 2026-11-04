// The artboard screenshots and browser gates (`make web-artboards` only, never under
// web-e2e). For each real surface, at each of its viewports, light and dark: open the real app on the
// surface's route with CATALOG_FIXTURE served by page.route, wait for its ready mark, write a
// screenshot named after its surface, and gate: no vertical scroll at 1440x900 where the surface says
// so (the data home and the session heatmap, including the 152-unit session), no sideways scroll at
// 375. A pending surface is skipped. Each screenshot is meant to be compared by eye with its reference image, over
// web/artboards/index.html.
import { existsSync, mkdirSync, writeFileSync } from "node:fs";
import { join, relative, resolve } from "node:path";
import { expect, type Page } from "@playwright/test";
import {
  datasetsListing,
  CATALOG_FIXTURE as F,
  RATES_CS62,
  rates152,
  UNITS_152,
  UNITS_CS62,
} from "../src/artboard/fixture";
import type { Surface, Viewport } from "../src/artboard/harness";
import { SURFACES } from "../src/artboard/surfaces";
import { ID_TOKEN, PLACES, USER_EMAIL } from "../src/artboard/surfaces/shell";
import { API, AUTH, CORS, stubBase, test } from "./support";

test.skip(process.env.ARTBOARDS !== "1", "runs under make web-artboards only");

const WEB = resolve(new URL(".", import.meta.url).pathname, "..");
const OUT = process.env.ARTBOARD_OUT ?? join(WEB, "artboards");
const PNG = join(OUT, "png");
const BOARDS = resolve(WEB, "../.agents/design/web-ux/handoff-1");
test.skip(!existsSync(BOARDS), "the design boards (.agents/design/web-ux/handoff-1) are absent");
const THEMES = ["light", "dark"] as const;
const SIZES: Record<Viewport, { width: number; height: number }> = {
  "1440x900": { width: 1440, height: 900 },
  "375x812": { width: 375, height: 812 },
};

function defaults(surface: Surface): Record<string, unknown> {
  const s = F.session;
  const rows = surface.id === "session-152" ? UNITS_152 : UNITS_CS62;
  return {
    "/catalog/studies": F.studies,
    "/catalog/links": F.links,
    "/catalog/datasets": datasetsListing(F.studies.datasets),
    [`/catalog/assets/${s.asset_id}/tables/units`]: rows,
    "/compute/rates": surface.id === "session-152" ? rates152() : RATES_CS62,
    "/uploads": { uploads: [] },
    // The home's Latest results card: no results, so no card, unless the surface serves some.
    "/artifacts": { artifacts: [] },
    ...surface.api,
  };
}

async function serve(page: Page, surface: Surface): Promise<string[]> {
  await stubBase(page);
  // Registered after stubBase, so it wins: the ID token carries the fixture user's `email` claim.
  await page.route(`${AUTH}/oauth2/token`, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      headers: CORS,
      body: JSON.stringify({
        access_token: "access-1",
        refresh_token: "refresh-1",
        id_token: ID_TOKEN,
      }),
    }),
  );
  const bodies = defaults(surface);
  const unserved: string[] = [];
  await page.route(`${API}/**`, (route) => {
    const req = route.request();
    if (req.method() === "OPTIONS") return route.fulfill({ status: 204, headers: CORS });
    const path = new URL(req.url()).pathname;
    // A held path is never answered: the page stays in its loading state.
    if (surface.hold?.includes(path)) return undefined;
    const body = bodies[path];
    if (body === undefined) unserved.push(path);
    return route.fulfill({
      status: body === undefined ? 404 : (surface.status?.[path] ?? 200),
      contentType: "application/json",
      headers: CORS,
      body: JSON.stringify(body ?? { error: "not in the artboard fixture" }),
    });
  });
  return unserved;
}

const shots: { surface: Surface; theme: string; viewport: Viewport; file: string }[] = [];

for (const surface of SURFACES) {
  for (const viewport of surface.viewports) {
    for (const theme of THEMES) {
      test(`${surface.id} ${viewport} ${theme} `, async ({ page, expectError }) => {
        test.skip(surface.pending !== null, `surface pending ${surface.pending}`);
        // A refusal state is drawn from an error answer, which the browser logs.
        if (surface.status !== undefined) expectError(`state card ${surface.id}`);
        const size = SIZES[viewport];
        await page.setViewportSize(size);
        await page.emulateMedia({ colorScheme: theme });
        const unserved = await serve(page, surface);
        await page.goto(surface.route);
        if (surface.waitFor === undefined) {
          await expect(page.locator(`main[data-ready="${surface.ready}"]`)).toBeVisible();
        } else {
          await expect(page.locator(surface.waitFor)).toBeVisible();
          // A held request: let the elapsed time and the bar move before the shot.
          if (surface.hold !== undefined) await page.waitForTimeout(2400);
        }
        expect(unserved, "API paths the fixture does not serve").toEqual([]);

        if (surface.id === "shell") {
          const nav = page.getByRole("navigation", { name: "Main" }).getByRole("link");
          await expect(nav).toHaveText([...PLACES]);
          await expect(page.getByTestId("user-label")).toHaveText(USER_EMAIL);
        }

        mkdirSync(PNG, { recursive: true });
        const file = join(PNG, `${surface.board}.${surface.id}.${theme}.${size.width}.png`);
        await page.screenshot({ path: file });
        shots.push({ surface, theme, viewport, file });

        const m = await page.evaluate(() => ({
          sh: document.documentElement.scrollHeight,
          sw: document.documentElement.scrollWidth,
          ih: window.innerHeight,
          iw: window.innerWidth,
        }));
        if (viewport === "375x812") {
          expect(m.sw, "page scrolls sideways at 375").toBeLessThanOrEqual(m.iw);
        }
        if (surface.noScroll && viewport === "1440x900") {
          expect(m.sh, "page scrolls at 1440x900").toBeLessThanOrEqual(m.ih);
        }
      });
    }
  }
}

test.afterAll(() => {
  if (shots.length === 0) return;
  const cells = shots
    .map(({ surface, theme, viewport, file }) => {
      const reference = join(BOARDS, `boards-${theme}`, `${surface.board}.png`);
      return `<section><h2>${surface.id} &middot; ${viewport} &middot; ${theme}</h2><div class="pair"><figure><img alt="reference ${surface.board} ${theme}" src="${relative(OUT, reference)}"><figcaption>reference ${surface.board}</figcaption></figure><figure><img alt="app ${surface.id} ${theme} ${viewport}" src="${relative(OUT, file)}"><figcaption>app</figcaption></figure></div></section>`;
    })
    .join("\n");
  writeFileSync(
    join(OUT, "index.html"),
    `<!doctype html><meta charset="utf-8"><title>Artboards</title><style>body{font:14px system-ui;margin:16px}.pair{display:flex;gap:16px}figure{margin:0;flex:1}img{width:100%;border:1px solid #ccc}</style><p>Reference image on the left, app on the right.</p>\n${cells}\n`,
  );
});
