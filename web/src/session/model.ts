// The session heatmap's model: which NWB file a session opens, the three
// requests the page waits on (`/compute/events`, the units table, `/compute/rates` at the display
// parameters) and the rows the heatmap draws. Pure functions plus loaders.
//
// The server computes every number the page shows except the per-unit mean of the server's z over
// the defaults block's response window, which ranks the rows and names their direction ("fire more"
// at a mean z of +1 or more, "fire less" at -1 or less): interactive inspection over returned data.
// The z baseline is the server's: the first edge to 0, so -1 to 0 s at `pre_s=1`.
import type { FetchFn, TokenSource } from "../auth/apiGet";
import type { AuthConfig } from "../auth/config";
import { type CatalogResult, get, type Row } from "../catalog/catalog";
import { parseTable } from "../catalog/file";
import { type NormalRegion, normalizeRegion, regionColour } from "../kit/regions";
import { NO_SESSION } from "../router/dataPaths";
import { type EventType, parseEventsBody } from "../synthesis/session";
import type { TuningBody } from "../synthesis/tuning";
import { loadAllUnits, parseRates, type RatesBody } from "../units/units";

/** The server's bound on one rates request (`compute_api.MAX_UNITS`). */
export const MAX_UNITS = 152;
/** The display window and bin (explicit values; the z baseline is -1 to 0 s). */
export const PRE_S = 1;
export const POST_S = 2;
export const BIN_S = 0.05;
/** The mean z at or beyond which a unit fires more (or, negated, less). */
export const THRESHOLD = 1;
/** The colour scale's end: z beyond +-3 draws at full colour. */
export const Z_RANGE = 3;
export const BASELINE_TEXT = "−1 to 0 s, z";
export const NULL_Z_TEXT = "no baseline variance";

type Rec = Record<string, unknown>;

