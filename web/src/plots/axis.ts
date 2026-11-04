// Axis maths for the in-house plots: a linear scale, "nice" ticks, tick labels and zoom helpers.
// Pure functions; the Timeline draws with them and plots.test.ts pins them.

export type Domain = readonly [number, number];

/** A linear map from `domain` to `range`; a zero-width domain maps to the range start. */
export function linearScale(domain: Domain, range: Domain): (v: number) => number {
  const [d0, d1] = domain;
  const [r0, r1] = range;
  const span = d1 - d0;
  return (v) => (span === 0 ? r0 : r0 + ((v - d0) / span) * (r1 - r0));
}

/** The inverse of `linearScale`. */
export function invertScale(domain: Domain, range: Domain): (px: number) => number {
  const [d0, d1] = domain;
  const [r0, r1] = range;
  const span = r1 - r0;
  return (px) => (span === 0 ? d0 : d0 + ((px - r0) / span) * (d1 - d0));
}

/** The tick step (1, 2 or 5 times a power of ten) that gives about `count` ticks over `span`. */
export function niceStep(span: number, count: number): number {
  if (!(span > 0) || count < 1) return 1;
  const raw = span / count;
  const pow = 10 ** Math.floor(Math.log10(raw));
  const f = raw / pow;
  const m = f >= 7.5 ? 10 : f >= 3.5 ? 5 : f >= 1.5 ? 2 : 1;
  return m * pow;
}

/** Tick positions inside `domain`, on multiples of a nice step. Empty for an empty domain. */
export function niceTicks(domain: Domain, count: number): number[] {
  const [d0, d1] = domain;
  if (!(d1 > d0)) return [];
  const step = niceStep(d1 - d0, count);
  const first = Math.ceil(d0 / step - 1e-9);
  const ticks: number[] = [];
  for (let i = first; i * step <= d1 + step * 1e-9; i += 1) {
    ticks.push(Number((i * step).toPrecision(12)));
  }
  return ticks;
}

/** The label for a tick at `step` spacing: just enough decimals to tell neighbours apart. */
export function formatTick(value: number, step: number): string {
  const decimals = step >= 1 ? 0 : Math.min(6, Math.ceil(-Math.log10(step) - 1e-9));
  return value.toFixed(decimals);
}

/** `[a, b]` ordered and clamped into `full`; null when it is narrower than `minSpan`. */
export function clampDomain(a: number, b: number, full: Domain, minSpan: number): Domain | null {
  const lo = Math.max(full[0], Math.min(a, b));
  const hi = Math.min(full[1], Math.max(a, b));
  return hi - lo < minSpan ? null : [lo, hi];
}

/** `view` scaled by `factor` (below 1 zooms in) around `centre`, kept inside `full`. */
export function zoomAround(view: Domain, centre: number, factor: number, full: Domain): Domain {
  const span = (view[1] - view[0]) * factor;
  const fullSpan = full[1] - full[0];
  if (span >= fullSpan) return full;
  const lo = Math.min(Math.max(centre - span / 2, full[0]), full[1] - span);
  return [lo, lo + span];
}
