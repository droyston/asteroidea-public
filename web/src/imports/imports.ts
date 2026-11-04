// Web-started imports: POST /imports, GET /imports, GET /imports/{run_id}.
// Shapes follow ingest/src/asteroidea_ingest/imports_api.py.
import {
  apiGet,
  apiSend,
  type FetchFn,
  serverError,
  TIMEOUT_MS,
  type TokenSource,
} from "../auth/apiGet";
import type { AuthConfig } from "../auth/config";
import { datasetViewPath } from "../router/dataPaths";
import { groupPicks, type PickGroup } from "../targets/groups";
import type { Target } from "../targets/schema";

export const MAX_GLOBS = 20;
export const MAX_GLOB_CHARS = 256;
export const POLL_MS = 5000;
export const SOURCE_RE =
  /^(?:dandi|openneuro):[A-Za-z0-9][A-Za-z0-9._-]{0,63}@[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;

export const SOURCE_HINT = "source must be dandi:<id>@<version> or openneuro:<id>@<version>";

/** Null when `source` is acceptable, else the problem in words. */
export function sourceProblem(source: string): string | null {
  return SOURCE_RE.test(source) ? null : SOURCE_HINT;
}

export type GlobsResult = { ok: true; globs: string[] } | { ok: false; message: string };

/** One glob per line; blank lines dropped, lines trimmed. */
export function parseGlobs(text: string): GlobsResult {
  const globs = text
    .split("\n")
    .map((l) => l.trim())
    .filter((l) => l !== "");
  if (globs.length > MAX_GLOBS) {
    return {
      ok: false,
      message: `include holds at most ${MAX_GLOBS} globs (${globs.length} given)`,
    };
  }
  if (globs.some((g) => g.length > MAX_GLOB_CHARS)) {
    return { ok: false, message: `each glob must be 1 to ${MAX_GLOB_CHARS} characters` };
  }
  return { ok: true, globs };
}

const TERMINAL: readonly string[] = ["DONE", "REFUSED", "FAILED"];

/** DONE, REFUSED and FAILED are final: the run page stops polling. */
export function isTerminal(state: string): boolean {
  return TERMINAL.includes(state);
}

export interface RunRow {
  run_id: string;
  source: string;
  include: readonly string[];
  state: string;
  created_at: number;
  dataset?: string;
  refusal?: string;
}

export interface RunFile {
  relpath: string;
  /** "PASS", "FAIL <why>", or null while the file is moving. */
  outcome: string | null;
  already_imported: boolean;
}

export interface Run {
  run_id: string;
  source: string;
  state: string;
  dataset?: string;
  refusal?: string;
  files: readonly RunFile[];
}

function rec(v: unknown, what: string): Record<string, unknown> {
  if (typeof v !== "object" || v === null || Array.isArray(v)) {
    throw new Error(`${what} is not an object`);
  }
  return v as Record<string, unknown>;
}

function str(o: Record<string, unknown>, key: string, what: string): string {
  const v = o[key];
  if (typeof v !== "string") throw new Error(`${what}.${key} is not a string`);
  return v;
}

function optStr(o: Record<string, unknown>, key: "dataset" | "refusal"): Partial<Run> {
  const v = o[key];
  return typeof v === "string" ? { [key]: v } : {};
}

export function parseRuns(body: unknown): readonly RunRow[] {
  const runs = rec(body, "the response").runs;
  if (!Array.isArray(runs)) throw new Error('the response has no "runs" list');
  return runs.map((raw: unknown, i): RunRow => {
    const at = `runs[${i}]`;
    const r = rec(raw, at);
    const include = Array.isArray(r.include)
      ? r.include.filter((g): g is string => typeof g === "string")
      : [];
    return {
      run_id: str(r, "run_id", at),
      source: str(r, "source", at),
      include,
      state: str(r, "state", at),
      created_at: typeof r.created_at === "number" ? r.created_at : 0,
      ...optStr(r, "dataset"),
      ...optStr(r, "refusal"),
    };
  });
}

export function parseRun(body: unknown): Run {
  const r = rec(body, "the response");
  if (!Array.isArray(r.files)) throw new Error('the response has no "files" list');
  const files = r.files.map((raw: unknown, i): RunFile => {
    const at = `files[${i}]`;
    const f = rec(raw, at);
    const outcome = f.outcome ?? null;
    if (outcome !== null && typeof outcome !== "string") {
      throw new Error(`${at}.outcome is not a string or null`);
    }
    return {
      relpath: str(f, "relpath", at),
      outcome,
      already_imported: f.already_imported === true,
    };
  });
  return {
    run_id: str(r, "run_id", "the response"),
    source: str(r, "source", "the response"),
    state: str(r, "state", "the response"),
    ...optStr(r, "dataset"),
    ...optStr(r, "refusal"),
    files,
  };
}

export type FileLabel = "PASS" | "FAIL" | "moving" | "already imported";

/** What one file row shows: the label, and the reason after FAIL. */
export function fileView(f: RunFile): { label: FileLabel; reason: string } {
  if (f.already_imported) return { label: "already imported", reason: "" };
  if (f.outcome === null) return { label: "moving", reason: "" };
  if (f.outcome === "PASS") return { label: "PASS", reason: "" };
  return { label: "FAIL", reason: f.outcome.replace(/^FAIL\s*/, "") };
}

const GROUP_HINT = "Your account is not in the curators group. Ask your administrator to add you.";

async function refusal(response: Response): Promise<string | null> {
  return (await serverError(response)) ?? (response.status === 403 ? GROUP_HINT : null);
}

export type RunsResult =
  | { kind: "loaded"; runs: readonly RunRow[] }
  | { kind: "error"; message: string }
  | { kind: "ended" };

export async function loadRuns(
  config: Pick<AuthConfig, "apiUrl">,
  tokens: TokenSource,
  fetchFn: FetchFn,
  timeoutMs: number = TIMEOUT_MS,
): Promise<RunsResult> {
  const r = await apiGet(config.apiUrl, "/imports", tokens, fetchFn, timeoutMs, {
    parse: parseRuns,
    refusal,
  });
  return r.kind === "loaded" ? { kind: "loaded", runs: r.value } : r;
}

export type RunResult =
  | { kind: "loaded"; run: Run }
  | { kind: "error"; message: string }
  | { kind: "ended" };

export async function loadRun(
  config: Pick<AuthConfig, "apiUrl">,
  runId: string,
  tokens: TokenSource,
  fetchFn: FetchFn,
  timeoutMs: number = TIMEOUT_MS,
): Promise<RunResult> {
  const r = await apiGet(
    config.apiUrl,
    `/imports/${encodeURIComponent(runId)}`,
    tokens,
    fetchFn,
    timeoutMs,
    { parse: parseRun, refusal },
  );
  return r.kind === "loaded" ? { kind: "loaded", run: r.value } : r;
}

export interface ImportRequest {
  source: string;
  include: readonly string[];
}

export type StartResult =
  | { kind: "started"; run_id: string }
  | { kind: "error"; message: string }
  | { kind: "ended" };

/** POST /imports: 202 with the run id. Never throws. */
export async function startImport(
  config: Pick<AuthConfig, "apiUrl">,
  request: ImportRequest,
  tokens: TokenSource,
  fetchFn: FetchFn,
  timeoutMs: number = TIMEOUT_MS,
): Promise<StartResult> {
  const r = await apiSend("POST", config.apiUrl, "/imports", request, tokens, fetchFn, timeoutMs, {
    parse: (body) => {
      const id = rec(body, "the response").run_id;
      if (typeof id !== "string" || id === "") throw new Error('the response has no "run_id"');
      return id;
    },
    refusal,
  });
  if (r.kind === "loaded") return { kind: "started", run_id: r.value };
  return r.kind === "error" ? { kind: "error", message: r.message } : r;
}

/** The server's cap on one run (`import_plan.py :: MAX_RUN_FILES`): a run that selects more is refused. */
export const MAX_RUN_FILES = 50;

/** A path as an fnmatch glob that matches only itself: `*`, `?` and `[` go inside brackets. */
export function exactGlob(path: string): string {
  return path.replace(/[*?[]/g, (c) => `[${c}]`);
}

export interface PickedImports {
  requests: ImportRequest[];
  /** Why the picks cannot go as imports: a bad source, a run over 50 files, or over 20 named files. */
  problem: string | null;
}

/**
 * One import per picked dataset: the files picked under a dataset are its include globs,
 * each matching only that path; a dataset with no files picked has none, which imports all of it.
 * `fileCount` gives a dataset's file count from the index (null when unknown), so a whole dataset
 * over the 50-file cap is stopped here with a sentence instead of being refused by the server.
 */
export function pickedImports(
  targets: readonly Target[],
  fileCount: (g: PickGroup) => number | null = () => null,
): PickedImports {
  const requests: ImportRequest[] = [];
  let problem: string | null = null;
  for (const g of groupPicks(targets)) {
    const d = g.dataset;
    const source = `${d.source}:${d.dataset_id}@${d.version}`;
    const name = `${d.source === "dandi" ? "DANDI" : "OpenNeuro"} ${d.dataset_id}`;
    const include = g.files.map((f) => exactGlob(f.target.path ?? ""));
    const whole = fileCount(g);
    let bad = sourceProblem(source);
    if (bad === null && include.length > MAX_RUN_FILES) {
      bad = `${name}: ${include.length} files are picked, and one import takes at most ${MAX_RUN_FILES}. Unpick ${include.length - MAX_RUN_FILES}.`;
    } else if (bad === null && include.length > MAX_GLOBS) {
      bad = `${name}: ${include.length} files are picked, and one import names at most ${MAX_GLOBS} files. Unpick ${include.length - MAX_GLOBS}.`;
    } else if (bad === null && include.length === 0 && whole !== null && whole > MAX_RUN_FILES) {
      bad = `${name} has ${whole.toLocaleString("en-US")} files, and one import takes at most ${MAX_RUN_FILES}. Open it in the picks and pick the files to import.`;
    }
    if (bad === null) requests.push({ source, include });
    else problem ??= bad;
  }
  return { requests, problem };
}

/** "47 of 50 files done", counting a file as done once it has an outcome or was already here. */
export function runProgress(run: Run): { done: number; total: number } {
  const done = run.files.filter((f) => f.already_imported || f.outcome !== null).length;
  return { done, total: run.files.length };
}

export type SquareState = "imported" | "already" | "failed" | "waiting";

export const SQUARE_LABEL: Record<SquareState, string> = {
  imported: "imported",
  already: "already here",
  failed: "failed",
  waiting: "waiting",
};

/** A run file's square: the state its colour and its text label show. */
export function squareState(f: RunFile): SquareState {
  const v = fileView(f);
  if (v.label === "PASS") return "imported";
  if (v.label === "already imported") return "already";
  if (v.label === "FAIL") return "failed";
  return "waiting";
}

/** A failed file as one plain sentence: the path, then the reason the import gave. */
export function failureSentence(f: RunFile): string | null {
  const v = fileView(f);
  if (v.label !== "FAIL") return null;
  const why = v.reason.trim();
  if (why === "") return `${f.relpath} failed; the import gave no reason.`;
  const text = `${why.charAt(0).toUpperCase()}${why.slice(1)}`;
  return `${f.relpath} failed: ${/[.!?]$/.test(text) ? text : `${text}.`}`;
}

/** Where "Open the new study" goes: the study that lists the run's dataset, else the dataset page. */
export function landingFor(
  dataset: string,
  studies: readonly { study: string; title: string; datasets: readonly string[] }[],
): { href: string; title: string | null } {
  const study = studies.find((s) => s.datasets.includes(dataset));
  return study === undefined
    ? { href: datasetViewPath(dataset), title: null }
    : { href: `/s/${encodeURIComponent(study.study)}`, title: study.title };
}

/** The studies a landing link needs, from `GET /catalog/studies` (other fields are ignored). */
export function parseStudyDatasets(
  body: unknown,
): { study: string; title: string; datasets: string[] }[] {
  const studies = rec(body, "the response").studies;
  if (!Array.isArray(studies)) throw new Error('the response has no "studies" list');
  return studies.map((raw: unknown, i) => {
    const at = `studies[${i}]`;
    const s = rec(raw, at);
    const datasets = Array.isArray(s.datasets) ? s.datasets : [];
    return {
      study: str(s, "study", at),
      title: typeof s.title === "string" ? s.title : str(s, "study", at),
      datasets: datasets.flatMap((d: unknown) => {
        if (typeof d === "string") return [d];
        if (typeof d === "object" && d !== null && typeof (d as Rec).dataset === "string") {
          return [(d as Rec).dataset as string];
        }
        return [];
      }),
    };
  });
}

type Rec = Record<string, unknown>;
