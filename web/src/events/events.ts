// The timeline model: NWB `intervals` rows (one lane per `table_name`, start to stop)
// and BIDS `events` rows (one lane per `trial_type`, onset plus duration) become lanes of bars. A bar
// carries its row values for the readout and a colour key taken from a chosen column. Pure functions;
// Timeline.tsx draws the result.
import type { Row } from "../catalog/catalog";
import type { Domain } from "../plots/axis";

export type Cells = Record<string, string>;

export interface Bar {
  /** Position in the lane, in row order. */
  index: number;
  start: number;
  /** End time; equal to `start` for a row with no duration. */
  end: number;
  /** Every value of the row, as text, in display order. */
  cells: Cells;
  /** The value of the colour column, or "" when the row has none. */
  value: string;
}

export interface Lane {
  name: string;
  kind: "interval" | "event";
  bars: Bar[];
}

/** The colour column that means "by trial type" on the BIDS events rows. */
export const TRIAL_TYPE = "trial_type";

/** Number of colours the palette tokens provide (`--color-cat-1` .. `--color-cat-6`). */
export const PALETTE_SIZE = 6;

function cell(v: unknown): string {
  if (v === null || v === undefined) return "";
  if (typeof v === "string") return v;
  if (typeof v === "number" || typeof v === "boolean") return String(v);
  return JSON.stringify(v);
}

/** The `columns_json` text as a flat record of text cells; text that is not a JSON object gives {}. */
export function parseColumns(json: unknown): Cells {
  if (typeof json !== "string" || json === "") return {};
  let parsed: unknown;
  try {
    parsed = JSON.parse(json);
  } catch {
    return {};
  }
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) return {};
  return Object.fromEntries(Object.entries(parsed).map(([k, v]) => [k, cell(v)]));
}

function num(v: unknown): number | null {
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

interface Source {
  lane: string;
  kind: Lane["kind"];
  start: number;
  end: number;
  cells: Cells;
}

function intervalSource(row: Row): Source | null {
  const start = num(row.start_time);
  if (start === null) return null;
  const stop = num(row.stop_time);
  const extra = parseColumns(row.columns_json);
  return {
    lane: cell(row.table_name) || "intervals",
    kind: "interval",
    start,
    end: stop === null || stop < start ? start : stop,
    cells: { start_time: cell(start), stop_time: stop === null ? "" : cell(stop), ...extra },
  };
}

function eventSource(row: Row): Source | null {
  const start = num(row.onset_s);
  if (start === null) return null;
  const duration = num(row.duration_s);
  const type = cell(row.trial_type);
  const extra = parseColumns(row.columns_json);
  return {
    lane: type === "" ? "(no trial type)" : type,
    kind: "event",
    start,
    end: duration === null || duration < 0 ? start : start + duration,
    cells: {
      onset: cell(start),
      duration: duration === null ? "" : cell(duration),
      [TRIAL_TYPE]: type,
      ...extra,
    },
  };
}

/** The column names a bar can be coloured by, in first-seen order. */
export function colourColumns(intervals: readonly Row[], events: readonly Row[]): string[] {
  const names = new Set<string>();
  if (events.some((r) => cell(r.trial_type) !== "")) names.add(TRIAL_TYPE);
  for (const row of [...intervals, ...events]) {
    for (const key of Object.keys(parseColumns(row.columns_json))) names.add(key);
  }
  return [...names];
}

function distinct(rows: readonly Row[], column: string): number {
  const seen = new Set<string>();
  for (const row of rows) {
    const v = column === TRIAL_TYPE ? cell(row.trial_type) : parseColumns(row.columns_json)[column];
    if (v !== undefined && v !== "") seen.add(v);
  }
  return seen.size;
}

/**
 * The column to colour by at first: a name that says condition or type when it has 2..12 values, else
 * the first column with 2..PALETTE_SIZE values, else the first column; "" when there are none.
 */
export function defaultColourColumn(intervals: readonly Row[], events: readonly Row[]): string {
  const columns = colourColumns(intervals, events);
  const rows = [...intervals, ...events];
  const small = (c: string, max: number) => {
    const n = distinct(rows, c);
    return n >= 2 && n <= max;
  };
  const named =
    columns.find((c) => /condition/i.test(c) && small(c, 12)) ??
    columns.find((c) => /type|category|class/i.test(c) && small(c, 12));
  return named ?? columns.find((c) => small(c, PALETTE_SIZE)) ?? columns[0] ?? "";
}

/**
 * Lanes for `intervals` (one per `table_name`, in the order first seen) then `events` (one per
 * trial type, sorted). `colour` names the column that sets `Bar.value`; rows lacking it get "".
 */
export function buildLanes(
  intervals: readonly Row[],
  events: readonly Row[],
  colour: string,
): Lane[] {
  const lanes = new Map<string, Lane>();
  const add = (s: Source | null) => {
    if (s === null) return;
    const key = `${s.kind}:${s.lane}`;
    let lane = lanes.get(key);
    if (lane === undefined) {
      lane = { name: s.lane, kind: s.kind, bars: [] };
      lanes.set(key, lane);
    }
    lane.bars.push({
      index: lane.bars.length,
      start: s.start,
      end: s.end,
      cells: s.cells,
      value: colour === "" ? "" : (s.cells[colour] ?? ""),
    });
  };
  for (const row of intervals) add(intervalSource(row));
  for (const row of events) add(eventSource(row));
  const all = [...lanes.values()];
  const eventLanes = all
    .filter((l) => l.kind === "event")
    .sort((a, b) => a.name.localeCompare(b.name));
  return [...all.filter((l) => l.kind === "interval"), ...eventLanes];
}

/** The time span of every bar; a single instant is padded to one second so the axis has a width. */
export function timeDomain(lanes: readonly Lane[]): Domain {
  let lo = Number.POSITIVE_INFINITY;
  let hi = Number.NEGATIVE_INFINITY;
  for (const lane of lanes) {
    for (const bar of lane.bars) {
      lo = Math.min(lo, bar.start);
      hi = Math.max(hi, bar.end);
    }
  }
  if (!Number.isFinite(lo)) return [0, 1];
  return hi > lo ? [lo, hi] : [lo - 0.5, hi + 0.5];
}

/** The distinct colour values over all bars, sorted; the legend order and the colour index. */
export function colourValues(lanes: readonly Lane[]): string[] {
  const seen = new Set<string>();
  for (const lane of lanes) for (const bar of lane.bars) if (bar.value !== "") seen.add(bar.value);
  return [...seen].sort((a, b) => a.localeCompare(b, undefined, { numeric: true }));
}

/** The palette slot (0-based) of `value` among `values`; -1 for "" (drawn in the neutral colour). */
export function colourSlot(values: readonly string[], value: string): number {
  if (value === "") return -1;
  const i = values.indexOf(value);
  return i < 0 ? -1 : i % PALETTE_SIZE;
}

/** The bars of `lane` that overlap `view`, as [bar, ...] in order. */
export function visibleBars(lane: Lane, view: Domain): Bar[] {
  return lane.bars.filter((b) => b.end >= view[0] && b.start <= view[1]);
}
