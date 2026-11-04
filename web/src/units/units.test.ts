import { describe, expect, it } from "vitest";
import { curvePath, curvePoints, valueDomain } from "../plots/curve";
import { rasterPath } from "../plots/raster";
import {
  DEFAULTS,
  eventsOptions,
  intervalOptions,
  parseRates,
  parseUnitsState,
  ratesPath,
  sceneCutOptions,
  stimulusSeries,
  type UnitsState,
  unitsSearch,
} from "./units";

const FULL: UnitsState = {
  units: [0, 3, 12],
  align: "frames:a1:shot_start_fr:/stimulus/movieframe_time",
  pre: 2,
  post: 0.5,
  bin: 0.1,
  z: true,
  raster: false,
};

describe("URL state", () => {
  it("round-trips every field", () => {
    expect(parseUnitsState(unitsSearch("", FULL))).toEqual(FULL);
  });

  it("leaves defaults out and keeps other parameters", () => {
    expect(unitsSearch("?tab=units", { ...DEFAULTS, units: [1], align: "intervals:trials" })).toBe(
      "?tab=units&unit=1&align=intervals%3Atrials",
    );
    expect(unitsSearch("?tab=units&z=1&unit=4", DEFAULTS)).toBe("?tab=units");
  });

  it("falls back to the default for a malformed value", () => {
    const s = parseUnitsState("?unit=a,2,-1,2,1.5&pre=x&post=-3&bin=&z=yes&raster=0");
    expect(s).toEqual({ ...DEFAULTS, units: [2], raster: false });
  });

  it("keeps at most 32 units", () => {
    const many = Array.from({ length: 40 }, (_v, i) => i).join(",");
    expect(parseUnitsState(`?unit=${many}`).units).toHaveLength(32);
  });
});

describe("ratesPath", () => {
  it("names the wire parameters", () => {
    const q = new URLSearchParams(ratesPath("a/1", FULL).split("?")[1]);
    expect(ratesPath("a/1", FULL).startsWith("/compute/rates?")).toBe(true);
    expect(Object.fromEntries(q)).toEqual({
      asset_id: "a/1",
      units: "0,3,12",
      align: FULL.align,
      pre_s: "2",
      post_s: "0.5",
      bin_s: "0.1",
      zscore: "1",
      raster: "0",
    });
  });
});

describe("parseRates", () => {
  it("keeps rate_hz and z exactly", () => {
    const rate = [0.1, 0.30000000000000004, null, 7];
    const body = parseRates({
      edges: [0, 1, 2, 3, 4],
      units: [{ unit_index: 2, rate_hz: rate, z: null }],
      n_trials: 5,
      align: "intervals:trials",
      source: "file",
      missing_frames: [3],
      query_ms: 1,
      compute_ms: 2,
    });
    expect(body.units[0]?.rate_hz).toEqual(rate);
    expect(body.units[0]?.z).toBeNull();
    expect(body.units[0]?.raster).toBeNull();
    expect(body.missing_frames).toEqual([3]);
  });

  it("refuses a body without edges", () => {
    expect(() => parseRates({ units: [], n_trials: 1 })).toThrow('"edges"');
  });
});

describe("align options", () => {
  const rows = [
    { table_name: "trials", columns_json: '{"condition":"go","id":"1"}' },
    { table_name: "trials", columns_json: '{"condition":"nogo","id":"1"}' },
    { table_name: "epochs", columns_json: null },
  ];

  it("offers each table and each small column value", () => {
    const values = intervalOptions(rows).map((o) => o.value);
    expect(values).toContain("intervals:trials");
    expect(values).toContain("intervals:epochs");
    expect(values).toContain("intervals:trials:condition=go");
    expect(values).toContain("intervals:trials:condition=nogo");
    expect(values).not.toContain("intervals:trials:id=1");
  });

  it("builds a scene-cut align only with a CSV asset and a stimulus series with timestamps", () => {
    const paths = stimulusSeries([
      { path: "/acquisition/LFP", has_timestamps: false },
      { path: "/stimulus/movieframe_time", has_timestamps: true },
      { path: "/stimulus/other", has_timestamps: false },
    ]);
    expect(paths).toEqual(["/stimulus/movieframe_time"]);
    expect(sceneCutOptions("asset-9", paths)).toEqual([
      {
        value: "frames:asset-9:shot_start_fr:/stimulus/movieframe_time",
        label: "Movie scene cuts",
      },
    ]);
    expect(sceneCutOptions(null, paths)).toEqual([]);
    expect(sceneCutOptions("asset-9", [])).toEqual([]);
  });

  it("offers events only from the same BIDS session", () => {
    const assets = [
      { asset_id: "e1", path: "sub-1/sub-1_ses-1_events.tsv", bids_session: "ses-1" },
      { asset_id: "e2", path: "sub-1/sub-1_ses-2_events.tsv", bids_session: "ses-2" },
      { asset_id: "n1", path: "sub-1/sub-1_ses-1.nwb", bids_session: "ses-1" },
    ];
    expect(eventsOptions(assets, "ses-1").map((o) => o.value)).toEqual(["events:e1"]);
  });
});

describe("plot helpers", () => {
  it("draws one point per value, at the value the server sent", () => {
    const values = [2.5, null, 14.2];
    const points = curvePoints(
      [0, 1, 2, 3],
      values,
      (v) => v * 10,
      (v) => 100 - v,
    );
    expect(points.map((p) => p.value)).toEqual(values);
    expect(points.map((p) => p.x)).toEqual([5, 15, 25]);
    expect(points[0]?.y).toBe(97.5);
    expect(curvePath(points)).toBe("M5.00 97.50M25.00 85.80");
  });

  it("starts a rate domain at zero and not a z domain", () => {
    expect(valueDomain([3, 5], true)[0]).toBe(0);
    expect(valueDomain([-2, 5], false)[0]).toBeLessThan(-2);
    expect(valueDomain([], true)).toEqual([0, 1]);
  });

  it("places a tick per spike", () => {
    const d = rasterPath([0, 1], [0.5, 1], 2, (v) => v * 10, 20);
    expect(d.match(/M/g)).toHaveLength(2);
    expect(d.startsWith("M5.00 ")).toBe(true);
  });
});
