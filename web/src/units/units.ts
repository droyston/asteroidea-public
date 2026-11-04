// The Units tab model: the URL state (`unit=`, `align=`, `pre=`, `post=`, `bin=`,
// `z=`), the `GET /compute/rates` request and response, and the align options built from the file's
// events data. Pure functions plus loaders. The web never bins: `parseRates` keeps `rate_hz` and `z`
// exactly as the server sent them and the plots draw those arrays.
import { type FetchFn, TIMEOUT_MS, type TokenSource } from "../auth/apiGet";
import type { AuthConfig } from "../auth/config";
import { type CatalogResult, get, loadAssets, type Row } from "../catalog/catalog";
import { loadTable } from "../catalog/file";
import { parseColumns } from "../events/events";

export const MAX_UNITS = 32;
/** The column of `stimuli/scenecut_info.csv` that holds each shot's first frame. */
export const SCENECUT_COLUMN = "shot_start_fr";
export const SCENECUT_PATH = "stimuli/scenecut_info.csv";
const MAX_CHOICES = 12;
const MAX_PAGES = 10;

export interface UnitsState {
  units: number[];
  align: string;
  pre: number;
  post: number;
  bin: number;
  z: boolean;
  raster: boolean;
}

export const DEFAULTS: UnitsState = {
  units: [],
  align: "",
  pre: 1,
  post: 2,
  bin: 0.05,
  z: false,
  raster: true,
};

function finite(text: string | null, fallback: number, min: number): number {
  if (text === null || text.trim() === "") return fallback;
  const n = Number(text);
  return Number.isFinite(n) && n >= min ? n : fallback;
}

/** The state in `search`; an absent or malformed value falls back to its default. */
export function parseUnitsState(search: string): UnitsState {
  const q = new URLSearchParams(search);
  const seen = new Set<number>();
  for (const part of (q.get("unit") ?? "").split(",")) {
    const n = Number(part);
    if (part.trim() !== "" && Number.isInteger(n) && n >= 0) seen.add(n);
  }
  return {
    units: [...seen].sort((a, b) => a - b).slice(0, MAX_UNITS),
    align: q.get("align") ?? "",
    pre: finite(q.get("pre"), DEFAULTS.pre, 0),
    post: finite(q.get("post"), DEFAULTS.post, 0),
    bin: finite(q.get("bin"), DEFAULTS.bin, 0),
    z: q.get("z") === "1",
    raster: q.get("raster") !== "0",
  };
}

/** `search` with the units parameters set from `state` (other parameters kept); defaults are left out. */
export function unitsSearch(search: string, state: UnitsState): string {
  const q = new URLSearchParams(search);
  for (const k of ["unit", "align", "pre", "post", "bin", "z", "raster"]) q.delete(k);
  if (state.units.length > 0) q.set("unit", state.units.join(","));
  if (state.align !== "") q.set("align", state.align);
  if (state.pre !== DEFAULTS.pre) q.set("pre", String(state.pre));
  if (state.post !== DEFAULTS.post) q.set("post", String(state.post));
  if (state.bin !== DEFAULTS.bin) q.set("bin", String(state.bin));
  if (state.z) q.set("z", "1");
  if (!state.raster) q.set("raster", "0");
  const text = q.toString();
  return text === "" ? "" : `?${text}`;
}

/** The `/compute/rates` path and query for `state`; the wire name of the z-score flag is `zscore`. */
export function ratesPath(assetId: string, state: UnitsState): string {
  const q = new URLSearchParams({
    asset_id: assetId,
    units: state.units.join(","),
    align: state.align,
    pre_s: String(state.pre),
    post_s: String(state.post),
    bin_s: String(state.bin),
    zscore: state.z ? "1" : "0",
    raster: state.raster ? "1" : "0",
  });
  return `/compute/rates?${q.toString()}`;
}

export interface Raster {
  trial: number[];
  t: number[];
}

export interface RatesUnit {
  unit_index: number;
  raster: Raster | null;
  rate_hz: (number | null)[];
  /** null when the request asked for no z-score or the server found a flat baseline. */
  z: (number | null)[] | null;
}

