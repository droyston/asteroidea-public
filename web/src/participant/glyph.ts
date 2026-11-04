// The small response glyph on an ephys card: every unit's z over the
// session's first average event type, one row per unit (grouped units when there are more than 48),
// one column per bin (grouped to at most 40). The z is the server's, from the stored summary's
// `rates-<type>` part or the live `/compute/rates` body; the browser only orders the rows (by mean z in
// the response window, within region) and averages neighbours so a 152-unit session stays a glyph.
// Loaders live here too: stored first, live when the summary is `missing` or holds no rates part.
import { type FetchFn, TIMEOUT_MS, type TokenSource } from "../auth/apiGet";
import type { AuthConfig } from "../auth/config";
import { type CatalogResult, get } from "../catalog/catalog";
import {
  buildHeatmap,
  chooseStored,
  chooseStoredUnits,
  loadRates,
  loadRatesStoredOrLive,
  loadStoredOrLive,
  loadSummary,
  loadUnitLabels,
  parseSessionEvents,
  pickableTypes,
  type SessionRates,
  type SummaryAnswer,
  storedUnitLabels,
  type UnitLabel,
  unitSelection,
} from "../session/model";

export const MAX_GLYPH_ROWS = 48;
export const MAX_GLYPH_COLS = 40;

export interface GlyphRow {
  /** The row's position, its key. */
  readonly id: number;
  /** `var(--color-region-*)` of the group's first unit. */
  readonly cssVar: string;
  /** Mean z per column that has one; a column where no unit had a finite value is left out. */
  readonly cells: readonly { readonly x: number; readonly z: number }[];
}

export interface Glyph {
  readonly assetId: string;
  readonly eventType: string;
  readonly nUnits: number;
  readonly cols: number;
  readonly rows: readonly GlyphRow[];
  readonly window: readonly [number, number];
  /** The summary's `built_at` when stored, null when computed live. */
  readonly storedAt: string | null;
}

function mean(values: readonly (number | null | undefined)[]): number | null {
  const ok = values.filter((v): v is number => typeof v === "number" && Number.isFinite(v));
  return ok.length === 0 ? null : ok.reduce((a, b) => a + b, 0) / ok.length;
}

export function buildGlyph(
  assetId: string,
  eventType: string,
  labels: readonly UnitLabel[],
  rates: SessionRates,
  storedAt: string | null,
): Glyph {
  const heat = buildHeatmap(labels, rates.rates, rates.defaults.responseWindow);
  const units = heat.groups.flatMap((g) => g.rows.map((r) => ({ row: r, cssVar: g.cssVar })));
  const bins = rates.rates.edges.length - 1;
  const cols = Math.min(MAX_GLYPH_COLS, Math.max(1, bins));
  const colSize = Math.ceil(bins / cols);
  const nCols = Math.ceil(bins / colSize);
  const rowSize = Math.max(1, Math.ceil(units.length / MAX_GLYPH_ROWS));
  const rows: GlyphRow[] = [];
  for (let i = 0; i < units.length; i += rowSize) {
    const chunk = units.slice(i, i + rowSize);
    const cells = Array.from({ length: nCols }, (_, x) => ({
      x,
      z: mean(
        chunk.flatMap(({ row }) =>
          row.z === null ? [] : row.z.slice(x * colSize, (x + 1) * colSize),
        ),
      ),
    })).flatMap((c) => (c.z === null ? [] : [{ x: c.x, z: c.z }]));
    rows.push({ id: rows.length, cssVar: chunk[0]?.cssVar ?? "var(--color-border)", cells });
  }
  return {
    assetId,
    eventType,
    nUnits: units.length,
    cols: nCols,
    rows,
    window: [rates.rates.edges[0] ?? 0, rates.rates.edges.at(-1) ?? 0],
    storedAt,
  };
}

/** The `rates-<type>` part names a built summary holds, in the summary's own order. */
export function storedRateTypes(summary: SummaryAnswer | null): string[] {
  if (summary === null || summary.status !== "built") return [];
  const names = [...Object.keys(summary.parts ?? {}), ...Object.keys(summary.partUrls ?? {})];
  return names.filter((n) => n.startsWith("rates-")).map((n) => n.slice("rates-".length));
}

export type GlyphState =
  | { readonly kind: "loading" }
  | { readonly kind: "ready"; readonly glyph: Glyph }
  | { readonly kind: "none"; readonly reason: string };

export interface SessionExtras {
  readonly glyph: GlyphState;
  /** The unit locations of the session's file; empty when they could not be read. */
  readonly locations: readonly (string | null)[];
}

type Config = Pick<AuthConfig, "apiUrl">;

/**
 * One ephys session's glyph and unit labels. The summary is asked first; its `rates-<type>` part and
 * its complete `units` part are used when it holds them for this file; otherwise the events route
 * names the first average type and `/compute/rates` (z, no raster) and the units table are asked live.
 */
export async function loadSessionExtras(
  config: Config,
  s: { dataset: string; subject: string; session: string; assetId: string; units: number },
  tokens: TokenSource,
  fetchFn: FetchFn,
  ms: number = TIMEOUT_MS,
): Promise<SessionExtras> {
  const summaryRes = await loadSummary(
    config,
    s.dataset,
    s.subject,
    s.session === "" ? "_" : s.session,
    tokens,
    fetchFn,
    ms,
  );
  const summary = summaryRes.kind === "loaded" ? summaryRes.value : null;
  const storedUnits = chooseStoredUnits(summary, s.assetId);
  const labelsRes = await loadStoredOrLive(
    storedUnits,
    storedUnitLabels,
    () => loadUnitLabels(config, s.assetId, tokens, fetchFn),
    fetchFn,
    ms,
  );
  const labels = labelsRes.kind === "loaded" ? labelsRes.value.value : [];
  const locations = labels.map((l) => l.location);

  let eventType = storedRateTypes(summary)[0] ?? null;
  if (eventType === null) {
    const events = await get(
      config,
      `/compute/events?${new URLSearchParams({ asset_id: s.assetId }).toString()}`,
      tokens,
      fetchFn,
      ms,
      parseSessionEvents,
    );
    if (events.kind !== "loaded") return refused(events, locations);
    eventType = pickableTypes(events.value.types)[0]?.name ?? null;
  }
  if (eventType === null) {
    return { glyph: { kind: "none", reason: "no events to align to" }, locations };
  }
  const type = eventType;
  const stored = chooseStored(summary, s.assetId, type);
  const indices = labels.length > 0 ? labels.map((l) => l.unit_index) : range(s.units);
  const ratesRes = await loadRatesStoredOrLive(
    stored,
    () => loadRates(config, s.assetId, type, unitSelection(indices), tokens, fetchFn, ms),
    fetchFn,
    ms,
  );
  if (ratesRes.kind !== "loaded") return refused(ratesRes, locations);
  const { rates, storedAt } = ratesRes.value;
  return {
    glyph: { kind: "ready", glyph: buildGlyph(s.assetId, type, labels, rates, storedAt) },
    locations,
  };
}

function range(n: number): number[] {
  return Array.from({ length: n }, (_, i) => i);
}

function refused(r: CatalogResult<unknown>, locations: SessionExtras["locations"]): SessionExtras {
  return {
    glyph: { kind: "none", reason: r.kind === "error" ? r.message : "the session ended" },
    locations,
  };
}
