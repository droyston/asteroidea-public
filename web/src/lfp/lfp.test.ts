import { readFileSync } from "node:fs";
import { describe, expect, it, vi } from "vitest";
import {
  FALLBACK_STOPS,
  type Grid,
  parseHex,
  rampColour,
  renderHeatmap,
  stopsFrom,
  valueRange,
} from "../plots/heatmap";
import { drawTrace, traceDomain } from "../plots/trace";
import { json, tokens } from "../testApi";
import {
  DEFAULTS,
  edgeLabel,
  edgeTicks,
  lfpSearch,
  lfpSeries,
  loadOverview,
  OVERVIEW_NOT_BUILT,
  overviewNotBuilt,
  overviewPath,
  panWindow,
  parseLfpState,
  parseSpectrogram,
  spectrogramPath,
  windowFromDrag,
  zoomWindow,
} from "./lfp";
import {
  halfToFloat,
  parseOverviewBinary,
  parseOverviewJson,
  parseOverviewReply,
  totalSeconds,
} from "./overview";

const FIXTURES = new URL("../../e2e/fixtures/", import.meta.url);

interface Expected {
  header_length: number;
  shape: [number, number, number];
  edges: number[];
  times: number[];
  rate_hz: number;
  segment_s: number;
  starting_time: number;
  db: number[][][];
  bytes: number;
}

const expected = JSON.parse(
  readFileSync(new URL("overview-expected.json", FIXTURES), "utf8"),
) as Expected;

function binary(): ArrayBuffer {
  const bytes = readFileSync(new URL("overview.bin", FIXTURES));
  return bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength);
}

describe("ASTLFPOV reader against the Python dumps fixture", () => {
  it("has the fixture the Python writer produced", () => {
    expect(binary().byteLength).toBe(expected.bytes);
    expect(new DataView(binary()).getUint32(8, true)).toBe(expected.header_length);
  });

  it("reads the header, times, edges and every dB value exactly", () => {
    const o = parseOverviewBinary(binary());
    const [nc, nt, nb] = expected.shape;
    expect([o.nChannels, o.nTimes, o.nBands]).toEqual([nc, nt, nb]);
    expect(Array.from(o.times)).toEqual(expected.times);
    expect(Array.from(o.edges)).toEqual(expected.edges);
    expect(o.rateHz).toBe(expected.rate_hz);
    expect(o.startingTime).toBe(expected.starting_time);
    expect(totalSeconds(o)).toBe(20);
    for (let c = 0; c < nc; c += 1) {
      for (let t = 0; t < nt; t += 1) {
        for (let b = 0; b < nb; b += 1) {
          expect(o.db[(c * nt + t) * nb + b]).toBe(expected.db[c]?.[t]?.[b]);
        }
      }
    }
  });

  it("refuses bad magic, a short body and an unknown version", () => {
    const good = new Uint8Array(binary());
    const bad = good.slice();
    bad[0] = 0x58;
    expect(() => parseOverviewBinary(bad.buffer)).toThrow("bad magic");
    expect(() => parseOverviewBinary(good.slice(0, good.length - 2).buffer)).toThrow("body bytes");
    expect(() => parseOverviewBinary(good.slice(0, 5).buffer)).toThrow("too short");
    const v2 = new TextEncoder().encode(JSON.stringify({ version: 2, dtype: "<f2" }));
    const blob = new Uint8Array(12 + v2.length);
    blob.set(new TextEncoder().encode("ASTLFPOV"));
    new DataView(blob.buffer).setUint32(8, v2.length, true);
    blob.set(v2, 12);
    expect(() => parseOverviewBinary(blob.buffer)).toThrow("unknown version");
  });

  it("widens half floats exactly", () => {
    expect(halfToFloat(0x3c00)).toBe(1);
    expect(halfToFloat(0xc000)).toBe(-2);
    expect(halfToFloat(0x0001)).toBe(2 ** -24);
    expect(halfToFloat(0x7c00)).toBe(Number.POSITIVE_INFINITY);
    expect(halfToFloat(0xd0a5)).toBe(-(1 + 0xa5 / 1024) * 2 ** 5);
  });
});

