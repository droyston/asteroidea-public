// The study page's parsers, loader and model. The goldens are the bodies the
// real routes answered over the test catalog (`web/e2e/fixtures/ux/catalog-*.golden.json`).
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import { CATALOG_FIXTURE as F } from "../artboard/fixture";
import { json, tokens } from "../testApi";
import { loadStudy } from "./load";
import { parseLinks, parseStudies, type StudyModel, studyModel } from "./model";
import { stripPx } from "./Strip";
import { StudyView } from "./StudyView";
import { loadSourcePage, recordingGroups } from "./sources";

const FIXTURES = join(new URL("..", import.meta.url).pathname, "..", "e2e", "fixtures", "ux");
const read = (name: string): unknown =>
  JSON.parse(readFileSync(join(FIXTURES, name), "utf8")) as unknown;
const STUDIES = read("catalog-studies.golden.json");
const LINKS = read("catalog-links.golden.json");
const CFG = { apiUrl: "https://api.example.test" };

function serve(over: Record<string, unknown> = {}) {
  const bodies: Record<string, unknown> = {
    "/catalog/studies": STUDIES,
    "/catalog/links?study=bmovie": LINKS,
    ...over,
  };
  return vi.fn((url: string) => {
    const path = url.replace(CFG.apiUrl, "");
    const body = bodies[path];
    return Promise.resolve(
      body === undefined ? json(404, { error: `no ${path}` }) : json(200, body),
    );
  });
}

describe("the goldens through the parsers", () => {
  it("parses the studies body: nullable n_units, trials and session labels survive", () => {
    const body = parseStudies(STUDIES);
    expect(body.studies.map((s) => s.study)).toEqual(["bmovie"]);
    expect(body.datasets).toHaveLength(6);
    const anm = body.datasets.find((d) => d.dataset === "dandi-000027")?.subjects[0];
    expect(anm?.sessions[0]?.session).toBe("_");
    expect(anm?.sessions[0]?.nwb[0]?.n_units).toBeNull();
  });

  it("parses the links body", () => {
    const links = parseLinks(LINKS).links;
    expect(links.length).toBeGreaterThan(0);
    expect(links.every((l) => l.status === "pending")).toBe(true);
    expect(links.every((l) => l.linked_by === null)).toBe(true);
  });

  it("refuses a body of the wrong shape with the field named", () => {
    expect(() => parseStudies({ studies: {}, datasets: [], query_ms: 1 })).toThrow('"studies"');
    expect(() => parseLinks({ links: [{ dataset: 1 }] })).toThrow('"links[0].dataset"');
  });
});

