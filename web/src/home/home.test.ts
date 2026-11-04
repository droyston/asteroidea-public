import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import linksGolden from "../../e2e/fixtures/ux/catalog-links.golden.json" with { type: "json" };
import studiesGolden from "../../e2e/fixtures/ux/catalog-studies.golden.json" with { type: "json" };
import { CATALOG_FIXTURE as F } from "../artboard/fixture";
import { json, tokens } from "../testApi";
import { loadHome, parseHome } from "./data";
import { EMPTY_QUERY, parseQuery, queryString, toggleFacet } from "./facets";
import { HomeView } from "./HomeView";
import { LatestCard, type LatestState, loadLatestRun } from "./latest";
import { homeModel, idPrefix } from "./model";

const CFG = { apiUrl: "https://api.example.test" };

/** The golden bodies through the loader, as the route would answer them. */
async function loadGolden() {
  const f = vi.fn((url: string) =>
    Promise.resolve(json(200, url.endsWith("/catalog/studies") ? studiesGolden : linksGolden)),
  );
  const r = await loadHome(CFG, tokens(), f);
  return { r, f };
}

describe("loadHome", () => {
  it("reads both routes with the bearer token and parses the golden bodies", async () => {
    const { r, f } = await loadGolden();
    expect(f.mock.calls.map((c) => c[0]).sort()).toEqual([
      "https://api.example.test/catalog/links",
      "https://api.example.test/catalog/studies",
    ]);
    if (r.kind !== "loaded") throw new Error(`not loaded: ${JSON.stringify(r)}`);
    expect(r.value.studies.map((s) => s.study)).toEqual(["bmovie"]);
    expect(r.value.datasets).toHaveLength(studiesGolden.datasets.length);
    expect(r.value.links).toHaveLength(linksGolden.links.length);
    const model = homeModel(r.value);
    // The studies' own rows, then a curated dataset outside every study as a row of its own.
    expect(model.rows.map((x) => x.title)).toEqual([
      "Human single neurons and fMRI during movie watching",
      "Title of dandi-000576",
    ]);
    expect(model.tiers.sandbox).toBe(
      studiesGolden.datasets.filter((d) => d.tier === "sandbox").length,
    );
  });

  it("shows the server's sentence for a refusal and never throws", async () => {
    const f = vi.fn((url: string) =>
      Promise.resolve(
        url.endsWith("/catalog/studies")
          ? json(503, { error: "slow", detail: "The catalog took too long. Try again." })
          : json(200, linksGolden),
      ),
    );
    const r = await loadHome(CFG, tokens(), f);
    expect(r).toMatchObject({
      kind: "error",
      message: "The catalog took too long. Try again.",
      status503: true,
    });
  });

  it("reports a body that is not the contract", async () => {
    const f = vi.fn(() => Promise.resolve(json(200, { studies: [{ study: 1 }], datasets: [] })));
    const r = await loadHome(CFG, tokens(), f);
    expect(r.kind === "error" && r.message).toContain("cannot read");
  });
});

const BODY = parseHome(F.studies, F.links);

describe("homeModel over the fixture", () => {
  const m = homeModel(BODY);

  it("has one row per study and counts the tiers", () => {
    expect(m.rows).toHaveLength(F.studies.studies.length);
    const sandbox = F.studies.datasets.filter((d) => d.tier === "sandbox").length;
    expect(sandbox).toBeGreaterThan(0);
    expect(m.tiers).toEqual({ curated: 5, mine: 0, sandbox });
  });

  it("puts the six default modalities first, in their display order, then others, then 'Other' ones", () => {
    expect(m.columns.map((c) => c.id)).toEqual([
      "units",
      "lfp",
      "ieeg",
      "anat",
      "func",
      "task",
      "other:fmap",
      "other:position",
    ]);
    expect(m.columns.map((c) => c.label).slice(0, 6)).toEqual([
      "Units",
      "LFP",
      "iEEG",
      "Anat MRI",
      "fMRI",
      "Task",
    ]);
  });

  it("names the MRI-and-ephys participants and draws pending dashed, confirmed solid", () => {
    expect(m.rail.map((e) => [e.name, e.line])).toEqual([
      ["CS41", "dashed"],
      ["CS62", "dashed"],
      ["s01", "solid"],
    ]);
  });

  it("merges a person across archives only on a confirmed link", () => {
    const movie = m.rows.find((r) => r.id === "movie-watching");
    expect(movie?.meta).toContain("17 participants");
    const synthetic = m.rows.find((r) => r.id === "synthetic-24-participants");
    expect(synthetic?.meta).toContain("24 participants");
  });

  it("gives each release its full version string and licence", () => {
    const movie = m.rows.find((r) => r.id === "movie-watching");
    expect(movie?.releases.map((r) => r.text)).toEqual([
      "DANDI 000623 · 0.240227.2023 · ephys",
      "OpenNeuro ds004798 · 1.0.5 · fMRI",
    ]);
    const byId = new Map(F.studies.datasets.map((d) => [d.dataset, d.license]));
    expect(movie?.releases.map((r) => r.license)).toEqual([
      byId.get("dandi-000623"),
      byId.get("openneuro-ds004798"),
    ]);
    expect(movie?.releases.every((r) => r.license !== null)).toBe(true);
  });

  it("keeps left and right regions apart and prints every label", () => {
    const html = renderToStaticMarkup(createElement(HomeView, { body: BODY }));
    for (const label of ["Left vmPFC", "Right vmPFC", "Left hippocampus", "Right hippocampus"]) {
      expect(html).toContain(label);
    }
    const colours = new Set(
      m.rows.flatMap((r) => r.regions.filter((g) => /vmpfc/i.test(g.label)).map((g) => g.cssVar)),
    );
    expect(colours.size).toBe(2);
  });

  it("folds the sandbox into one line with its count and prefixes", () => {
    const total = F.studies.datasets.filter((d) => d.tier === "sandbox").length;
    expect(m.sandbox.count).toBe(total);
    const conc = F.studies.datasets.filter((d) => d.dataset.startsWith("conc-")).length;
    expect(m.sandbox.prefixes[0]).toEqual({ prefix: "conc", count: conc });
    expect(m.sandbox.prefixes.reduce((n, p) => n + p.count, 0)).toBe(total);
  });
});

