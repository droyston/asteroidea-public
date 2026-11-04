// The participant page's parsers, model, timeline layout, glyph and loaders. The
// catalog is the artboard fixture (a frozen live catalog listing), so a number here is one a person
// can read off the fixture.
import { describe, expect, it, vi } from "vitest";
import synthesisEvents from "../../e2e/fixtures/synthesis-events.json" with { type: "json" };
import { CATALOG_FIXTURE as F, RATES_CS62, UNITS_CS62 } from "../artboard/fixture";
import { parseSessionRates, unitLabels } from "../session/model";
import { parseStudies } from "../study/model";
import { json, tokens } from "../testApi";
import { buildGlyph, loadSessionExtras, MAX_GLYPH_ROWS, storedRateTypes } from "./glyph";
import { loadParticipant } from "./load";
import {
  CARDS_SHOWN,
  layoutLane,
  MERGE_PX,
  parseParticipantLinks,
  participantModel,
  regionLines,
  sexAgeOf,
  type Tick,
  t1wState,
} from "./model";

const CFG = { apiUrl: "https://api.example.test" };
const STUDIES = parseStudies(F.studies);
const LINKS = parseParticipantLinks(F.links);
const KEY = "bmovie-patient-62";
const MINE = LINKS.filter((l) => l.subject_key === KEY);

describe("links", () => {
  it("reads basis and note, which the study page's parser drops", () => {
    expect(MINE).toHaveLength(2);
    expect(MINE[0]).toMatchObject({ basis: null, status: "pending" });
    expect(MINE[0]?.note).toContain("Patient 62");
    const [one] = parseParticipantLinks({
      links: [
        {
          dataset: "d",
          subject: "s",
          subject_key: "k",
          status: "confirmed",
          linked_by: "m@x.test",
          linked_at: "2026-10-20 12:00:00.000",
          basis: "same code",
          note: null,
        },
      ],
    });
    expect(one).toMatchObject({ basis: "same code", linked_by: "m@x.test" });
  });

  it("refuses a body of the wrong shape with the field named", () => {
    expect(() => parseParticipantLinks({ links: {} })).toThrow('"links"');
    expect(() => parseParticipantLinks({ links: [{ dataset: 1 }] })).toThrow("dataset");
  });
});