export interface RatesBody {
  edges: number[];
  units: RatesUnit[];
  n_trials: number;
  align: string;
  source: string;
  missing_frames: number[] | null;
  query_ms: number;
  compute_ms: number;
}

function isRecord(v: unknown): v is Row {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

function numbers(v: unknown, name: string): number[] {
  if (!Array.isArray(v) || !v.every((x) => typeof x === "number")) {
    throw new Error(`"${name}" is not a list of numbers`);
  }
  return v as number[];
}

function nullable(v: unknown, name: string): (number | null)[] {
  if (!Array.isArray(v) || !v.every((x) => x === null || typeof x === "number")) {
    throw new Error(`"${name}" is not a list of numbers`);
  }
  return v as (number | null)[];
}

function whole(v: unknown, name: string): number {
  if (typeof v !== "number") throw new Error(`"${name}" is not a number`);
  return v;
}

export function parseRates(raw: unknown): RatesBody {
  if (!isRecord(raw)) throw new Error("the response is not an object");
  if (!Array.isArray(raw.units)) throw new Error('"units" is not a list');
  const units = (raw.units as unknown[]).map((u, i): RatesUnit => {
    if (!isRecord(u)) throw new Error(`units[${i}] is not an object`);
    const r = u.raster;
    let raster: Raster | null = null;
    if (r !== undefined && r !== null) {
      if (!isRecord(r)) throw new Error(`units[${i}].raster is not an object`);
      raster = { trial: numbers(r.trial, "raster.trial"), t: numbers(r.t, "raster.t") };
    }
    return {
      unit_index: whole(u.unit_index, "unit_index"),
      raster,
      rate_hz: nullable(u.rate_hz, "rate_hz"),
      z: u.z === undefined || u.z === null ? null : nullable(u.z, "z"),
    };
  });
  return {
    edges: numbers(raw.edges, "edges"),
    units,
    n_trials: whole(raw.n_trials, "n_trials"),
    align: typeof raw.align === "string" ? raw.align : "",
    source: typeof raw.source === "string" ? raw.source : "",
    missing_frames:
      raw.missing_frames === undefined ? null : numbers(raw.missing_frames, "missing_frames"),
    query_ms: typeof raw.query_ms === "number" ? raw.query_ms : 0,
    compute_ms: typeof raw.compute_ms === "number" ? raw.compute_ms : 0,
  };
}

export function loadRates(
  config: Pick<AuthConfig, "apiUrl">,
  assetId: string,
  state: UnitsState,
  tokens: TokenSource,
  fetchFn: FetchFn,
  timeoutMs: number = TIMEOUT_MS,
) {
  return get(config, ratesPath(assetId, state), tokens, fetchFn, timeoutMs, parseRates);
}

export interface AlignOption {
  value: string;
  label: string;
}

/**
 * `intervals:<table>` for each table of the file's intervals, then `intervals:<table>:<column>=<value>`
 * for each `columns_json` column that has 2 to 12 distinct values.
 */
export function intervalOptions(intervals: readonly Row[]): AlignOption[] {
  const tables = new Map<string, Map<string, Set<string>>>();
  for (const row of intervals) {
    const table = typeof row.table_name === "string" ? row.table_name : "";
    if (table === "") continue;
    let columns = tables.get(table);
    if (columns === undefined) {
      columns = new Map();
      tables.set(table, columns);
    }
    for (const [k, v] of Object.entries(parseColumns(row.columns_json))) {
      if (v === "" || k.includes(":") || k.includes("=") || v.includes(":")) continue;
      const set = columns.get(k) ?? new Set<string>();
      set.add(v);
      columns.set(k, set);
    }
  }
  const out: AlignOption[] = [];
  for (const [table, columns] of tables) {
    out.push({ value: `intervals:${table}`, label: `Intervals: ${table}` });
    for (const [column, values] of columns) {
      if (values.size < 2 || values.size > MAX_CHOICES) continue;
      const sorted = [...values].sort((a, b) => a.localeCompare(b, undefined, { numeric: true }));
      for (const v of sorted) {
        out.push({
          value: `intervals:${table}:${column}=${v}`,
          label: `Intervals: ${table}, ${column} = ${v}`,
        });
      }
    }
  }
  return out;
}

/** The series (path) a scene-cut align reads: one with timestamps whose path says stimulus. */
export function stimulusSeries(rows: readonly Row[]): string[] {
  return rows
    .filter(
      (r) => r.has_timestamps === true && typeof r.path === "string" && /stimul/i.test(r.path),
    )
    .map((r) => String(r.path));
}

export function sceneCutOptions(
  csvAssetId: string | null,
  paths: readonly string[],
): AlignOption[] {
  if (csvAssetId === null) return [];
  return paths.map((p) => ({
    value: `frames:${csvAssetId}:${SCENECUT_COLUMN}:${p}`,
    label: paths.length > 1 ? `Movie scene cuts (${p})` : "Movie scene cuts",
  }));
}

/** `events:<id>` for a BIDS events file of the same session as the target (the server refuses others). */
export function eventsOptions(
  assets: readonly { asset_id: string; path: string; bids_session: string | null }[],
  session: string | null,
): AlignOption[] {
  return assets
    .filter((a) => a.path.endsWith("_events.tsv") && a.bids_session === session)
    .map((a) => ({ value: `events:${a.asset_id}`, label: `Events file: ${a.path}` }));
}

/** Every page of the units table, up to MAX_PAGES. */
export async function loadAllUnits(
  config: Pick<AuthConfig, "apiUrl">,
  assetId: string,
  tokens: TokenSource,
  fetchFn: FetchFn,
): Promise<CatalogResult<Row[]>> {
  const rows: Row[] = [];
  for (let page = 0; page < MAX_PAGES; page += 1) {
    const r = await loadTable(config, assetId, "units", page, tokens, fetchFn);
    if (r.kind !== "loaded") return r;
    rows.push(...r.value.rows);
    if (!r.value.more) break;
  }
  return { kind: "loaded", value: rows };
}

/** The scene-cut CSV's asset id in `dataset`, or null; pages through the dataset's CSV files. */
async function findSceneCsv(
  config: Pick<AuthConfig, "apiUrl">,
  dataset: string,
  tokens: TokenSource,
  fetchFn: FetchFn,
): Promise<string | null> {
  for (let page = 0; page < MAX_PAGES; page += 1) {
    const r = await loadAssets(
      config,
      dataset,
      { subject: "", format: "csv", page },
      tokens,
      fetchFn,
    );
    if (r.kind !== "loaded") return null;
    const hit = r.value.assets.find((a) => a.path === SCENECUT_PATH);
    if (hit !== undefined) return hit.asset_id;
    if (!r.value.more) return null;
  }
  return null;
}

export interface FileRef {
  assetId: string;
  dataset: string | null;
  subject: string | null;
  session: string | null;
}

/**
 * Scene-cut and events options for a file: the dataset's `stimuli/scenecut_info.csv` plus a stimulus
 * series with timestamps, and an events file of the same BIDS session. A failed lookup offers nothing.
 */
export async function loadExtraOptions(
  config: Pick<AuthConfig, "apiUrl">,
  file: FileRef,
  tokens: TokenSource,
  fetchFn: FetchFn,
): Promise<AlignOption[]> {
  const { dataset, subject } = file;
  if (dataset === null) return [];
  const [csv, series, siblings] = await Promise.all([
    findSceneCsv(config, dataset, tokens, fetchFn),
    loadTable(config, file.assetId, "series", 0, tokens, fetchFn),
    subject === null
      ? Promise.resolve(null)
      : loadAssets(config, dataset, { subject, format: "", page: 0 }, tokens, fetchFn),
  ]);
  const paths = series.kind === "loaded" ? stimulusSeries(series.value.rows) : [];
  const events =
    siblings?.kind === "loaded" ? eventsOptions(siblings.value.assets, file.session) : [];
  return [...sceneCutOptions(csv, paths), ...events];
}
