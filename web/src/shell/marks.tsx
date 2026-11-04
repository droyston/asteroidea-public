// What a routed view tells the shell: its spine trail (`Spine`) and that its data has drawn (`Ready`,
// or `Settled` for a view that cannot say so itself). The shell turns the ready flag into
// `data-ready="<view>"` on its `<main>`; the manual walk-through and the e2e specs wait on it.
import { type ReactNode, useEffect, useRef } from "react";
import { createSlot } from "./slot";

export interface SpineCrumb {
  readonly label: string;
  /** Absent on the last crumb: the object the reader is on. */
  readonly to?: string;
}

export const spineSlot = createSlot<readonly SpineCrumb[] | null>(null);
export const readySlot = createSlot<boolean>(false);

/** Render nothing; publish `trail` as the shell's spine while mounted. */
export function Spine({ trail }: { trail: readonly SpineCrumb[] }): null {
  const key = JSON.stringify(trail);
  useEffect(() => {
    spineSlot.set(JSON.parse(key) as SpineCrumb[]);
    return () => spineSlot.set(null);
  }, [key]);
  return null;
}

/** Render nothing; mark the routed view ready while mounted. Mount it where the data has drawn. */
export function Ready(): null {
  useEffect(() => {
    readySlot.set(true);
    return () => readySlot.set(false);
  }, []);
  return null;
}

/** The loading markers the pages use: `data-testid` ending in `-loading`. */
export const LOADING = '[data-testid$="-loading"]';

/**
 * What `Settled` watches. React reuses a DOM node when the next state renders the same tag, so a
 * loading `<p data-testid="uploads-loading">` that becomes the empty state `<p data-testid="uploads-empty">`
 * changes an attribute and its text, adds and removes no child: `childList` alone never fires.
 */
export const SETTLED_OBSERVE: MutationObserverInit = {
  childList: true,
  subtree: true,
  attributes: true,
  attributeFilter: ["data-testid"],
};

/** True when `root` holds a loading marker. */
export function isLoading(root: { querySelector(selector: string): unknown }): boolean {
  return root.querySelector(LOADING) !== null;
}

/**
 * Ready once no loading marker is inside. For views whose own code does not call `Ready`
 * (Results, Imports, Setup, Uploads); an error state counts as settled.
 */
export function Settled({ children }: { children: ReactNode }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const el = ref.current;
    if (el === null) return;
    const check = () => readySlot.set(!isLoading(el));
    check();
    const observer = new MutationObserver(check);
    observer.observe(el, SETTLED_OBSERVE);
    return () => {
      observer.disconnect();
      readySlot.set(false);
    };
  }, []);
  return (
    <div ref={ref} className="app-view">
      {children}
    </div>
  );
}
