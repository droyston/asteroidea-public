import { describe, expect, it, vi } from "vitest";
import { json, tokens } from "../testApi";
import {
  gridColumns,
  loadComputeEvents,
  loadSessionFiles,
  loadSessionRates,
  MAX_UNITS,
  parseEventsBody,
  parseSessionRates,
  parseSortValue,
  sessionNwb,
  sessionRatesPath,
  sortRows,
  sortValue,
  unitPages,
  unitRows,
} from "./session";

const CFG = { apiUrl: "https://api.example.test" };

const DEFAULTS = {
  version: "defaults-1",
  block: "dandi-000623",
  source: "Keles et al. 2024",
  baseline: "none",
  bin_s: 0.25,
  pre_s: 0.5,
  post_s: 1.0,
  response_window_s: [0, 1],
};

const RATES = {
  edges: [-0.5, -0.25, 0, 0.25, 0.5, 0.75, 1],
  units: [
    { unit_index: 0, rate_hz: [1, 2, 3, 4, 5, 6] },
    { unit_index: 1, rate_hz: [6, 5, 4, 3, 2, 1] },
  ],
  n_trials: 93,
  align: "event:scene_cut",
  source: "catalog",
  query_ms: 10,
  compute_ms: 5,
  defaults: DEFAULTS,
  responsive: [
    { unit_index: 0, location: "hippocampus", p: 0.2, q: 0.4, screened: false },
    { unit_index: 1, location: "amygdala", p: 0.001, q: 0.01, screened: true },
  ],
};

describe("rates with align=event", () => {
  it("omits pre_s, post_s and bin_s; the server fills them", () => {
    const q = new URL(`https://x${sessionRatesPath("a/1", "scene_cut", "all")}`).searchParams;
    expect(q.get("asset_id")).toBe("a/1");
    expect(q.get("units")).toBe("all");
    expect(q.get("align")).toBe("event:scene_cut");
    expect(q.get("raster")).toBe("0");
    for (const k of ["pre_s", "post_s", "bin_s"]) expect(q.has(k)).toBe(false);
  });

  it("parses defaults and responsive and keeps rate_hz as sent", () => {
    const r = parseSessionRates(RATES);
    expect(r.defaults.block).toBe("dandi-000623");
    expect(r.defaults.baseline).toBe("none");
    expect(r.defaults.response_window_s).toBe("0 to 1 s");
    expect(r.responsive[1]).toEqual({
      unit_index: 1,
      location: "amygdala",
      p: 0.001,
      q: 0.01,
      screened: true,
    });
    expect(r.rates.units[0]?.rate_hz).toEqual([1, 2, 3, 4, 5, 6]);
  });

  it("refuses a body without the explicit-window keys", () => {
    const { defaults: _d, ...noDefaults } = RATES;
    expect(() => parseSessionRates(noDefaults)).toThrow(/defaults/);
    const { responsive: _r, ...noResponsive } = RATES;
    expect(() => parseSessionRates(noResponsive)).toThrow(/responsive/);
  });

  it("loads through apiGet and carries a refusal's code and detail", async () => {
    const ok = vi.fn((_url: string, _init: RequestInit) => Promise.resolve(json(200, RATES)));
    const r = await loadSessionRates(CFG, "a1", "scene_cut", "0-151", tokens(), ok);
    expect(r.kind).toBe("loaded");
    expect(ok.mock.calls[0]?.[0]).toContain("units=0-151");
    const refused = vi.fn(() =>
      Promise.resolve(json(400, { error: "too_many_units", detail: "over the bound" })),
    );
    const e = await loadSessionRates(CFG, "a1", "scene_cut", "all", tokens(), refused);
    expect(e).toMatchObject({ kind: "error", message: "over the bound", code: "too_many_units" });
  });
});

describe("unitPages", () => {
  it("is one `all` page up to the bound", () => {
    expect(unitPages([])).toEqual([]);
    expect(unitPages(Array.from({ length: 44 }, (_, i) => i))).toEqual(["all"]);
    expect(unitPages(Array.from({ length: MAX_UNITS }, (_, i) => i))).toEqual(["all"]);
  });

  it("pages a larger session by ranges of at most the bound", () => {
    expect(unitPages(Array.from({ length: 160 }, (_, i) => i))).toEqual(["0-151", "152-159"]);
    expect(unitPages([9, 3, 5, 1, 7], 2)).toEqual(["1-3", "5-7", "9-9"]);
  });
});

