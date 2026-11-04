// Flexible inspection's model: the browser re-bins each unit's per-trial raster from
// `/compute/rates?...&raster=1` and re-normalises it against a baseline window the reader drags, so
// moving a window or the bin never sends a request. Interactive inspection over returned data only:
// nothing here is saved, and a figure to keep is made in the SDK.
//
// Definitions (the parity golden `e2e/fixtures/ux/inspect-parity.golden.json` pins them against
// the SDK): the PSTH is `analysis.rates` (counts / (trials x bin), bin = floor((t + pre) / bin));
// baseline and response bins are those lying wholly inside the window; z is `analysis.zscore`
// (sample SD, ddof 1; null under two baseline bins or an SD at most 1e-9); Δ Hz is the rate minus
// the baseline mean; % change is 100 x Δ Hz / the baseline mean, null when that mean is 0.
import { normalizeRegion, regionColour } from "../kit/regions";
import type { Raster, RatesBody } from "../units/units";

export const MIN_STD = 1e-9;
const EDGE_TOL = 1e-9;

/** How the response is expressed. */
export type Norm = "hz" | "dhz" | "z" | "pct";
export type Scales = "linked" | "unit";
/** A unit attribute the scatter can put on either axis. */
export type Attr = "response" | "baseline_hz" | "response_hz" | "spikes" | "unit";

export const NORMS: readonly (readonly [Norm, string])[] = [
  ["hz", "Raw Hz"],
  ["dhz", "Δ Hz"],
  ["z", "z"],
  ["pct", "% change"],
];
export const BINS: readonly number[] = [0.05, 0.1, 0.2];
export const ATTRS: readonly (readonly [Attr, string])[] = [
  ["response", "response"],
  ["baseline_hz", "baseline rate"],
  ["response_hz", "response rate"],
  ["spikes", "spike count"],
  ["unit", "unit index"],
];
/** Attributes drawn on a log axis (rates span decades). */
export const LOG_ATTRS: ReadonlySet<Attr> = new Set(["baseline_hz", "response_hz", "spikes"]);
export const TOP = 12;

export interface InspectState {
  readonly norm: Norm;
  readonly bin: number;
  readonly base: readonly [number, number];
  readonly resp: readonly [number, number];
  readonly scales: Scales;
  readonly x: Attr;
  readonly y: Attr;
}

export interface Window {
  readonly pre: number;
  readonly post: number;
}

/** The view's defaults: z against −pre to 0 s at 50 ms, the response window the server's defaults name. */
export function defaultState(w: Window, response: readonly [number, number]): InspectState {
  return {
    norm: "z",
    bin: 0.05,
    base: [-w.pre, 0],
    resp: response,
    scales: "linked",
    x: "baseline_hz",
    y: "response",
  };
}

function pair(text: string | null, lo: number, hi: number): [number, number] | null {
  if (text === null) return null;
  const parts = text.split(",").map(Number);
  const [a, b] = parts;
  if (parts.length !== 2 || a === undefined || b === undefined) return null;
  if (!Number.isFinite(a) || !Number.isFinite(b) || a >= b || a < lo || b > hi) return null;
  return [a, b];
}

function oneOf<T extends string>(
  v: string | null,
  all: readonly (readonly [T, string])[],
): T | null {
  return all.find(([k]) => k === v)?.[0] ?? null;
}

/** The state in `search` (`norm`, `bin`, `base`, `resp`, `scales`, `x`, `y`); a bad value is its default. */
export function parseInspect(search: string, d: InspectState, w: Window): InspectState {
  const q = new URLSearchParams(search);
  const bin = Number(q.get("bin"));
  return {
    norm: oneOf(q.get("norm"), NORMS) ?? d.norm,
    bin: BINS.includes(bin) ? bin : d.bin,
    base: pair(q.get("base"), -w.pre, 0) ?? d.base,
    resp: pair(q.get("resp"), 0, w.post) ?? d.resp,
    scales: q.get("scales") === "unit" ? "unit" : d.scales,
    x: oneOf(q.get("x"), ATTRS) ?? d.x,
    y: oneOf(q.get("y"), ATTRS) ?? d.y,
  };
}