describe("the home's query in the URL", () => {
  it("reads tier and facets, ignores what it does not know, and writes a canonical query", () => {
    const q = parseQuery(
      "?tier=mine&region=Right+amygdala&modality=lfp&modality=units&x=1&tier=bad",
    );
    expect(q).toEqual({
      tier: "mine",
      modality: ["lfp", "units"],
      region: ["Right amygdala"],
      species: [],
      archive: [],
    });
    expect(queryString(q)).toBe("?tier=mine&modality=lfp&modality=units&region=Right+amygdala");
    expect(queryString(parseQuery(queryString(q)))).toBe(queryString(q));
  });

  it("leaves the URL clean for the default view and for an unknown tier", () => {
    expect(queryString(EMPTY_QUERY)).toBe("");
    expect(parseQuery("?tier=nope")).toEqual(EMPTY_QUERY);
    expect(parseQuery("")).toEqual(EMPTY_QUERY);
  });

  it("toggles one value of one facet and keeps the rest", () => {
    const a = toggleFacet(EMPTY_QUERY, "modality", "lfp");
    const b = toggleFacet(a, "region", "Left vmPFC");
    expect(b.modality).toEqual(["lfp"]);
    expect(b.region).toEqual(["Left vmPFC"]);
    expect(toggleFacet(b, "modality", "lfp").modality).toEqual([]);
  });
});

describe("homeModel narrowed by facets", () => {
  const all = homeModel(BODY);
  const withModality = (id: string) =>
    new Set(
      F.studies.studies
        .filter((s) =>
          s.datasets.some((r) =>
            F.studies.datasets
              .find((d) => d.dataset === r.dataset)
              ?.subjects.some((x) => x.modalities.includes(id)),
          ),
        )
        .map((s) => s.study),
    );

  it("shows every study when nothing is chosen, and counts the options over all of them", () => {
    expect(all.rows).toHaveLength(5);
    const modality = all.facets.find((g) => g.id === "modality");
    expect(modality?.options.find((o) => o.value === "anat")?.count).toBe(
      withModality("anat").size,
    );
    expect(all.facets.map((g) => g.id)).toEqual(["modality", "region", "species", "archive"]);
  });

  it("a modality keeps the studies that have it and drops the columns nothing shows", () => {
    const m = homeModel(BODY, { ...EMPTY_QUERY, modality: ["anat"] });
    expect(new Set(m.rows.map((r) => r.id))).toEqual(withModality("anat"));
    expect(m.rows.length).toBeLessThan(all.rows.length);
    for (const c of m.columns) expect(m.rows.some((r) => r.cells[c.id] !== undefined)).toBe(true);
    expect(m.tiers.curated).toBe(all.tiers.curated);
  });

  it("two values of one facet widen, two facets narrow", () => {
    const either = homeModel(BODY, { ...EMPTY_QUERY, modality: ["anat", "ieeg"] });
    const both = homeModel(BODY, {
      ...EMPTY_QUERY,
      modality: ["anat"],
      region: ["Left hippocampus"],
    });
    const anat = homeModel(BODY, { ...EMPTY_QUERY, modality: ["anat"] });
    expect(either.rows.length).toBeGreaterThanOrEqual(anat.rows.length);
    expect(both.rows.length).toBeLessThanOrEqual(anat.rows.length);
    for (const r of both.rows)
      expect(r.regions.some((g) => g.label === "Left hippocampus")).toBe(true);
  });

  it("species and archive narrow the rows", () => {
    const mouse = homeModel(BODY, { ...EMPTY_QUERY, species: ["Mus musculus"] });
    expect(mouse.rows.length).toBeGreaterThan(0);
    expect(mouse.rows.length).toBeLessThan(all.rows.length);
    const openneuro = homeModel(BODY, { ...EMPTY_QUERY, archive: ["openneuro"] });
    expect(
      openneuro.rows.every((r) => r.releases.some((x) => x.text.startsWith("OpenNeuro"))),
    ).toBe(true);
  });

  it("a facet nothing has leaves no row, and the view says so with a way back", () => {
    const query = { ...EMPTY_QUERY, region: ["Nowhere"] };
    expect(homeModel(BODY, query).rows).toEqual([]);
    const html = renderToStaticMarkup(createElement(HomeView, { body: BODY, query }));
    expect(html).toContain("No study matches these facets");
    expect(html).toContain("Clear facets");
  });
});

