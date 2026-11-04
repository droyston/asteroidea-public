// The rate curve: one point per bin, at the bin centre, with the value the server returned. The
// web never bins, smooths or resamples: `curvePoints` maps each `values[i]` to one point and keeps it,
// so the drawn series is the response's `rate_hz` (or `z`) exactly. A null value leaves a gap.
import type { Domain } from "./axis";

export interface CurvePoint {
  x: number;
  y: number;
  /** The server's value for this bin; null for a gap. */
  value: number | null;
}

/** One point per value: x at the centre of bin `i` (`edges[i]` to `edges[i + 1]`), y from `value`. */
export function curvePoints(
  edges: readonly number[],
  values: readonly (number | null)[],
  xs: (v: number) => number,
  ys: (v: number) => number,
): CurvePoint[] {
  return values.map((value, i) => {
    const lo = edges[i] ?? 0;
    const hi = edges[i + 1] ?? lo;
    return { x: xs((lo + hi) / 2), y: value === null ? 0 : ys(value), value };
  });
}

/** The SVG path of `points`: a line, broken at each gap. */
export function curvePath(points: readonly CurvePoint[]): string {
  let d = "";
  let pen = false;
  for (const p of points) {
    if (p.value === null) {
      pen = false;
      continue;
    }
    d += `${pen ? "L" : "M"}${p.x.toFixed(2)} ${p.y.toFixed(2)}`;
    pen = true;
  }
  return d;
}

/** The y domain of `values`, padded; rates start at zero (`zero`), a z-score does not. */
export function valueDomain(values: readonly (number | null)[], zero: boolean): Domain {
  let lo = Number.POSITIVE_INFINITY;
  let hi = Number.NEGATIVE_INFINITY;
  for (const v of values) {
    if (v === null) continue;
    lo = Math.min(lo, v);
    hi = Math.max(hi, v);
  }
  if (!Number.isFinite(lo)) return [0, 1];
  if (zero) lo = Math.min(lo, 0);
  if (hi <= lo) return [lo - 0.5, hi + 0.5];
  const pad = (hi - lo) * 0.05;
  return [zero && lo === 0 ? 0 : lo - pad, hi + pad];
}
