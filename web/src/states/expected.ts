// How long each API route usually takes, for honest progress in the loading state.
// Values are typical warm p50 times from a deployed stack; an estimate is marked as one until a
// measured time replaces it. The loading bar fills against these and the sentence says "usually N s".

export interface Expected {
  /** Usual time in milliseconds. */
  readonly ms: number;
  /** Where the number comes from. */
  readonly source: string;
}

const MEASURED = "typical warm p50";

export const EXPECTED: Readonly<Record<string, Expected>> = {
  "/catalog/studies": { ms: 4500, source: MEASURED },
  "/catalog/links": { ms: 1900, source: MEASURED },
  "/catalog/datasets": { ms: 3100, source: MEASURED },
  // The live path took 6.4 s to 8.4 s; the upper end, so the bar does not stall early.
  "/compute/rates": { ms: 8400, source: `${MEASURED} (6.4 s to 8.4 s)` },
  "/compute/events": { ms: 5900, source: `${MEASURED} (5.2 s to 5.9 s)` },
  "/catalog/session-summary": { ms: 1000, source: "estimate (well under 1 s)" },
  "/catalog/assets/{id}/tables/units": { ms: 2000, source: "estimate (~2 s)" },
};

/** A route the table does not list: an estimate, so the bar still moves. */
export const DEFAULT_EXPECTED: Expected = { ms: 3000, source: "estimate (no measured time)" };

/** The table key for a request path: query dropped, an asset id folded to `{id}`. */
export function routeKey(path: string): string {
  const bare = path.split("?")[0] ?? "";
  return bare.replace(/^\/catalog\/assets\/[^/]+\/tables\//, "/catalog/assets/{id}/tables/");
}

export function expectedFor(path: string): Expected {
  return EXPECTED[routeKey(path)] ?? DEFAULT_EXPECTED;
}

/**
 * The bar's fill, 0 to 1, after `elapsedMs` against `expectedMs`: linear to 90 % at the usual time, then
 * creeping toward 99 % (never full until the answer arrives).
 */
export function progress(elapsedMs: number, expectedMs: number): number {
  if (elapsedMs <= 0 || expectedMs <= 0) return 0;
  const r = elapsedMs / expectedMs;
  if (r <= 1) return 0.9 * r;
  return Math.min(0.99, 0.9 + 0.09 * (1 - 1 / r));
}

/** "2.4 s" (one decimal under 10 s, whole seconds above). */
export function seconds(ms: number): string {
  const s = ms / 1000;
  return s < 10 ? `${s.toFixed(1)} s` : `${Math.round(s)} s`;
}
