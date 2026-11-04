import { describe, expect, it } from "vitest";
import {
  clampDomain,
  formatTick,
  invertScale,
  linearScale,
  niceStep,
  niceTicks,
  zoomAround,
} from "./axis";

describe("linearScale", () => {
  it("maps the domain ends to the range ends and inverts", () => {
    const f = linearScale([10, 20], [0, 100]);
    expect(f(10)).toBe(0);
    expect(f(15)).toBe(50);
    expect(f(20)).toBe(100);
    expect(invertScale([10, 20], [0, 100])(25)).toBe(12.5);
  });

  it("maps an empty domain to the range start", () => {
    expect(linearScale([5, 5], [3, 9])(5)).toBe(3);
  });
});

describe("niceTicks", () => {
  it("steps by 1, 2 or 5 times a power of ten", () => {
    expect(niceStep(100, 5)).toBe(20);
    expect(niceStep(10, 10)).toBe(1);
    expect(niceStep(0.7, 7)).toBe(0.1);
    expect(niceStep(0, 5)).toBe(1);
  });

  it("returns ticks inside the domain", () => {
    expect(niceTicks([0, 100], 5)).toEqual([0, 20, 40, 60, 80, 100]);
    expect(niceTicks([3, 47], 4)).toEqual([10, 20, 30, 40]);
  });

  it("has no float noise and no ticks for an empty domain", () => {
    expect(niceTicks([0, 0.5], 5)).toEqual([0, 0.1, 0.2, 0.3, 0.4, 0.5]);
    expect(niceTicks([2, 2], 5)).toEqual([]);
  });

  it("labels with just enough decimals", () => {
    expect(formatTick(40, 20)).toBe("40");
    expect(formatTick(0.3, 0.1)).toBe("0.3");
    expect(formatTick(0.25, 0.05)).toBe("0.25");
  });
});

describe("zoom", () => {
  it("orders and clamps a drag, and refuses a sliver", () => {
    expect(clampDomain(30, 10, [0, 100], 1)).toEqual([10, 30]);
    expect(clampDomain(-5, 120, [0, 100], 1)).toEqual([0, 100]);
    expect(clampDomain(10, 10.2, [0, 100], 1)).toBeNull();
  });

  it("zooms around a point and stays inside the full range", () => {
    expect(zoomAround([0, 100], 50, 0.5, [0, 100])).toEqual([25, 75]);
    expect(zoomAround([0, 100], 0, 0.5, [0, 100])).toEqual([0, 50]);
    expect(zoomAround([25, 75], 50, 4, [0, 100])).toEqual([0, 100]);
  });
});
