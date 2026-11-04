import { describe, expect, it } from "vitest";
import goldenSummary from "../../e2e/fixtures/ux/session-summary.golden.json";
import { CATALOG_FIXTURE as F, RATES_CS62, UNITS_CS62 } from "../artboard/fixture";
import {
  buildHeatmap,
  chooseStored,
  chooseStoredEvents,
  chooseStoredUnits,
  direction,
  loadRatesStoredOrLive,
  loadStoredOrLive,
  MAX_UNITS,
  parseSessionEvents,
  parseSessionRates,
  parseSummaryAnswer,
  pickAsset,
  pickableTypes,
  ratesPath,
  rowPx,
  sessionAssets,
  signed,
  storedUnitLabels,
  unitLabels,
  unitSelection,
  windowMean,
} from "./model";

describe("the session's NWB file", () => {
  it("resolves CS62 R2 from the studies body", () => {
    const assets = sessionAssets(F.studies, "dandi-000623", "sub-CS62", "ses-2");
    expect(assets).toEqual([{ asset_id: "asset-cs62-r2", n_units: 44 }]);
  });

  it("is null for a session the body does not list", () => {
    expect(sessionAssets(F.studies, "dandi-000623", "sub-CS62", "ses-9")).toBeNull();
  });

  it("picks the most units, ties by asset id", () => {
    expect(
      pickAsset([
        { asset_id: "b", n_units: 10 },
        { asset_id: "c", n_units: 12 },
        { asset_id: "a", n_units: 12 },
      ]),
    ).toEqual({ asset_id: "a", n_units: 12 });
    expect(pickAsset([])).toBeNull();
  });
});

describe("the rates request", () => {
  it("asks for every unit at the display parameters, z on, no raster", () => {
    const q = new URLSearchParams(ratesPath("asset-cs62-r2", "scene_cut", "all").split("?")[1]);
    expect(Object.fromEntries(q)).toEqual({
      asset_id: "asset-cs62-r2",
      units: "all",
      align: "event:scene_cut",
      pre_s: "1",
      post_s: "2",
      bin_s: "0.05",
      zscore: "1",
      raster: "0",
    });
  });

  it("asks for the first 152 units of a larger file", () => {
    const many = Array.from({ length: 160 }, (_, i) => 159 - i);
    expect(unitSelection(many)).toBe(`0-${MAX_UNITS - 1}`);
    expect(unitSelection(many.slice(0, 152))).toBe("all");
  });
});

describe("events", () => {
  it("reads the default-recipe flag; a file with no events offers nothing to pick", () => {
    const e = parseSessionEvents({
      asset_id: "asset-000576-01",
      dataset: "dandi-000576",
      recipe: { version: "default", default: true, lfp_clock_offset_s: 0 },
      event_types: [],
    });
    expect(e.recipeDefault).toBe(true);
    expect(pickableTypes(e.types)).toEqual([]);
  });
});

describe("the heatmap model", () => {
  const parsed = parseSessionRates(RATES_CS62);
  const labels = unitLabels(UNITS_CS62.rows.map((r) => ({ ...r })));
  const heat = buildHeatmap(labels, parsed.rates, parsed.defaults.responseWindow);

  it("reads the defaults block's response window", () => {
    expect(parsed.defaults).toEqual({
      block: "dandi-000623",
      baseline: "none",
      responseWindow: [0, 1],
    });
  });

  it("groups 44 units into the 4 side-kept regions; every unit appears once", () => {
    expect(heat.units).toBe(44);
    expect(heat.groups.map((g) => [g.label, g.rows.length])).toEqual([
      ["Right ACC", 1],
      ["Right amygdala", 35],
      ["Right preSMA", 6],
      ["Right vmPFC", 2],
    ]);
    const units = heat.groups.flatMap((g) => g.rows.map((r) => r.unit)).sort((a, b) => a - b);
    expect(units).toEqual(Array.from({ length: 44 }, (_, i) => i));
  });

  it("the null-z unit counts in neither direction and is listed", () => {
    expect(heat.totals.nullUnits).toEqual([F.session.null_z_unit]);
    const t = heat.totals;
    expect(t.more + t.less + t.flat + t.nullUnits.length).toBe(44);
    expect(heat.groups.reduce((n, g) => n + g.more + g.less, 0)).toBe(t.more + t.less);
    const row = heat.groups.flatMap((g) => g.rows).find((r) => r.unit === F.session.null_z_unit);
    expect(row?.direction).toBe("null");
    expect(row?.delta).toBeNull();
  });

  it("sorts within a region by mean z, highest first, null-z last", () => {
    for (const g of heat.groups) {
      const d = g.rows.map((r) => r.delta);
      const finite = d.filter((v): v is number => v !== null);
      expect(finite).toEqual([...finite].sort((a, b) => b - a));
      expect(d.indexOf(null) === -1 || d.slice(d.indexOf(null)).every((v) => v === null)).toBe(
        true,
      );
    }
  });

  it("the mean covers the bins wholly inside the window (20 bins of 50 ms over 0 to 1 s)", () => {
    const edges = parsed.rates.edges;
    const z = edges.slice(0, -1).map((e) => (e >= 0 && e < 1 - 1e-9 ? 2 : -5));
    expect(windowMean(z, edges, [0, 1])).toBeCloseTo(2, 9);
    expect(windowMean([null, null], [0, 0.5, 1], [0, 1])).toBeNull();
  });

  it("names a direction at the threshold of 1", () => {
    expect([direction(1), direction(0.99), direction(-1), direction(null)]).toEqual([
      "more",
      "flat",
      "less",
      "null",
    ]);
  });
});

