// The session page model: the session's files, the
// `GET /compute/events` and `GET /compute/rates?units=all&align=event:<type>` requests and their
// bodies, the unit pages (ranges of at most MAX_UNITS) and the row sort. Pure functions plus loaders.
// The web computes nothing: the window, bin, baseline, p and q are the server's (an
// `event:` request omits pre_s, post_s and bin_s and the server fills them from the analysis defaults).
import { type FetchFn, TIMEOUT_MS, type TokenSource } from "../auth/apiGet";
import type { AuthConfig } from "../auth/config";
import {
  type AssetsBody,
  type CatalogAssetRow,
  type CatalogResult,
  get,
  parseAssets,
  type Row,
} from "../catalog/catalog";
import { NO_SESSION } from "../router/dataPaths";
import { parseRates, type RatesBody } from "../units/units";

/** The server's bound on one rates request (compute_api.MAX_UNITS); a larger session pages by range. */
export const MAX_UNITS = 152;
const MAX_FILE_PAGES = 10;

type Rec = Record<string, unknown>;

function isRecord(v: unknown): v is Rec {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

function str(v: unknown, name: string): string {
  if (typeof v !== "string") throw new Error(`"${name}" is not a string`);
  return v;
}

function num(v: unknown, name: string): number {
  if (typeof v !== "number") throw new Error(`"${name}" is not a number`);
  return v;
}

function optNum(v: unknown, name: string): number | null {
  return v === null || v === undefined ? null : num(v, name);
}

/** A value as the server wrote it, for display: a list of numbers as "a to b", else its text. */
export function shown(v: unknown): string {
  if (v === null || v === undefined) return "";
  if (Array.isArray(v) && v.length === 2 && v.every((x) => typeof x === "number")) {
    return `${v[0]} to ${v[1]} s`;
  }
  if (typeof v === "string" || typeof v === "number" || typeof v === "boolean") return String(v);
  return JSON.stringify(v);
}

// ---- /compute/rates with align=event:<type> ----

export interface RatesDefaults {
  version: string;
  /** The dataset's defaults block the server used (its dataset id), or "default" (compute_api.defaults_block). */
  block: string;
  source: string;
  baseline: string;
  bin_s: number;
  pre_s: number;
  post_s: number;
  response_window_s: string;
}

export interface Responsive {
  unit_index: number;
  location: string | null;
  p: number | null;
  q: number | null;
  screened: boolean;
}

export interface SessionRates {
  rates: RatesBody;
  defaults: RatesDefaults;
  responsive: Responsive[];
}

function parseDefaults(raw: unknown): RatesDefaults {
  if (!isRecord(raw)) throw new Error('the response has no "defaults" object');
  return {
    version: shown(raw.version),
    block: str(raw.block, "defaults.block"),
    source: shown(raw.source),
    baseline: shown(raw.baseline),
    bin_s: num(raw.bin_s, "defaults.bin_s"),
    pre_s: num(raw.pre_s, "defaults.pre_s"),
    post_s: num(raw.post_s, "defaults.post_s"),
    response_window_s: shown(raw.response_window_s),
  };
}

function parseResponsive(raw: unknown): Responsive[] {
  if (!Array.isArray(raw)) throw new Error('"responsive" is not a list');
  return (raw as unknown[]).map((r, i) => {
    if (!isRecord(r)) throw new Error(`responsive[${i}] is not an object`);
    return {
      unit_index: num(r.unit_index, "responsive.unit_index"),
      location:
        r.location === null || r.location === undefined
          ? null
          : str(r.location, "responsive.location"),
      p: optNum(r.p, "responsive.p"),
      q: optNum(r.q, "responsive.q"),
      screened: r.screened === true,
    };
  });
}

export function parseSessionRates(raw: unknown): SessionRates {
  const rates = parseRates(raw);
  if (!isRecord(raw)) throw new Error("the response is not an object");
  return {
    rates,
    defaults: parseDefaults(raw.defaults),
    responsive: parseResponsive(raw.responsive),
  };
}

/** One unit page: `all` for a session within the bound, else a `<a>-<b>` range of unit indices. */
export type UnitSelection = string;

/**
 * The pages of `indices` (the file's unit indices): one `all` page at most MAX_UNITS units, else
 * consecutive ranges of at most MAX_UNITS units each.
 */
export function unitPages(indices: readonly number[], max: number = MAX_UNITS): UnitSelection[] {
  const sorted = [...indices].sort((a, b) => a - b);
  if (sorted.length === 0) return [];
  if (sorted.length <= max) return ["all"];
  const pages: string[] = [];
  for (let i = 0; i < sorted.length; i += max) {
    const chunk = sorted.slice(i, i + max);
    pages.push(`${chunk[0]}-${chunk[chunk.length - 1]}`);
  }
  return pages;
}

/** The rates path for one event type and one unit page; the server fills the window. */
export function sessionRatesPath(assetId: string, eventType: string, units: UnitSelection): string {
  const q = new URLSearchParams({
    asset_id: assetId,
    units,
    align: `event:${eventType}`,
    raster: "0",
  });
  return `/compute/rates?${q.toString()}`;
}

export function loadSessionRates(
  config: Pick<AuthConfig, "apiUrl">,
  assetId: string,
  eventType: string,
  units: UnitSelection,
  tokens: TokenSource,
  fetchFn: FetchFn,
  timeoutMs: number = TIMEOUT_MS,
) {
  const path = sessionRatesPath(assetId, eventType, units);
  return get(config, path, tokens, fetchFn, timeoutMs, parseSessionRates);
}

// ---- /compute/events ----

export interface ResolvedEvent {
  onset_s: number;
  stop_s: number | null;
  attributes: Readonly<Record<string, unknown>>;
  source: string;
}

export interface EventType {
  name: string;
  averaging: string;
  n_events: number;
  events: ResolvedEvent[];
  missing_frames: number[] | null;
}

export interface EventsBody {
  asset_id: string;
  dataset: string;
  recipe_version: string;
  event_types: EventType[];
}

function parseSource(v: unknown): string {
  if (!isRecord(v)) return shown(v);
  return [v.kind, v.ref, v.row].filter((p) => p !== null && p !== undefined).join(" ");
}

export function parseEventsBody(raw: unknown): EventsBody {
  if (!isRecord(raw)) throw new Error("the response is not an object");
  if (!Array.isArray(raw.event_types)) throw new Error('"event_types" is not a list');
  const recipe = isRecord(raw.recipe) ? raw.recipe : {};
  return {
    asset_id: str(raw.asset_id, "asset_id"),
    dataset: shown(raw.dataset),
    recipe_version: shown(recipe.version),
    event_types: (raw.event_types as unknown[]).map((t, i): EventType => {
      if (!isRecord(t)) throw new Error(`event_types[${i}] is not an object`);
      if (!Array.isArray(t.events)) throw new Error(`event_types[${i}].events is not a list`);
      const mf = t.missing_frames;
      return {
        name: str(t.name, "name"),
        averaging: str(t.averaging, "averaging"),
        n_events: num(t.n_events, "n_events"),
        events: (t.events as unknown[]).map((e, j) => {
          if (!isRecord(e)) throw new Error(`events[${j}] is not an object`);
          return {
            onset_s: num(e.onset_s, "onset_s"),
            stop_s: optNum(e.stop_s, "stop_s"),
            attributes: isRecord(e.attributes) ? e.attributes : {},
            source: parseSource(e.source),
          };
        }),
        missing_frames:
          mf === undefined || mf === null
            ? null
            : Array.isArray(mf) && mf.every((x) => typeof x === "number")
              ? (mf as number[])
              : (() => {
                  throw new Error('"missing_frames" is not a list of numbers');
                })(),
      };
    }),
  };
}

export function loadComputeEvents(
  config: Pick<AuthConfig, "apiUrl">,
  assetId: string,
  tokens: TokenSource,
  fetchFn: FetchFn,
  timeoutMs: number = TIMEOUT_MS,
) {
  const path = `/compute/events?${new URLSearchParams({ asset_id: assetId }).toString()}`;
  return get(config, path, tokens, fetchFn, timeoutMs, parseEventsBody);
}

/** A column of the Units grid: an `average` event type, or a single-trial type. */
export interface GridColumn {
  eventType: string;
  single: boolean;
  label: string;
}

/** Average types first, in the server's order, then each single type with at least one event. */
export function gridColumns(types: readonly EventType[]): GridColumn[] {
  const label = (name: string) => name.replaceAll("_", " ");
  const avg = types
    .filter((t) => t.averaging === "average" && t.n_events > 0)
    .map((t) => ({ eventType: t.name, single: false, label: label(t.name) }));
  const single = types
    .filter((t) => t.averaging === "single" && t.n_events > 0)
    .map((t) => ({ eventType: t.name, single: true, label: `${label(t.name)}, single trial` }));
  return [...avg, ...single];
}

// ---- the session's files ----

/** The assets route for one session (`session=`; `_` is "no session"), one page. */
export function sessionFilesPath(
  dataset: string,
  subject: string,
  session: string | null,
  page: number,
): string {
  const q = new URLSearchParams({ subject, session: session ?? NO_SESSION });
  if (page > 0) q.set("page", String(page));
  return `/catalog/datasets/${encodeURIComponent(dataset)}/assets?${q.toString()}`;
}

/** Every file of `subject` whose session is `session` (null: files with no session label). */
export async function loadSessionFiles(
  config: Pick<AuthConfig, "apiUrl">,
  dataset: string,
  subject: string,
  session: string | null,
  tokens: TokenSource,
  fetchFn: FetchFn,
): Promise<CatalogResult<CatalogAssetRow[]>> {
  const files: CatalogAssetRow[] = [];
  for (let page = 0; page < MAX_FILE_PAGES; page += 1) {
    const path = sessionFilesPath(dataset, subject, session, page);
    const r: CatalogResult<AssetsBody> = await get(
      config,
      path,
      tokens,
      fetchFn,
      TIMEOUT_MS,
      parseAssets,
    );
    if (r.kind !== "loaded") return r;
    // The server filters; the check keeps a server that ignores `session=` from showing other sessions.
    files.push(...r.value.assets.filter((a) => (a.bids_session ?? null) === session));
    if (!r.value.more) break;
  }
  return { kind: "loaded", value: files };
}

/** The session's NWB file (the first, by path), or null. */
export function sessionNwb(files: readonly CatalogAssetRow[]): CatalogAssetRow | null {
  const nwb = files.filter((f) => f.format === "nwb").sort((a, b) => a.path.localeCompare(b.path));
  return nwb[0] ?? null;
}

/** The unit indices of the file's units table rows. */
export function unitIndices(rows: readonly Row[]): number[] {
  return rows.flatMap((r) => (typeof r.unit_index === "number" ? [r.unit_index] : []));
}

// ---- the grid rows and their sort ----

export interface UnitRow {
  unit_index: number;
  location: string | null;
  /** Per event type: the server's responsive entry for this unit, when it sent one. */
  screen: Readonly<Record<string, Responsive>>;
}

/** One row per unit of the page, in unit order, with each type's responsive entry. */
export function unitRows(
  units: readonly number[],
  byType: Readonly<Record<string, readonly Responsive[]>>,
): UnitRow[] {
  return [...units]
    .sort((a, b) => a - b)
    .map((unit_index) => {
      const screen: Record<string, Responsive> = {};
      let location: string | null = null;
      for (const [type, list] of Object.entries(byType)) {
        const hit = list.find((r) => r.unit_index === unit_index);
        if (hit !== undefined) {
          screen[type] = hit;
          location ??= hit.location;
        }
      }
      return { unit_index, location, screen };
    });
}

export type SortKey = { by: "unit" } | { by: "location" } | { by: "p" | "q"; eventType: string };

/** `key` as the select value: `unit`, `location`, `p:<type>` or `q:<type>`. */
export function sortValue(key: SortKey): string {
  return key.by === "p" || key.by === "q" ? `${key.by}:${key.eventType}` : key.by;
}

export function parseSortValue(value: string): SortKey {
  if (value === "location") return { by: "location" };
  const [by, ...rest] = value.split(":");
  const eventType = rest.join(":");
  if ((by === "p" || by === "q") && eventType !== "") return { by, eventType };
  return { by: "unit" };
}

/**
 * Rows sorted by `key`, ties by unit index: location A to Z, p or q smallest first. A row with no
 * location or no value sorts last.
 */
export function sortRows(rows: readonly UnitRow[], key: SortKey): UnitRow[] {
  const value = (r: UnitRow): number | string | null => {
    switch (key.by) {
      case "unit":
        return r.unit_index;
      case "location":
        return r.location;
      default:
        return r.screen[key.eventType]?.[key.by] ?? null;
    }
  };
  return [...rows].sort((a, b) => {
    const va = value(a);
    const vb = value(b);
    if (va === null && vb !== null) return 1;
    if (vb === null && va !== null) return -1;
    if (va !== null && vb !== null && va !== vb) {
      if (typeof va === "string" && typeof vb === "string") return va.localeCompare(vb);
      return Number(va) - Number(vb);
    }
    return a.unit_index - b.unit_index;
  });
}
