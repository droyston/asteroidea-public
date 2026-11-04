// Event lanes and the labels list of the signals view. Pure functions:
// the clock that puts events and labels on the signals view's time axis, the lanes built from
// `GET /compute/events` (or, for a BIDS events file, from the catalog's events rows), the marks one lane
// draws at the window's resolution (a tick per event, binned into density when two fall in one pixel),
// the nearest labels to the window, and the window that shows a label.
//
// Clocks. Events and labels are on the trials clock (labels feed the recipe's alignment). The signals
// view's axis is seconds from the LFP series' first sample. The recipe's `lfp_clock_offset_s` is the LFP
// clock minus the trials clock, and the series starts at `starting_time_s` on the LFP clock, so a trials
// time t sits at `t + offset - startingTime` on the axis.
import type { FetchFn, TokenSource } from "../auth/apiGet";
import { TIMEOUT_MS } from "../auth/apiGet";
import type { AuthConfig } from "../auth/config";
import { type CatalogResult, get, type Row } from "../catalog/catalog";
import { panWindow, type TimeWindow } from "../lfp/lfp";
import type { Domain } from "../plots/axis";
import { type EventsBody, parseEventsBody } from "../synthesis/session";
import { buildLanes } from "./events";
import type { Label } from "./labels";

export interface Clock {
  /** The recipe's `lfp_clock_offset_s`: LFP clock minus trials clock. */
  offset: number;
  /** The series' first sample on the LFP clock. */
  startingTime: number;
}

export const IDENTITY: Clock = { offset: 0, startingTime: 0 };

/** A trials-clock time on the signals axis. */
export function toAxis(clock: Clock, t: number): number {
  return t + clock.offset - clock.startingTime;
}

/** A signals-axis time on the trials clock (what the label routes store). */
export function fromAxis(clock: Clock, x: number): number {
  return x - clock.offset + clock.startingTime;
}

export interface LaneItem {
  start: number;
  /** Equal to `start` for an instant. */
  stop: number;
  /** Text for a hover title. */
  title: string;
}

export interface EventLane {
  name: string;
  items: LaneItem[];
}

export interface LaneEventsBody extends EventsBody {
  offset: number;
}

export function parseLaneEvents(raw: unknown): LaneEventsBody {
  const body = parseEventsBody(raw);
  const recipe =
    typeof raw === "object" && raw !== null && !Array.isArray(raw)
      ? (raw as Record<string, unknown>).recipe
      : null;
  const off =
    typeof recipe === "object" && recipe !== null
      ? (recipe as Record<string, unknown>).lfp_clock_offset_s
      : null;
  if (off !== undefined && off !== null && (typeof off !== "number" || !Number.isFinite(off))) {
    throw new Error('"recipe.lfp_clock_offset_s" is not a number');
  }
  return { ...body, offset: typeof off === "number" ? off : 0 };
}

export function loadLaneEvents(
  config: Pick<AuthConfig, "apiUrl">,
  assetId: string,
  tokens: TokenSource,
  fetchFn: FetchFn,
  timeoutMs: number = TIMEOUT_MS,
): Promise<CatalogResult<LaneEventsBody>> {
  const path = `/compute/events?${new URLSearchParams({ asset_id: assetId }).toString()}`;
  return get(config, path, tokens, fetchFn, timeoutMs, parseLaneEvents);
}

function laneName(name: string): string {
  const spaced = name.replaceAll("_", " ");
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}

/** One lane per event type with events, in the server's order, on the axis of `clock`. */
export function computeLanes(body: EventsBody, clock: Clock): EventLane[] {
  return body.event_types
    .filter((t) => t.events.length > 0)
    .map((t) => ({
      name: laneName(t.name),
      items: t.events
        .map((e) => {
          const start = toAxis(clock, e.onset_s);
          const stop = e.stop_s === null || e.stop_s < e.onset_s ? start : toAxis(clock, e.stop_s);
          return { start, stop, title: `${laneName(t.name)} at ${round1(start)} s` };
        })
        .sort((a, b) => a.start - b.start),
    }));
}

/** Lanes from the catalog's intervals and BIDS events rows (a file with no recipe events). */
export function catalogLanes(intervals: readonly Row[], events: readonly Row[]): EventLane[] {
  return buildLanes(intervals, events, "").map((l) => ({
    name: l.name,
    items: l.bars
      .map((b) => ({ start: b.start, stop: b.end, title: `${l.name} at ${round1(b.start)} s` }))
      .sort((a, b) => a.start - b.start),
  }));
}

