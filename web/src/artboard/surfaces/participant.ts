// The participant page: the real `ParticipantView` over the fixture's studies
// and links bodies for patient 62 (two NWB sessions, 20 and 44 units, and an MRI-only session of the
// other archive's record), parsed by the page's own parsers. Each ephys card draws the glyph the page
// builds from the CS62 R2 rates body; the T1w card stands "not built". Expectations are derived here
// from the fixture's bodies, not from the model.
import { createElement, type ReactElement } from "react";
import { normalizeRegion } from "../../kit/regions";
import { buildGlyph, type SessionExtras } from "../../participant/glyph";
import {
  type Extras,
  parseParticipantLinks,
  participantModel,
  sexAgeOf,
  t1wState,
} from "../../participant/model";
import { ParticipantView } from "../../participant/ParticipantView";
import { parseSessionRates, unitLabels } from "../../session/model";
import { parseStudies } from "../../study/model";
import { CATALOG_FIXTURE as F, RATES_CS62, UNITS_CS62 } from "../fixture";
import { ab, type Expectation, type Surface } from "../harness";
import { EVENTS_CS62 } from "./session";

const ALLOW = [
  "div",
  "section",
  "header",
  "h2",
  "h3",
  "h4",
  "p",
  "button",
  "span",
  "a",
  "ul",
  "li",
  "article",
  "aside",
  "figure",
  "figcaption",
  "img",
  "strong",
  "small",
  "svg",
  "g",
  "rect",
  "line",
  "path",
  "text",
  "title",
];

export const PARTICIPANT_KEY = "bmovie-patient-62";
const STUDY = "movie-watching";

/** Session start times served by the dataset route in the browser run, and read by the model here. */
export const DATES: Readonly<Record<string, string>> = {
  "dandi-000623/sub-CS62/ses-1": "2023-06-15 10:00:00",
  "dandi-000623/sub-CS62/ses-2": "2023-06-25 10:00:00",
  "openneuro-ds004798/sub-p62cs/ses-mri": "2023-06-20 10:00:00",
};

export interface ParticipantFacts {
  readonly records: readonly string[];
  readonly dashed: number;
  readonly solid: number;
  /** Ticks per lane label, by the rule in `participant/model.ts`'s header, read off the fixture. */
  readonly lanes: Readonly<Record<string, number>>;
  readonly ephysCards: number;
  readonly glyphRows: number;
  readonly regions: readonly string[];
}

const NOT_LANES = new Set(["units", "lfp", "task"]);
const LANE_LABEL: Readonly<Record<string, string>> = {
  ephys: "Ephys",
  anat: "Anatomical MRI",
  func: "Functional MRI",
  ieeg: "iEEG",
};

export function participantFacts(key: string): ParticipantFacts {
  const links = F.links.links.filter((l) => l.subject_key === key);
  const subjects = links.flatMap((l) => {
    const s = F.studies.datasets
      .find((d) => d.dataset === l.dataset)
      ?.subjects.find((x) => x.subject === l.subject);
    return s === undefined ? [] : [s];
  });
  const lanes: Record<string, number> = {};
  const bump = (label: string) => {
    lanes[label] = (lanes[label] ?? 0) + 1;
  };
  let cards = 0;
  for (const s of subjects) {
    for (const x of s.sessions) {
      if (x.nwb.length > 0) {
        bump("Ephys");
        if (x.nwb.some((n) => n.n_units > 0)) cards += 1;
      } else {
        for (const m of s.modalities.filter((q) => !NOT_LANES.has(q))) {
          bump(LANE_LABEL[m] ?? m.replace(/^other:/, ""));
        }
      }
    }
  }
  const rows = UNITS_CS62.rows;
  return {
    records: links.map((l) => l.subject),
    dashed: links.every((l) => l.status === "confirmed") ? 0 : links.length - 1,
    solid: links.every((l) => l.status === "confirmed") ? links.length - 1 : 0,
    lanes,
    ephysCards: cards,
    glyphRows: cards * RATES_CS62.units.length,
    regions: [...new Set(rows.map((r) => normalizeRegion(r.location).key))],
  };
}

