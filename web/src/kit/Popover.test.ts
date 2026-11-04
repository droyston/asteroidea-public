import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { Popover, popoverLeft } from "./Popover";

describe("popoverLeft", () => {
  it("centres the box on the anchor", () => {
    expect(popoverLeft(500, 200, 1000)).toBe(400);
  });

  it("keeps the box inside the left edge", () => {
    expect(popoverLeft(20, 200, 1000)).toBe(0);
  });

  it("keeps the box inside the right edge", () => {
    expect(popoverLeft(990, 200, 1000)).toBe(800);
  });
});

describe("Popover", () => {
  const html = renderToStaticMarkup(
    createElement(
      Popover,
      {
        title: "New label · 2–4 s",
        x: 100,
        y: 40,
        bound: 600,
        onClose: () => undefined,
        testId: "p",
      },
      createElement("input", { type: "text" }),
    ),
  );

  it("is a dialog labelled by its title, with the Esc hint", () => {
    const id = /aria-labelledby="([^"]+)"/.exec(html)?.[1];
    expect(html).toContain('role="dialog"');
    expect(html).toContain(`<strong id="${id}">New label · 2–4 s</strong>`);
    expect(html).toContain("Esc");
  });

  it("sits at the anchor, inside its parent", () => {
    expect(html).toContain("left:0;top:40px;width:272px");
  });
});
