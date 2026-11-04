import { describe, expect, it } from "vitest";
import { isLoading, LOADING, SETTLED_OBSERVE } from "./marks";

describe("Settled", () => {
  it("watches the data-testid attribute, because an empty state reuses the loading node", () => {
    // Uploads: `<p data-testid="uploads-loading">` becomes `<p data-testid="uploads-empty">` in place.
    expect(SETTLED_OBSERVE.attributes).toBe(true);
    expect(SETTLED_OBSERVE.attributeFilter).toContain("data-testid");
    expect(SETTLED_OBSERVE.childList).toBe(true);
    expect(SETTLED_OBSERVE.subtree).toBe(true);
  });

  it("is loading while a marker is inside and settled once it is gone", () => {
    const has = { querySelector: (s: string) => (s === LOADING ? {} : null) };
    const none = { querySelector: () => null };
    expect(isLoading(has)).toBe(true);
    expect(isLoading(none)).toBe(false);
  });

  it("matches test ids ending in -loading only", () => {
    expect(LOADING).toBe('[data-testid$="-loading"]');
  });
});
