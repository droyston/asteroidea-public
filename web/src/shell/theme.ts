// The colour-theme preference: System, Light or Dark. The choice is a display preference kept
// in localStorage (sign-in tokens stay in sessionStorage) and shown as `data-theme` on <html>; System
// removes the attribute so prefers-color-scheme decides (kit/tokens.css). index.html applies the saved
// choice before first paint; this store applies it again at start and on every change.
import { useSyncExternalStore } from "react";
import { createSlot } from "./slot";

export type ThemeMode = "system" | "light" | "dark";

export const THEME_KEY = "asteroidea.theme/1";
export const MODES: readonly ThemeMode[] = ["system", "light", "dark"];

export interface ThemeRoot {
  setAttribute(name: string, value: string): void;
  removeAttribute(name: string): void;
}

export interface ThemeStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
}

export interface ThemeStore {
  get(): ThemeMode;
  set(mode: ThemeMode): void;
  cycle(): void;
  subscribe(listener: () => void): () => void;
}

export function nextMode(mode: ThemeMode): ThemeMode {
  return MODES[(MODES.indexOf(mode) + 1) % MODES.length] ?? "system";
}

function parse(raw: string | null): ThemeMode {
  return raw === "light" || raw === "dark" ? raw : "system";
}

function apply(root: ThemeRoot, mode: ThemeMode): void {
  if (mode === "system") root.removeAttribute("data-theme");
  else root.setAttribute("data-theme", mode);
}

/** A store over `storage` and `root`; storage that is absent or throws leaves the choice at System. */
export function createThemeStore(storage: ThemeStorage | null, root: ThemeRoot): ThemeStore {
  let initial: ThemeMode = "system";
  try {
    initial = parse(storage?.getItem(THEME_KEY) ?? null);
  } catch {
    initial = "system";
  }
  const slot = createSlot<ThemeMode>(initial);
  apply(root, initial);
  const set = (mode: ThemeMode) => {
    apply(root, mode);
    try {
      storage?.setItem(THEME_KEY, mode);
    } catch {
      // Blocked storage: the choice holds for this page load only.
    }
    slot.set(mode);
  };
  return {
    get: slot.get,
    set,
    cycle: () => set(nextMode(slot.get())),
    subscribe: slot.subscribe,
  };
}

let shared: ThemeStore | null = null;

/** The page's store, made on first use. */
export function themeStore(): ThemeStore {
  if (shared === null) {
    let storage: ThemeStorage | null = null;
    try {
      storage = window.localStorage;
    } catch {
      storage = null;
    }
    shared = createThemeStore(storage, document.documentElement);
  }
  return shared;
}

const subscribe = (listener: () => void) => themeStore().subscribe(listener);
const snapshot = () => themeStore().get();
const serverSnapshot = (): ThemeMode => "system";

/** The current mode; a component that reads it re-renders when the theme changes. Rendered to a string
 * (tests, artboards) there is no document, so it reads System. */
export function useThemeMode(): ThemeMode {
  return useSyncExternalStore(subscribe, snapshot, serverSnapshot);
}