function isRecord(v: unknown): v is Rec {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

function list(v: unknown, name: string): unknown[] {
  if (!Array.isArray(v)) throw new Error(`"${name}" is not a list`);
  return v as unknown[];
}

function text(v: unknown, name: string): string {
  if (typeof v !== "string") throw new Error(`"${name}" is not a string`);
  return v;
}

function number(v: unknown, name: string): number {
  if (typeof v !== "number") throw new Error(`"${name}" is not a number`);
  return v;
}

// ---- the session's NWB file, from GET /catalog/studies ----

export interface NwbAsset {
  readonly asset_id: string;
  readonly n_units: number;
}

/** The session's NWB assets from the studies body; null when the session is not in it. */
export function sessionAssets(
  raw: unknown,
  dataset: string,
  subject: string,
  session: string,
): NwbAsset[] | null {
  if (!isRecord(raw)) throw new Error("the response is not an object");
  for (const d of list(raw.datasets, "datasets")) {
    if (!isRecord(d) || d.dataset !== dataset) continue;
    for (const s of list(d.subjects, "subjects")) {
      if (!isRecord(s) || s.subject !== subject) continue;
      for (const ses of list(s.sessions, "sessions")) {
        if (!isRecord(ses)) continue;
        const label = ses.session === null || ses.session === "" ? NO_SESSION : ses.session;
        if (label !== session) continue;
        return list(ses.nwb ?? [], "nwb").map((n, i) => {
          if (!isRecord(n)) throw new Error(`nwb[${i}] is not an object`);
          return {
            asset_id: text(n.asset_id, "nwb.asset_id"),
            n_units: typeof n.n_units === "number" ? n.n_units : 0,
          };
        });
      }
    }
  }
  return null;
}

/** The NWB asset with the most units, ties by `asset_id`; null when there is none. */
export function pickAsset(assets: readonly NwbAsset[]): NwbAsset | null {
  const sorted = [...assets].sort(
    (a, b) => b.n_units - a.n_units || a.asset_id.localeCompare(b.asset_id),
  );
  return sorted[0] ?? null;
}

// ---- /compute/events ----

export interface SessionEvents {
  readonly dataset: string;
  /** True when the dataset has no recipe file and the server used the default rule. */
  readonly recipeDefault: boolean;
  readonly types: readonly EventType[];
}

export function parseSessionEvents(raw: unknown): SessionEvents {
  const body = parseEventsBody(raw);
  const recipe = isRecord(raw) && isRecord(raw.recipe) ? raw.recipe : {};
  return { dataset: body.dataset, recipeDefault: recipe.default === true, types: body.event_types };
}

/** Event types with at least one event: average types first, each in the server's order. */
export function pickableTypes(types: readonly EventType[]): EventType[] {
  const kept = types.filter((t) => t.n_events > 0);
  return [
    ...kept.filter((t) => t.averaging === "average"),
    ...kept.filter((t) => t.averaging !== "average"),
  ];
}

export function typeLabel(name: string): string {
  return name.replaceAll("_", " ");
}

// ---- /compute/rates at the display parameters ----

export interface Defaults {
  /** The defaults block the server used (a dataset id, or "default"). */
  readonly block: string;
  /** The analysis default's baseline, as the server wrote it (e.g. "none"). */
  readonly baseline: string;
  readonly responseWindow: readonly [number, number];
}

export interface SessionRates {
  readonly rates: RatesBody;
  readonly defaults: Defaults;
}

export function parseSessionRates(raw: unknown): SessionRates {
  const rates = parseRates(raw);
  if (!isRecord(raw) || !isRecord(raw.defaults)) {
    throw new Error('the response has no "defaults" object');
  }
  const d = raw.defaults;
  const w = list(d.response_window_s, "defaults.response_window_s");
  return {
    rates,
    defaults: {
      block: text(d.block, "defaults.block"),
      baseline: typeof d.baseline === "string" ? d.baseline : String(d.baseline ?? ""),
      responseWindow: [number(w[0], "response_window_s[0]"), number(w[1], "response_window_s[1]")],
    },
  };
}

/** `all` within the bound, else the range of the first MAX_UNITS unit indices. */
export function unitSelection(indices: readonly number[]): string {
  const sorted = [...indices].sort((a, b) => a - b);
  if (sorted.length <= MAX_UNITS) return "all";
  return `${sorted[0]}-${sorted[MAX_UNITS - 1]}`;
}

/** The rates path at the display parameters; `raster` asks for each unit's per-trial spikes too. */
export function ratesPath(
  assetId: string,
  eventType: string,
  units: string,
  raster = false,
): string {
  const q = new URLSearchParams({
    asset_id: assetId,
    units,
    align: `event:${eventType}`,
    pre_s: String(PRE_S),
    post_s: String(POST_S),
    bin_s: String(BIN_S),
    zscore: "1",
    raster: raster ? "1" : "0",
  });
  return `/compute/rates?${q.toString()}`;
}

// ---- the units table ----

export interface UnitLabel {
  readonly unit_index: number;
  readonly location: string | null;
}

/** A stored `units` part (the units route's first page) as labels; refuses a partial table. */
export function storedUnitLabels(raw: unknown): UnitLabel[] {
  const table = parseTable(raw);
  if (table.more) throw new Error("the stored units part holds only the first page");
  return unitLabels(table.rows);
}

export function unitLabels(rows: readonly Row[]): UnitLabel[] {
  return rows.flatMap((r) =>
    typeof r.unit_index === "number"
      ? [
          {
            unit_index: r.unit_index,
            location: typeof r.location === "string" && r.location !== "" ? r.location : null,
          },
        ]
      : [],
  );
}

// ---- the heatmap rows ----

export type Direction = "more" | "less" | "flat" | "null";

export interface HeatRow {
  readonly unit: number;
  readonly location: string;
  /** The server's z per bin; null when the unit has no baseline variance. */
  readonly z: readonly (number | null)[] | null;
  /** Mean z over the response window; null with `z`. */
  readonly delta: number | null;
  readonly direction: Direction;
}

export interface Group {
  readonly key: string;
  /** Every author label in the group (one, unless two spellings share a key). */
  readonly label: string;
  readonly cssVar: string;
  readonly rows: readonly HeatRow[];
  readonly more: number;
  readonly less: number;
}

export interface Totals {
  readonly more: number;
  readonly less: number;
  readonly flat: number;
  /** Units with no baseline variance: counted in neither direction. */
  readonly nullUnits: readonly number[];
}

export interface Heatmap {
  readonly edges: readonly number[];
  readonly groups: readonly Group[];
  readonly totals: Totals;
  readonly units: number;
}

const NO_LOCATION = "no location";
const EDGE_TOL = 1e-9;

/** Mean of the finite `z` over bins lying wholly inside `window`; null when none. */
export function windowMean(
  z: readonly (number | null)[],
  edges: readonly number[],
  window: readonly [number, number],
): number | null {
  let sum = 0;
  let n = 0;
  for (let b = 0; b < z.length; b += 1) {
    const lo = edges[b];
    const hi = edges[b + 1];
    const v = z[b];
    if (lo === undefined || hi === undefined || v === null || v === undefined) continue;
    if (lo >= window[0] - EDGE_TOL && hi <= window[1] + EDGE_TOL && Number.isFinite(v)) {
      sum += v;
      n += 1;
    }
  }
  return n === 0 ? null : sum / n;
}

export function direction(delta: number | null): Direction {
  if (delta === null) return "null";
  if (delta >= THRESHOLD) return "more";
  if (delta <= -THRESHOLD) return "less";
  return "flat";
}

/**
 * The heatmap: one row per unit in the rates body, grouped by side-kept region (`regions.ts`), groups
 * in key order, rows by mean z (highest first) with no-baseline-variance rows last, ties by unit.
 */
export function buildHeatmap(
  labels: readonly UnitLabel[],
  rates: RatesBody,
  window: readonly [number, number],
): Heatmap {
  const where = new Map(labels.map((l) => [l.unit_index, l.location]));
  const groups = new Map<string, { region: NormalRegion; labels: string[]; rows: HeatRow[] }>();
  for (const u of rates.units) {
    const location = where.get(u.unit_index) ?? NO_LOCATION;
    const delta = u.z === null ? null : windowMean(u.z, rates.edges, window);
    const row: HeatRow = {
      unit: u.unit_index,
      location,
      z: u.z,
      delta,
      direction: direction(delta),
    };
    const region = normalizeRegion(location);
    const g = groups.get(region.key) ?? { region, labels: [], rows: [] };
    if (!g.labels.includes(location)) g.labels.push(location);
    g.rows.push(row);
    groups.set(region.key, g);
  }
  const out: Group[] = [...groups.entries()]
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([key, g]) => {
      const rows = [...g.rows].sort((a, b) => {
        if (a.delta === null && b.delta !== null) return 1;
        if (b.delta === null && a.delta !== null) return -1;
        if (a.delta !== null && b.delta !== null && a.delta !== b.delta) return b.delta - a.delta;
        return a.unit - b.unit;
      });
      return {
        key,
        label: g.labels.join(" / "),
        cssVar: regionColour(g.labels[0] ?? key).cssVar,
        rows,
        more: rows.filter((r) => r.direction === "more").length,
        less: rows.filter((r) => r.direction === "less").length,
      };
    });
  const all = out.flatMap((g) => g.rows);
  return {
    edges: rates.edges,
    groups: out,
    units: all.length,
    totals: {
      more: all.filter((r) => r.direction === "more").length,
      less: all.filter((r) => r.direction === "less").length,
      flat: all.filter((r) => r.direction === "flat").length,
      nullUnits: all
        .filter((r) => r.direction === "null")
        .map((r) => r.unit)
        .sort((a, b) => a - b),
    },
  };
}

