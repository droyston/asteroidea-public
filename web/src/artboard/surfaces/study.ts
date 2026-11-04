// The study page: the real `StudyView` over the fixture's studies and links
// bodies, parsed by the page's own parsers. Expectations are derived here from the fixture, not from the
// view's model.
import { createElement, type ReactElement } from "react";
import { parseLinks, parseStudies, STRIP_ABOVE, studyModel } from "../../study/model";
import { StudyView } from "../../study/StudyView";
import { CATALOG_FIXTURE as F } from "../fixture";
import { ab, type Expectation, type Surface } from "../harness";

const MODALITY_LABEL: Readonly<Record<string, string>> = {
  units: "Units",
  lfp: "LFP",
  ieeg: "iEEG",
  anat: "Anat MRI",
  func: "fMRI",
  task: "Task",
};

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
  "table",
  "thead",
  "tbody",
  "tr",
  "th",
  "td",
  "strong",
  "small",
  "code",
  "svg",
  "g",
  "rect",
  "line",
  "path",
  "text",
  "title",
  "button",
];

export interface StudyFacts {
  readonly title: string;
  readonly releases: readonly { dataset: string; license: string | null; version: string | null }[];
  /** One matrix column per modality present in a release, as the label the page prints. */
  readonly columnLabels: readonly string[];
  readonly regions: readonly string[];
  readonly participants: number;
  readonly sessions: number;
  /** Releases above 20 participants, drawn as strips: no session cells, one mark per participant. */
  readonly strips: number;
  /** Session cells drawn: those of the releases that are not strips. */
  readonly sessionCells: number;
  /** Sessions with an NWB of `n_units > 0` link to the session page; the rest to their file. */
  readonly sessionLinks: number;
  readonly fileLinks: number;
  readonly pendingLinks: number;
  readonly confirmedLinks: number;
}

export function studyFacts(study: string): StudyFacts {
  const s = F.studies.studies.find((x) => x.study === study);
  if (s === undefined) throw new Error(`no study ${study} in ${F.name}`);
  const ids = new Set(s.datasets.map((d) => d.dataset));
  const datasets = F.studies.datasets.filter((d) => ids.has(d.dataset));
  const subjects = datasets.flatMap((d) => d.subjects);
  const sessions = subjects.flatMap((x) => x.sessions);
  // A release above 20 participants is a strip: its sessions are counted, not drawn as cells.
  const drawn = datasets
    .filter((d) => d.subjects.length <= STRIP_ABOVE)
    .flatMap((d) => d.subjects.flatMap((x) => x.sessions));
  const sessionLinks = drawn.filter((x) => x.nwb.some((n) => n.n_units > 0)).length;
  const links = F.links.links.filter((l) => ids.has(l.dataset));
  const mods = new Set(subjects.flatMap((x) => x.modalities));
  return {
    columnLabels: [...mods].map((m) => MODALITY_LABEL[m] ?? m.replace(/^other:/, "")),
    regions: [...new Set(datasets.flatMap((d) => d.regions.map((r) => r.region)))],
    title: s.title,
    releases: datasets.map((d) => ({
      dataset: d.dataset,
      license: d.license,
      version: d.source_version,
    })),
    participants: subjects.length,
    sessions: sessions.length,
    strips: datasets.filter((d) => d.subjects.length > STRIP_ABOVE).length,
    sessionCells: drawn.length,
    sessionLinks,
    fileLinks: drawn.length - sessionLinks,
    pendingLinks: links.filter((l) => l.status !== "confirmed").length,
    confirmedLinks: links.filter((l) => l.status === "confirmed").length,
  };
}

export function studyExpectation(f: StudyFacts): Expectation {
  return {
    allow: ALLOW,
    counts: [
      { noun: "Copy code buttons", n: 1, match: ab("copy-code") },
      { noun: "releases", n: f.releases.length, match: ab("release") },
      { noun: "participant rows", n: f.participants, match: ab("participant-row") },
      { noun: "strips (releases above 20 participants)", n: f.strips, match: ab("strip") },
      { noun: "relation rows, one per release", n: f.releases.length, match: ab("relate-row") },
      { noun: "Source files buttons", n: f.releases.length, match: ab("sources-button") },
      { noun: "session cells", n: f.sessionCells, match: ab("session-cell") },
      {
        noun: "session cells linking the session page",
        n: f.sessionLinks,
        match: ab("session-cell", { "data-ab-to": "session" }),
      },
      {
        noun: "session cells linking a file page",
        n: f.fileLinks,
        match: ab("session-cell", { "data-ab-to": "file" }),
      },
      {
        noun: "dashed (pending) link cells",
        n: f.pendingLinks,
        match: ab("link-cell", { "data-ab-line": "dashed" }),
      },
      {
        noun: "solid (confirmed) link cells",
        n: f.confirmedLinks,
        match: ab("link-cell", { "data-ab-line": "solid" }),
      },
    ],
    texts: [
      f.title,
      ...f.releases.flatMap((r) => [r.dataset, r.license ?? "", r.version ?? ""]).filter(Boolean),
      ...f.columnLabels,
      ...f.regions,
      "Readiness for analysis",
    ],
  };
}

function study(id: string, slug: string, noScroll: boolean): Surface {
  return {
    id,
    board: "03-study",
    title: "Study page",
    pending: null,
    route: `/s/${slug}`,
    ready: "study",
    viewports: ["1440x900"],
    noScroll,
    render: (): ReactElement => {
      const model = studyModel(
        { kind: "study", study: slug },
        parseStudies(F.studies),
        parseLinks(F.links).links,
      );
      if (model === null) throw new Error(`no study ${slug} in ${F.name}`);
      return createElement(StudyView, { model });
    },
    expect: () => studyExpectation(studyFacts(slug)),
  };
}

export const studySurfaces: readonly Surface[] = [
  study("study", "movie-watching", false),
  // 24 participants: the sideways strip keeps the page on one screen.
  study("study-24", "synthetic-24-participants", true),
];