describe("inline JSON overview", () => {
  const raw = {
    times_s: [0.5, 1.5],
    band_edges_hz: [1, 10, 100],
    channels: [0, 1],
    power_db: [
      [
        [-1, -2],
        [-3, -4],
      ],
      [
        [-5, -6],
        [-7, -8],
      ],
    ],
    rate_hz: 1000,
    starting_time_s: 3,
  };

  it("lays the values out [channel][time][band]", () => {
    const o = parseOverviewJson(raw);
    expect(Array.from(o.db)).toEqual([-1, -2, -3, -4, -5, -6, -7, -8]);
    expect([o.nChannels, o.nTimes, o.nBands, o.startingTime]).toEqual([2, 2, 2, 3]);
  });

  it("tells the two reply shapes apart", () => {
    expect(parseOverviewReply(raw).kind).toBe("inline");
    const reply = parseOverviewReply({ url: "https://s3.test/x", expires_in: 900 });
    expect(reply).toEqual({ kind: "url", url: "https://s3.test/x", expiresIn: 900 });
    expect(() => parseOverviewReply({ url: "http://s3.test/x" })).toThrow("https");
    expect(() => parseOverviewJson({ ...raw, power_db: [[[1, 2]]] })).toThrow(
      "one entry per channel",
    );
  });
});

describe("loadOverview", () => {
  const CFG = { apiUrl: "https://api.example.test" };

  it("fetches the presigned URL with plain fetch and no credentials", async () => {
    const api = vi.fn(() =>
      Promise.resolve(json(200, { url: "https://s3.test/o?sig=1", expires_in: 900 })),
    );
    const plain = vi.fn(() => Promise.resolve(new Response(binary())));
    const r = await loadOverview(CFG, "a/1", "/acquisition/LFP", tokens(), api, plain);
    expect(r.kind).toBe("loaded");
    if (r.kind === "loaded") expect(r.value.nTimes).toBe(20);
    expect(plain).toHaveBeenCalledOnce();
    const [url, init] = plain.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("https://s3.test/o?sig=1");
    expect(init.credentials).toBe("omit");
    expect(init.headers).toBeUndefined();
  });

  it("returns the code and text of the not-built 404 and a message for a bad download", async () => {
    const text = "overview not built for this file version";
    const notBuilt = vi.fn(() =>
      Promise.resolve(json(404, { error: OVERVIEW_NOT_BUILT, detail: text })),
    );
    const r = await loadOverview(CFG, "a", "/s", tokens(), notBuilt, vi.fn());
    expect(r).toEqual({
      kind: "error",
      message: text,
      status503: false,
      code: "overview_not_built",
      status: 404,
    });
    const api = vi.fn(() => Promise.resolve(json(200, { url: "https://s3.test/o" })));
    const gone = vi.fn(() => Promise.resolve(new Response("expired", { status: 403 })));
    const g = await loadOverview(CFG, "a", "/s", tokens(), api, gone);
    expect(g.kind === "error" && g.message).toContain("HTTP 403");
    const junk = vi.fn(() => Promise.resolve(new Response(new Uint8Array(40))));
    const j = await loadOverview(CFG, "a", "/s", tokens(), api, junk);
    expect(j.kind === "error" && j.message).toContain("bad magic");
  });
});

describe("overviewNotBuilt", () => {
  it("is true for the coded 404, and for an older API's sentence with no code", () => {
    expect(overviewNotBuilt({ code: "overview_not_built", message: "anything" })).toBe(true);
    expect(
      overviewNotBuilt({ code: null, message: "overview not built for this file version" }),
    ).toBe(true);
  });

  it("is false for any other refusal, even one that repeats the sentence under another code", () => {
    expect(overviewNotBuilt({ code: "not_found", message: "no such asset" })).toBe(false);
    expect(overviewNotBuilt({ code: "index_not_built", message: "x" })).toBe(false);
    expect(
      overviewNotBuilt({ code: "not_found", message: "overview not built for this file version" }),
    ).toBe(false);
    expect(overviewNotBuilt({ code: null, message: "HTTP 403" })).toBe(false);
  });
});

describe("URL state", () => {
  it("round-trips every field and leaves defaults out", () => {
    const state = { series: "/acquisition/LFP", channel: 3, start: 12.5, dur: 30 };
    expect(parseLfpState(lfpSearch("", state))).toEqual(state);
    expect(lfpSearch("?tab=lfp&channel=2", DEFAULTS)).toBe("?tab=lfp");
    expect(lfpSearch("?tab=lfp", { ...DEFAULTS, start: 5, dur: 10 })).toBe(
      "?tab=lfp&start=5&dur=10",
    );
  });

  it("falls back for malformed values and needs both start and dur", () => {
    expect(parseLfpState("?channel=-1&start=x&dur=3")).toEqual(DEFAULTS);
    expect(parseLfpState("?channel=1.5&start=4")).toEqual(DEFAULTS);
    expect(parseLfpState("?start=4&dur=0.5")).toEqual(DEFAULTS);
  });

  it("names the wire parameters", () => {
    expect(
      Object.fromEntries(new URLSearchParams(overviewPath("a/1", "/x").split("?")[1])),
    ).toEqual({
      asset_id: "a/1",
      series: "/x",
    });
    const q = new URLSearchParams(
      spectrogramPath("a", "/x", 2, { start: 5, dur: 10 }).split("?")[1],
    );
    expect(Object.fromEntries(q)).toEqual({
      asset_id: "a",
      series: "/x",
      channel: "2",
      start_s: "5",
      duration_s: "10",
    });
  });
});