/** Row height: 10 px up to 45 units, shrinking to 3 px at 152. */
export function rowPx(units: number): number {
  if (units <= 0) return 10;
  return Math.max(3, Math.min(10, Math.floor(456 / units)));
}

/** A signed z for display: "+1.7", "−0.4" (U+2212). */
export function signed(v: number): string {
  const r = Math.round(v * 10) / 10;
  if (r === 0) return "0.0";
  return r > 0 ? `+${r.toFixed(1)}` : `−${Math.abs(r).toFixed(1)}`;
}

/** Seconds as the page prints them: "0 to 1 s". */
export function windowText(w: readonly [number, number]): string {
  return `${w[0]} to ${w[1]} s`;
}

// ---- loaders ----

type Config = Pick<AuthConfig, "apiUrl">;

export function loadStudies(config: Config, tokens: TokenSource, fetchFn: FetchFn, ms: number) {
  return get(config, "/catalog/studies", tokens, fetchFn, ms, (raw) => raw);
}

export function loadEvents(
  config: Config,
  assetId: string,
  tokens: TokenSource,
  fetchFn: FetchFn,
  ms: number,
) {
  const path = `/compute/events?${new URLSearchParams({ asset_id: assetId }).toString()}`;
  return get(config, path, tokens, fetchFn, ms, parseSessionEvents);
}

