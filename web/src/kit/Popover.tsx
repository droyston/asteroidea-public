// Popover: a small, non-modal box anchored to a point inside a positioned
// parent, for naming a drafted span in place. Esc cancels (`onClose`), from anywhere inside it. The
// control marked `data-autofocus` (else the first input) takes focus on open; on close, focus returns to
// the element that had it. The box stays inside the parent's width.
import { type KeyboardEvent, type ReactNode, useEffect, useId, useRef } from "react";

export interface PopoverProps {
  title: string;
  /** The anchor's x in the parent, px. */
  x: number;
  /** The anchor's y in the parent, px: the box's top edge. */
  y: number;
  /** The parent's width, px. */
  bound: number;
  width?: number;
  onClose: () => void;
  children?: ReactNode;
  testId?: string;
}

export const POPOVER_W = 272;

/** The box's left edge: centred on `x`, then kept inside [0, bound]. */
export function popoverLeft(x: number, width: number, bound: number): number {
  return Math.max(0, Math.min(x - width / 2, bound - width));
}

export function Popover({
  title,
  x,
  y,
  bound,
  width = POPOVER_W,
  onClose,
  children,
  testId,
}: PopoverProps) {
  const ref = useRef<HTMLDivElement>(null);
  const titleId = useId();
  useEffect(() => {
    const opener = document.activeElement;
    const box = ref.current;
    const first =
      box?.querySelector<HTMLElement>("[data-autofocus]") ??
      box?.querySelector<HTMLElement>("input, textarea, button");
    first?.focus();
    return () => {
      if (opener instanceof HTMLElement && opener.isConnected) opener.focus();
    };
  }, []);
  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.key !== "Escape") return;
    e.preventDefault();
    e.stopPropagation();
    onClose();
  };
  const w = Math.min(width, bound);
  return (
    <div
      ref={ref}
      className="popover"
      role="dialog"
      aria-labelledby={titleId}
      data-testid={testId}
      style={{ left: popoverLeft(x, w, bound), top: y, width: w }}
      onKeyDown={onKeyDown}
    >
      <div className="popover-head">
        <strong id={titleId}>{title}</strong>
        <span className="meta" aria-hidden="true">
          Esc
        </span>
      </div>
      {children}
    </div>
  );
}