describe("participantModel", () => {
  const model = participantModel(KEY, STUDIES, MINE);

  it("is one person with the record of each archive", () => {
    expect(model?.heading).toBe("sub-CS62");
    expect(model?.linked).toBe(true);
    expect(model?.records.map((r) => `${r.dataset}/${r.subject}`)).toEqual([
      "dandi-000623/sub-CS62",
      "openneuro-ds004798/sub-p62cs",
    ]);
    expect(model?.studyKey).toBe("movie-watching");
  });

  it("draws the bridge dashed while pending, with who, when and on what basis", () => {
    expect(model?.bridge).toMatchObject({
      confirmed: false,
      status: "pending",
      linkedBy: null,
      date: null,
      basis: null,
    });
    const confirmed = participantModel(
      KEY,
      STUDIES,
      MINE.map((l) => ({
        ...l,
        status: "confirmed",
        linked_by: "m@x.test",
        linked_at: "2026-10-20 12:00:00.000",
        basis: "same code",
      })),
    );
    expect(confirmed?.bridge).toMatchObject({
      confirmed: true,
      status: "confirmed",
      linkedBy: "m@x.test",
      date: "2026-10-20",
      basis: "same code",
    });
  });

  it("a person is confirmed only when every link is", () => {
    const [a, b] = MINE;
    const mixed = participantModel(
      KEY,
      STUDIES,
      [a, b].flatMap((l, i) =>
        l === undefined ? [] : [{ ...l, status: i === 0 ? "confirmed" : "pending" }],
      ),
    );
    expect(mixed?.bridge).toMatchObject({ confirmed: false, status: "pending" });
  });

  it("puts one lane per modality and one tick per recording", () => {
    expect(model?.lanes.map((l) => [l.label, l.ticks.length])).toEqual([
      ["Ephys", 2],
      ["Anatomical MRI", 1],
      ["Functional MRI", 1],
      ["fmap", 1],
    ]);
    expect(model?.axis.kind).toBe("order");
  });

  it("uses start times when every recording has one", () => {
    const dated = participantModel(KEY, STUDIES, MINE, {
      sexAge: new Map(),
      dates: {
        "dandi-000623/sub-CS62/ses-1": "2023-06-15 10:00:00",
        "dandi-000623/sub-CS62/ses-2": "2023-06-25 10:00:00",
        "openneuro-ds004798/sub-p62cs/ses-mri": "2023-06-20 10:00:00",
      },
    });
    expect(dated?.axis).toEqual({ kind: "time", from: "2023-06-15", to: "2023-06-25" });
    const ephys = dated?.lanes.find((l) => l.key === "ephys");
    expect(ephys?.ticks.map((t) => t.frac)).toEqual([0, 1]);
    expect(dated?.lanes.find((l) => l.key === "anat")?.ticks[0]?.frac).toBeCloseTo(0.5);
    expect(dated?.ephys.map((e) => e.session)).toEqual(["ses-2", "ses-1"]);
  });

  it("makes an ephys card of each session with units, the one with most units chosen", () => {
    expect(model?.ephys.map((e) => [e.session, e.assetId, e.units])).toEqual([
      ["ses-1", "asset-cs62-1", 20],
      ["ses-2", "asset-cs62-r2", 44],
    ]);
    expect(model?.ephysHidden).toBe(0);
    expect(model?.t1w).toMatchObject({ session: "ses-mri", assetIds: ["asset-p62cs-t1w"] });
  });

  it("an unlinked subject is a person of one record, keyed dataset:subject", () => {
    const one = participantModel("dandi-000623:sub-CS62", STUDIES, []);
    expect(one?.linked).toBe(false);
    expect(one?.bridge).toBeNull();
    expect(one?.records).toHaveLength(1);
    expect(one?.lanes.map((l) => l.key)).toEqual(["ephys"]);
    expect(one?.t1w).toBeNull();
  });

  it("is null for a person with no record in the catalog", () => {
    expect(participantModel("nobody", STUDIES, [])).toBeNull();
    expect(participantModel("d:nobody", STUDIES, [])).toBeNull();
  });

  it("shows six ephys cards and counts the rest", () => {
    const many = {
      ...F.studies,
      datasets: F.studies.datasets.map((d) =>
        d.dataset !== "dandi-000623"
          ? d
          : {
              ...d,
              subjects: d.subjects.map((s) =>
                s.subject !== "sub-CS62"
                  ? s
                  : {
                      ...s,
                      sessions: Array.from({ length: 9 }, (_, i) => ({
                        session: `ses-${i}`,
                        asset_ids: [`a${i}`],
                        nwb: [{ asset_id: `a${i}`, n_units: 5 }],
                      })),
                    },
              ),
            },
      ),
    };
    const m = participantModel("dandi-000623:sub-CS62", parseStudies(many), []);
    expect(m?.ephys).toHaveLength(CARDS_SHOWN);
    expect(m?.ephysHidden).toBe(3);
  });

  it("reads sex and age from the studies body when the body has them", () => {
    const raw = {
      datasets: [{ dataset: "d", subjects: [{ subject: "s", sex: "F", age: "42 years" }] }],
    };
    expect(sexAgeOf(raw).get("d/s")).toEqual({ sex: "F", age: "42 years" });
  });
});

describe("layoutLane", () => {
  const tick = (frac: number): Tick => ({ session: "s", dataset: "d", label: "ses", frac });
  // 108 px wide with 8 px padding on each side gives a 92 px span.
  const WIDTH = 108;

  it("merges ticks closer than 6 px into a counted block and keeps ticks 6 px or more apart", () => {
    const blocks = layoutLane([tick(0), tick(5 / 92), tick(11 / 92), tick(1)], WIDTH);
    expect(blocks.map((b) => b.count)).toEqual([2, 1, 1]);
    expect(blocks[0]?.label).toBe("2 recordings");
    expect(blocks[1]?.label).toBe("ses");
    expect(MERGE_PX).toBe(6);
  });

  it("merges every tick of a lane that is narrower than the gap", () => {
    expect(layoutLane([tick(0), tick(0.2), tick(1)], 20).map((b) => b.count)).toEqual([3]);
  });
});