export async function loadUnitLabels(
  config: Config,
  assetId: string,
  tokens: TokenSource,
  fetchFn: FetchFn,
): Promise<CatalogResult<UnitLabel[]>> {
  const r = await loadAllUnits(config, assetId, tokens, fetchFn);
  return r.kind === "loaded" ? { kind: "loaded", value: unitLabels(r.value) } : r;
}

export function loadRates(
  config: Config,
  assetId: string,
  eventType: string,
  units: string,
  tokens: TokenSource,
  fetchFn: FetchFn,
  ms: number,
) {
  return get(config, ratesPath(assetId, eventType, units), tokens, fetchFn, ms, parseSessionRates);
}

// ---- the stored per-session summary (GET /catalog/session-summary) ----

/** What the summary route answered, narrowed to what the page reads. */
export type SummaryAnswer =
  | { readonly status: "missing" }
  | {
      readonly status: "built";
      readonly assetId: string;
      readonly builtAt: string;
      readonly display: {
        readonly pre_s: number;
        readonly post_s: number;
        readonly bin_s: number;
        readonly z_baseline_s: readonly number[];
      };
      /** Part bodies by name (inline delivery), else null. */
      readonly parts: Rec | null;
      /** Presigned GET URLs by name (presigned delivery), else null. */
      readonly partUrls: Record<string, string> | null;
      /** True when the manifest says the stored `units` part holds every row of the table. */
      readonly unitsComplete: boolean;
    };

export function parseSummaryAnswer(raw: unknown): SummaryAnswer {
  if (!isRecord(raw)) throw new Error("the summary response is not an object");
  if (raw.status === "missing") return { status: "missing" };
  if (raw.status !== "built") throw new Error('the summary "status" is neither missing nor built');
  const m = raw.manifest;
  if (!isRecord(m) || !isRecord(m.display))
    throw new Error('the summary has no "manifest.display"');
  const d = m.display;
  const display = {
    pre_s: number(d.pre_s, "display.pre_s"),
    post_s: number(d.post_s, "display.post_s"),
    bin_s: number(d.bin_s, "display.bin_s"),
    z_baseline_s: list(d.z_baseline_s, "display.z_baseline_s").map((v) =>
      number(v, "z_baseline_s"),
    ),
  };
  const table = m.units_table;
  const common = {
    status: "built" as const,
    assetId: text(m.asset_id, "manifest.asset_id"),
    builtAt: text(m.built_at, "manifest.built_at"),
    display,
    unitsComplete: isRecord(table) && table.more === false,
  };
  if (raw.delivery === "inline") {
    if (!isRecord(raw.parts)) throw new Error('the summary has no "parts" object');
    return { ...common, parts: raw.parts, partUrls: null };
  }
  if (raw.delivery === "presigned") {
    if (!isRecord(raw.part_urls)) throw new Error('the summary has no "part_urls" object');
    const urls: Record<string, string> = {};
    for (const [k, v] of Object.entries(raw.part_urls)) urls[k] = text(v, `part_urls.${k}`);
    return { ...common, parts: null, partUrls: urls };
  }
  throw new Error('the summary "delivery" is neither inline nor presigned');
}

/** The stored part for an event type: its body (inline delivery) or its presigned URL. */
export type StoredChoice =
  | { readonly kind: "inline"; readonly body: unknown; readonly builtAt: string }
  | { readonly kind: "url"; readonly url: string; readonly builtAt: string };

/** The part `name` when the summary is built for this file and holds it; else null (live). */
export function chooseStoredPart(
  summary: SummaryAnswer | null,
  assetId: string,
  name: string,
): StoredChoice | null {
  if (summary === null || summary.status !== "built" || summary.assetId !== assetId) return null;
  const body = summary.parts?.[name];
  if (body !== undefined) return { kind: "inline", body, builtAt: summary.builtAt };
  const url = summary.partUrls?.[name];
  if (url !== undefined) return { kind: "url", url, builtAt: summary.builtAt };
  return null;
}