/** Lanes built on the trials clock, moved onto the axis of `clock` (times and titles). */
export function shiftLanes(lanes: readonly EventLane[], clock: Clock): EventLane[] {
  return lanes.map((l) => ({
    name: l.name,
    items: l.items.map((i) => {
      const start = toAxis(clock, i.start);
      return { start, stop: toAxis(clock, i.stop), title: `${l.name} at ${round1(start)} s` };
    }),
  }));
}

/** The span every lane covers; [0, 1] when there is nothing. */
export function lanesDomain(lanes: readonly EventLane[]): Domain {
  let lo = Number.POSITIVE_INFINITY;
  let hi = Number.NEGATIVE_INFINITY;
  for (const l of lanes) {
    for (const i of l.items) {
      lo = Math.min(lo, i.start);
      hi = Math.max(hi, i.stop);
    }
  }
  if (!Number.isFinite(lo)) return [0, 1];
  return hi > lo ? [lo, hi] : [lo - 0.5, hi + 0.5];
}

function round1(v: number): number {
  return Math.round(v * 10) / 10;
}

export type Mark =
  | { kind: "span"; x0: number; x1: number; title: string }
  | { kind: "tick"; x: number; title: string }
  | { kind: "bin"; x: number; n: number };

/** A span narrower than this many pixels is drawn as a tick. */
export const SPAN_MIN_PX = 2;

/**
 * What one lane draws over `domain` in `plotW` pixels (x from 0): a span when it is at least
 * SPAN_MIN_PX wide, else a tick in its pixel column; a column that two or more items fall in is one
 * density bin with their count. Items outside the domain draw nothing.
 */
export function laneMarks(items: readonly LaneItem[], domain: Domain, plotW: number): Mark[] {
  const [d0, d1] = domain;
  const span = d1 - d0;
  if (!(span > 0) || plotW <= 0) return [];
  const px = (t: number) => ((t - d0) / span) * plotW;
  const marks: Mark[] = [];
  const columns = new Map<number, { n: number; title: string }>();
  for (const it of items) {
    if (it.stop < d0 || it.start > d1) continue;
    const x0 = Math.max(0, px(it.start));
    const x1 = Math.min(plotW, px(it.stop));
    if (x1 - x0 >= SPAN_MIN_PX) {
      marks.push({ kind: "span", x0, x1, title: it.title });
      continue;
    }
    const col = Math.min(Math.floor(plotW) - 1, Math.max(0, Math.floor(x0)));
    const had = columns.get(col);
    columns.set(col, { n: (had?.n ?? 0) + 1, title: it.title });
  }
  for (const [x, c] of [...columns.entries()].sort((a, b) => a[0] - b[0])) {
    marks.push(
      c.n === 1 ? { kind: "tick", x: x + 0.5, title: c.title } : { kind: "bin", x, n: c.n },
    );
  }
  return marks;
}

/** A label on the signals axis. */
export interface AxisLabel extends Label {
  /** `start_s` and `stop_s` on the signals axis. */
  a: number;
  b: number;
}

export function axisLabels(labels: readonly Label[], clock: Clock): AxisLabel[] {
  return labels.map((l) => ({ ...l, a: toAxis(clock, l.start_s), b: toAxis(clock, l.stop_s) }));
}

/** Seconds from the label to the window: 0 when they overlap. */
function gap(l: AxisLabel, win: Domain): number {
  if (l.b < win[0]) return win[0] - l.b;
  if (l.a > win[1]) return l.a - win[1];
  return 0;
}

/** The `n` labels nearest the window (overlapping first, then by distance, then by start). */
export function nearestLabels(labels: readonly AxisLabel[], win: Domain, n = 3): AxisLabel[] {
  return [...labels]
    .sort((x, y) => gap(x, win) - gap(y, win) || x.a - y.a || x.label_id.localeCompare(y.label_id))
    .slice(0, n);
}

/** The window that shows a label: three times its length (at least 10 s), centred, inside the recording. */
export function windowAround(a: number, b: number, total: number): TimeWindow {
  const dur = Math.max(10, (b - a) * 3);
  return panWindow({ start: (a + b) / 2 - dur / 2, dur }, total, 0);
}

/** Seconds to one decimal, for spans in text. */
export function seconds(v: number): string {
  return String(round1(v));
}