describe("lfpSeries", () => {
  it("keeps uniformly sampled two-dimensional electrical series", () => {
    const rows = [
      {
        path: "/acquisition/LFP",
        neurodata_type: "ElectricalSeries",
        rate_hz: 1000,
        shape: [1000, 2],
      },
      { path: "/stimulus/t", neurodata_type: "TimeSeries", rate_hz: null, shape: [4500] },
      { path: "/x", neurodata_type: "ElectricalSeries", rate_hz: null, shape: [10, 2] },
      { path: "/y", neurodata_type: "ElectricalSeries", rate_hz: 30, shape: [10] },
    ];
    expect(lfpSeries(rows)).toEqual([
      { path: "/acquisition/LFP", rateHz: 1000, samples: 1000, channels: 2 },
    ]);
  });
});

describe("window arithmetic", () => {
  it("orders and clamps a drag, refuses a sliver and caps at 1,200 s", () => {
    expect(windowFromDrag(15, 5, 20)).toEqual({ start: 5, dur: 10 });
    expect(windowFromDrag(-3, 40, 20)).toEqual({ start: 0, dur: 20 });
    expect(windowFromDrag(5, 5.4, 20)).toBeNull();
    expect(windowFromDrag(100, 4000, 5000)).toEqual({ start: 100, dur: 1200 });
  });

  it("zooms about the centre, opens a window from none, and zooming out to the whole clears it", () => {
    expect(zoomWindow({ start: 5, dur: 10 }, 100, 0.5)).toEqual({ start: 7.5, dur: 5 });
    expect(zoomWindow({ start: 5, dur: 10 }, 100, 2)).toEqual({ start: 0, dur: 20 });
    expect(zoomWindow(null, 100, 0.5)).toEqual({ start: 20, dur: 60 });
    expect(zoomWindow(null, 100, 2)).toBeNull();
    expect(zoomWindow({ start: 0, dur: 15 }, 20, 2)).toBeNull();
    expect(zoomWindow({ start: 0, dur: 1000 }, 5000, 2)).toEqual({ start: 0, dur: 1200 });
    expect(zoomWindow({ start: 5, dur: 1 }, 20, 0.5)).toEqual({ start: 5, dur: 1 });
  });

  it("pans by a fraction of the window and stays inside the recording", () => {
    expect(panWindow({ start: 5, dur: 10 }, 100, 0.5)).toEqual({ start: 10, dur: 10 });
    expect(panWindow({ start: 5, dur: 10 }, 100, -1)).toEqual({ start: 0, dur: 10 });
    expect(panWindow({ start: 85, dur: 10 }, 100, 1)).toEqual({ start: 90, dur: 10 });
  });
});

describe("frequency axis from band edges", () => {
  it("labels with three significant digits and keeps the last edge", () => {
    expect(edgeLabel(250 ** (8 / 64))).toBe("1.99");
    expect(edgeLabel(250)).toBe("250");
    expect(edgeLabel(1)).toBe("1");
    const edges = Array.from({ length: 9 }, (_v, i) => 250 ** (i / 8));
    expect(edgeTicks(edges, 4).map((t) => [t.index, t.label])).toEqual([
      [0, "1"],
      [4, "15.8"],
      [8, "250"],
    ]);
    expect(edgeTicks(edges, 3).at(-1)?.index).toBe(8);
  });
});

describe("spectrogram response", () => {
  const body = JSON.parse(readFileSync(new URL("spectrogram.json", FIXTURES), "utf8")) as Record<
    string,
    unknown
  >;

  it("keeps power_db and the trace exactly as sent", () => {
    const s = parseSpectrogram(body);
    expect(s.power).toEqual(body.power_db);
    expect(s.trace.values).toEqual((body.trace as { values: number[] }).values);
    expect([s.bytesRead, s.requests, s.computeMs]).toEqual([123456, 7, 87]);
  });

  it("refuses ragged data", () => {
    expect(() => parseSpectrogram({ ...body, power_db: [[1]] })).toThrow("one row per frequency");
    expect(() => parseSpectrogram({ ...body, freqs_hz: [0], power_db: [[1]] })).toThrow(
      "one value per time",
    );
  });
});

