// The Tuning tab's model: `GET /compute/tuning?units=all` for
// one (event type, attribute), its body and the attribute names an event type offers. The web
// computes nothing: SI, level means, n per level, p, q and screened are the server's.
// A refusal (`too_few_events_per_level`, `no_such_attribute`, `not_average`) arrives as the server's
// `detail` sentence, which names the counts.
import { type FetchFn, TIMEOUT_MS, type TokenSource } from "../auth/apiGet";
import type { AuthConfig } from "../auth/config";
import { get } from "../catalog/catalog";
import type { EventType } from "./session";

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

function numberMap(v: unknown, name: string): Record<string, number> {
  if (!isRecord(v)) throw new Error(`"${name}" is not an object`);
  return Object.fromEntries(Object.entries(v).map(([k, n]) => [k, num(n, `${name}.${k}`)]));
}

export interface TuningUnit {
  unit_index: number;
  location: string | null;
  level_means_hz: Readonly<Record<string, number>>;
  selectivity_index: number;
  p: number | null;
  q: number | null;
  screened: boolean;
}

export interface TuningArea {
  location: string;
  n_units: number;
  n_below_alpha: number;
  fraction: number;
  p: number | null;
}

export interface TuningBody {
  label: string;
  event_type: string;
  attribute: string;
  levels: string[];
  /** The server's count of events per level (every unit row carries the same one). */
  n_per_level: Readonly<Record<string, number>>;
  alpha: number | null;
  units: TuningUnit[];
  areas: TuningArea[];
}

export function parseTuning(raw: unknown): TuningBody {
  if (!isRecord(raw)) throw new Error("the response is not an object");
  if (!Array.isArray(raw.units)) throw new Error('"units" is not a list');
  if (!Array.isArray(raw.areas)) throw new Error('"areas" is not a list');
  const settings = isRecord(raw.settings) ? raw.settings : {};
  const levels = settings.levels;
  if (!Array.isArray(levels) || !levels.every((l) => typeof l === "string")) {
    throw new Error('"settings.levels" is not a list of text');
  }
  let counts: Record<string, number> = {};
  const units = (raw.units as unknown[]).map((u, i): TuningUnit => {
    if (!isRecord(u)) throw new Error(`units[${i}] is not an object`);
    if (i === 0) counts = numberMap(u.n_per_level, "n_per_level");
    return {
      unit_index: num(u.unit_index, "unit_index"),
      location:
        u.location === null || u.location === undefined ? null : str(u.location, "location"),
      level_means_hz: numberMap(u.level_means_hz, "level_means_hz"),
      selectivity_index: num(u.selectivity_index, "selectivity_index"),
      p: optNum(u.p, "p"),
      q: optNum(u.q, "q"),
      screened: u.screened === true,
    };
  });
  const areas = (raw.areas as unknown[]).map((a, i): TuningArea => {
    if (!isRecord(a)) throw new Error(`areas[${i}] is not an object`);
    return {
      location: str(a.location, "area.location"),
      n_units: num(a.n_units, "area.n_units"),
      n_below_alpha: num(a.n_below_alpha, "area.n_below_alpha"),
      fraction: num(a.fraction, "area.fraction"),
      p: optNum(a.p, "area.p"),
    };
  });
  return {
    label: str(raw.label, "label"),
    event_type: str(raw.event_type, "event_type"),
    attribute: str(raw.attribute, "attribute"),
    levels: levels as string[],
    n_per_level: counts,
    alpha: optNum(settings.alpha, "settings.alpha"),
    units,
    areas,
  };
}

/** The tuning path: the whole file (`units=all`) for one event type and attribute. */
export function tuningPath(assetId: string, eventType: string, attribute: string): string {
  const q = new URLSearchParams({ asset_id: assetId, event: eventType, attribute, units: "all" });
  return `/compute/tuning?${q.toString()}`;
}

export function loadTuning(
  config: Pick<AuthConfig, "apiUrl">,
  assetId: string,
  eventType: string,
  attribute: string,
  tokens: TokenSource,
  fetchFn: FetchFn,
  timeoutMs: number = TIMEOUT_MS,
) {
  const path = tuningPath(assetId, eventType, attribute);
  return get(config, path, tokens, fetchFn, timeoutMs, parseTuning);
}

/** The attribute names an event type's events carry, sorted. */
export function attributeNames(type: EventType): string[] {
  const names = new Set<string>();
  for (const e of type.events) for (const k of Object.keys(e.attributes)) names.add(k);
  return [...names].sort((a, b) => a.localeCompare(b));
}

/** One area line: "amygdala left: 3 of 10 units below alpha (30.0%), p 0.012". */
export function areaLine(a: TuningArea): string {
  const p = a.p === null ? "" : `, p ${a.p.toPrecision(3)}`;
  return `${a.location}: ${a.n_below_alpha} of ${a.n_units} units below alpha (${(a.fraction * 100).toFixed(1)}%)${p}`;
}
