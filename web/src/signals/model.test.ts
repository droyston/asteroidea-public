import { describe, expect, it } from "vitest";
import type { SpectrogramBody } from "../lfp/lfp";
import type { Overview } from "../lfp/overview";
import {
  bandIndices,
  bandPowerDb,
  bandSearch,
  channelLayout,
  logBinned,
  overviewGrid,
  parseBand,
  sharedRange,
  skippedReason,
  UNKNOWN_REGION,
} from "./model";

function overview(db: number[][][], edges: number[]): Overview {
  const nChannels = db.length;
  const nTimes = db[0]?.length ?? 0;
  const nBands = edges.length - 1;
  return {
    nChannels,
    nTimes,
    nBands,
    times: Float64Array.from({ length: nTimes }, (_v, i) => i + 0.5),
    edges: Float64Array.from(edges),
    db: Float32Array.from(db.flat(2)),
    rateHz: 1000,
    startingTime: 0,
  };
}

describe("band selection", () => {
  it("parses band= and leaves the default out of the URL", () => {
    expect(parseBand("").id).toBe("4-30");
    expect(parseBand("?band=80-250").lo).toBe(80);
    expect(parseBand("?band=nonsense").id).toBe("4-30");
    expect(bandSearch("?tab=lfp", "1-4")).toBe("?tab=lfp&band=1-4");
    expect(bandSearch("?tab=lfp&band=1-4", "4-30")).toBe("?tab=lfp");
  });

  it("picks the bands whose geometric centre is inside the range", () => {
    // Centres: sqrt(1*2)=1.41, sqrt(2*4)=2.83, sqrt(4*8)=5.66, sqrt(8*16)=11.3
    expect(bandIndices([1, 2, 4, 8, 16], 1, 4)).toEqual([0, 1]);
    expect(bandIndices([1, 2, 4, 8, 16], 4, 30)).toEqual([2, 3]);
    expect(bandIndices([1, 2, 4, 8, 16], 100, 200)).toEqual([]);
  });

  it("averages bands in linear power weighted by width, back in dB", () => {
    // One channel, one time, bands 1-2 Hz (10 dB = 10) and 2-4 Hz (20 dB = 100).
    const o = overview([[[10, 20]]], [1, 2, 4]);
    const [v] = bandPowerDb(o, [0, 1]);
    // (10 * 1 + 100 * 2) / 3 = 70 -> 18.4510 dB
    expect(v).toBeCloseTo(10 * Math.log10(70), 4);
    expect(bandPowerDb(o, [1])[0]).toBeCloseTo(20, 5);
    expect(Number.isNaN(bandPowerDb(o, [])[0])).toBe(true);
  });

  it("reads the shared dB range over every channel and band", () => {
    const o = overview(
      [
        [
          [0, 10],
          [20, 30],
        ],
        [
          [40, 50],
          [60, 70],
        ],
      ],
      [1, 2, 4],
    );
    const [lo, hi] = sharedRange(o);
    expect(lo).toBe(0);
    expect(hi).toBe(70);
  });
});

describe("channel layout", () => {
  const rows = [
    { electrode_index: 0, location: "amygdala left" },
    { electrode_index: 1, location: "hippocampus right" },
    { electrode_index: 2, location: "Amygdala_L" },
    { electrode_index: 3, location: null },
  ];

  it("groups channels by region when the electrodes table has one row per channel", () => {
    const l = channelLayout(4, rows);
    expect(l.mapped).toBe(true);
    expect(l.groups.map((g) => [g.label, g.channels])).toEqual([
      ["amygdala left", [0, 2]],
      ["hippocampus right", [1]],
      [UNKNOWN_REGION, [3]],
    ]);
    expect(l.order).toEqual([0, 2, 1, 3]);
    expect(l.groups[0]?.token).toBe("--color-region-amygdala-l");
    expect(l.groups[2]?.token).toBeNull();
    expect(l.regionOf(2)).toBe("Amygdala_L");
    expect(l.regionOf(3)).toBeNull();
  });

  it("does not guess when the electrode count differs from the channel count", () => {
    for (const electrodes of [rows.slice(0, 3), null]) {
      const l = channelLayout(5, electrodes);
      expect(l.mapped).toBe(false);
      expect(l.groups).toEqual([{ label: UNKNOWN_REGION, token: null, channels: [0, 1, 2, 3, 4] }]);
      expect(l.regionOf(0)).toBeNull();
    }
  });

  it("draws the first channel of the order at the top row", () => {
    const values = Float32Array.from([1, 2, 3, 4, 5, 6]); // 3 channels x 2 times
    const g = overviewGrid(values, 2, [2, 0, 1]);
    expect([g.nx, g.ny]).toEqual([2, 3]);
    // Grid row ny-1 is the top of the image: channel 2.
    expect([g.at(0, 2), g.at(1, 2)]).toEqual([5, 6]);
    expect([g.at(0, 0), g.at(1, 0)]).toEqual([3, 4]);
  });
});

describe("the window spectrogram on the log axis", () => {
  const body = (freqs: number[], power: number[][]): SpectrogramBody => ({
    freqs,
    times: power[0]?.map((_v, i) => i) ?? [],
    power,
    trace: { times: [], values: [] },
    rateHz: 1000,
    bytesRead: 0,
    requests: 0,
    computeMs: 0,
    fetchMs: 0,
  });

  it("averages linear bins inside each band, the last band closed", () => {
    // Bins at 1, 2, 3, 4 Hz; bands [1, 2) [2, 4]. 10 dB = 10, 20 dB = 100.
    const out = logBinned(body([1, 2, 3, 4], [[10], [10], [20], [20]]), [1, 2, 4]);
    expect(out[0]?.[0]).toBeCloseTo(10, 6);
    expect(out[1]?.[0]).toBeCloseTo(10 * Math.log10((10 + 100 + 100) / 3), 6);
  });

  it("gives a band with no bin the bin nearest its centre", () => {
    // Bands [1.1, 1.5) (centre 1.28, no bin) and [1.5, 250]; bins at 1 and 5 Hz.
    const out = logBinned(body([1, 5], [[-5], [7]]), [1.1, 1.5, 250]);
    expect(out[0]?.[0]).toBeCloseTo(-5, 6);
    expect(out[1]?.[0]).toBeCloseTo(7, 6);
  });
});

describe("the builder's skip reason", () => {
  it("reads the reason from the 404 and 422 sentences, and nothing else", () => {
    expect(skippedReason("series not indexed: LfpIndexError: dataset is not chunked")).toBe(
      "LfpIndexError: dataset is not chunked",
    );
    expect(skippedReason("the series has no uniform rate: LfpIndexError: timestamps series")).toBe(
      "LfpIndexError: timestamps series",
    );
    expect(skippedReason("overview not built for this file version")).toBeNull();
    expect(skippedReason("no such series in the file's manifest: /x")).toBeNull();
  });
});