describe("loadStudy over the goldens", () => {
  it("asks for the studies and the study's links, and builds the model", async () => {
    const f = serve();
    const r = await loadStudy(CFG, { kind: "study", study: "bmovie" }, tokens(), f);
    expect(f.mock.calls.map((c) => c[0]).sort()).toEqual([
      "https://api.example.test/catalog/links?study=bmovie",
      "https://api.example.test/catalog/studies",
    ]);
    if (r.kind !== "loaded") throw new Error(r.kind);
    const model = r.value.model;
    expect(model?.title).toBe("Human single neurons and fMRI during movie watching");
    expect(model?.releases.map((x) => x.dataset)).toEqual(["dandi-000623", "openneuro-ds004798"]);
    const rows = model?.releases.flatMap((x) => x.rows) ?? [];
    const raw = parseStudies(STUDIES);
    const want = raw.datasets
      .filter((d) => ["dandi-000623", "openneuro-ds004798"].includes(d.dataset))
      .reduce((n, d) => n + d.subjects.length, 0);
    expect(rows).toHaveLength(want);
    const linked = rows.filter((x) => x.link !== null);
    expect(linked).toHaveLength(
      parseLinks(LINKS).links.filter((l) =>
        ["dandi-000623", "openneuro-ds004798"].includes(l.dataset),
      ).length,
    );
    expect(linked.every((x) => x.link?.confirmed === false)).toBe(true);
  });

  it("a dataset in no study asks for the studies body only", async () => {
    const f = serve();
    const r = await loadStudy(CFG, { kind: "dataset", dataset: "dandi-000027" }, tokens(), f);
    expect(f.mock.calls.map((c) => c[0])).toEqual(["https://api.example.test/catalog/studies"]);
    if (r.kind !== "loaded") throw new Error(r.kind);
    expect(r.value.model?.mode).toBe("dataset");
    expect(r.value.model?.partOf).toBeNull();
    expect(r.value.model?.releases).toHaveLength(1);
    expect(r.value.model?.releases[0]?.rows.every((x) => x.link === null)).toBe(true);
  });

  it("a dataset in a study also asks for that study's links and names the study", async () => {
    const f = serve();
    const r = await loadStudy(CFG, { kind: "dataset", dataset: "dandi-000623" }, tokens(), f);
    expect(f.mock.calls).toHaveLength(2);
    if (r.kind !== "loaded") throw new Error(r.kind);
    expect(r.value.model?.partOf?.study).toBe("bmovie");
    expect(r.value.model?.releases[0]?.rows.some((x) => x.link !== null)).toBe(true);
  });

  it("an unknown study or dataset is a null model, not an error", async () => {
    const f = serve({ "/catalog/links?study=nope": { links: [] } });
    const a = await loadStudy(CFG, { kind: "study", study: "nope" }, tokens(), f);
    const b = await loadStudy(CFG, { kind: "dataset", dataset: "nope" }, tokens(), f);
    expect(a).toMatchObject({ kind: "loaded", value: { model: null } });
    expect(b).toMatchObject({ kind: "loaded", value: { model: null } });
  });

  it("shows the server's refusal sentence, and an ended session ends", async () => {
    const refused = vi.fn((url: string) =>
      Promise.resolve(
        url.endsWith("/catalog/studies")
          ? json(503, { error: "the catalog is slow to answer; try again" })
          : json(200, LINKS),
      ),
    );
    const r = await loadStudy(CFG, { kind: "study", study: "bmovie" }, tokens(), refused);
    expect(r).toMatchObject({
      kind: "error",
      message: "the catalog is slow to answer; try again",
      status503: true,
    });
    const none = await loadStudy(
      CFG,
      { kind: "study", study: "bmovie" },
      tokens({ getAccessToken: () => null }),
      serve(),
    );
    expect(none.kind).toBe("ended");
  });
});

