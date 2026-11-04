// Flexible inspection: CS62 R2's 44 units re-plotted in the browser from the raster body
// (`e2e/fixtures/ux/gen_inspect.py`: the display body's counts as per-trial spikes; unit 43 has a flat
// baseline, so its z is undefined). Renders the real `inspect/InspectView` over `inspect/model.ts ::
// inspect` at the default state; its expectations are derived here from the fixture.
import { createElement, type ReactElement } from "react";
import rasterJson from "../../../e2e/fixtures/ux/rates-cs62-scene_cut-raster.json" with {
  type: "json",
};
import { InspectView } from "../../inspect/InspectView";
import { defaultState, inspect, TOP } from "../../inspect/model";
import { normalizeRegion } from "../../kit/regions";
import { parseSessionRates } from "../../session/model";
import { CATALOG_FIXTURE as F, UNITS_CS62 } from "../fixture";
import { ab, type Expectation, type Surface } from "../harness";
import { EVENTS_CS62 } from "./session";

const ALLOW = [
  "section",
  "header",
  "h2",
  "h3",
  "p",
  "span",
  "a",
  "div",
  "fieldset",
  "legend",
  "button",
  "input",
  "output",
  "label",
  "select",
  "option",
  "svg",
  "g",
  "line",
  "text",
  "circle",
  "title",
  "aside",
  "small",
  "ol",
  "li",
  "i",
];

/** The `/compute/rates?...&raster=1` body the browser run serves. */
export const RASTER_CS62: unknown = rasterJson;

const s = F.session;
const BASE = `/s/${s.study}/${s.dataset}/${s.subject}/${s.session}`;
const WINDOW = { pre: 1, post: 2 };

function model() {
  const parsed = parseSessionRates(RASTER_CS62);
  const where = new Map(UNITS_CS62.rows.map((r) => [r.unit_index, r.location]));
  const d = defaultState(WINDOW, parsed.defaults.responseWindow);
  return { parsed, d, data: inspect(parsed.rates, where, WINDOW, d) };
}

function render(): ReactElement {
  const { parsed, d, data } = model();
  return createElement(InspectView, {
    subject: s.subject,
    session: s.session,
    eventName: "scene cut",
    nTrials: parsed.rates.n_trials,
    window: WINDOW,
    state: d,
    defaults: d,
    data,
    heatmapHref: BASE,
  });
}

function expectation(): Expectation {
  const { data } = model();
  const defined = data.points.filter((p) => p.response !== null).length;
  const regions = new Set(UNITS_CS62.rows.map((r) => normalizeRegion(r.location).key)).size;
  return {
    allow: ALLOW,
    counts: [
      { noun: "control rows", n: 1, match: ab("control-row") },
      { noun: "local-only notes", n: 1, match: ab("local-note") },
      { noun: "scatter dots", n: defined, match: ab("scatter-dot") },
      { noun: "region strips", n: regions, match: ab("strip") },
      { noun: "strip dots", n: defined, match: ab("strip-dot") },
      { noun: "ranked rows", n: Math.min(TOP, defined), match: ab("ranked-row") },
    ],
    texts: [
      "Computed in your browser · nothing is saved",
      ...new Set(data.regions.map((r) => r.label)),
    ],
  };
}

export const inspectSurface: Surface = {
  id: "inspect",
  board: "06-flexible-inspection",
  title: "Flexible inspection",
  pending: null,
  route: `${BASE}?view=inspect&align=${s.event}`,
  ready: "session",
  viewports: ["1440x900"],
  noScroll: true,
  render,
  expect: expectation,
  api: {
    "/compute/events": EVENTS_CS62,
    "/catalog/session-summary": { status: "missing" },
    "/compute/rates": RASTER_CS62,
  },
};
