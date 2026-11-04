// A smaller request that fits: after a `slow`, `too_large`,
// `body_estimate_too_large` or `too_many_units` refusal, the page offers the same request cut down,
// computed from the request and, when the server's detail names sizes, from those sizes.

/** The parts of a request that can shrink; a request carries the ones it has. */
export interface Shrinkable {
  /** How many units (or channels) the request covers. */
  readonly units?: number;
  /** The window length in seconds. */
  readonly windowS?: number;
}

export interface Smaller extends Shrinkable {
  /** The primary button's label: "Use 72 units", "Use 600 s window". */
  readonly label: string;
}

/** The server's unit bound (`compute_api.py :: MAX_UNITS`); the detail's own number wins. */
export const MAX_UNITS = 152;
/** Leave this much room under a byte limit, so the smaller request is not refused again. */
const MARGIN = 0.8;

/** The fraction of the request that fits, from "<size> bytes, over the <limit> limit"; else null. */
export function fitFraction(detail: string | null): number | null {
  if (detail === null) return null;
  const m = /(\d+) bytes, over the (\d+)/.exec(detail);
  if (m === null) return null;
  const size = Number(m[1]);
  const limit = Number(m[2]);
  if (!(size > 0 && limit > 0) || limit >= size) return null;
  return (limit / size) * MARGIN;
}

/** The unit bound named in a `too_many_units` detail ("over the bound of 152"); else MAX_UNITS. */
function unitBound(detail: string | null): number {
  const m = detail === null ? null : /bound of (\d+)/.exec(detail);
  return m === null ? MAX_UNITS : Number(m[1]);
}

function niceSeconds(s: number): number {
  if (s >= 10) return Math.max(1, Math.floor(s / 10) * 10);
  if (s >= 1) return Math.floor(s);
  return Math.max(0.1, Math.floor(s * 10) / 10);
}

function unitsLabel(n: number): string {
  return `Use ${n} unit${n === 1 ? "" : "s"}`;
}

/**
 * The smaller request for `code`, or null when this refusal is not about size or nothing can shrink.
 * Units shrink first (they scale the work and the answer linearly); a request of one unit shrinks its
 * window instead.
 */
export function smaller(req: Shrinkable, code: string, detail: string | null): Smaller | null {
  if (code === "too_many_units") {
    const bound = unitBound(detail);
    if (req.units === undefined || req.units <= bound) return null;
    return { ...req, units: bound, label: unitsLabel(bound) };
  }
  if (code !== "slow" && code !== "too_large" && code !== "body_estimate_too_large") return null;
  const factor = fitFraction(detail) ?? 0.5;
  if (req.units !== undefined && req.units > 1) {
    const units = Math.max(1, Math.floor(req.units * factor));
    return { ...req, units, label: unitsLabel(units) };
  }
  if (req.windowS !== undefined) {
    const windowS = niceSeconds(req.windowS * factor);
    if (windowS >= req.windowS) return null;
    return { ...req, windowS, label: `Use ${windowS} s window` };
  }
  return null;
}
