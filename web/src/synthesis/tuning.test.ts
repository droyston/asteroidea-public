import { describe, expect, it, vi } from "vitest";
import { json, tokens } from "../testApi";
import { sessionFilesPath } from "./session";
import { loadSessionResults } from "./sessionResults";
import { areaLine, attributeNames, loadTuning, parseTuning, tuningPath } from "./tuning";

const CFG = { apiUrl: "https://api.example.test" };

const TUNING = {
  label: "exploratory screen",
  asset_id: "a1",
  event_type: "scene_cut",
  attribute: "cut_kind",
  units: [
    {
      unit_index: 3,
      location: "amygdala left",
      level_means_hz: { hard: 4.1, soft: 2.2 },
      n_per_level: { hard: 40, soft: 53 },
      selectivity_index: 0.31,
      statistic: 5.5,
      p: 0.012,
      q: 0.04,
      screened: true,
    },
    {
      unit_index: 5,
      location: null,
      level_means_hz: { hard: 1, soft: 1 },
      n_per_level: { hard: 40, soft: 53 },
      selectivity_index: 0,
      statistic: 0,
      p: null,
      q: null,
      screened: false,
    },
  ],
  areas: [{ location: "amygdala left", n_units: 10, n_below_alpha: 3, fraction: 0.3, p: 0.0123 }],
  settings: { levels: ["hard", "soft"], alpha: 0.05 },
};

describe("tuning", () => {
  it("builds the path with units=all", () => {
    const q = new URL(`${CFG.apiUrl}${tuningPath("a 1", "scene_cut", "cut_kind")}`).searchParams;
    expect(q.get("asset_id")).toBe("a 1");
    expect(q.get("event")).toBe("scene_cut");
    expect(q.get("attribute")).toBe("cut_kind");
    expect(q.get("units")).toBe("all");
  });

  it("parses the server's body: levels, n per level, p and q, null p", () => {
    const b = parseTuning(TUNING);
    expect(b.levels).toEqual(["hard", "soft"]);
    expect(b.n_per_level).toEqual({ hard: 40, soft: 53 });
    expect(b.units[0]?.screened).toBe(true);
    expect(b.units[1]?.p).toBeNull();
    expect(b.units[1]?.location).toBeNull();
    expect(b.alpha).toBe(0.05);
    expect(areaLine(b.areas[0] ?? (undefined as never))).toBe(
      "amygdala left: 3 of 10 units below alpha (30.0%), p 0.0123",
    );
  });

  it("refuses a body without units, areas or levels", () => {
    expect(() => parseTuning({ ...TUNING, units: null })).toThrow('"units"');
    expect(() => parseTuning({ ...TUNING, areas: null })).toThrow('"areas"');
    expect(() => parseTuning({ ...TUNING, settings: {} })).toThrow("settings.levels");
  });

  it("returns the server's refusal sentence, with the counts, and its code", async () => {
    const detail =
      "attribute cut_kind of scene_cut needs at least 2 levels of 10 counted events; counts: hard 40, soft 3";
    const f = vi.fn((_u: string, _i: RequestInit) =>
      Promise.resolve(json(400, { error: "too_few_events_per_level", detail })),
    );
    const r = await loadTuning(CFG, "a1", "scene_cut", "cut_kind", tokens(), f);
    expect(r).toMatchObject({ kind: "error", message: detail, code: "too_few_events_per_level" });
  });

  it("lists the attribute names of an event type, sorted", () => {
    const ev = (attributes: Record<string, unknown>) => ({
      onset_s: 0,
      stop_s: null,
      attributes,
      source: "",
    });
    const names = attributeNames({
      name: "t",
      averaging: "average",
      n_events: 2,
      events: [ev({ b: 1 }), ev({ a: 1, b: 2 })],
      missing_frames: null,
    });
    expect(names).toEqual(["a", "b"]);
  });
});

describe("session files path", () => {
  it("sends session=, and _ for no session", () => {
    expect(sessionFilesPath("d 1", "sub-1", "ses-1", 0)).toBe(
      "/catalog/datasets/d%201/assets?subject=sub-1&session=ses-1",
    );
    expect(sessionFilesPath("d", "sub-1", null, 2)).toBe(
      "/catalog/datasets/d/assets?subject=sub-1&session=_&page=2",
    );
  });
});

describe("session results", () => {
  const art = (id: string, created_at: number) => ({
    artifact_id: id,
    name: `${id}.csv`,
    size: 1,
    state: "COMMITTED",
    created_at,
    author_email: "ada@example.test",
  });
  const byInput: Record<string, unknown[]> = {
    f1: [art("x", 10), art("y", 30)],
    f2: [art("y", 30), art("z", 20)],
    f3: [],
  };

  it("takes the union over the files, each result once, newest first", async () => {
    const f = vi.fn((url: string, _i: RequestInit) => {
      const q = new URL(url).searchParams;
      expect(q.get("scope")).toBe("all");
      return Promise.resolve(json(200, { artifacts: byInput[q.get("input") ?? ""] }));
    });
    const r = await loadSessionResults(CFG, ["f1", "f2", "f3"], tokens(), f);
    expect(f).toHaveBeenCalledTimes(3);
    if (r.kind !== "loaded") throw new Error("not loaded");
    expect(r.artifacts.map((a) => a.artifact_id)).toEqual(["y", "z", "x"]);
  });

  it("ends with the first failing file's message", async () => {
    const f = vi.fn((url: string, _i: RequestInit) =>
      Promise.resolve(
        new URL(url).searchParams.get("input") === "f2"
          ? json(403, {})
          : json(200, { artifacts: [] }),
      ),
    );
    const r = await loadSessionResults(CFG, ["f1", "f2"], tokens(), f);
    expect(r.kind).toBe("error");
  });

  it("a session with no files lists nothing and calls nothing", async () => {
    const f = vi.fn((_u: string, _i: RequestInit) => Promise.resolve(json(200, { artifacts: [] })));
    const r = await loadSessionResults(CFG, [], tokens(), f);
    expect(r).toEqual({ kind: "loaded", artifacts: [] });
    expect(f).not.toHaveBeenCalled();
  });
});