/** `search` with the inspect parameters set; a default is left out, other parameters kept. */
export function inspectSearch(search: string, s: InspectState, d: InspectState): string {
  const q = new URLSearchParams(search);
  for (const k of ["norm", "bin", "base", "resp", "scales", "x", "y"]) q.delete(k);
  if (s.norm !== d.norm) q.set("norm", s.norm);
  if (s.bin !== d.bin) q.set("bin", String(s.bin));
  if (s.base[0] !== d.base[0] || s.base[1] !== d.base[1]) q.set("base", s.base.join(","));
  if (s.resp[0] !== d.resp[0] || s.resp[1] !== d.resp[1]) q.set("resp", s.resp.join(","));
  if (s.scales !== d.scales) q.set("scales", s.scales);
  if (s.x !== d.x) q.set("x", s.x);
  if (s.y !== d.y) q.set("y", s.y);
  const text = q.toString();
  return text === "" ? "" : `?${text}`;
}

// ---- arithmetic (pinned by the parity golden) ----

/** Bin edges as `analysis.rates` computes them: −pre + i × bin. */
export function binEdges(pre: number, post: number, bin: number): number[] {
  const n = Math.round((pre + post) / bin);
  return Array.from({ length: n + 1 }, (_, i) => -pre + i * bin);
}

/** The PSTH of one unit's raster: counts / (trials × bin), empty bins 0. */
export function rebin(
  raster: Raster,
  nTrials: number,
  pre: number,
  post: number,
  bin: number,
): number[] {
  const n = Math.round((pre + post) / bin);
  const counts = new Array<number>(n).fill(0);
  for (const t of raster.t) {
    const b = Math.floor((t + pre) / bin);
    if (b >= 0 && b < n) counts[b] = (counts[b] ?? 0) + 1;
  }
  return nTrials === 0 ? counts : counts.map((c) => c / (nTrials * bin));
}

/** Indices of the bins lying wholly inside `window`. */
export function binsInside(edges: readonly number[], window: readonly [number, number]): number[] {
  const out: number[] = [];
  for (let b = 0; b + 1 < edges.length; b += 1) {
    const lo = edges[b] ?? 0;
    const hi = edges[b + 1] ?? 0;
    if (lo >= window[0] - EDGE_TOL && hi <= window[1] + EDGE_TOL) out.push(b);
  }
  return out;
}

function mean(values: readonly number[], at: readonly number[]): number {
  let sum = 0;
  for (const i of at) sum += values[i] ?? 0;
  return sum / at.length;
}

/** `analysis.zscore` for one row: null under two baseline bins or at an SD of at most MIN_STD. */
export function zscore(rate: readonly number[], base: readonly number[]): number[] | null {
  if (base.length < 2) return null;
  const m = mean(rate, base);
  let ss = 0;
  for (const i of base) ss += ((rate[i] ?? 0) - m) ** 2;
  const sd = Math.sqrt(ss / (base.length - 1));
  if (sd <= MIN_STD) return null;
  return rate.map((v) => (v - m) / sd);
}

export function deltaHz(rate: readonly number[], base: readonly number[]): number[] {
  const m = mean(rate, base);
  return rate.map((v) => v - m);
}

export function pctChange(rate: readonly number[], base: readonly number[]): number[] | null {
  const m = mean(rate, base);
  if (m === 0) return null;
  return rate.map((v) => (100 * (v - m)) / m);
}

/** The unit's series under `norm`; null when it is undefined (no baseline variance, or a zero baseline). */
export function normalise(
  rate: readonly number[],
  base: readonly number[],
  norm: Norm,
): number[] | null {
  if (norm === "hz") return [...rate];
  if (norm === "dhz") return deltaHz(rate, base);
  if (norm === "z") return zscore(rate, base);
  return pctChange(rate, base);
}

