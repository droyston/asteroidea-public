import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { Dialog, trapIndex } from "./Dialog";

describe("trapIndex", () => {
  it("Tab moves forward and wraps from the last control to the first", () => {
    expect(trapIndex(0, 3, false)).toBe(1);
    expect(trapIndex(2, 3, false)).toBe(0);
  });

  it("Shift+Tab moves back and wraps from the first control to the last", () => {
    expect(trapIndex(1, 3, true)).toBe(0);
    expect(trapIndex(0, 3, true)).toBe(2);
  });

  it("focus outside the controls enters at the first (Tab) or the last (Shift+Tab)", () => {
    expect(trapIndex(-1, 3, false)).toBe(0);
    expect(trapIndex(-1, 3, true)).toBe(2);
  });

  it("no controls: nowhere to go", () => {
    expect(trapIndex(-1, 0, false)).toBe(-1);
  });
});

describe("Dialog", () => {
  const render = (open: boolean) =>
    renderToStaticMarkup(
      createElement(
        Dialog,
        { open, title: "Leave a note", onClose: () => undefined, testId: "d" },
        createElement("button", { type: "button" }, "Inside"),
      ),
    );

  it("is a modal dialog labelled by its title", () => {
    const html = render(true);
    expect(html).toMatch(/^<dialog class="dialog" aria-labelledby="([^"]+)" aria-modal="true"/);
    const id = /aria-labelledby="([^"]+)"/.exec(html)?.[1];
    expect(html).toContain(`<h2 id="${id}" class="dialog-title">Leave a note</h2>`);
    expect(html).toContain("Inside");
  });

  it("renders no content while closed (nothing to tab into)", () => {
    expect(render(false)).not.toContain("Inside");
  });
});
