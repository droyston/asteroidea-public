import { describe, expect, it } from "vitest";
import { createThemeStore, nextMode, THEME_KEY, type ThemeRoot } from "./theme";

function fakeRoot(): ThemeRoot & { attrs: Map<string, string> } {
  const attrs = new Map<string, string>();
  return {
    attrs,
    setAttribute: (k, v) => void attrs.set(k, v),
    removeAttribute: (k) => void attrs.delete(k),
  };
}

function fakeStorage(seed?: string) {
  const data = new Map<string, string>(seed === undefined ? [] : [[THEME_KEY, seed]]);
  return {
    data,
    getItem: (k: string) => data.get(k) ?? null,
    setItem: (k: string, v: string) => void data.set(k, v),
  };
}

const throwing = {
  getItem: (): string | null => {
    throw new Error("blocked");
  },
  setItem: (): void => {
    throw new Error("blocked");
  },
};

describe("theme store", () => {
  it("cycles System, Light, Dark, System", () => {
    expect(nextMode("system")).toBe("light");
    expect(nextMode("light")).toBe("dark");
    expect(nextMode("dark")).toBe("system");
  });

  it("sets data-theme on the root, and removes it for System", () => {
    const root = fakeRoot();
    const store = createThemeStore(fakeStorage(), root);
    store.cycle();
    expect(root.attrs.get("data-theme")).toBe("light");
    store.cycle();
    expect(root.attrs.get("data-theme")).toBe("dark");
    store.cycle();
    expect(root.attrs.has("data-theme")).toBe(false);
    expect(store.get()).toBe("system");
  });

  it("persists the choice under asteroidea.theme/1 and reads it back", () => {
    const storage = fakeStorage();
    createThemeStore(storage, fakeRoot()).set("dark");
    expect(THEME_KEY).toBe("asteroidea.theme/1");
    expect(storage.data.get(THEME_KEY)).toBe("dark");
    const root = fakeRoot();
    expect(createThemeStore(storage, root).get()).toBe("dark");
    expect(root.attrs.get("data-theme")).toBe("dark");
  });

  it("falls back to System on an unknown stored value", () => {
    expect(createThemeStore(fakeStorage("sepia"), fakeRoot()).get()).toBe("system");
  });

  it("falls back to System, and still switches, when storage throws", () => {
    const root = fakeRoot();
    const store = createThemeStore(throwing, root);
    expect(store.get()).toBe("system");
    store.cycle();
    expect(store.get()).toBe("light");
    expect(root.attrs.get("data-theme")).toBe("light");
  });

  it("falls back to System when there is no storage at all", () => {
    expect(createThemeStore(null, fakeRoot()).get()).toBe("system");
  });

  it("tells subscribers once per change", () => {
    const store = createThemeStore(fakeStorage(), fakeRoot());
    let n = 0;
    const off = store.subscribe(() => n++);
    store.set("dark");
    store.set("dark");
    off();
    store.set("light");
    expect(n).toBe(1);
  });
});
