// Parity: the browser's re-binning and z / Δ Hz / % change against numbers the SDK computed
// (`e2e/fixtures/ux/gen_inspect.py`: `analysis.rates`, `analysis.zscore`) for the same raster.
import { describe, expect, it } from "vitest";
import golden from "../../e2e/fixtures/ux/inspect-parity.golden.json";
import rasterBody from "../../e2e/fixtures/ux/rates-cs62-scene_cut-raster.json";
import { parseRates } from "../units/units";
import {
  binEdges,
  binsInside,
  defaultState,
  deltaHz,
  type InspectState,
  inspect,
  inspectSearch,
  parseInspect,
  pctChange,
  ranked,
  rebin,
  zscore,
} from "./model";

const REL = 1e-9;

function close(actual: number | null | undefined, expected: number | null, what: string): void {
  if (expected === null) {
    expect(actual, what).toBeNull();
    return;
  }
  if (actual === null || actual === undefined) throw new Error(`${what}: got ${actual}`);
  const tol = REL * Math.max(1, Math.abs(expected));
  if (Math.abs(actual - expected) > tol)
    throw new Error(`${what}: ${actual} differs from the SDK's ${expected} by more than ${tol}`);
}

function closeRow(
  actual: readonly number[] | null,
  expected: readonly number[] | null,
  what: string,
): void {
  if (expected === null) {
    expect(actual, what).toBeNull();
    return;
  }
  if (actual === null) throw new Error(`${what}: got null`);
  expect(actual.length, what).toBe(expected.length);
  for (const [i, e] of expected.entries()) close(actual[i], e, `${what}[${i}]`);
}

const body = parseRates(rasterBody);
const byUnit = new Map(body.units.map((u) => [u.unit_index, u]));

function meanAt(v: readonly number[], at: readonly number[]): number {
  return at.reduce((s, i) => s + (v[i] ?? 0), 0) / at.length;
}

describe("re-binning and normalisation match the SDK (analysis.rates, analysis.zscore)", () => {
  for (const c of golden.cases) {
    const label = `bin ${c.bin_s} s, baseline ${c.baseline.join(" to ")}, response ${c.response.join(" to ")}`;
    it(label, () => {
      const edges = binEdges(golden.pre_s, golden.post_s, c.bin_s);
      closeRow(edges, c.edges, "edges");
      const base = binsInside(edges, [c.baseline[0] ?? 0, c.baseline[1] ?? 0]);
      const resp = binsInside(edges, [c.response[0] ?? 0, c.response[1] ?? 0]);
      for (const g of c.units) {
        const u = byUnit.get(g.unit_index);
        if (u?.raster == null) throw new Error(`unit ${g.unit_index} has no raster`);
        const what = `unit ${g.unit_index}`;
        const rate = rebin(u.raster, golden.n_trials, golden.pre_s, golden.post_s, c.bin_s);
        closeRow(rate, g.rate_hz, `${what} rate_hz`);
        const z = zscore(rate, base);
        closeRow(z, g.z, `${what} z`);
        const d = deltaHz(rate, base);
        closeRow(d, g.delta_hz, `${what} delta_hz`);
        const p = pctChange(rate, base);
        closeRow(p, g.pct, `${what} pct`);
        close(meanAt(rate, base), g.baseline_hz, `${what} baseline_hz`);
        close(meanAt(rate, resp), g.response_hz, `${what} response_hz`);
        close(z === null ? null : meanAt(z, resp), g.response_z, `${what} response_z`);
        close(meanAt(d, resp), g.response_delta_hz, `${what} response_delta_hz`);
        close(p === null ? null : meanAt(p, resp), g.response_pct, `${what} response_pct`);
      }
    });
  }

  it("covers a unit with no baseline variance (z null) in every case", () => {
    for (const c of golden.cases) expect(c.units.some((u) => u.z === null)).toBe(true);
  });
});

describe("the inspect view's numbers", () => {
  const w = { pre: 1, post: 2 };
  const d = defaultState(w, [0, 1]);
  const where = new Map<number, string | null>();

  it("z by default: the response is the mean z over the response bins", () => {
    const out = inspect(body, where, w, d);
    expect(out.points).toHaveLength(44);
    const unit0 = golden.cases[0]?.units.find((u) => u.unit_index === 0);
    close(out.points.find((p) => p.unit === 0)?.response, unit0?.response_z ?? null, "unit 0");
    expect(out.undefinedUnits).toEqual([43]);
  });

  it("per-unit scales put every response within ±1", () => {
    const out = inspect(body, where, w, { ...d, scales: "unit" });
    for (const p of out.points)
      if (p.response !== null) expect(Math.abs(p.response)).toBeLessThanOrEqual(1);
  });

  it("ranks the top 12 by |response|", () => {
    const top = ranked(inspect(body, where, w, d).points);
    expect(top).toHaveLength(12);
    const mags = top.map((p) => Math.abs(p.response ?? 0));
    expect([...mags].sort((a, b) => b - a)).toEqual(mags);
  });

  it("keeps a changed state in the URL and leaves defaults out", () => {
    const s: InspectState = { ...d, norm: "pct", base: [-0.5, 0], bin: 0.1, x: "spikes" };
    const search = inspectSearch("?view=inspect", s, d);
    expect(search).toBe("?view=inspect&norm=pct&bin=0.1&base=-0.5%2C0&x=spikes");
    expect(parseInspect(search, d, w)).toEqual(s);
    expect(inspectSearch(search, d, d)).toBe("?view=inspect");
    expect(parseInspect("?base=0,-1&bin=7&norm=nope", d, w)).toEqual(d);
  });
});
