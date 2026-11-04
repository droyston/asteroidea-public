import { describe, expect, it } from "vitest";
import { allInView, dragPath } from "../e2e/live/smoke-drag";

const PHONE = { width: 375, height: 812 };

describe("smoke LFP drag path", () => {
  it("runs left to right along the plot's middle line", () => {
    const [a, b, c] = dragPath({ x: 16, y: 700, width: 343, height: 170 }, 1000, 300, 60);
    expect(a?.y).toBe(700 + 6 + 75);
    expect(b?.y).toBe(a?.y);
    expect((a?.x ?? 0) < (b?.x ?? 0) && (b?.x ?? 0) < (c?.x ?? 0)).toBe(true);
  });

  it("is below the fold of a phone until the overview is scrolled up", () => {
    const below = dragPath({ x: 16, y: 800, width: 343, height: 170 }, 1000, 300, 60);
    expect(allInView(below, PHONE)).toBe(false);
    const scrolled = dragPath({ x: 16, y: 300, width: 343, height: 170 }, 1000, 300, 60);
    expect(allInView(scrolled, PHONE)).toBe(true);
  });
});
