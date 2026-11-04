// Results and provenance: the real Results place over the golden bodies
// (the All listing, the batch asset lookup, the rates record and what used it), parsed by the page's own
// parsers, with the fixture's studies for the tree's titles. Expectations are derived here from the
// golden bodies, not from the view's model. The reference's preview box is on the known-differences list;
// the golden record is not an image or a page, so the box holds its sentence. The browser run
// serves one body per path, so its Mine list and "used by" read the All listing; the markup render
// here passes each its own body.
import { createElement, type ReactElement } from "react";
import recordGolden from "../../../e2e/fixtures/ux/artifact-record.golden.json" with {
  type: "json",
};
import allGolden from "../../../e2e/fixtures/ux/artifacts-all.golden.json" with { type: "json" };
import usedByGolden from "../../../e2e/fixtures/ux/artifacts-used-by.golden.json" with {
  type: "json",
};
import assetsGolden from "../../../e2e/fixtures/ux/catalog-assets.golden.json" with {
  type: "json",
};
import { chainModel, listModel, parseStudyNames, runsModel } from "../../results/model";
import { ResultChain, ResultsLayout, ResultsList } from "../../results/ResultsView";
import { parseArtifacts, parseAssetNames, parseRecord } from "../../results/results";
import { CATALOG_FIXTURE as F } from "../fixture";
import { ab, type Expectation, type Surface } from "../harness";
import { USER_EMAIL } from "./shell";

const ALLOW = [
  "section",
  "aside",
  "article",
  "header",
  "nav",
  "figure",
  "figcaption",
  "div",
  "h2",
  "h3",
  "p",
  "q",
  "span",
  "a",
  "ul",
  "li",
  "button",
  "strong",
  "small",
  "dl",
  "dt",
  "dd",
  "svg",
  "line",
  "circle",
  "text",
  "title",
];

const NOOP = () => undefined;

function render(): ReactElement {
  const all = parseArtifacts(allGolden);
  const names = new Map(parseAssetNames(assetsGolden).map((n) => [n.asset_id, n]));
  const { record } = parseRecord(recordGolden);
  const list = listModel({
    scope: "all",
    rows: all,
    mineCount: all.filter((a) => a.author_email === USER_EMAIL).length,
    me: USER_EMAIL,
    names,
    studies: parseStudyNames(F.studies),
    open: null,
    selected: record.artifact_id,
  });
  const chain = chainModel({
    record,
    names,
    known: new Map(all.map((a) => [a.artifact_id, a])),
    usedBy: parseArtifacts(usedByGolden),
    sub: record.author_sub,
  });
  return createElement(ResultsLayout, {
    side: createElement(ResultsList, {
      list,
      runs: runsModel(all, names, USER_EMAIL),
      on: { onScope: NOOP, onOpen: NOOP },
    }),
    main: createElement(ResultChain, {
      chain,
      on: {
        busy: false,
        onDownload: NOOP,
        onRename: NOOP,
        onNote: NOOP,
        onWithdraw: NOOP,
        onDelete: NOOP,
      },
    }),
  });
}

function expectation(): Expectation {
  const all = allGolden.artifacts;
  const { record } = recordGolden;
  const used = usedByGolden.artifacts;
  const fileInputs = record.sources.length + record.inputs.filter((i) => i.kind === "asset").length;
  const resultInputs = record.inputs.filter((i) => i.kind === "artifact").length;
  return {
    allow: ALLOW,
    counts: [
      {
        noun: "Runs box rows (one per run id)",
        n: new Set(all.map((a) => a.run_id).filter((r) => r !== null)).size,
        match: ab("run-row"),
      },
      { noun: "timeline dots", n: all.length, match: ab("dot") },
      {
        noun: "darker (mine) dots",
        n: all.filter((a) => a.author_email === USER_EMAIL).length,
        match: ab("dot", { "data-ab-mine": "1" }),
      },
      { noun: "input cards", n: fileInputs + resultInputs, match: ab("input") },
      {
        noun: "current file inputs",
        n: fileInputs,
        match: ab("input", { "data-ab-current": "true" }),
      },
      { noun: "used-by cards", n: used.length, match: ab("used-by") },
      { noun: "preview boxes", n: 1, match: ab("preview") },
    ],
    texts: [
      record.name,
      ...used.map((u) => u.name),
      ...used.map((u) => u.author_email),
      USER_EMAIL,
      `Preview not built for ${record.content_type}; download it`,
      "By the data they used",
      "Runs",
    ],
  };
}

export const resultsSurfaces: readonly Surface[] = [
  {
    id: "results",
    board: "09-results-provenance",
    title: "Results and provenance",
    pending: null,
    route: `/results/${recordGolden.record.artifact_id}`,
    ready: "result",
    viewports: ["1440x900", "375x812"],
    noScroll: false,
    render,
    expect: expectation,
    api: {
      "/artifacts": allGolden,
      [`/artifacts/${recordGolden.record.artifact_id}`]: recordGolden,
      "/catalog/assets": assetsGolden,
    },
  },
];