export function participantExpectation(f: ParticipantFacts): Expectation {
  return {
    allow: ALLOW,
    counts: [
      { noun: "Copy code buttons", n: 1, match: ab("copy-code") },
      { noun: "records", n: f.records.length, match: ab("record") },
      {
        noun: "dashed (pending) bridges",
        n: f.dashed,
        match: ab("bridge", { "data-ab-line": "dashed" }),
      },
      {
        noun: "solid (confirmed) bridges",
        n: f.solid,
        match: ab("bridge", { "data-ab-line": "solid" }),
      },
      { noun: "timeline lanes", n: Object.keys(f.lanes).length, match: ab("lane") },
      {
        noun: "timeline ticks",
        n: Object.values(f.lanes).reduce((a, b) => a + b, 0),
        match: ab("tick"),
      },
      { noun: "ephys cards", n: f.ephysCards, match: ab("ephys-card") },
      { noun: "glyphs", n: f.ephysCards, match: ab("glyph") },
      { noun: "glyph rows", n: f.glyphRows, match: ab("glyph-row") },
      { noun: "T1w cards", n: 1, match: ab("t1w-card") },
      { noun: "T1w cards not built", n: 1, match: ab("t1w-card", { "data-ab-state": "missing" }) },
      { noun: "region panels", n: 1, match: ab("region-panel") },
      { noun: "region lines", n: f.regions.length, match: ab("region-line") },
    ],
    texts: [
      ...f.records,
      ...Object.keys(f.lanes),
      "Preview not built yet",
      "Open in viewer",
      "Regions recorded",
      ...UNITS_CS62.rows.map((r) => r.location),
    ],
  };
}

/** `GET /catalog/datasets/{dataset}` as the dataset route answers it: the dated sessions the page reads. */
export function datasetBody(dataset: string): unknown {
  const sessions = Object.entries(DATES)
    .filter(([id]) => id.startsWith(`${dataset}/`))
    .map(([id, start]) => {
      const [, subject, session] = id.split("/");
      return { subject, bids_session: session, start_time: start, description: null };
    });
  return {
    dataset,
    facts: {
      dataset,
      title: null,
      license: null,
      doi: null,
      source_archive: null,
      source_dataset: null,
      source_version: null,
      asset_count: 0,
      subject_count: 0,
      total_bytes: 0,
    },
    subjects: [],
    sessions,
    formats: [],
    query_ms: 1,
  };
}

export function renderParticipant(key: string): ReactElement {
  const studies = parseStudies(F.studies);
  const links = parseParticipantLinks(F.links).filter((l) => l.subject_key === key);
  const extras: Extras = { sexAge: sexAgeOf(F.studies), dates: DATES };
  const model = participantModel(key, studies, links, extras);
  if (model === null) throw new Error(`no participant ${key} in ${F.name}`);
  const labels = unitLabels(UNITS_CS62.rows.map((r) => ({ ...r })));
  const rates = parseSessionRates(RATES_CS62);
  const all: Record<string, SessionExtras> = {};
  for (const e of model.ephys) {
    all[e.id] = {
      glyph: {
        kind: "ready",
        glyph: buildGlyph(e.assetId, F.session.event, labels, rates, null),
      },
      locations: labels.map((l) => l.location),
    };
  }
  return createElement(ParticipantView, { model, extras: all, t1w: t1wState(model.t1w) });
}

export const participantSurfaces: readonly Surface[] = [
  {
    id: "participant",
    board: "04-participant",
    title: "Participant page",
    pending: null,
    route: `/s/${STUDY}/p/${PARTICIPANT_KEY}`,
    ready: "participant",
    viewports: ["1440x900", "375x812"],
    noScroll: false,
    render: () => renderParticipant(PARTICIPANT_KEY),
    expect: () => participantExpectation(participantFacts(PARTICIPANT_KEY)),
    api: {
      "/catalog/datasets/dandi-000623": datasetBody("dandi-000623"),
      "/catalog/datasets/openneuro-ds004798": datasetBody("openneuro-ds004798"),
      "/catalog/session-summary": { status: "missing" },
      "/compute/events": EVENTS_CS62,
      "/catalog/assets/asset-cs62-1/tables/units": { rows: UNITS_CS62.rows, page: 0, more: false },
      "/catalog/assets/asset-cs62-r2/tables/units": { rows: UNITS_CS62.rows, page: 0, more: false },
    },
  },
];
