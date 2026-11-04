// The artifact writer, gated out of the default run: `ARTBOARD_WRITE=1` writes each surface's markup
// to `ARTBOARD_OUT` (default web/artboards/html/), one file per surface, wrapped with the app's tokens
// and kit CSS so a human can open it. `make web-artboards` runs it before the screenshots.
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { renderSurface } from "./harness";
import { SURFACES } from "./surfaces";

const WEB = resolve(new URL(".", import.meta.url).pathname, "../..");
const OUT = process.env.ARTBOARD_OUT ?? join(WEB, "artboards", "html");
const CSS = ["src/kit/tokens.css", "src/kit/kit.css", "src/app.css"]
  .map((p) => readFileSync(join(WEB, p), "utf8"))
  .join("\n");

describe("artboard writer", () => {
  it.skipIf(process.env.ARTBOARD_WRITE !== "1")("writes every surface's markup", () => {
    mkdirSync(OUT, { recursive: true });
    for (const s of SURFACES) {
      const html = renderSurface(s);
      const page = `<!doctype html><html lang="en"><head><meta charset="utf-8"><title>${s.id}</title><style>${CSS}</style></head><body data-artboard="${s.id}" data-board="${s.board}" data-pending="${s.pending ?? ""}">${html}</body></html>\n`;
      writeFileSync(join(OUT, `${s.id}.html`), page);
    }
    console.info(`artboards: wrote ${SURFACES.length} surfaces to ${OUT}`);
    expect(SURFACES.length).toBeGreaterThan(0);
  });
});
