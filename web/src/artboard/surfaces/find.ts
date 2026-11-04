// The Find page: the real `FindView` and `PicksRail` over the golden index (the one
// the browser run serves as index.json) with the catalog join from the fixture's datasets. Expectations
// are derived here from the index rows and the model's classifiers, not from the view's markup.
import { createElement, type ReactElement } from "react";
import type { CatalogDataset } from "../../catalog/catalog";
import { BEST, FindView } from "../../find/FindView";
import {
  applyFind,
  bestFit,
  catalogJoin,
  emptyFind,
  joinKey,
  matrix,
  SPECIES,
} from "../../find/model";
import { PicksRail } from "../../find/PicksRail";
import { readGolden } from "../../index/golden";
import { parseIndex } from "../../index/schema";
import { TargetsStore } from "../../targets/store";
import { CATALOG_FIXTURE as F } from "../fixture";
import { ab, type Expectation, type Surface } from "../harness";

const ALLOW = [
  "div",
  "section",
  "aside",
  "header",
  "h2",
  "p",
  "span",
  "a",
  "ol",
  "li",
  "ul",
  "table",
  "thead",
  "tbody",
  "tr",
  "th",
  "td",
  "strong",
  "code",
  "i",
  "label",
  "input",
  "fieldset",
  "legend",
  "button",
  "dialog",
  "nav",
  "small",
];

const index = () => parseIndex(readGolden());

function catalog(): ReadonlyMap<string, string> {
  const rows: CatalogDataset[] = F.studies.datasets.map((d) => ({
    dataset: d.dataset,
    title: d.title,
    license: d.license,
    doi: d.doi,
    source_archive: d.source_archive,
    source_dataset: d.source_dataset,
    source_version: d.source_version,
    ingest_method: d.ingest_method,
    asset_count: d.asset_count ?? 0,
    subject_count: d.subject_count ?? 0,
    total_bytes: d.total_bytes ?? 0,
  }));
  return catalogJoin(rows);
}

function filters() {
  const f = emptyFind();
  f.species = new Set(["human"]);
  return f;
}

export function findFacts() {
  const rows = index().datasets;
  const f = filters();
  const shown = applyFind(rows, f);
  const best = bestFit(rows, f, BEST);
  const join = catalog();
  return {
    total: rows.length,
    shown: shown.length,
    best,
    cells: [...matrix(rows).values()].filter((n) => n > 0).length,
    inCatalog: best.filter((d) => join.has(joinKey(d))).length,
  };
}

export function findExpectation(): Expectation {
  const f = findFacts();
  return {
    allow: ALLOW,
    counts: [
      { noun: "matrix columns", n: 6, match: ab("matrix-column") },
      { noun: "matrix cells with datasets", n: f.cells, match: ab("matrix-cell") },
      { noun: "best fits", n: f.best.length, match: ab("fit-row") },
      { noun: "fits marked in catalog", n: f.inCatalog, match: ab("in-catalog") },
      { noun: "histograms", n: 2, match: ab("histogram") },
    ],
    texts: [
      `${f.shown} of ${f.total} datasets`,
      ...SPECIES.map((s) => s.label),
      ...f.best.map((d) => d.title),
      "Picks",
      "Ask a curator to import",
    ],
  };
}

export const findSurface: Surface = {
  id: "find",
  board: "08-find-import",
  title: "Find",
  pending: null,
  route: "/discover",
  ready: "find",
  viewports: ["1440x900", "375x812"],
  noScroll: false,
  render: (): ReactElement => {
    const store = new TargetsStore(null);
    return createElement(FindView, {
      index: index(),
      filters: filters(),
      onFilters: () => undefined,
      picked: new Set<string>(),
      onPick: () => undefined,
      catalog: catalog(),
      rail: createElement(PicksRail, { store, state: store.getSnapshot(), notice: null }),
    });
  },
  expect: findExpectation,
};
