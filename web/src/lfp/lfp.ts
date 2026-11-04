// The LFP tab model: the URL state (`series=`, `channel=`, `start=`, `dur=`), the
// `GET /compute/overview` and `GET /compute/spectrogram` requests and responses, the LFP series of a
// file, and the window arithmetic for drag, zoom and pan. Pure functions plus loaders. The web draws
// what the server computed: the window's `power_db` and trace are kept exactly as sent.
import { type FetchFn, TIMEOUT_MS, type TokenSource } from "../auth/apiGet";
import type { AuthConfig } from "../auth/config";
import { type CatalogResult, get, type Row } from "../catalog/catalog";
import { loadTable } from "../catalog/file";
import { errorText } from "../errors";
import { type Overview, parseOverviewBinary, parseOverviewReply } from "./overview";

/** The server's limits on a spectrogram window (`MAX_SPECTROGRAM_WINDOW_S`, `SPEC_SEGMENT_S`). */
export const MAX_WINDOW_S = 1200;
export const MIN_WINDOW_S = 1;
/** The code of the 404 the overview route sends before the file's overview is built. */
export const OVERVIEW_NOT_BUILT = "overview_not_built";
/**
 * The same 404 from an API older than the uniform body, which sent only this sentence as `error`.
 * Kept for one release, so a web app published before the API deploy still says "being prepared".
 */
const OLD_NO_OVERVIEW = "overview not built for this file version";

/** Whether a failed overview load means the overview is not built yet. */
export function overviewNotBuilt(failure: { code: string | null; message: string }): boolean {
  return (
    failure.code === OVERVIEW_NOT_BUILT ||
    (failure.code === null && failure.message === OLD_NO_OVERVIEW)
  );
}
const MAX_PAGES = 10;

export interface LfpState {
  /** The series path (`/acquisition/LFP`); "" picks the first LFP series. */
  series: string;
  channel: number;
  start: number | null;
  dur: number | null;
}

export const DEFAULTS: LfpState = { series: "", channel: 0, start: null, dur: null };

export interface TimeWindow {
  start: number;
  dur: number;
}

/** The window of `state`, or null when it has none. */
export function windowOf(state: LfpState): TimeWindow | null {
  return state.start === null || state.dur === null ? null : { start: state.start, dur: state.dur };
}

function finite(text: string | null, min: number): number | null {
  if (text === null || text.trim() === "") return null;
  const n = Number(text);
  return Number.isFinite(n) && n >= min ? n : null;
}

/** The state in `search`; an absent or malformed value falls back to its default. */
export function parseLfpState(search: string): LfpState {
  const q = new URLSearchParams(search);
  const channel = Number(q.get("channel"));
  const start = finite(q.get("start"), 0);
  const dur = finite(q.get("dur"), MIN_WINDOW_S);
  const both = start !== null && dur !== null;
  return {
    series: q.get("series") ?? "",
    channel: Number.isInteger(channel) && channel > 0 ? channel : 0,
    start: both ? start : null,
    dur: both ? dur : null,
  };
}

/** `search` with the LFP parameters set from `state` (other parameters kept); defaults are left out. */
export function lfpSearch(search: string, state: LfpState): string {
  const q = new URLSearchParams(search);
  for (const k of ["series", "channel", "start", "dur"]) q.delete(k);
  if (state.series !== "") q.set("series", state.series);
  if (state.channel !== 0) q.set("channel", String(state.channel));
  if (state.start !== null && state.dur !== null) {
    q.set("start", String(state.start));
    q.set("dur", String(state.dur));
  }
  const text = q.toString();
  return text === "" ? "" : `?${text}`;
}

export function overviewPath(assetId: string, series: string): string {
  return `/compute/overview?${new URLSearchParams({ asset_id: assetId, series }).toString()}`;
}

export function spectrogramPath(
  assetId: string,
  series: string,
  channel: number,
  win: TimeWindow,
): string {
  const q = new URLSearchParams({
    asset_id: assetId,
    series,
    channel: String(channel),
    start_s: String(win.start),
    duration_s: String(win.dur),
  });
  return `/compute/spectrogram?${q.toString()}`;
}

