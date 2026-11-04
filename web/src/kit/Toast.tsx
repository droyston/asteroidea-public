// Toast: a short message that confirms an action ("Copied") and leaves on its own.
// `toast(text)` queues one from anywhere; `ToastHost`, mounted once by the app, draws the queue in a
// polite live region, so a screen reader announces each message without moving focus; an error goes
// in an assertive one. The regions carry `aria-live` and no role, so an empty host is not an extra
// status or alert landmark on every page. Each toast has a Dismiss button and leaves after `TOAST_MS`.
import { useEffect, useSyncExternalStore } from "react";
import { createSlot } from "../shell/slot";

export type ToastKind = "info" | "error";

export interface ToastItem {
  readonly id: number;
  readonly text: string;
  readonly kind: ToastKind;
}

/** How long a toast stays, in milliseconds. */
export const TOAST_MS = 4000;
/** At most this many toasts show at once; an older one leaves first. */
export const TOAST_MAX = 3;

const queue = createSlot<readonly ToastItem[]>([]);
let nextId = 1;

/** The queue after adding `item`: the newest last, at most `TOAST_MAX`. */
export function pushed(list: readonly ToastItem[], item: ToastItem): readonly ToastItem[] {
  return [...list, item].slice(-TOAST_MAX);
}

/** Show `text` as a toast. Returns its id. */
export function toast(text: string, kind: ToastKind = "info"): number {
  const id = nextId;
  nextId += 1;
  queue.set(pushed(queue.get(), { id, text, kind }));
  return id;
}

/** Remove the toast `id` (a no-op when it already left). */
export function dismissToast(id: number): void {
  const list = queue.get();
  if (list.some((t) => t.id === id)) queue.set(list.filter((t) => t.id !== id));
}

/** The toasts showing now (for tests and the artboard). */
export function toasts(): readonly ToastItem[] {
  return queue.get();
}

function ToastView({ item }: { item: ToastItem }) {
  useEffect(() => {
    const timer = window.setTimeout(() => dismissToast(item.id), TOAST_MS);
    return () => window.clearTimeout(timer);
  }, [item.id]);
  return (
    <div className={`toast toast-${item.kind}`} data-testid="toast">
      <span className="toast-text">{item.text}</span>
      <button
        type="button"
        className="toast-close"
        aria-label="Dismiss"
        onClick={() => dismissToast(item.id)}
      >
        ×
      </button>
    </div>
  );
}

/** The toast region. Mount once. Errors go in an assertive live region, the rest in a polite one. */
export function ToastHost({ items }: { items?: readonly ToastItem[] }) {
  const live = useSyncExternalStore(queue.subscribe, queue.get, queue.get);
  const list = items ?? live;
  return (
    <div className="toast-host">
      <div aria-live="polite" className="toast-stack">
        {list
          .filter((t) => t.kind === "info")
          .map((t) => (
            <ToastView key={t.id} item={t} />
          ))}
      </div>
      <div aria-live="assertive" className="toast-stack">
        {list
          .filter((t) => t.kind === "error")
          .map((t) => (
            <ToastView key={t.id} item={t} />
          ))}
      </div>
    </div>
  );
}

/** Copy `text` to the clipboard and say so in a toast; a refusal says how to copy by hand. */
export async function copyWithToast(text: string, what: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    toast(`Copied ${what}`);
    return true;
  } catch {
    toast(
      `Could not copy ${what}: the browser refused. Select the text and copy it by hand.`,
      "error",
    );
    return false;
  }
}
