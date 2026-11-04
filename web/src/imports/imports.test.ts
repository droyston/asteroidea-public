import { describe, expect, it, vi } from "vitest";
import { isCurator, tokenGroups } from "../auth/groups";
import type { Target } from "../targets/schema";
import { json, tokens } from "../testApi";
import {
  exactGlob,
  failureSentence,
  fileView,
  isTerminal,
  landingFor,
  loadRun,
  parseGlobs,
  parseRun,
  parseRuns,
  parseStudyDatasets,
  pickedImports,
  runProgress,
  sourceProblem,
  squareState,
  startImport,
} from "./imports";

function jwt(claims: unknown): string {
  const b64 = (v: unknown) =>
    btoa(JSON.stringify(v)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  return `${b64({ alg: "none" })}.${b64(claims)}.sig`;
}

describe("token groups", () => {
  it("reads cognito:groups from the payload", () => {
    const t = jwt({ sub: "u", "cognito:groups": ["researchers", "curators"] });
    expect(tokenGroups(t)).toEqual(["researchers", "curators"]);
    expect(isCurator(t)).toBe(true);
  });

  it("is not a curator without the group, with a lookalike, or without a usable token", () => {
    expect(isCurator(jwt({ "cognito:groups": ["researchers"] }))).toBe(false);
    expect(isCurator(jwt({ "cognito:groups": ["curators-x"] }))).toBe(false);
    expect(isCurator(jwt({ sub: "u" }))).toBe(false);
    expect(isCurator(null)).toBe(false);
    expect(isCurator("access-1")).toBe(false);
    expect(isCurator("a.!!!.c")).toBe(false);
    expect(isCurator(`a.${btoa("[1]")}.c`)).toBe(false);
  });

  it("accepts a single group given as a string, and unicode payloads", () => {
    expect(tokenGroups(jwt({ "cognito:groups": "curators" }))).toEqual(["curators"]);
    expect(tokenGroups(jwt({ name: "Zoë", "cognito:groups": ["curators"] }))).toEqual(["curators"]);
  });
});

describe("source and globs", () => {
  it("accepts dandi and openneuro sources and names the form otherwise", () => {
    expect(sourceProblem("dandi:000623@0.250801.1445")).toBeNull();
    expect(sourceProblem("openneuro:ds000001@1.0.0")).toBeNull();
    for (const bad of [
      "",
      "dandi:000623",
      "zenodo:1@2",
      "dandi:@1",
      "dandi:1@",
      " dandi:1@2",
      "dandi:1 2@3",
    ]) {
      expect(sourceProblem(bad), bad).toContain("dandi:<id>@<version>");
    }
  });

  it("splits globs one per line, trims, drops blanks, and caps at 20", () => {
    expect(parseGlobs(" a/*.nwb \n\n b \n")).toEqual({ ok: true, globs: ["a/*.nwb", "b"] });
    expect(parseGlobs("")).toEqual({ ok: true, globs: [] });
    expect(parseGlobs(Array.from({ length: 20 }, (_, i) => `g${i}`).join("\n")).ok).toBe(true);
    const many = parseGlobs(Array.from({ length: 21 }, (_, i) => `g${i}`).join("\n"));
    expect(many).toEqual({ ok: false, message: "include holds at most 20 globs (21 given)" });
    expect(parseGlobs("x".repeat(257)).ok).toBe(false);
  });
});

describe("run parsing", () => {
  it("parses the list in the API shape", () => {
    const runs = parseRuns({
      runs: [
        {
          run_id: "r1",
          source: "dandi:1@2",
          include: ["a"],
          state: "REFUSED",
          created_at: 5,
          refusal: "too many",
        },
      ],
    });
    expect(runs[0]).toMatchObject({ run_id: "r1", refusal: "too many", include: ["a"] });
    expect(() => parseRuns({})).toThrow(/runs/);
  });

  it("parses a run and labels each file", () => {
    const run = parseRun({
      run_id: "r1",
      source: "dandi:1@2",
      state: "RUNNING",
      dataset: "dandi-1",
      files: [
        { relpath: "a", outcome: "PASS" },
        { relpath: "b", outcome: "FAIL extraction bad header" },
        { relpath: "c", outcome: null },
        { relpath: "d", outcome: "PASS", already_imported: true },
      ],
    });
    expect(run.files.map(fileView)).toEqual([
      { label: "PASS", reason: "" },
      { label: "FAIL", reason: "extraction bad header" },
      { label: "moving", reason: "" },
      { label: "already imported", reason: "" },
    ]);
    expect(() => parseRun({ run_id: "r", source: "s", state: "x" })).toThrow(/files/);
  });

  it("stops polling at DONE, REFUSED and FAILED only", () => {
    expect(["PLANNING", "RUNNING"].some(isTerminal)).toBe(false);
    expect(["DONE", "REFUSED", "FAILED"].every(isTerminal)).toBe(true);
  });
});

describe("calls", () => {
  const CFG = { apiUrl: "https://api.example.test" };
  const fetchOf = (res: Response) =>
    vi.fn<(url: string, init: RequestInit) => Promise<Response>>().mockResolvedValue(res);

  it("POSTs the body and returns the run id from a 202", async () => {
    const f = fetchOf(json(202, { run_id: "abc" }));
    const r = await startImport(CFG, { source: "dandi:1@2", include: ["x"] }, tokens(), f);
    expect(r).toEqual({ kind: "started", run_id: "abc" });
    const [url, init] = f.mock.calls[0] ?? [];
    expect(url).toBe("https://api.example.test/imports");
    expect(init?.method).toBe("POST");
    expect(JSON.parse(String(init?.body))).toEqual({ source: "dandi:1@2", include: ["x"] });
  });

  it("shows the server's text on a 403 and 400", async () => {
    const denied = await startImport(
      CFG,
      { source: "dandi:1@2", include: [] },
      tokens(),
      fetchOf(json(403, { error: "caller is not in the curators group" })),
    );
    expect(denied).toEqual({
      kind: "error",
      message: "caller is not in the curators group",
    });
    const bad = await loadRun(CFG, "r/1", tokens(), fetchOf(json(404, { error: "no such run" })));
    expect(bad).toEqual({ kind: "error", message: "no such run" });
  });
});

describe("pickedImports", () => {
  const t = (dataset_id: string, path: string | null, version = "1.0"): Target => ({
    source: "dandi",
    dataset_id,
    version,
    path,
    note: "",
  });

  // Files picked under a dataset are the subset, even when the dataset's own entry is there
  // (dataset 3); only a dataset with no files picked imports whole.
  it("makes one import per dataset, files as globs, a dataset with no files as no globs", () => {
    const r = pickedImports([
      t("1", "a.nwb"),
      t("2", null),
      t("1", "b.nwb"),
      t("3", "c"),
      t("3", null),
    ]);
    expect(r.problem).toBeNull();
    expect(r.requests).toEqual([
      { source: "dandi:1@1.0", include: ["a.nwb", "b.nwb"] },
      { source: "dandi:2@1.0", include: [] },
      { source: "dandi:3@1.0", include: ["c"] },
    ]);
  });

  it("reports more than 20 picked files of one dataset instead of sending", () => {
    const many = Array.from({ length: 21 }, (_, i) => t("1", `f${i}.nwb`));
    const r = pickedImports([...many, t("2", "x")]);
    expect(r.problem).toContain("at most 20");
    expect(r.requests).toEqual([{ source: "dandi:2@1.0", include: ["x"] }]);
  });

  it("stops a run over 50 files with a sentence: picked files, or a whole dataset the index counts", () => {
    const many = Array.from({ length: 51 }, (_, i) => t("1", `f${i}.nwb`));
    expect(pickedImports(many).problem).toBe(
      "DANDI 1: 51 files are picked, and one import takes at most 50. Unpick 1.",
    );
    const counts = (g: { dataset: { dataset_id: string } }) =>
      g.dataset.dataset_id === "2" ? 812 : 12;
    const r = pickedImports([t("2", null), t("4", null)], counts);
    expect(r.problem).toBe(
      "DANDI 2 has 812 files, and one import takes at most 50. Open it in the picks and pick the files to import.",
    );
    expect(r.requests).toEqual([{ source: "dandi:4@1.0", include: [] }]);
  });

  it("names a picked path so it matches only itself", () => {
    expect(exactGlob("sub-1/a[1]*?.nwb")).toBe("sub-1/a[[]1][*][?].nwb");
    expect(pickedImports([t("1", "x[2].nwb")]).requests[0]?.include).toEqual(["x[[]2].nwb"]);
  });
});

describe("the run in the rail", () => {
  const file = (relpath: string, outcome: string | null, already = false) => ({
    relpath,
    outcome,
    already_imported: already,
  });
  const run = {
    run_id: "r",
    source: "dandi:1@1.0",
    state: "RUNNING",
    files: [
      file("a.nwb", "PASS"),
      file("b.nwb", null),
      file("c.nwb", "FAIL extraction bad header"),
      file("d.nwb", "PASS", true),
    ],
  };

  it("counts done files and gives each square a state", () => {
    expect(runProgress(run)).toEqual({ done: 3, total: 4 });
    expect(run.files.map(squareState)).toEqual(["imported", "waiting", "failed", "already"]);
  });

  it("writes one plain sentence per failed file", () => {
    expect(run.files.map(failureSentence)).toEqual([
      null,
      null,
      "c.nwb failed: Extraction bad header.",
      null,
    ]);
    expect(failureSentence(file("e.nwb", "FAIL"))).toBe("e.nwb failed; the import gave no reason.");
  });

  it("lands on the study that lists the dataset, else the dataset page", () => {
    const studies = parseStudyDatasets({
      studies: [{ study: "aversive", title: "Aversive stimuli", datasets: [{ dataset: "d1" }] }],
    });
    expect(landingFor("d1", studies)).toEqual({ href: "/s/aversive", title: "Aversive stimuli" });
    expect(landingFor("d2", studies)).toEqual({ href: "/d/d2", title: null });
  });
});