describe("glyph", () => {
  const parsed = parseSessionRates(RATES_CS62);
  const labels = unitLabels(UNITS_CS62.rows.map((r) => ({ ...r })));

  it("draws one row per unit up to 48, in at most 40 columns", () => {
    const g = buildGlyph("asset-cs62-r2", "scene_cut", labels, parsed, null);
    expect(g.nUnits).toBe(44);
    expect(g.rows).toHaveLength(44);
    expect(g.cols).toBeLessThanOrEqual(40);
    expect(g.rows.every((r) => r.cells.every((c) => c.x < g.cols))).toBe(true);
  });

  it("groups units when there are more than 48", () => {
    const first = RATES_CS62.units[0];
    if (first === undefined) throw new Error("fixture has no units");
    const big = parseSessionRates({
      ...RATES_CS62,
      units: Array.from({ length: 152 }, (_, i) => ({ ...first, unit_index: i })),
    });
    const g = buildGlyph("a", "scene_cut", [], big, null);
    expect(g.nUnits).toBe(152);
    expect(g.rows.length).toBeLessThanOrEqual(MAX_GLYPH_ROWS);
  });

  it("leaves a unit with no baseline variance out of the colour, not out of the row", () => {
    const g = buildGlyph("asset-cs62-r2", "scene_cut", labels, parsed, null);
    const nullUnit = RATES_CS62.units.filter((u) => u.z === null).length;
    expect(nullUnit).toBe(1);
    expect(g.rows.some((r) => r.cells.length === 0)).toBe(true);
  });
});

describe("regionLines", () => {
  it("counts units per region and puts the release's channels beside, side kept", () => {
    const lines = regionLines(
      ["Right amygdala", "Right amygdala", "Left vmPFC", null],
      [
        { region: "Right amygdala", channels: 64 },
        { region: "Left vmPFC", channels: 32 },
        { region: "Left ACC", channels: 32 },
      ],
    );
    expect(lines.map((l) => [l.label, l.units, l.channels])).toEqual([
      ["Right amygdala", 2, 64],
      ["Left vmPFC", 1, 32],
    ]);
  });
});

function serve(over: Record<string, unknown> = {}) {
  const calls: string[] = [];
  const bodies: Record<string, unknown> = {
    "/catalog/studies": F.studies,
    [`/catalog/links?subject_key=${KEY}`]: F.links,
    "/catalog/links?dataset=dandi-000623&subject=sub-CS62": {
      links: LINKS.filter((l) => l.dataset === "dandi-000623" && l.subject === "sub-CS62"),
    },
    ...over,
  };
  const fn = vi.fn((url: string) => {
    const path = url.replace(CFG.apiUrl, "");
    calls.push(path);
    const body = bodies[path];
    return Promise.resolve(
      body === undefined ? json(404, { error: `no ${path}` }) : json(200, body),
    );
  });
  return { fn, calls };
}

describe("loadParticipant", () => {
  it("reads the studies and the person's links, then the dated sessions of each release", async () => {
    const { fn, calls } = serve();
    const r = await loadParticipant(CFG, KEY, tokens(), fn);
    expect(r.kind).toBe("loaded");
    if (r.kind !== "loaded") return;
    expect(r.value.model?.records).toHaveLength(2);
    expect(calls).toContain("/catalog/studies");
    expect(calls).toContain(`/catalog/links?subject_key=${KEY}`);
    expect(calls).toContain("/catalog/datasets/dandi-000623");
    expect(calls).toContain("/catalog/datasets/openneuro-ds004798");
    // A release whose detail cannot be read costs the dates, not the page.
    expect(r.value.model?.axis.kind).toBe("order");
  });

  it("resolves dataset:subject to the person when a link names the subject", async () => {
    const { fn, calls } = serve();
    const r = await loadParticipant(CFG, "dandi-000623:sub-CS62", tokens(), fn);
    expect(r.kind === "loaded" && r.value.model?.key).toBe(KEY);
    expect(r.kind === "loaded" && r.value.model?.records).toHaveLength(2);
    expect(calls).toContain("/catalog/links?dataset=dandi-000623&subject=sub-CS62");
    expect(calls).toContain(`/catalog/links?subject_key=${KEY}`);
  });

  it("keeps only the links of this person when the body holds others", async () => {
    const { fn } = serve();
    const r = await loadParticipant(CFG, KEY, tokens(), fn);
    expect(r.kind === "loaded" && r.value.model?.records.map((x) => x.subject)).toEqual([
      "sub-CS62",
      "sub-p62cs",
    ]);
  });

  it("answers a null model for a person that is not in the catalog", async () => {
    const { fn } = serve({ "/catalog/links?subject_key=nobody": { links: [] } });
    const r = await loadParticipant(CFG, "nobody", tokens(), fn);
    expect(r).toEqual({ kind: "loaded", value: { model: null } });
  });
});