/** The stored `events` part for this file, else null and the page asks `/compute/events`. */
export function chooseStoredEvents(
  summary: SummaryAnswer | null,
  assetId: string,
): StoredChoice | null {
  return chooseStoredPart(summary, assetId, "events");
}

/**
 * The stored `units` part for this file when the manifest says it holds the whole table; else null
 * and the page pages through the units route.
 */
export function chooseStoredUnits(
  summary: SummaryAnswer | null,
  assetId: string,
): StoredChoice | null {
  if (summary === null || summary.status !== "built" || !summary.unitsComplete) return null;
  return chooseStoredPart(summary, assetId, "units");
}

/**
 * Stored or live: the stored part when the summary is built for this file at the page's display
 * settings (pre_s 1, post_s 2, bin_s 0.05, z baseline -1 to 0 s) and holds a `rates-<type>` part; else
 * null, and the page computes live.
 */
export function chooseStored(
  summary: SummaryAnswer | null,
  assetId: string,
  eventType: string,
): StoredChoice | null {
  if (summary === null || summary.status !== "built" || summary.assetId !== assetId) return null;
  const d = summary.display;
  const same =
    d.pre_s === PRE_S &&
    d.post_s === POST_S &&
    d.bin_s === BIN_S &&
    d.z_baseline_s.length === 2 &&
    d.z_baseline_s[0] === -PRE_S &&
    d.z_baseline_s[1] === 0;
  if (!same) return null;
  return chooseStoredPart(summary, assetId, `rates-${eventType}`);
}

export function loadSummary(
  config: Config,
  dataset: string,
  subject: string,
  session: string,
  tokens: TokenSource,
  fetchFn: FetchFn,
  ms: number,
) {
  const q = new URLSearchParams({ dataset, subject, session });
  return get(
    config,
    `/catalog/session-summary?${q.toString()}`,
    tokens,
    fetchFn,
    ms,
    parseSummaryAnswer,
  );
}

export interface RatesAnswer {
  readonly rates: SessionRates;
  /** The summary's `built_at` when the rates came from it, else null (computed live). */
  readonly storedAt: string | null;
}

export interface StoredOrLive<T> {
  readonly value: T;
  /** The summary's `built_at` when the value came from it, else null (asked live). */
  readonly storedAt: string | null;
}

/**
 * One part of the page: parsed from the stored part when `stored` is set, else (or when the part
 * cannot be fetched or read) from `live`. A stored-part failure is logged, never shown.
 */
export async function loadStoredOrLive<T>(
  stored: StoredChoice | null,
  parse: (raw: unknown) => T,
  live: () => Promise<CatalogResult<T>>,
  fetchFn: FetchFn,
  ms: number,
): Promise<CatalogResult<StoredOrLive<T>>> {
  if (stored !== null) {
    try {
      let raw: unknown = stored.kind === "inline" ? stored.body : null;
      if (stored.kind === "url") {
        const ctl = new AbortController();
        const timer = setTimeout(() => ctl.abort(), ms);
        try {
          const res = await fetchFn(stored.url, { signal: ctl.signal });
          if (!res.ok) throw new Error(`the stored part answered HTTP ${res.status}`);
          raw = await res.json();
        } finally {
          clearTimeout(timer);
        }
      }
      return { kind: "loaded", value: { value: parse(raw), storedAt: stored.builtAt } };
    } catch (err) {
      console.warn("the stored summary part could not be read; computing live", err);
    }
  }
  const r = await live();
  return r.kind === "loaded" ? { kind: "loaded", value: { value: r.value, storedAt: null } } : r;
}

/** The rates for the chosen type, stored or live (`loadStoredOrLive` with the rates parser). */
export async function loadRatesStoredOrLive(
  stored: StoredChoice | null,
  live: () => Promise<CatalogResult<SessionRates>>,
  fetchFn: FetchFn,
  ms: number,
): Promise<CatalogResult<RatesAnswer>> {
  const r = await loadStoredOrLive(stored, parseSessionRates, live, fetchFn, ms);
  return r.kind === "loaded"
    ? { kind: "loaded", value: { rates: r.value.value, storedAt: r.value.storedAt } }
    : r;
}