describe("HomeView facets", () => {
  it("draws a chip per option with its count, pressed when chosen", () => {
    const html = renderToStaticMarkup(
      createElement(HomeView, {
        body: BODY,
        query: { ...EMPTY_QUERY, modality: ["lfp"] },
      }),
    );
    expect(html).toMatch(/aria-pressed="true"[^>]*data-facet="modality"[^>]*data-value="lfp"/);
    expect(html).toContain('data-facet="region"');
    expect(html).toContain('data-facet="species"');
    expect(html).toContain('data-facet="archive"');
  });

  it("says how many of the studies the facets leave", () => {
    const html = renderToStaticMarkup(
      createElement(HomeView, { body: BODY, query: { ...EMPTY_QUERY, modality: ["anat"] } }),
    );
    expect(html).toMatch(/\d of 5 curated studies/);
  });
});

describe("idPrefix", () => {
  it("drops stamps and hashes, keeps the words", () => {
    expect(idPrefix("conc-20261024185101-0b7e")).toBe("conc");
    expect(idPrefix("sdk-live-b74626f2")).toBe("sdk-live");
    expect(idPrefix("artifact-bad-20261025205108-a612")).toBe("artifact-bad");
    expect(idPrefix("dandi-000027")).toBe("dandi");
    expect(idPrefix("plain")).toBe("plain");
  });
});

describe("the Latest results card", () => {
  const row = (id: string, run: string | null, at: number, over: object = {}) => ({
    artifact_id: id,
    name: `${id}.png`,
    size: 1,
    state: "COMMITTED",
    created_at: at,
    finalized_at: at,
    content_type: "image/png",
    snapshot_id: null,
    source_asset_ids: [],
    source_asset_count: 0,
    run_id: run,
    run_function: run === null ? null : "showcase-cs62",
    author_email: "ada@example.test",
    ...over,
  });

  it("asks one scope=all listing and keeps the newest run", async () => {
    const f = vi.fn((_url: string) =>
      Promise.resolve(
        json(200, {
          artifacts: [
            row("v1", null, 1_792_000_900),
            row("new-b", "run-new", 1_792_000_500),
            row("old", "run-old", 1_792_000_100, { author_email: "grace@example.test" }),
            row("new-a", "run-new", 1_792_000_400),
          ],
        }),
      ),
    );
    const latest = await loadLatestRun(CFG, tokens(), f, "ada@example.test");
    expect(f.mock.calls.map((c) => c[0])).toEqual(["https://api.example.test/artifacts?scope=all"]);
    expect(latest.kind === "loaded" && latest.run).toMatchObject({
      run_id: "run-new",
      analysis: "showcase-cs62",
      files: 2,
      author: "you",
      href: "/results/new-a",
      subject: null,
    });
  });

  it("is empty when no run exists, and an error naming the status when refused", async () => {
    const none = vi.fn((_url: string) =>
      Promise.resolve(json(200, { artifacts: [row("v1", null, 1)] })),
    );
    expect(await loadLatestRun(CFG, tokens(), none, null)).toEqual({ kind: "loaded", run: null });
    const refused = vi.fn((_url: string) => Promise.resolve(json(503, {})));
    expect(await loadLatestRun(CFG, tokens(), refused, null)).toEqual({
      kind: "error",
      message: "the platform API answered HTTP 503",
    });
  });

  it("draws its loading, error, empty and loaded states", () => {
    const run = {
      run_id: "r",
      analysis: "showcase-cs62",
      subject: null,
      files: 20,
      date: "28 Oct",
      author: "you",
      mine: true,
      href: "/results/fig",
    };
    const draw = (state: LatestState) => renderToStaticMarkup(createElement(LatestCard, { state }));
    const loaded = draw({ kind: "loaded", run });
    expect(loaded).toContain('aria-label="Latest results"');
    expect(loaded).toContain('href="/results/fig"');
    expect(loaded).toContain("showcase-cs62");
    expect(loaded).toContain("20 files · 28 Oct · you");
    expect(draw({ kind: "loading" })).toContain("Listing the latest results…");
    expect(draw({ kind: "loaded", run: null })).toContain("No saved runs yet.");
    expect(draw({ kind: "error", message: "the platform API answered HTTP 503" })).toContain(
      "The latest results could not be listed: the platform API answered HTTP 503.",
    );
  });

  it("is a slot in the home view: the view draws no card of its own", () => {
    expect(renderToStaticMarkup(createElement(HomeView, { body: BODY }))).not.toContain(
      "Latest results",
    );
    const slot = createElement("p", { "data-testid": "slot" }, "card");
    expect(renderToStaticMarkup(createElement(HomeView, { body: BODY, latest: slot }))).toContain(
      'data-testid="slot"',
    );
  });
});