describe("studyModel", () => {
  const body = parseStudies({
    studies: [
      {
        study: "st",
        title: "A study",
        datasets: [
          { dataset: "d-ephys", role: "ephys" },
          { dataset: "d-mri", role: "mri" },
        ],
      },
    ],
    datasets: [
      {
        dataset: "d-ephys",
        title: "Ephys",
        license: "CC-BY-4.0",
        source_archive: "dandi",
        source_version: "0.1.2",
        subjects: [
          {
            subject: "sub-a",
            modalities: ["task", "units", "lfp"],
            units: 7,
            trials: 41,
            lfp: [{ asset_id: "x", path: "p", channels: 16, rate_hz: 1000 }],
            sessions: [
              { session: "ses-1", asset_ids: ["n1"], nwb: [{ asset_id: "n1", n_units: 7 }] },
              { session: "ses-2", asset_ids: ["n2"], nwb: [{ asset_id: "n2", n_units: 0 }] },
              { session: "ses-3", asset_ids: ["n3"], nwb: [{ asset_id: "n3", n_units: null }] },
              { session: "ses-4", asset_ids: ["f4"], nwb: [] },
              { session: "ses-5", asset_ids: [], nwb: [] },
            ],
          },
        ],
        regions: [{ region: "Right amygdala", units: 7, channels: 16 }],
      },
      {
        dataset: "d-mri",
        license: "CC0",
        source_version: "1.0.5",
        subjects: [
          {
            subject: "sub-b",
            modalities: ["func", "anat", "other:fmap"],
            sessions: [{ session: "ses-mri", asset_ids: ["t1w"], nwb: [] }],
          },
        ],
      },
      { dataset: "d-lone", title: "Lone", subjects: [{ subject: "sub-z", modalities: [] }] },
    ],
    query_ms: 1,
  });
  const links = parseLinks({
    links: [
      {
        dataset: "d-ephys",
        subject: "sub-a",
        subject_key: "k",
        status: "confirmed",
        linked_by: "m@x.test",
        linked_at: "2026-10-20 12:00:00.000",
      },
      { dataset: "d-mri", subject: "sub-b", subject_key: "k", status: "pending" },
    ],
  }).links;

  const model = studyModel({ kind: "study", study: "st" }, body, links);

  it("links a session to the session page only with an NWB of units above zero", () => {
    const cells = model?.releases[0]?.rows[0]?.sessions ?? [];
    expect(cells.map((c) => [c.label, c.to, c.href])).toEqual([
      ["ses-1", "session", "/s/st/d-ephys/sub-a/ses-1"],
      ["ses-2", "file", "/files/n2"],
      ["ses-3", "file", "/files/n3"],
      ["ses-4", "file", "/files/f4"],
      ["ses-5", "none", null],
    ]);
    const mri = model?.releases[1]?.rows[0]?.sessions[0];
    expect(mri).toMatchObject({ to: "file", href: "/files/t1w" });
  });

  it("orders columns in the default modality order and draws counts, ticks and dashes", () => {
    const [e, m] = model?.releases ?? [];
    expect(e?.columns.map((c) => c.label)).toEqual(["Units", "LFP", "Task"]);
    expect(e?.rows[0]?.cells.map((c) => c.text)).toEqual(["7", "16", "41"]);
    expect(m?.columns.map((c) => c.label)).toEqual(["Anat MRI", "fMRI", "fmap"]);
    expect(m?.rows[0]?.cells.map((c) => c.text)).toEqual(["✓", "✓", "✓"]);
  });

  it("draws a link solid when confirmed and dashed otherwise, naming the other record", () => {
    const [e, m] = model?.releases ?? [];
    expect(e?.rows[0]?.link).toMatchObject({
      confirmed: true,
      linkedBy: "m@x.test",
      date: "2026-10-20",
      counterparts: ["sub-b in d-mri"],
    });
    expect(m?.rows[0]?.link).toMatchObject({
      confirmed: false,
      status: "pending",
      linkedBy: null,
      date: null,
    });
  });

  it("links the participant to the participant page: the subject key when linked, else dataset:subject", () => {
    expect(model?.releases[0]?.rows[0]?.href).toBe("/s/st/p/k");
    const one = studyModel({ kind: "dataset", dataset: "d-lone" }, body, links);
    expect(one?.releases[0]?.rows[0]?.href).toBe("/s/_/p/d-lone%3Asub-z");
  });

  it("reads readiness from what the route gives", () => {
    expect(model?.readiness).toMatchObject({
      trials: { total: 41, participants: 1 },
      units: { total: 7, regions: 1 },
      lfp: { channels: 16, series: 1, rates: [1000] },
      regions: [{ region: "Right amygdala", units: 7, channels: 16 }],
    });
    expect(model?.releases.map((r) => r.license)).toEqual(["CC-BY-4.0", "CC0"]);
    expect(model?.releases.map((r) => r.version)).toEqual(["0.1.2", "1.0.5"]);
  });

  it("one-dataset mode: the session path carries the placeholder study", () => {
    const one = studyModel({ kind: "dataset", dataset: "d-ephys" }, body, links);
    expect(one?.releases).toHaveLength(1);
    expect(one?.releases[0]?.rows[0]?.sessions[0]?.href).toBe("/s/_/d-ephys/sub-a/ses-1");
    expect(one?.partOf).toEqual({ study: "st", title: "A study" });
    expect(studyModel({ kind: "dataset", dataset: "d-lone" }, body, links)?.partOf).toBeNull();
    expect(studyModel({ kind: "dataset", dataset: "missing" }, body, links)).toBeNull();
  });
});

