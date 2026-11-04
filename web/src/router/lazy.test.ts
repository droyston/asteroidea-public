import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";
import { loadChunk, reloadPage, renderChunk } from "./lazy";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("lazy route chunks", () => {
  it("a failing import renders the reload Notice with a Reload button", async () => {
    const result = await loadChunk(() =>
      Promise.reject(new Error("Failed to fetch dynamically imported module")),
    );
    expect(result).toEqual({ kind: "stale" });
    const html = renderToStaticMarkup(
      createElement(
        "div",
        null,
        renderChunk(result, () => "the-page"),
      ),
    );
    expect(html).toContain("The app was updated. Reload to continue.");
    expect(html).toContain('role="alert"');
    expect(html).toMatch(/<button[^>]*>Reload<\/button>/);
    expect(html).not.toContain("the-page");
  });

  it("a working import renders the page and no Notice", async () => {
    const result = await loadChunk(() => Promise.resolve({ title: "Files" }));
    const html = renderToStaticMarkup(
      createElement(
        "div",
        null,
        renderChunk(result, (m) => m.title),
      ),
    );
    expect(html).toContain("Files");
    expect(html).not.toContain("Reload");
  });

  it("Reload reloads the page", () => {
    const reload = vi.fn();
    vi.stubGlobal("location", { reload });
    reloadPage();
    expect(reload).toHaveBeenCalledOnce();
  });
});