const EVENTS = { ...synthesisEvents, asset_id: "asset-cs62-r2" };

describe("loadSessionExtras", () => {
  const s = {
    dataset: "dandi-000623",
    subject: "sub-CS62",
    session: "ses-2",
    assetId: "asset-cs62-r2",
    units: 44,
  };
  const built = {
    status: "built",
    delivery: "inline",
    manifest: {
      asset_id: "asset-cs62-r2",
      built_at: "2026-10-26T10:00:00Z",
      display: { pre_s: 1, post_s: 2, bin_s: 0.05, z_baseline_s: [-1, 0] },
      units_table: { rows: 44, more: false },
    },
    parts: {
      "rates-scene_cut": RATES_CS62,
      units: { rows: UNITS_CS62.rows, page: 0, more: false },
    },
  };

  it("draws the glyph from the stored rates part and asks no compute route", async () => {
    const { fn, calls } = serve({
      "/catalog/session-summary?dataset=dandi-000623&subject=sub-CS62&session=ses-2": built,
    });
    const x = await loadSessionExtras(CFG, s, tokens(), fn);
    expect(x.glyph.kind).toBe("ready");
    if (x.glyph.kind === "ready") {
      expect(x.glyph.glyph.storedAt).toBe("2026-10-26T10:00:00Z");
      expect(x.glyph.glyph.eventType).toBe("scene_cut");
    }
    expect(x.locations).toHaveLength(44);
    expect(calls.filter((c) => c.startsWith("/compute"))).toEqual([]);
  });

  it("falls back to the live rates (z, no raster) when the summary is missing", async () => {
    const { fn, calls } = serve({
      "/catalog/session-summary?dataset=dandi-000623&subject=sub-CS62&session=ses-2": {
        status: "missing",
      },
      "/compute/events?asset_id=asset-cs62-r2": EVENTS,
      "/catalog/assets/asset-cs62-r2/tables/units": {
        rows: UNITS_CS62.rows,
        page: 0,
        more: false,
      },
      "/compute/rates?asset_id=asset-cs62-r2&units=all&align=event%3Ascene_cut&pre_s=1&post_s=2&bin_s=0.05&zscore=1&raster=0":
        RATES_CS62,
    });
    const x = await loadSessionExtras(CFG, s, tokens(), fn);
    expect(x.glyph.kind).toBe("ready");
    if (x.glyph.kind === "ready") expect(x.glyph.glyph.storedAt).toBeNull();
    const rates = calls.find((c) => c.startsWith("/compute/rates"));
    expect(rates).toContain("zscore=1");
    expect(rates).toContain("raster=0");
  });

  it("says why when the live compute is refused, and still returns the unit locations", async () => {
    const { fn } = serve({
      "/catalog/session-summary?dataset=dandi-000623&subject=sub-CS62&session=ses-2": {
        status: "missing",
      },
      "/compute/events?asset_id=asset-cs62-r2": EVENTS,
      "/catalog/assets/asset-cs62-r2/tables/units": {
        rows: UNITS_CS62.rows,
        page: 0,
        more: false,
      },
    });
    const x = await loadSessionExtras(CFG, s, tokens(), fn);
    expect(x.glyph.kind).toBe("none");
    expect(x.locations).toHaveLength(44);
  });

  it("names the stored rate types in the summary's order", () => {
    expect(storedRateTypes(null)).toEqual([]);
    expect(
      storedRateTypes({
        status: "built",
        assetId: "a",
        builtAt: "t",
        display: { pre_s: 1, post_s: 2, bin_s: 0.05, z_baseline_s: [-1, 0] },
        parts: { events: {}, "rates-b": {}, "rates-a": {} },
        partUrls: null,
        unitsComplete: true,
      }),
    ).toEqual(["b", "a"]);
  });
});

describe("T1w card state", () => {
  it("is always not built (the preview route is deferred): the target's first file, or none", () => {
    expect(t1wState({ dataset: "d", subject: "s", session: "x", assetIds: ["a", "b"] })).toEqual({
      kind: "missing",
      assetId: "a",
    });
    expect(t1wState(null)).toEqual({ kind: "none" });
  });
});