// --- the file's LFP series ---------------------------------------------------------------------------

export interface LfpSeries {
  path: string;
  rateHz: number;
  samples: number;
  channels: number;
}

/** The series rows that are electrical, two-dimensional and uniformly sampled. */
export function lfpSeries(rows: readonly Row[]): LfpSeries[] {
  const out: LfpSeries[] = [];
  for (const r of rows) {
    const shape = r.shape;
    if (
      typeof r.path !== "string" ||
      typeof r.neurodata_type !== "string" ||
      !/ElectricalSeries|LFP/.test(r.neurodata_type) ||
      typeof r.rate_hz !== "number" ||
      !(r.rate_hz > 0) ||
      !Array.isArray(shape) ||
      shape.length !== 2 ||
      !shape.every((n) => typeof n === "number")
    ) {
      continue;
    }
    out.push({
      path: r.path,
      rateHz: r.rate_hz,
      samples: shape[0] as number,
      channels: shape[1] as number,
    });
  }
  return out;
}

export async function loadAllSeries(
  config: Pick<AuthConfig, "apiUrl">,
  assetId: string,
  tokens: TokenSource,
  fetchFn: FetchFn,
): Promise<CatalogResult<Row[]>> {
  const rows: Row[] = [];
  for (let page = 0; page < MAX_PAGES; page += 1) {
    const r = await loadTable(config, assetId, "series", page, tokens, fetchFn);
    if (r.kind !== "loaded") return r;
    rows.push(...r.value.rows);
    if (!r.value.more) break;
  }
  return { kind: "loaded", value: rows };
}

// --- the overview ------------------------------------------------------------------------------------

export type PlainFetch = (url: string, init: RequestInit) => Promise<Response>;

function failure(message: string): CatalogResult<never> {
  return { kind: "error", message, status503: false, code: null };
}

/**
 * The whole-recording overview of `series`: the JSON body, or the binary object at the presigned URL,
 * which is fetched with `plain` and no credentials (the URL is signed; a bearer token has no place there).
 */
export async function loadOverview(
  config: Pick<AuthConfig, "apiUrl">,
  assetId: string,
  series: string,
  tokens: TokenSource,
  fetchFn: FetchFn,
  plain: PlainFetch,
  timeoutMs: number = TIMEOUT_MS,
): Promise<CatalogResult<Overview>> {
  const reply = await get(
    config,
    overviewPath(assetId, series),
    tokens,
    fetchFn,
    timeoutMs,
    parseOverviewReply,
  );
  if (reply.kind !== "loaded") return reply;
  if (reply.value.kind === "inline") return { kind: "loaded", value: reply.value.overview };
  let response: Response;
  try {
    response = await plain(reply.value.url, {
      credentials: "omit",
      signal: AbortSignal.timeout(timeoutMs),
    });
  } catch (err) {
    return failure(`Could not fetch the overview (${errorText(err)}). Try again.`);
  }
  if (!response.ok) {
    return failure(
      `The overview download answered HTTP ${response.status}. Try again; the link may have expired.`,
    );
  }
  try {
    return { kind: "loaded", value: parseOverviewBinary(await response.arrayBuffer()) };
  } catch (err) {
    return failure(
      `The overview file could not be read (${errorText(err)}). Tell your administrator.`,
    );
  }
}

// --- the window spectrogram --------------------------------------------------------------------------