describe("heatmap and trace", () => {
  const grid: Grid = { nx: 2, ny: 2, at: (ix, iy) => ix + 10 * iy };

  it("reads hex tokens and falls back for anything else", () => {
    expect(parseHex("#fde725")).toEqual([253, 231, 37]);
    expect(parseHex(" #FDE725 ")).toEqual([253, 231, 37]);
    expect(parseHex("rgb(1,2,3)")).toBeNull();
    expect(stopsFrom(() => "")).toEqual(FALLBACK_STOPS);
    expect(stopsFrom((t) => (t === "--color-ramp-1" ? "#000000" : ""))[0]).toEqual([0, 0, 0]);
  });

  it("interpolates the ramp and clamps", () => {
    const stops = [
      [0, 0, 0],
      [100, 200, 50],
      [200, 100, 250],
    ] as const;
    expect(rampColour(stops, 0)).toEqual([0, 0, 0]);
    expect(rampColour(stops, 0.25)).toEqual([50, 100, 25]);
    expect(rampColour(stops, 1)).toEqual([200, 100, 250]);
    expect(rampColour(stops, 9)).toEqual([200, 100, 250]);
    expect(rampColour(stops, Number.NaN)).toEqual([0, 0, 0]);
  });

  it("puts row 0 (lowest frequency) at the bottom and leaves a NaN transparent", () => {
    const g: Grid = {
      nx: 2,
      ny: 2,
      at: (ix, iy) => (ix === 1 && iy === 1 ? Number.NaN : ix + 10 * iy),
    };
    const img = renderHeatmap(
      g,
      [0, 10],
      [
        [0, 0, 0],
        [255, 255, 255],
      ],
    );
    expect([img.width, img.height]).toEqual([2, 2]);
    // top row is iy = 1: (0, 1) = 10 -> white; (1, 1) NaN -> transparent
    expect(Array.from(img.data.slice(0, 4))).toEqual([255, 255, 255, 255]);
    expect(img.data[7]).toBe(0);
    // bottom row is iy = 0: (0, 0) = 0 -> black, (1, 0) = 1 -> 10 % grey
    expect(Array.from(img.data.slice(8, 12))).toEqual([0, 0, 0, 255]);
    expect(Array.from(img.data.slice(12, 16))).toEqual([26, 26, 26, 255]);
  });

  it("clips the colour range to quantiles and survives a flat or empty grid", () => {
    const wide: Grid = { nx: 100, ny: 1, at: (ix) => (ix === 99 ? 1e6 : ix) };
    const [lo, hi] = valueRange(wide);
    expect(lo).toBe(2);
    expect(hi).toBeLessThan(100);
    expect(valueRange({ nx: 3, ny: 1, at: () => 5 })).toEqual([5, 6]);
    expect(valueRange({ nx: 0, ny: 0, at: () => 0 })).toEqual([0, 1]);
    expect(valueRange(grid)).toEqual([0, 11]);
  });

  it("pads the trace domain and strokes without bridging a gap", () => {
    expect(traceDomain([])).toEqual([-1, 1]);
    expect(traceDomain([2, 2])).toEqual([1.5, 2.5]);
    const [a, b] = traceDomain([0, 10]);
    expect([a, b]).toEqual([-0.5, 10.5]);
    const calls: string[] = [];
    const ctx = {
      save: () => calls.push("save"),
      restore: () => calls.push("restore"),
      beginPath: () => calls.push("begin"),
      rect: () => calls.push("rect"),
      clip: () => calls.push("clip"),
      stroke: () => calls.push("stroke"),
      moveTo: (x: number) => calls.push(`M${x}`),
      lineTo: (x: number) => calls.push(`L${x}`),
      set strokeStyle(_v: string) {},
      set lineWidth(_v: number) {},
    } as unknown as CanvasRenderingContext2D;
    drawTrace(
      ctx,
      [0, 1, 2, 3],
      [1, 2, Number.NaN, 4],
      (v) => v,
      (v) => v,
      { x: 0, y: 0, w: 4, h: 4 },
      "#000",
    );
    expect(calls.filter((c) => /^[ML]/.test(c))).toEqual(["M0", "L1", "M3"]);
  });
});
