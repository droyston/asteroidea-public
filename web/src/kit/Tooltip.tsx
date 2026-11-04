// Tooltip: a short label for a control, shown on hover and on keyboard focus,
// hidden on Esc, mouse leave and blur. The tip is `role="tooltip"` and the trigger names it with
// `aria-describedby`, so a screen reader reads it with the control. The tip never holds a control:
// anything to click belongs in a Popover.
import { cloneElement, type ReactElement, useId, useState } from "react";

export interface TooltipProps {
  text: string;
  /** One focusable element (a button or a link); it gets the describedby and the show/hide handlers. */
  trigger: ReactElement<Record<string, unknown>>;
  /** Start shown (for the artboard and tests). */
  initialOpen?: boolean;
}

/** What a key does to an open tip: Esc hides it, anything else leaves it. */
export function afterKey(open: boolean, key: string): boolean {
  return key === "Escape" ? false : open;
}

export function Tooltip({ text, trigger: child, initialOpen = false }: TooltipProps) {
  const id = useId();
  const [open, setOpen] = useState(initialOpen);
  // The child's own handlers still run, before the tip's.
  const chain =
    (name: string, then: (event: { key?: string }) => void) => (event: { key?: string }) => {
      const own = child.props[name];
      if (typeof own === "function") own(event);
      then(event);
    };
  const trigger = cloneElement(child, {
    "aria-describedby": id,
    onMouseEnter: chain("onMouseEnter", () => setOpen(true)),
    onMouseLeave: chain("onMouseLeave", () => setOpen(false)),
    onFocus: chain("onFocus", () => setOpen(true)),
    onBlur: chain("onBlur", () => setOpen(false)),
    onKeyDown: chain("onKeyDown", (event) => setOpen((o) => afterKey(o, event.key ?? ""))),
  });
  return (
    <span className="tooltip-anchor">
      {trigger}
      <span id={id} role="tooltip" className="tooltip" hidden={!open}>
        {text}
      </span>
    </span>
  );
}
