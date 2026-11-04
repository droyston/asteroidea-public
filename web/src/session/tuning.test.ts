import { describe, expect, it } from "vitest";
import goldenSummary from "../../e2e/fixtures/ux/session-summary.golden.json";
import { UNITS_CS62 } from "../artboard/fixture";
import { EVENTS_CS62, TUNING_CS62 } from "../artboard/surfaces/session";
import { parseTuning } from "../synthesis/tuning";
import { cellPx } from "./Multiples";
import {
  chooseStoredTuning,
  layoutFromSearch,
  parseSessionEvents,
  parseSummaryAnswer,
  searchWithLayout,
  tuningAttribute,
  tuningMarks,
} from "./model";

const types = parseSessionEvents(EVENTS_CS62).types;
const type = (name: string) => {
  const t = types.find((x) => x.name === name);
  if (t === undefined) throw new Error(name);
  return t;
};

describe("the exploratory tuning screen", () => {
  it("screens the first attribute with 2 to 12 levels, never an identifier", () => {
    // scene_id has 93 levels and comes first; cut_kind has two.
    expect(tuningAttribute(type("scene_cut"))).toBe("cut_kind");
    expect(tuningAttribute(type("movie_onset"))).toBeNull();
  });

  it("reads a stored tuning part for the chosen type with its attribute", () => {
    const summary = parseSummaryAnswer({
      ...goldenSummary,
      parts: { ...goldenSummary.parts, "tuning-scene_cut-cut_kind": TUNING_CS62 },
    });
    if (summary.status !== "built") throw new Error("built");
    const hit = chooseStoredTuning(summary, summary.assetId, "scene_cut");
    expect(hit?.attribute).toBe("cut_kind");
    expect(hit?.choice.kind).toBe("inline");
    expect(chooseStoredTuning(summary, summary.assetId, "recognition_onset")).toBeNull();
    expect(chooseStoredTuning(summary, "another-asset", "scene_cut")).toBeNull();
    expect(chooseStoredTuning(null, summary.assetId, "scene_cut")).toBeNull();
  });

  it("the golden summary holds no tuning part, so the page asks live", () => {
    const summary = parseSummaryAnswer(goldenSummary);
    if (summary.status !== "built") throw new Error("built");
    expect(chooseStoredTuning(summary, summary.assetId, "scene_cut")).toBeNull();
  });

  it("marks exactly the units the server screened", () => {
    const m = tuningMarks(parseTuning(TUNING_CS62), null);
    const rows = UNITS_CS62.rows;
    expect([...m.screened].sort((a, b) => a - b)).toEqual(
      rows
        .filter((_, i) => i % 9 === 3)
        .map((r) => r.unit_index)
        .sort((a, b) => a - b),
    );
    expect(m.levels).toEqual(["change", "continuity"]);
    expect(m.counts).toEqual({ change: 13, continuity: 80 });
  });
});

describe("the view toggle", () => {
  it("keeps small multiples in the URL and leaves the heatmap out", () => {
    expect(layoutFromSearch("")).toBe("heatmap");
    expect(layoutFromSearch("?view=multiples")).toBe("multiples");
    expect(layoutFromSearch("?view=other")).toBe("heatmap");
    expect(searchWithLayout("?a=1", "multiples")).toBe("?a=1&view=multiples");
    expect(searchWithLayout("?view=multiples", "heatmap")).toBe("");
  });

  it("shrinks cells with the unit count", () => {
    expect([cellPx(44), cellPx(96), cellPx(152)]).toEqual([64, 46, 34]);
  });
});
