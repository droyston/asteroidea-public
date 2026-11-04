// Dialog: a small modal on the native <dialog> element. `showModal()` makes the
// rest of the page inert; Esc closes (the dialog's `cancel` event calls `onClose`); Tab and Shift+Tab
// wrap inside the dialog; on close, focus returns to the element that had it when the dialog opened.
// The first focusable control (or the one marked `data-autofocus`) takes focus on open.
import { type KeyboardEvent, type ReactNode, useEffect, useId, useRef } from "react";

export interface DialogProps {
  open: boolean;
  title: string;
  /** A small-capitals line above the title ("Session ended"). */
  eyebrow?: string;
  onClose: () => void;
  children?: ReactNode;
  testId?: string;
}

const FOCUSABLE =
  'button:not([disabled]), [href], input:not([disabled]), textarea:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])';

/**
 * The index Tab (or Shift+Tab) moves focus to among `count` focusable controls, from `current` (-1 when
 * focus is outside them): it wraps at both ends, so focus never leaves the dialog.
 */
export function trapIndex(current: number, count: number, back: boolean): number {
  if (count === 0) return -1;
  if (current < 0) return back ? count - 1 : 0;
  return back ? (current - 1 + count) % count : (current + 1) % count;
}

export function Dialog({ open, title, eyebrow, onClose, children, testId }: DialogProps) {
  const ref = useRef<HTMLDialogElement>(null);
  const opener = useRef<Element | null>(null);
  const titleId = useId();

  useEffect(() => {
    const dialog = ref.current;
    if (dialog === null) return;
    if (open && !dialog.open) {
      opener.current = document.activeElement;
      dialog.showModal();
      const first =
        dialog.querySelector<HTMLElement>("[data-autofocus]") ??
        dialog.querySelector<HTMLElement>(FOCUSABLE);
      first?.focus();
    } else if (!open && dialog.open) {
      dialog.close();
    }
  }, [open]);

  // Return focus after close, whichever way it closed.
  useEffect(() => {
    if (open) return;
    const back = opener.current;
    opener.current = null;
    if (back instanceof HTMLElement) back.focus();
  }, [open]);

  const onKeyDown = (event: KeyboardEvent<HTMLDialogElement>) => {
    if (event.key !== "Tab" || ref.current === null) return;
    const items = Array.from(ref.current.querySelectorAll<HTMLElement>(FOCUSABLE));
    const next = trapIndex(
      items.indexOf(document.activeElement as HTMLElement),
      items.length,
      event.shiftKey,
    );
    event.preventDefault();
    items[next]?.focus();
  };

  return (
    <dialog
      ref={ref}
      className="dialog"
      aria-labelledby={titleId}
      aria-modal="true"
      data-testid={testId}
      onCancel={(event) => {
        event.preventDefault();
        onClose();
      }}
      onKeyDown={onKeyDown}
    >
      {eyebrow === undefined ? null : <p className="dialog-eyebrow">{eyebrow}</p>}
      <h2 id={titleId} className="dialog-title">
        {title}
      </h2>
      {open ? children : null}
    </dialog>
  );
}