const EVENTS = {
  asset_id: "a1",
  dataset: "dandi-000623",
  recipe: { version: "recipe-1", default: false, lfp_clock_offset_s: 0 },
  event_types: [
    {
      name: "scene_cut",
      averaging: "average",
      n_events: 1,
      events: [
        {
          onset_s: 12.5,
          stop_s: null,
          attributes: { cut_type: "hard" },
          source: { kind: "frames", ref: "stimuli/scenecut_info.csv", row: 0 },
        },
      ],
      missing_frames: [7],
    },
    { name: "movie_onset", averaging: "single", n_events: 1, events: [] },
    { name: "recognition_onset", averaging: "average", n_events: 40, events: [] },
    { name: "empty_kind", averaging: "average", n_events: 0, events: [] },
  ],
};

describe("events", () => {
  it("parses event types, attributes, sources and missing frames", () => {
    const e = parseEventsBody(EVENTS);
    expect(e.recipe_version).toBe("recipe-1");
    expect(e.event_types[0]?.missing_frames).toEqual([7]);
    expect(e.event_types[0]?.events[0]?.source).toBe("frames stimuli/scenecut_info.csv 0");
    expect(e.event_types[1]?.missing_frames).toBeNull();
  });

  it("columns: average types first, then a labelled single-trial column", () => {
    const cols = gridColumns(parseEventsBody(EVENTS).event_types);
    expect(cols.map((c) => c.label)).toEqual([
      "scene cut",
      "recognition onset",
      "movie onset, single trial",
    ]);
  });

  it("loads /compute/events?asset_id", async () => {
    const f = vi.fn((_url: string, _init: RequestInit) => Promise.resolve(json(200, EVENTS)));
    const r = await loadComputeEvents(CFG, "a 1", tokens(), f);
    expect(r.kind).toBe("loaded");
    expect(f.mock.calls[0]?.[0]).toBe(`${CFG.apiUrl}/compute/events?asset_id=a+1`);
  });
});

describe("session files", () => {
  const asset = (id: string, session: string | null, format: string) => ({
    asset_id: id,
    path: `sub-1/${id}.${format}`,
    subject: "sub-1",
    bids_session: session,
    datatype: null,
    format,
    size_bytes: 1,
    committed_at: null,
  });

  it("keeps the session's files across pages; null means no session label", async () => {
    const pages = [
      { assets: [asset("b", "ses-1", "nwb"), asset("x", "ses-2", "nwb")], page: 0, more: true },
      { assets: [asset("a", "ses-1", "nwb"), asset("n", null, "tsv")], page: 1, more: false },
    ];
    const f = vi.fn((url: string) => {
      const page = Number(new URL(url).searchParams.get("page") ?? "0");
      return Promise.resolve(json(200, { ...pages[page], query_ms: 1 }));
    });
    const r = await loadSessionFiles(CFG, "d", "sub-1", "ses-1", tokens(), f);
    if (r.kind !== "loaded") throw new Error("not loaded");
    expect(r.value.map((a) => a.asset_id)).toEqual(["b", "a"]);
    expect(sessionNwb(r.value)?.asset_id).toBe("a");
    const none = await loadSessionFiles(CFG, "d", "sub-1", null, tokens(), f);
    if (none.kind !== "loaded") throw new Error("not loaded");
    expect(none.value.map((a) => a.asset_id)).toEqual(["n"]);
    expect(sessionNwb(none.value)).toBeNull();
  });
});

describe("sortRows", () => {
  const scene = parseSessionRates(RATES).responsive;
  const recog = [
    { unit_index: 0, location: "hippocampus", p: 0.03, q: 0.05, screened: true },
    { unit_index: 2, location: null, p: 0.5, q: 0.6, screened: false },
  ];
  const rows = unitRows([2, 0, 1], { scene_cut: scene, recognition_onset: recog });
  const order = (v: string) => sortRows(rows, parseSortValue(v)).map((r) => r.unit_index);

  it("builds one row per unit with each type's entry and the first location", () => {
    expect(rows.map((r) => r.unit_index)).toEqual([0, 1, 2]);
    expect(rows[0]?.location).toBe("hippocampus");
    expect(rows[2]?.screen.scene_cut).toBeUndefined();
  });

  it("sorts by unit, location, p or q; a missing value sorts last", () => {
    expect(order("unit")).toEqual([0, 1, 2]);
    expect(order("location")).toEqual([1, 0, 2]);
    expect(order("p:scene_cut")).toEqual([1, 0, 2]);
    expect(order("q:recognition_onset")).toEqual([0, 2, 1]);
  });

  it("round-trips sort values; an unknown value is unit order", () => {
    for (const v of ["unit", "location", "p:scene_cut", "q:a:b"]) {
      expect(sortValue(parseSortValue(v))).toBe(v);
    }
    expect(parseSortValue("p:")).toEqual({ by: "unit" });
    expect(parseSortValue("bogus")).toEqual({ by: "unit" });
  });
});