// ---- per-unit values ----

export interface UnitPoint {
  readonly unit: number;
  readonly location: string;
  readonly regionKey: string;
  readonly regionLabel: string;
  readonly cssVar: string;
  readonly spikes: number;
  readonly baselineHz: number;
  readonly responseHz: number;
  /** Mean of the normalised series over the response bins (÷ the unit's peak when per unit); null when undefined. */
  readonly response: number | null;
}

export interface Inspected {
  readonly points: readonly UnitPoint[];
  /** Region groups in key order, each with its label and colour. */
  readonly regions: readonly { key: string; label: string; cssVar: string }[];
  /** Units whose response is undefined under the norm. */
  readonly undefinedUnits: readonly number[];
}

const NO_LOCATION = "no location";

export function inspect(
  rates: RatesBody,
  where: ReadonlyMap<number, string | null>,
  w: Window,
  s: InspectState,
): Inspected {
  const edges = binEdges(w.pre, w.post, s.bin);
  const base = binsInside(edges, s.base);
  const resp = binsInside(edges, s.resp);
  const regions = new Map<string, { key: string; label: string; cssVar: string }>();
  const points = rates.units.map((u): UnitPoint => {
    const location = where.get(u.unit_index) ?? NO_LOCATION;
    const region = normalizeRegion(location);
    if (!regions.has(region.key))
      regions.set(region.key, {
        key: region.key,
        label: location,
        cssVar: regionColour(location).cssVar,
      });
    const raster = u.raster ?? { trial: [], t: [] };
    const rate = rebin(raster, rates.n_trials, w.pre, w.post, s.bin);
    const series = normalise(rate, base, s.norm);
    let response: number | null = null;
    if (series !== null && resp.length > 0) {
      response = mean(series, resp);
      if (s.scales === "unit") {
        const peak = Math.max(...series.map(Math.abs));
        response = peak > 0 ? response / peak : 0;
      }
    }
    const group = regions.get(region.key);
    return {
      unit: u.unit_index,
      location,
      regionKey: region.key,
      regionLabel: group?.label ?? location,
      cssVar: group?.cssVar ?? "",
      spikes: raster.t.length,
      baselineHz: base.length === 0 ? 0 : mean(rate, base),
      responseHz: resp.length === 0 ? 0 : mean(rate, resp),
      response,
    };
  });
  return {
    points,
    regions: [...regions.values()].sort((a, b) => a.key.localeCompare(b.key)),
    undefinedUnits: points
      .filter((p) => p.response === null)
      .map((p) => p.unit)
      .sort((a, b) => a - b),
  };
}

export function attrValue(p: UnitPoint, a: Attr): number | null {
  switch (a) {
    case "response":
      return p.response;
    case "baseline_hz":
      return p.baselineHz;
    case "response_hz":
      return p.responseHz;
    case "spikes":
      return p.spikes;
    default:
      return p.unit;
  }
}

/** The top `n` units by |response|, largest first, ties by unit. */
export function ranked(points: readonly UnitPoint[], n: number = TOP): UnitPoint[] {
  return points
    .filter((p) => p.response !== null)
    .sort((a, b) => Math.abs(b.response ?? 0) - Math.abs(a.response ?? 0) || a.unit - b.unit)
    .slice(0, n);
}

/** The axis label of the response under a norm and scale. */
export function responseLabel(s: InspectState): string {
  const norm = NORMS.find(([k]) => k === s.norm)?.[1] ?? s.norm;
  return s.scales === "unit" ? `response ${norm} ÷ unit's peak` : `response ${norm}`;
}

/** Seconds as the control row prints them: "−1.0 to 0.0 s". */
export function windowLabel(w: readonly [number, number]): string {
  const f = (v: number) => (v < 0 ? `−${Math.abs(v).toFixed(2)}` : v.toFixed(2));
  return `${f(w[0])} to ${f(w[1])} s`;
}