// ---- the exploratory tuning screen ----

/** The level bounds the summary builder uses for a stored tuning attribute. */
export const MIN_LEVELS = 2;
export const MAX_LEVELS = 12;

/**
 * The attribute the page screens when no stored part holds one: the first attribute, in the events'
 * order, with 2 to 12 levels (the summary builder's rule, so an identifier column such as `scene_id`
 * is never tried); null when the type has none.
 */
export function tuningAttribute(type: EventType): string | null {
  const levels = new Map<string, Set<string>>();
  const order: string[] = [];
  for (const e of type.events) {
    for (const [name, value] of Object.entries(e.attributes)) {
      if (value === null || value === undefined) continue;
      let seen = levels.get(name);
      if (seen === undefined) {
        seen = new Set();
        levels.set(name, seen);
        order.push(name);
      }
      seen.add(String(value));
    }
  }
  return (
    order.find((n) => {
      const k = levels.get(n)?.size ?? 0;
      return k >= MIN_LEVELS && k <= MAX_LEVELS;
    }) ?? null
  );
}

/** The stored `tuning-<type>-<attribute>` part for this file and type, with its attribute; else null. */
export function chooseStoredTuning(
  summary: SummaryAnswer | null,
  assetId: string,
  eventType: string,
): { readonly choice: StoredChoice; readonly attribute: string } | null {
  if (summary === null || summary.status !== "built" || summary.assetId !== assetId) return null;
  const prefix = `tuning-${eventType}-`;
  const names = [...Object.keys(summary.parts ?? {}), ...Object.keys(summary.partUrls ?? {})];
  const name = names.find((n) => n.startsWith(prefix));
  if (name === undefined) return null;
  const choice = chooseStoredPart(summary, assetId, name);
  return choice === null ? null : { choice, attribute: name.slice(prefix.length) };
}

/** Units that passed the screen and the screen's settings, as the page marks them. */
export interface TuningMarks {
  readonly attribute: string;
  readonly levels: readonly string[];
  readonly counts: Readonly<Record<string, number>>;
  readonly alpha: number | null;
  readonly screened: ReadonlySet<number>;
  /** The summary's `built_at` when the screen came from it, else null (asked live). */
  readonly storedAt: string | null;
}

export function tuningMarks(body: TuningBody, storedAt: string | null): TuningMarks {
  return {
    attribute: body.attribute,
    levels: body.levels,
    counts: body.n_per_level,
    alpha: body.alpha,
    screened: new Set(body.units.filter((u) => u.screened).map((u) => u.unit_index)),
    storedAt,
  };
}

/** What the rail shows for the screen: loading, the marks, or why there are none. */
export type TuningState =
  | { readonly kind: "loading" }
  | { readonly kind: "marks"; readonly marks: TuningMarks }
  | { readonly kind: "none"; readonly reason: string };

/**
 * The view the session page draws: the heatmap, one PSTH per unit (`?view=multiples`), or flexible
 * inspection (`?view=inspect`).
 */
export type SessionLayout = "heatmap" | "multiples" | "inspect";

export function layoutFromSearch(search: string): SessionLayout {
  const v = new URLSearchParams(search).get("view");
  return v === "multiples" || v === "inspect" ? v : "heatmap";
}

/** The inspect view's query for an event type (the heatmap keeps its choice in page state). */
export function inspectQuery(eventType: string): string {
  return `?${new URLSearchParams({ view: "inspect", align: eventType }).toString()}`;
}

/** The event type the URL's `align` names when it is pickable, else the first pickable type. */
export function alignFromSearch(search: string, types: readonly EventType[]): string {
  const a = new URLSearchParams(search).get("align");
  return types.find((t) => t.name === a)?.name ?? types[0]?.name ?? "";
}

/** `search` with `view` set for `layout` (the heatmap is the default and leaves it out). */
export function searchWithLayout(search: string, layout: SessionLayout): string {
  const q = new URLSearchParams(search);
  q.delete("view");
  if (layout !== "heatmap") q.set("view", layout);
  const s = q.toString();
  return s === "" ? "" : `?${s}`;
}
