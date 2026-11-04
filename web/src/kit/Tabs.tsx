// Tabs: an accessible tablist. Left / Right / Home / End move between tabs
// (automatic activation, roving tabindex). The selected tab lives in the URL query as `tab=`, so a
// link or reload keeps it; `tabFromSearch` and `searchWithTab` are the two pure halves of that.
import type { KeyboardEvent, ReactNode } from "react";

export interface TabItem {
  id: string;
  label: string;
}

export const TAB_PARAM = "tab";

/** The tab id in `search` when it names one of `ids`, else the first id. */
export function tabFromSearch(search: string, ids: readonly string[]): string {
  const want = new URLSearchParams(search).get(TAB_PARAM);
  return want !== null && ids.includes(want) ? want : (ids[0] ?? "");
}

/** `search` with `tab=<id>` set (other parameters kept); the first tab leaves the query clean. */
export function searchWithTab(search: string, id: string, first: string): string {
  const q = new URLSearchParams(search);
  if (id === first) q.delete(TAB_PARAM);
  else q.set(TAB_PARAM, id);
  const text = q.toString();
  return text === "" ? "" : `?${text}`;
}

/** The id a key moves to, or null for a key that does not move. Wraps at both ends. */
export function nextTab(ids: readonly string[], current: string, key: string): string | null {
  const i = ids.indexOf(current);
  if (i < 0 || ids.length === 0) return null;
  switch (key) {
    case "ArrowRight":
      return ids[(i + 1) % ids.length] ?? null;
    case "ArrowLeft":
      return ids[(i - 1 + ids.length) % ids.length] ?? null;
    case "Home":
      return ids[0] ?? null;
    case "End":
      return ids[ids.length - 1] ?? null;
    default:
      return null;
  }
}

export interface TabsProps {
  tabs: readonly TabItem[];
  selected: string;
  onSelect: (id: string) => void;
  label: string;
  children?: ReactNode;
}

export function Tabs({ tabs, selected, onSelect, label, children }: TabsProps) {
  const ids = tabs.map((t) => t.id);
  const onKeyDown = (e: KeyboardEvent<HTMLElement>) => {
    const to = nextTab(ids, selected, e.key);
    if (to === null) return;
    e.preventDefault();
    onSelect(to);
    document.getElementById(`tab-${to}`)?.focus();
  };
  return (
    <div className="tabs">
      <div className="tablist" role="tablist" aria-label={label} onKeyDown={onKeyDown}>
        {tabs.map((t) => (
          <button
            key={t.id}
            id={`tab-${t.id}`}
            type="button"
            role="tab"
            className="tab"
            aria-selected={t.id === selected}
            aria-controls={`panel-${t.id}`}
            tabIndex={t.id === selected ? 0 : -1}
            onClick={() => onSelect(t.id)}
          >
            {t.label}
          </button>
        ))}
      </div>
      <div
        className="tabpanel"
        role="tabpanel"
        id={`panel-${selected}`}
        aria-labelledby={`tab-${selected}`}
      >
        {children}
      </div>
    </div>
  );
}