describe("the strip and the recording relation", () => {
  const body = parseStudies(F.studies);
  const links = parseLinks(F.links).links;
  const big = studyModel({ kind: "study", study: "synthetic-24-participants" }, body, links);
  const movie = studyModel({ kind: "study", study: "movie-watching" }, body, links);

  it("turns a release sideways above 20 participants and keeps the table at 20 or fewer", () => {
    expect(big?.releases.map((r) => [r.dataset, r.rows.length, r.strip])).toEqual([
      ["synthetic-24p-ephys", 24, true],
      ["synthetic-24p-mri", 1, false],
    ]);
    expect(movie?.releases.every((r) => !r.strip)).toBe(true);
  });

  it("counts participants, sessions and recording groups per release, from the body", () => {
    const ephys = big?.releases[0]?.relate;
    expect(ephys).toMatchObject({ participants: 24, sessions: 24, files: 24 });
    expect(ephys?.recordings).toBe(48);
    expect(ephys?.byModality.map((m) => [m.label, m.n])).toEqual([
      ["Units", 24],
      ["Task", 24],
    ]);
  });

  it("draws the 24-participant release as a strip: no table, one mark per participant", () => {
    const html = renderToStaticMarkup(createElement(StudyView, { model: big as StudyModel }));
    const ephys = html.slice(html.indexOf("release-synthetic-24p-ephys"));
    const ephysOnly = ephys.slice(0, ephys.indexOf("</section>"));
    expect(ephysOnly).not.toContain("<table");
    expect(ephysOnly.match(/data-ab="participant-row"/g)).toHaveLength(24);
    expect(ephysOnly).toContain('data-ab="strip"');
    const mri = html.slice(html.indexOf("release-synthetic-24p-mri"));
    expect(mri.slice(0, mri.indexOf("</section>"))).toContain("<table");
  });

  it("draws one pixel per participant at 608 participants", () => {
    expect(stripPx(24)).toBeGreaterThan(stripPx(150));
    expect(stripPx(608)).toBe(1);
  });

  it("shows how the records nest, with the files collapsed into recording groups", () => {
    const html = renderToStaticMarkup(createElement(StudyView, { model: big as StudyModel }));
    expect(html).toContain("How the recordings relate");
    expect(html).toContain("24 participants");
    expect(html).toContain("48 recording groups");
  });
});

describe("the Source files drawer's data", () => {
  const rows = (n: number, from = 0) =>
    Array.from({ length: n }, (_, i) => {
      const k = from + i;
      return {
        asset_id: `a${k}`,
        path: `sub-0${k % 2}/ses-1/f${k}.nii.gz`,
        subject: `sub-0${k % 2}`,
        bids_session: "ses-1",
        datatype: k % 4 < 2 ? "func" : "anat",
        format: "nii.gz",
        size_bytes: 100,
        committed_at: null,
      };
    });

  it("groups files by participant, session and datatype, summing sizes", () => {
    const groups = recordingGroups(rows(8));
    expect(groups.map((g) => [g.subject, g.session, g.datatype, g.files.length, g.bytes])).toEqual([
      ["sub-00", "ses-1", "anat", 2, 200],
      ["sub-00", "ses-1", "func", 2, 200],
      ["sub-01", "ses-1", "anat", 2, 200],
      ["sub-01", "ses-1", "func", 2, 200],
    ]);
  });

  it("collapses a file with no session, participant or datatype into a named group", () => {
    const [g] = recordingGroups([
      {
        asset_id: "x",
        path: "dataset_description.json",
        subject: null,
        bids_session: null,
        datatype: null,
        format: "json",
        size_bytes: null,
        committed_at: null,
      },
    ]);
    expect(g).toMatchObject({
      subject: "(no participant)",
      session: "(no session)",
      datatype: "other",
      bytes: null,
    });
  });

  it("asks the existing assets route one page at a time", async () => {
    const f = vi.fn((url: string) => {
      const page = Number(new URL(url).searchParams.get("page") ?? "0");
      return Promise.resolve(
        json(200, { assets: rows(2, page * 2), page, more: page < 1, query_ms: 3 }),
      );
    });
    const a = await loadSourcePage(CFG, "ds 1", 0, tokens(), f);
    const b = await loadSourcePage(CFG, "ds 1", 1, tokens(), f);
    expect(f.mock.calls.map((c) => c[0])).toEqual([
      "https://api.example.test/catalog/datasets/ds%201/assets",
      "https://api.example.test/catalog/datasets/ds%201/assets?page=1",
    ]);
    expect(a).toMatchObject({ kind: "loaded", value: { more: true } });
    expect(b).toMatchObject({ kind: "loaded", value: { more: false } });
  });
});
