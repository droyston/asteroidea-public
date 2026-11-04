// The session heatmap: CS62 R2 (44 units, the live rates body with one null z) and a 152-unit
// surface (MAX_UNITS). Each renders the real `session/SessionView` over the model the page builds
// (`buildHeatmap` over the units table and the parsed rates body) with the events body the browser run
// also serves; its expectations are derived here from the fixture. The tuning screen is a body of the
// route's shape over the same units (every ninth unit from the fourth passes), and the small-multiples
// surfaces draw the same model as one PSTH per unit.
import { createElement, type ReactElement } from "react";
import synthesisEvents from "../../../e2e/fixtures/synthesis-events.json" with { type: "json" };
import { normalizeRegion } from "../../kit/regions";
import {
  buildHeatmap,
  parseSessionEvents,
  parseSessionRates,
  pickableTypes,
  type SessionLayout,
  tuningMarks,
  unitLabels,
} from "../../session/model";
import { SessionView } from "../../session/SessionView";
import { parseTuning } from "../../synthesis/tuning";
import {
  CATALOG_FIXTURE as F,
  RATES_CS62,
  type RatesBody,
  rates152,
  UNITS_152,
  UNITS_CS62,
  type UnitRow,
} from "../fixture";
import { ab, type Expectation, type Surface } from "../harness";

const ALLOW = [
  "div",
  "section",
  "header",
  "h2",
  "h3",
  "p",
  "span",
  "a",
  "ul",
  "li",
  "strong",
  "small",
  "canvas",
  "svg",
  "g",
  "rect",
  "line",
  "path",
  "text",
  "title",
  "aside",
  "button",
  "fieldset",
  "legend",
  "circle",
  "ul",
  "li",
  "i",
  "figure",
  "figcaption",
];

export interface SessionFacts {
  readonly units: number;
  /** Side-kept region groups (`regions.ts :: normalizeRegion` keys), in first-seen order. */
  readonly groups: readonly string[];
  readonly labels: readonly string[];
  readonly nullZ: number;
  /** Units the tuning fixture marks as passed. */
  readonly screened: number;
  readonly layout: SessionLayout;
}

export function sessionFacts(
  rows: readonly UnitRow[],
  rates: RatesBody,
  layout: SessionLayout = "heatmap",
): SessionFacts {
  const keys = new Map<string, string>();
  for (const r of rows) {
    const k = normalizeRegion(r.location).key;
    if (!keys.has(k)) keys.set(k, r.location);
  }
  return {
    units: rows.length,
    groups: [...keys.keys()],
    labels: [...new Set(rows.map((r) => r.location))],
    nullZ: rates.units.filter((u) => u.z === null).length,
    screened: rows.filter((_, i) => passes(i)).length,
    layout,
  };
}

/** The tuning fixture's rule: every ninth unit from the fourth passed the screen. */
function passes(i: number): boolean {
  return i % 9 === 3;
}

/** A `/compute/tuning` body of the route's shape for `rows` (scene cuts by cut kind, 13 and 80). */
export function tuningBody(rows: readonly UnitRow[]): unknown {
  const counts = { change: 13, continuity: 80 };
  const units = rows.map((r, i) => ({
    unit_index: r.unit_index,
    location: r.location,
    level_means_hz: { change: 4 + (i % 5), continuity: 3 + (i % 3) },
    n_per_level: counts,
    selectivity_index: passes(i) ? 0.31 : 0.04,
    statistic: passes(i) ? 5.5 : 0.4,
    p: passes(i) ? 0.002 : 0.4,
    q: passes(i) ? 0.03 : 0.6,
    screened: passes(i),
  }));
  return {
    label: "exploratory screen",
    asset_id: F.session.asset_id,
    event_type: F.session.event,
    attribute: "cut_kind",
    units,
    areas: [],
    settings: {
      response_window_s: [0, 1],
      levels: ["change", "continuity"],
      level_counts: counts,
      n_events: 93,
      alpha: 0.05,
    },
    source: "nwb",
    query_ms: 1,
    compute_ms: 1,
  };
}

export const TUNING_CS62: unknown = tuningBody(UNITS_CS62.rows);

