import { describe, expect, it } from "vitest";
import {
  buildLanes,
  colourColumns,
  colourSlot,
  colourValues,
  defaultColourColumn,
  PALETTE_SIZE,
  parseColumns,
  timeDomain,
  visibleBars,
} from "./events";

const interval = (table: string, i: number, start: number, stop: number | null, cols: object) => ({
  table_name: table,
  row_index: i,
  start_time: start,
  stop_time: stop,
  columns_json: JSON.stringify(cols),
});

const trials = [
  interval("trials", 0, 1, 3, { condition: "A", correct: true }),
  interval("trials", 1, 5, 6.5, { condition: "B", correct: false }),
  interval("trials", 2, 8, 9, { condition: "A", correct: true }),
];
const epochs = [interval("epochs", 0, 0, 10, { label: "rest" })];
const events = [
  {
    events_for: "x",
    row_index: 0,
    onset_s: 2,
    duration_s: 0.5,
    trial_type: "go",
    columns_json: null,
  },
  {
    events_for: "x",
    row_index: 1,
    onset_s: 4,
    duration_s: null,
    trial_type: "stop",
    columns_json: "{}",
  },
  {
    events_for: "x",
    row_index: 2,
    onset_s: 7,
    duration_s: 1,
    trial_type: "go",
    columns_json: "{}",
  },
];

describe("parseColumns", () => {
  it("reads an object as text cells", () => {
    expect(parseColumns('{"a":1,"b":"x","c":null,"d":true}')).toEqual({
      a: "1",
      b: "x",
      c: "",
      d: "true",
    });
  });

  it("gives {} for null, bad JSON, arrays and scalars", () => {
    for (const v of [null, "", "{oops", "[1]", "3", 5]) expect(parseColumns(v)).toEqual({});
  });
});

describe("buildLanes", () => {
  const lanes = buildLanes([...trials, ...epochs], events, "condition");

  it("makes one lane per table_name, then one per trial type", () => {
    expect(lanes.map((l) => `${l.kind}:${l.name}`)).toEqual([
      "interval:trials",
      "interval:epochs",
      "event:go",
      "event:stop",
    ]);
    expect(lanes[0]?.bars).toHaveLength(3);
    expect(lanes[2]?.bars).toHaveLength(2);
  });

  it("draws an interval from start to stop and an event from onset for its duration", () => {
    expect(lanes[0]?.bars[1]).toMatchObject({ start: 5, end: 6.5, value: "B" });
    expect(lanes[2]?.bars[0]).toMatchObject({ start: 2, end: 2.5 });
    expect(lanes[3]?.bars[0]).toMatchObject({ start: 4, end: 4 });
  });

  it("keeps the row values for the readout", () => {
    expect(lanes[0]?.bars[0]?.cells).toEqual({
      start_time: "1",
      stop_time: "3",
      condition: "A",
      correct: "true",
    });
  });

  it("skips a row without a start and names a blank trial type", () => {
    const odd = buildLanes(
      [{ table_name: "t", start_time: null }],
      [{ onset_s: 1, duration_s: 1, trial_type: null, columns_json: null }],
      "",
    );
    expect(odd.map((l) => l.name)).toEqual(["(no trial type)"]);
  });
});

describe("colour", () => {
  it("offers trial_type and every columns_json key", () => {
    expect(colourColumns([...trials, ...epochs], events)).toEqual([
      "trial_type",
      "condition",
      "correct",
      "label",
    ]);
  });

  it("defaults to a condition-like column with a few values", () => {
    expect(defaultColourColumn(trials, [])).toBe("condition");
    expect(defaultColourColumn([], events)).toBe("trial_type");
    expect(defaultColourColumn(trials, events)).toBe("condition");
    expect(defaultColourColumn([], [])).toBe("");
  });

  it("indexes values in sorted order, wraps at the palette size, and leaves blanks neutral", () => {
    const lanes = buildLanes(trials, [], "condition");
    const values = colourValues(lanes);
    expect(values).toEqual(["A", "B"]);
    expect(colourSlot(values, "A")).toBe(0);
    expect(colourSlot(values, "B")).toBe(1);
    expect(colourSlot(values, "")).toBe(-1);
    const many = Array.from({ length: PALETTE_SIZE + 2 }, (_, i) => `v${i}`);
    expect(colourSlot(many, `v${PALETTE_SIZE}`)).toBe(0);
  });
});

describe("domain and view", () => {
  const lanes = buildLanes(trials, events, "");

  it("spans the earliest start to the latest end, padding a single instant", () => {
    expect(timeDomain(lanes)).toEqual([1, 9]);
    expect(timeDomain([])).toEqual([0, 1]);
    expect(timeDomain(buildLanes([], [events[1] ?? {}], ""))).toEqual([3.5, 4.5]);
  });

  it("lists the bars that overlap the view", () => {
    expect(
      visibleBars(lanes[0] ?? { name: "", kind: "interval", bars: [] }, [4, 7]).map((b) => b.index),
    ).toEqual([1]);
  });
});
