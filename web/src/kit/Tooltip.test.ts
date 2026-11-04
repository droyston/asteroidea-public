import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { afterKey, Tooltip } from "./Tooltip";

describe("Tooltip", () => {
  const render = (initialOpen: boolean) =>
    renderToStaticMarkup(
      createElement(Tooltip, {
        text: "Copy the SDK call",
        initialOpen,
        trigger: createElement("button", { type: "button" }, "Copy code"),
      }),
    );

  it("the trigger is described by the tip, which is role=tooltip", () => {
    const html = render(false);
    const id = /aria-describedby="([^"]+)"/.exec(html)?.[1];
    expect(id).toBeDefined();
    expect(html).toContain(
      `<span id="${id}" role="tooltip" class="tooltip" hidden="">Copy the SDK call</span>`,
    );
    expect(html).toContain(">Copy code</button>");
  });

  it("starts hidden; shown when open", () => {
    expect(render(false)).toContain('hidden=""');
    expect(render(true)).not.toContain("hidden");
  });

  it("Esc hides an open tip; other keys leave it", () => {
    expect(afterKey(true, "Escape")).toBe(false);
    expect(afterKey(true, "Tab")).toBe(true);
    expect(afterKey(false, "a")).toBe(false);
  });
});