export function sessionExpectation(f: SessionFacts): Expectation {
  const common = [
    { noun: "Copy code buttons", n: 1, match: ab("copy-code") },
    { noun: "totals lines", n: 1, match: ab("totals") },
    { noun: "baseline boxes", n: 1, match: ab("baseline-box") },
    { noun: "tuning summaries", n: 1, match: ab("tuning-summary") },
    { noun: "tuning region lines", n: f.groups.length, match: ab("tuning-region") },
  ];
  const texts = [...f.labels, ...(f.nullZ > 0 ? ["no baseline variance"] : []), "Exploratory"];
  if (f.layout === "multiples") {
    return {
      allow: ALLOW,
      counts: [{ noun: "unit PSTHs", n: f.units, match: ab("multiple") }, ...common],
      texts,
    };
  }
  return {
    allow: ALLOW,
    counts: [
      { noun: "unit rows", n: f.units, match: ab("unit-row") },
      { noun: "region groups", n: f.groups.length, match: ab("region-group") },
      { noun: "Δ bars", n: f.units - f.nullZ, match: ab("delta-bar") },
      { noun: "hatched null-z rows", n: f.nullZ, match: ab("null-z-row") },
      { noun: "tuning marks", n: f.screened, match: ab("tuning-mark") },
      ...common,
    ],
    texts,
    canvasRows: f.units,
  };
}

const s = F.session;
const ROUTE = `/s/${s.study}/${s.dataset}/${s.subject}/${s.session}`;

/** The events body for the session's file: the resolver's CS62 fixture (93 scene cuts), on its asset id. */
export const EVENTS_CS62: unknown = { ...synthesisEvents, asset_id: s.asset_id };

export function renderSession(
  rows: readonly UnitRow[],
  raw: RatesBody,
  layout: SessionLayout = "heatmap",
): ReactElement {
  const events = parseSessionEvents(EVENTS_CS62);
  const parsed = parseSessionRates(raw);
  const labels = unitLabels(rows.map((r) => ({ ...r })));
  return createElement(SessionView, {
    dataset: s.dataset,
    subject: s.subject,
    session: s.session,
    assetId: s.asset_id,
    fileHref: `/files/${s.asset_id}`,
    unitsInFile: labels.length,
    types: pickableTypes(events.types),
    eventType: s.event,
    body: {
      heat: buildHeatmap(labels, parsed.rates, parsed.defaults.responseWindow),
      defaults: parsed.defaults,
      nTrials: parsed.rates.n_trials,
    },
    tuning: { kind: "marks", marks: tuningMarks(parseTuning(tuningBody(rows)), null) },
    layout,
  });
}

function session(
  id: string,
  rows: readonly UnitRow[],
  rates: () => RatesBody,
  api: Readonly<Record<string, unknown>> = {},
  layout: SessionLayout = "heatmap",
): Surface {
  return {
    id,
    board: "05-session-density",
    title: layout === "multiples" ? "Session small multiples" : "Session heatmap",
    pending: null,
    route: layout === "multiples" ? `${ROUTE}?view=multiples` : ROUTE,
    ready: "session",
    viewports: ["1440x900"],
    noScroll: true,
    render: (): ReactElement => renderSession(rows, rates(), layout),
    expect: () => sessionExpectation(sessionFacts(rows, rates(), layout)),
    api: {
      "/compute/events": EVENTS_CS62,
      "/catalog/session-summary": { status: "missing" },
      "/compute/tuning": tuningBody(rows),
      // The surface's own rates: the browser run's default is keyed to the `session-152` id only.
      "/compute/rates": rates(),
      ...api,
    },
  };
}

const UNITS_152_API = {
  [`/catalog/assets/${s.asset_id}/tables/units`]: { rows: UNITS_152.rows, page: 0, more: false },
};

export const sessionSurfaces: readonly Surface[] = [
  session("session", UNITS_CS62.rows, () => RATES_CS62),
  // `UNITS_152` is rows only; the units route also answers `page` and `more` (one page here).
  session("session-152", UNITS_152.rows, rates152, UNITS_152_API),
  session("session-multiples", UNITS_CS62.rows, () => RATES_CS62, {}, "multiples"),
  session("session-152-multiples", UNITS_152.rows, rates152, UNITS_152_API, "multiples"),
];
