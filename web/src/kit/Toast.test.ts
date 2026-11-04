import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { dismissToast, pushed, TOAST_MAX, ToastHost, toast, toasts } from "./Toast";

describe("toast queue", () => {
  it("adds newest last and keeps at most TOAST_MAX", () => {
    let list: ReturnType<typeof toasts> = [];
    for (let id = 1; id <= TOAST_MAX + 2; id++)
      list = pushed(list, { id, text: `t${id}`, kind: "info" });
    expect(list.map((t) => t.id)).toEqual([3, 4, 5]);
  });

  it("toast() shows a message and dismissToast() removes only that one", () => {
    const a = toast("Copied details");
    const b = toast("Copied code");
    expect(toasts().map((t) => t.text)).toEqual(
      expect.arrayContaining(["Copied details", "Copied code"]),
    );
    dismissToast(a);
    expect(toasts().some((t) => t.id === a)).toBe(false);
    expect(toasts().some((t) => t.id === b)).toBe(true);
    dismissToast(b);
    dismissToast(b);
    expect(toasts().some((t) => t.id === b)).toBe(false);
  });
});

describe("ToastHost", () => {
  const html = renderToStaticMarkup(
    createElement(ToastHost, {
      items: [
        { id: 1, text: "Copied", kind: "info" },
        { id: 2, text: "Could not copy", kind: "error" },
      ],
    }),
  );

  it("announces info politely and errors assertively", () => {
    expect(html).toMatch(
      /<div aria-live="polite" class="toast-stack"><div class="toast toast-info"[^>]*><span class="toast-text">Copied<\/span>/,
    );
    expect(html).toMatch(
      /<div aria-live="assertive" class="toast-stack"><div class="toast toast-error"[^>]*><span class="toast-text">Could not copy/,
    );
  });

  it("every toast has a labelled Dismiss button", () => {
    expect(
      html.match(/<button type="button" class="toast-close" aria-label="Dismiss">/g),
    ).toHaveLength(2);
  });
});
