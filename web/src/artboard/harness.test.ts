// The checker's own teeth: each check fails, with its specific message, on markup that breaks it.
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { ab, check, type Expectation, parse } from "./harness";

const EXP: Expectation = {
  allow: ["div", "span", "canvas", "svg", "rect"],
  counts: [
    { noun: "unit rows", n: 44, match: ab("unit-row") },
    { noun: "dashed links", n: 1, match: ab("link", { "data-ab-line": "dashed" }) },
  ],
  texts: ["Right amygdala"],
  canvasRows: 44,
};

function markup(
  opts: { rows?: number; extra?: string; line?: string; rect?: object } = {},
): string {
  const rows = Array.from({ length: opts.rows ?? 44 }, (_, i) =>
    createElement("span", { key: i, "data-ab": "unit-row" }, "Right amygdala"),
  );
  return renderToStaticMarkup(
    createElement(
      "div",
      null,
      ...rows,
      createElement("span", { "data-ab": "link", "data-ab-line": opts.line ?? "dashed" }),
      createElement("canvas", { "data-ab-standin": "heatmap", "data-ab-rows": 44 }),
      createElement(
        "svg",
        null,
        createElement("rect", opts.rect ?? { x: 0, y: 0, width: 4, height: 10 }),
      ),
      opts.extra === undefined ? null : createElement(opts.extra),
    ),
  );
}

describe("artboard check", () => {
  it("passes complete markup", () => {
    expect(check(markup(), EXP)).toEqual([]);
  });

  it("fails a dropped unit with the count", () => {
    expect(check(markup({ rows: 43 }), EXP)).toEqual(["expected 44 unit rows, got 43"]);
  });

  it("fails a pending link drawn solid", () => {
    expect(check(markup({ line: "solid" }), EXP)).toEqual(["expected 1 dashed links, got 0"]);
  });

  it("fails an element outside the allowlist", () => {
    expect(check(markup({ extra: "iframe" }), EXP)).toEqual(["unexpected elements: iframe"]);
  });

  it("fails NaN or missing geometry", () => {
    expect(check(markup({ rect: { x: "NaN", y: 0, width: 4 } }), EXP)).toEqual([
      '<rect> x="NaN" is not a number',
      "<rect> without height",
    ]);
  });

  it("fails a missing label and a canvas without its stand-in marks", () => {
    const html = renderToStaticMarkup(createElement("div", null, createElement("canvas")));
    expect(check(html, { allow: ["div", "canvas"], counts: [], texts: ["x"] })).toEqual([
      'missing text: "x"',
      "<canvas> without data-ab-standin",
      "<canvas> without integer data-ab-rows",
    ]);
  });

  it("parses nesting and decodes text", () => {
    const { els, text } = parse('<div a="1"><span>R &amp; L</span><br/></div>');
    expect(els.map((e) => [e.tag, e.parent?.tag ?? null])).toEqual([
      ["div", null],
      ["span", "div"],
      ["br", "div"],
    ]);
    expect(text).toBe("R & L");
  });
});
