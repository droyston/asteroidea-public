// The data home: the real `HomeView` over the fixture's studies and links bodies. The
// expectations are derived here from the fixture, not from the view.
import { createElement, type ReactElement } from "react";
import { parseHome } from "../../home/data";
import { HomeView } from "../../home/HomeView";
import {
  type Dataset,
  datasetsListing,
  CATALOG_FIXTURE as F,
  type Link,
  type Study,
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
  "table",
  "thead",
  "tbody",
  "tr",
  "th",
  "td",
  "button",
  "details",
  "summary",
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
];

const MRI_ROLES = new Set(["mri", "fmri"]);

export interface HomeFacts {
  readonly studies: readonly Study[];
  /** Modalities present across the curated studies (matrix columns). */
  readonly modalities: readonly string[];
  /** Participant keys with ephys and MRI in one study (rail entries), and whether each is confirmed. */
  readonly rail: readonly { readonly key: string; readonly confirmed: boolean }[];
  readonly sandbox: number;
  readonly mine: number;
  readonly others: number;
  readonly regionLabels: readonly string[];
  /** Facet options, one chip each: the distinct values across the curated studies. */
  readonly facetOptions: Readonly<Record<"modality" | "region" | "species" | "archive", number>>;
}

export function homeFacts(datasets: readonly Dataset[], links: readonly Link[]): HomeFacts {
  const byId = new Map(datasets.map((d) => [d.dataset, d]));
  const studies = F.studies.studies;
  const modalities = new Set<string>();
  const regionLabels = new Set<string>();
  const rail: { key: string; confirmed: boolean }[] = [];
  const species = new Set<string>();
  const archives = new Set<string>();
  for (const s of studies) {
    const roleOf = new Map(s.datasets.map((d) => [d.dataset, d.role]));
    for (const ref of s.datasets) {
      const d = byId.get(ref.dataset);
      const archive = d?.source_archive ?? null;
      if (archive !== null) archives.add(archive);
      for (const sub of d?.subjects ?? []) {
        for (const m of sub.modalities) modalities.add(m);
        if (sub.species !== null) species.add(sub.species);
      }
      for (const r of d?.regions ?? []) regionLabels.add(r.region);
    }
    const keys = new Map<string, Link[]>();
    for (const l of links) {
      if (!roleOf.has(l.dataset)) continue;
      keys.set(l.subject_key, [...(keys.get(l.subject_key) ?? []), l]);
    }
    for (const [key, ls] of keys) {
      const roles = ls.map((l) => roleOf.get(l.dataset) ?? "");
      if (roles.some((r) => MRI_ROLES.has(r)) && roles.some((r) => !MRI_ROLES.has(r))) {
        rail.push({ key, confirmed: ls.every((l) => l.status === "confirmed") });
      }
    }
  }
  return {
    studies,
    modalities: [...modalities].sort(),
    rail,
    sandbox: datasets.filter((d) => d.tier === "sandbox").length,
    mine: datasets.filter((d) => d.tier === "upload" && d.mine).length,
    others: datasets.filter((d) => d.tier === "upload" && !d.mine).length,
    regionLabels: [...regionLabels],
    facetOptions: {
      modality: modalities.size,
      region: new Set([...regionLabels].map((r) => r.trim())).size,
      species: species.size,
      archive: archives.size,
    },
  };
}

export function homeExpectation(facts: HomeFacts): Expectation {
  return {
    allow: ALLOW,
    counts: [
      { noun: "Copy code buttons", n: 1, match: ab("copy-code") },
      { noun: "study rows", n: facts.studies.length, match: ab("study-row") },
      { noun: "matrix columns", n: facts.modalities.length, match: ab("matrix-col") },
      { noun: "rail entries", n: facts.rail.length, match: ab("rail-entry") },
      {
        noun: "dashed (pending) rail links",
        n: facts.rail.filter((r) => !r.confirmed).length,
        match: ab("rail-entry", { "data-ab-line": "dashed" }),
      },
      {
        noun: "solid (confirmed) rail links",
        n: facts.rail.filter((r) => r.confirmed).length,
        match: ab("rail-entry", { "data-ab-line": "solid" }),
      },
      ...(["modality", "region", "species", "archive"] as const).map((f) => ({
        noun: `${f} facet chips`,
        n: facts.facetOptions[f],
        match: ab("facet-chip", { "data-facet": f }),
      })),
      { noun: "sandbox fold lines", n: 1, match: ab("sandbox-fold") },
      { noun: "Mine rows", n: facts.mine, match: ab("mine-row") },
      { noun: "Mine empty states", n: facts.mine === 0 ? 1 : 0, match: ab("mine-empty") },
      {
        noun: "other researchers' lines",
        n: facts.others > 0 ? 1 : 0,
        match: ab("others-uploads"),
      },
    ],
    texts: [
      ...facts.studies.map((s) => s.title),
      ...facts.regionLabels,
      String(facts.sandbox),
      ...(facts.mine === 0 ? ["No uploads of yours yet"] : []),
    ],
  };
}

const DEFAULT = [...F.studies.datasets];
const MINE_TWO = [...F.studies.datasets, ...F.variants.mine_two];

function home(
  id: string,
  board: string,
  datasets: readonly Dataset[],
  viewports: Surface["viewports"],
  noScroll: boolean,
): Surface {
  return {
    id,
    board,
    title: "Data home",
    pending: null,
    route: "/",
    ready: "home",
    viewports,
    noScroll,
    render: (): ReactElement =>
      createElement(HomeView, { body: parseHome({ ...F.studies, datasets }, F.links) }),
    api: {
      "/catalog/studies": { ...F.studies, datasets },
      "/catalog/datasets": datasetsListing(datasets),
    },
    expect: () => homeExpectation(homeFacts(datasets, F.links.links)),
  };
}

export const homeSurfaces: readonly Surface[] = [
  home("home", "02-data-home", DEFAULT, ["1440x900"], true),
  home("home-mine", "02-data-home", MINE_TWO, ["1440x900"], true),
  home("home-375", "10b-data-home-375", DEFAULT, ["375x812"], false),
];
