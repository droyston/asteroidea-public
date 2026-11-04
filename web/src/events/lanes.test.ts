import { describe, expect, it } from "vitest";
import type { Label } from "./labels";
import {
  axisLabels,
  catalogLanes,
  computeLanes,
  fromAxis,
  laneMarks,
  lanesDomain,
  nearestLabels,
  parseLaneEvents,
  shiftLanes,
  toAxis,
  windowAround,
} from "./lanes";

const BODY = {
  asset_id: "a",
  dataset: "dandi-000623",
  recipe: { version: "1", default: false, lfp_clock_offset_s: 10 },
  event_types: [
    {
      name: "scene_cut",
      averaging: "average",
      n_events: 2,
      events: [
        { onset_s: 5, stop_s: null, attributes: {}, source: { kind: "x" } },
        { onset_s: 1, stop_s: null, attributes: {}, source: { kind: "x" } },
      ],
    },
    { name: "empty", averaging: "single", n_events: 0, events: [] },
    {
      name: "trial",
      averaging: "single",
      n_events: 1,
      events: [{ onset_s: 2, stop_s: 4, attributes: {}, source: { kind: "x" } }],
    },
  ],
};

describe("the clock", () => {
  it("puts a trials-clock time on the series axis and back", () => {
    const clock = { offset: 10, startingTime: 3 };
    expect(toAxis(clock, 5)).toBe(12);
    expect(fromAxis(clock, 12)).toBe(5);
  });

  it("reads the recipe's offset, 0 when the recipe has none", () => {
    expect(parseLaneEvents(BODY).offset).toBe(10);
    expect(parseLaneEvents({ ...BODY, recipe: {} }).offset).toBe(0);
    expect(() => parseLaneEvents({ ...BODY, recipe: { lfp_clock_offset_s: "ten" } })).toThrow(
      "lfp_clock_offset_s",
    );
  });
});

describe("computeLanes", () => {
  it("one lane per type with events, sorted, on the axis", () => {
    const lanes = computeLanes(parseLaneEvents(BODY), { offset: 10, startingTime: 0 });
    expect(lanes.map((l) => l.name)).toEqual(["Scene cut", "Trial"]);
    expect(lanes[0]?.items.map((i) => [i.start, i.stop])).toEqual([
      [11, 11],
      [15, 15],
    ]);
    expect(lanes[1]?.items.map((i) => [i.start, i.stop])).toEqual([[12, 14]]);
    expect(lanesDomain(lanes)).toEqual([11, 15]);
  });

  it("catalog rows make the same lanes", () => {
    const lanes = catalogLanes(
      [{ table_name: "trials", start_time: 1, stop_time: 2 }],
      [{ onset_s: 3, duration_s: 0, trial_type: "go" }],
    );
    expect(lanes.map((l) => [l.name, l.items.length])).toEqual([
      ["trials", 1],
      ["go", 1],
    ]);
  });
});

describe("laneMarks", () => {
  it("draws a tick per event at the window's resolution", () => {
    const marks = laneMarks(
      [
        { start: 1, stop: 1, title: "a" },
        { start: 5, stop: 5, title: "b" },
      ],
      [0, 10],
      100,
    );
    expect(marks).toEqual([
      { kind: "tick", x: 10.5, title: "a" },
      { kind: "tick", x: 50.5, title: "b" },
    ]);
  });

  it("bins events closer than a pixel into one density mark with their count", () => {
    const items = Array.from({ length: 20_000 }, (_v, i) => ({
      start: i / 20,
      stop: i / 20,
      title: "",
    }));
    const marks = laneMarks(items, [0, 1000], 500);
    expect(marks.length).toBeLessThanOrEqual(500);
    expect(marks.every((m) => m.kind === "bin")).toBe(true);
    expect(marks.reduce((n, m) => n + (m.kind === "bin" ? m.n : 1), 0)).toBe(20_000);
  });

  it("draws a wide span as a span and leaves out what is outside the window", () => {
    const marks = laneMarks(
      [
        { start: 2, stop: 4, title: "t" },
        { start: 20, stop: 30, title: "out" },
      ],
      [0, 10],
      100,
    );
    expect(marks).toEqual([{ kind: "span", x0: 20, x1: 40, title: "t" }]);
  });
});

function label(id: string, start: number, stop: number, mine = false): Label {
  return { label_id: id, name: id, note: "", start_s: start, stop_s: stop, mine, version: 1 };
}

describe("nearestLabels", () => {
  const all = axisLabels(
    [label("far", 500, 510), label("in", 250, 260), label("near", 300, 302), label("early", 2, 4)],
    { offset: 0, startingTime: 0 },
  );

  it("overlapping first, then by distance; three of them", () => {
    expect(nearestLabels(all, [240, 280]).map((l) => l.label_id)).toEqual(["in", "near", "far"]);
  });

  it("puts labels on the axis with the clock", () => {
    const moved = axisLabels([label("x", 1, 2)], { offset: 10, startingTime: 0 });
    expect([moved[0]?.a, moved[0]?.b]).toEqual([11, 12]);
  });
});

describe("windowAround", () => {
  it("centres a window of three times the label, at least 10 s, inside the recording", () => {
    expect(windowAround(100, 110, 1000)).toEqual({ start: 90, dur: 30 });
    expect(windowAround(1, 2, 1000)).toEqual({ start: 0, dur: 10 });
    expect(windowAround(995, 999, 1000)).toEqual({ start: 988, dur: 12 });
  });
});

describe("shiftLanes", () => {
  it("moves times and titles from the trials clock onto the series axis", () => {
    const lanes = computeLanes(parseLaneEvents(BODY), { offset: 0, startingTime: 0 });
    const moved = shiftLanes(lanes, { offset: 10, startingTime: 12.5 });
    expect(moved[0]?.items.map((i) => [i.start, i.title])).toEqual([
      [-1.5, "Scene cut at -1.5 s"],
      [2.5, "Scene cut at 2.5 s"],
    ]);
    expect(moved[1]?.items.map((i) => [i.start, i.stop])).toEqual([[-0.5, 1.5]]);
  });
});