describe("display", () => {
  it("rows are 10 px at 44 units and 3 px at 152", () => {
    expect(rowPx(44)).toBe(10);
    expect(rowPx(152)).toBe(3);
    expect(rowPx(1)).toBe(10);
  });

  it("signs z with a true minus", () => {
    expect([signed(1.66), signed(-0.44), signed(0.01)]).toEqual(["+1.7", "−0.4", "0.0"]);
  });
});

describe("stored or live (the session summary)", () => {
  const built = parseSummaryAnswer(goldenSummary);
  const AID = "asset-cs62-r2";

  it("parses the golden body and its rates part through the rates parser", () => {
    expect(built.status).toBe("built");
    const c = chooseStored(built, AID, "scene_cut");
    expect(c?.kind).toBe("inline");
    if (c?.kind !== "inline") throw new Error("not inline");
    expect(parseSessionRates(c.body).rates.units.length).toBe(44);
    expect(c.builtAt).toBe("2026-10-27T12:00:00+00:00");
  });

  it("stored when built, the settings match and the type has a part", () => {
    expect(chooseStored(built, AID, "scene_cut")).not.toBeNull();
  });

  it("live when the summary is missing", () => {
    expect(chooseStored(parseSummaryAnswer({ status: "missing" }), AID, "scene_cut")).toBeNull();
  });

  it("live when there was an error (no summary)", () => {
    expect(chooseStored(null, AID, "scene_cut")).toBeNull();
  });

  it("live when a display setting differs", () => {
    for (const patch of [
      { pre_s: 2 },
      { post_s: 3 },
      { bin_s: 0.1 },
      { z_baseline_s: [-0.5, 0] },
    ]) {
      const raw = structuredClone(goldenSummary) as typeof goldenSummary;
      Object.assign(raw.manifest.display, patch);
      expect(chooseStored(parseSummaryAnswer(raw), AID, "scene_cut")).toBeNull();
    }
  });

  it("live for a type with no stored part, and for another file", () => {
    expect(chooseStored(built, AID, "recognition_onset")).toBeNull();
    expect(chooseStored(built, "asset-other", "scene_cut")).toBeNull();
  });

  it("a presigned answer yields the part's URL", () => {
    const raw = {
      status: "built",
      delivery: "presigned",
      manifest: goldenSummary.manifest,
      part_urls: { "rates-scene_cut": "https://s3.example/part" },
      expires_in: 900,
    };
    expect(chooseStored(parseSummaryAnswer(raw), AID, "scene_cut")).toEqual({
      kind: "url",
      url: "https://s3.example/part",
      builtAt: "2026-10-27T12:00:00+00:00",
    });
  });

  it("rejects a body that is not the stored shape", () => {
    expect(() => parseSummaryAnswer({ status: "built", delivery: "inline" })).toThrow();
    expect(() => parseSummaryAnswer([])).toThrow();
  });

  it("an unreadable stored part falls back to the live load", async () => {
    let live = 0;
    const r = await loadRatesStoredOrLive(
      { kind: "inline", body: { nonsense: true }, builtAt: "t" },
      () => {
        live += 1;
        return Promise.resolve({ kind: "loaded", value: parseSessionRates(RATES_CS62) });
      },
      () => Promise.reject(new Error("no fetch")),
      1000,
    );
    expect(live).toBe(1);
    expect(r.kind === "loaded" && r.value.storedAt).toBeNull();
  });

  it("the golden's events and units parts parse with the live parsers", () => {
    const e = chooseStoredEvents(built, AID);
    const u = chooseStoredUnits(built, AID);
    if (e?.kind !== "inline" || u?.kind !== "inline") throw new Error("not inline");
    expect(parseSessionEvents(e.body).types.map((t) => t.name)).toEqual([
      "scene_cut",
      "recognition_onset",
      "movie_onset",
    ]);
    expect(storedUnitLabels(u.body)).toEqual(unitLabels(UNITS_CS62.rows.map((r) => ({ ...r }))));
  });

  it("events and units: stored when built for this file and present, else live", () => {
    expect(chooseStoredEvents(built, AID)?.kind).toBe("inline");
    expect(chooseStoredUnits(built, AID)?.kind).toBe("inline");
    for (const s of [null, parseSummaryAnswer({ status: "missing" })]) {
      expect(chooseStoredEvents(s, AID)).toBeNull();
      expect(chooseStoredUnits(s, AID)).toBeNull();
    }
    expect(chooseStoredEvents(built, "asset-other")).toBeNull();
    expect(chooseStoredUnits(built, "asset-other")).toBeNull();
  });

  it("events and units do not depend on the display settings", () => {
    const raw = structuredClone(goldenSummary) as typeof goldenSummary;
    Object.assign(raw.manifest.display, { pre_s: 2 });
    const s = parseSummaryAnswer(raw);
    expect(chooseStored(s, AID, "scene_cut")).toBeNull();
    expect(chooseStoredEvents(s, AID)).not.toBeNull();
    expect(chooseStoredUnits(s, AID)).not.toBeNull();
  });

  it("a summary stored without the parts (schema 1) reads them live", () => {
    const { events: _e, units: _u, ...rates } = goldenSummary.parts;
    const { units_table: _t, ...manifest } = goldenSummary.manifest;
    const s = parseSummaryAnswer({ ...goldenSummary, manifest, parts: rates });
    expect(chooseStoredEvents(s, AID)).toBeNull();
    expect(chooseStoredUnits(s, AID)).toBeNull();
    expect(chooseStored(s, AID, "scene_cut")).not.toBeNull();
  });

  it("a units part that is only the first page reads the units live", () => {
    const manifest = { ...goldenSummary.manifest, units_table: { rows: 200, more: true } };
    const s = parseSummaryAnswer({ ...goldenSummary, manifest });
    expect(chooseStoredUnits(s, AID)).toBeNull();
    expect(chooseStoredEvents(s, AID)).not.toBeNull();
    expect(() => storedUnitLabels({ rows: [], page: 0, more: true })).toThrow();
  });

  it("presigned events and units parts yield their URLs", () => {
    const raw = {
      status: "built",
      delivery: "presigned",
      manifest: goldenSummary.manifest,
      part_urls: { events: "https://s3.example/e", units: "https://s3.example/u" },
      expires_in: 900,
    };
    const s = parseSummaryAnswer(raw);
    expect(chooseStoredEvents(s, AID)).toMatchObject({ kind: "url", url: "https://s3.example/e" });
    expect(chooseStoredUnits(s, AID)).toMatchObject({ kind: "url", url: "https://s3.example/u" });
  });

  it("a stored events part skips the live load; an unreadable one falls back", async () => {
    const live = () => Promise.reject(new Error("live must not run"));
    const noFetch = () => Promise.reject(new Error("no fetch"));
    const r = await loadStoredOrLive(
      chooseStoredEvents(built, AID),
      parseSessionEvents,
      live,
      noFetch,
      1000,
    );
    expect(r.kind === "loaded" && r.value.storedAt).toBe("2026-10-27T12:00:00+00:00");
    let asked = 0;
    const back = await loadStoredOrLive(
      { kind: "inline", body: { rows: "no" }, builtAt: "t" },
      storedUnitLabels,
      () => {
        asked += 1;
        return Promise.resolve({ kind: "loaded", value: [] });
      },
      noFetch,
      1000,
    );
    expect(asked).toBe(1);
    expect(back.kind === "loaded" && back.value.storedAt).toBeNull();
  });

  it("a stored part skips the live load", async () => {
    const c = chooseStored(built, AID, "scene_cut");
    const r = await loadRatesStoredOrLive(
      c,
      () => Promise.reject(new Error("live must not run")),
      () => Promise.reject(new Error("no fetch")),
      1000,
    );
    expect(r.kind === "loaded" && r.value.storedAt).toBe("2026-10-27T12:00:00+00:00");
  });
});