export interface SpectrogramBody {
  freqs: number[];
  times: number[];
  /** dB, [frequency][time], exactly as sent. */
  power: number[][];
  trace: { times: number[]; values: number[] };
  rateHz: number;
  bytesRead: number;
  requests: number;
  computeMs: number;
  fetchMs: number;
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

function count(v: unknown): number {
  return typeof v === "number" ? v : 0;
}

export function parseSpectrogram(raw: unknown): SpectrogramBody {
  if (!isRecord(raw)) throw new Error("the response is not an object");
  const freqs = numbers(raw.freqs_hz, "freqs_hz");
  const times = numbers(raw.times_s, "times_s");
  if (!Array.isArray(raw.power_db) || raw.power_db.length !== freqs.length) {
    throw new Error('"power_db" does not hold one row per frequency');
  }
  const power = (raw.power_db as unknown[]).map((row, i) => {
    const values = numbers(row, `power_db[${i}]`);
    if (values.length !== times.length) throw new Error(`power_db[${i}] is not one value per time`);
    return values;
  });
  if (!isRecord(raw.trace)) throw new Error('"trace" is not an object');
  const trace = {
    times: numbers(raw.trace.times_s, "trace.times_s"),
    values: numbers(raw.trace.values, "trace.values"),
  };
  if (trace.times.length !== trace.values.length)
    throw new Error("the trace arrays differ in length");
  return {
    freqs,
    times,
    power,
    trace,
    rateHz: count(raw.rate_hz),
    bytesRead: count(raw.bytes_read),
    requests: count(raw.requests),
    computeMs: count(raw.compute_ms),
    fetchMs: count(raw.fetch_ms),
  };
}

export function loadSpectrogram(
  config: Pick<AuthConfig, "apiUrl">,
  assetId: string,
  series: string,
  channel: number,
  win: TimeWindow,
  tokens: TokenSource,
  fetchFn: FetchFn,
  timeoutMs: number = TIMEOUT_MS,
) {
  return get(
    config,
    spectrogramPath(assetId, series, channel, win),
    tokens,
    fetchFn,
    timeoutMs,
    parseSpectrogram,
  );
}

// --- the window: drag, zoom, pan ---------------------------------------------------------------------

const TENTH = 10;

function tenths(v: number): number {
  return Math.round(v * TENTH) / TENTH;
}

/** The longest window the UI asks for in a recording of `total` seconds. */
export function longestWindow(total: number): number {
  return Math.min(MAX_WINDOW_S, total);
}

function clampWindow(start: number, dur: number, total: number): TimeWindow {
  const d = Math.min(Math.max(MIN_WINDOW_S, dur), longestWindow(total));
  return { start: tenths(Math.min(Math.max(0, start), Math.max(0, total - d))), dur: tenths(d) };
}

/** The window a drag from `a` to `b` seconds selects: ordered, inside the recording, at most 1,200 s. */
export function windowFromDrag(a: number, b: number, total: number): TimeWindow | null {
  const lo = Math.max(0, Math.min(a, b));
  const hi = Math.min(total, Math.max(a, b));
  if (!(hi - lo >= MIN_WINDOW_S)) return null;
  return clampWindow(lo, Math.min(hi - lo, MAX_WINDOW_S), total);
}

/** `win` scaled by `factor` about its centre. Zooming out to the whole recording gives null. */
export function zoomWindow(
  win: TimeWindow | null,
  total: number,
  factor: number,
): TimeWindow | null {
  if (win === null)
    return factor < 1 ? clampWindow(total / 2 - 30, Math.min(60, total), total) : null;
  if (factor >= 1 && win.dur * factor >= total && total <= MAX_WINDOW_S) return null;
  const dur = Math.min(Math.max(MIN_WINDOW_S, win.dur * factor), longestWindow(total));
  return clampWindow(win.start + win.dur / 2 - dur / 2, dur, total);
}

/** `win` moved by `fraction` of its own length (negative is earlier), kept inside the recording. */
export function panWindow(win: TimeWindow, total: number, fraction: number): TimeWindow {
  return clampWindow(win.start + win.dur * fraction, win.dur, total);
}

// --- axis labels -------------------------------------------------------------------------------------

/** A band edge as a label: three significant digits, no trailing zeros. */
export function edgeLabel(hz: number): string {
  return String(Number(hz.toPrecision(3)));
}

/** The edges to label on the overview's frequency axis: every `every`-th one, and the last. */
export function edgeTicks(
  edges: ArrayLike<number>,
  every: number,
): { index: number; label: string }[] {
  const out: { index: number; label: string }[] = [];
  for (let i = 0; i < edges.length; i += every)
    out.push({ index: i, label: edgeLabel(edges[i] ?? 0) });
  const last = edges.length - 1;
  if (last > 0 && last % every !== 0) out.push({ index: last, label: edgeLabel(edges[last] ?? 0) });
  return out;
}
