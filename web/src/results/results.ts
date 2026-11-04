// Results: GET /artifacts (scope mine or all, the results
// that used one input, or the results of one run), GET /artifacts/{id} (the record and a presigned link:
// inline, or `?download=1` for an attachment), PATCH /artifacts/{id} and POST /artifacts/{id}/withdraw.
// Shapes follow ingest/.../artifacts.py. Every call goes through apiGet / apiSend; the bucket needs no
// CORS because a download is a top-level navigation and a preview is an <img> or a sandboxed <iframe>
// of the presigned URL, never a fetch. The browser draws what the API returns.
import {
  type ApiGetResult,
  apiGet,
  apiSend,
  type FetchFn,
  type Refusal,
  serverError,
  serverRefusal,
  TIMEOUT_MS,
  type TokenSource,
} from "../auth/apiGet";
import type { AuthConfig } from "../auth/config";
import { type Artifact, items, optStr, parseArtifact, parseArtifacts, rec, str } from "./rows";

export { type Artifact, parseArtifact, parseArtifacts } from "./rows";

type Cfg = Pick<AuthConfig, "apiUrl">;

export const COMMITTED = "COMMITTED";
export const WITHDRAWN = "WITHDRAWN";
export const NOT_AUTHOR = "not_author";

export type Scope = "mine" | "all";

export interface Source {
  asset_id: string;
  s3_version_id: string;
}

export interface Input {
  kind: string;
  id: string;
  version: string | number | null;
  role: string | null;
}

export interface Run {
  run_id: string | null;
  function: string;
  parameters: unknown;
  defaults_version: string | null;
  sdk_version: string | null;
}

export interface ArtifactRecord extends Artifact {
  author_sub: string;
  note: string | null;
  sources: readonly Source[];
  inputs: readonly Input[];
  /** Null for a version 1 record, which has no run. */
  run: Run | null;
  sdk_version: string | null;
}

function parseRun(raw: unknown): Run | null {
  if (typeof raw !== "object" || raw === null) return null;
  const o = raw as Record<string, unknown>;
  return {
    run_id: optStr(o, "run_id"),
    function: str(o, "function", "run"),
    parameters: o.parameters ?? {},
    defaults_version: optStr(o, "defaults_version"),
    sdk_version: optStr(o, "sdk_version"),
  };
}

export function parseRecord(body: unknown): { record: ArtifactRecord; url: string } {
  const b = rec(body, "the response");
  const o = rec(b.record, "record");
  const url = b.url;
  if (typeof url !== "string" || !/^https:\/\//.test(url)) throw new Error('"url" is not https');
  const run = parseRun(o.run);
  const record: ArtifactRecord = {
    ...parseArtifact(o, "record"),
    // A record carries its run id inside `run`; rows carry it flat.
    run_id: run?.run_id ?? null,
    run_function: run?.function ?? null,
    author_sub: str(o, "author_sub", "record"),
    note: optStr(o, "note"),
    sources: items(o, "sources").map((s, i) => {
      const at = `sources[${i}]`;
      const r = rec(s, at);
      return { asset_id: str(r, "asset_id", at), s3_version_id: str(r, "s3_version_id", at) };
    }),
    inputs: items(o, "inputs").map((s, i): Input => {
      const at = `inputs[${i}]`;
      const r = rec(s, at);
      const version = r.version;
      return {
        kind: str(r, "kind", at),
        id: str(r, "id", at),
        version: typeof version === "string" || typeof version === "number" ? version : null,
        role: optStr(r, "role"),
      };
    }),
    run,
    sdk_version: optStr(o, "sdk_version"),
  };
  return { record, url };
}

export type ListResult =
  | { kind: "loaded"; artifacts: readonly Artifact[] }
  | { kind: "error"; message: string }
  | { kind: "ended" };

const FORBIDDEN =
  "Your account is not in the researchers group, so it cannot list results. Ask your administrator to add you.";

async function getList(
  config: Cfg,
  path: string,
  tokens: TokenSource,
  fetchFn: FetchFn,
  timeoutMs: number,
): Promise<ListResult> {
  const result: ApiGetResult<readonly Artifact[]> = await apiGet(
    config.apiUrl,
    path,
    tokens,
    fetchFn,
    timeoutMs,
    {
      parse: parseArtifacts,
      refusal: async (r) => (r.status === 403 ? FORBIDDEN : serverError(r)),
    },
  );
  return result.kind === "loaded" ? { kind: "loaded", artifacts: result.value } : result;
}

/** The results of `scope`, newest first (the server sorts and caps at 100). Never throws. */
export function loadArtifacts(
  config: Cfg,
  scope: Scope,
  tokens: TokenSource,
  fetchFn: FetchFn,
  timeoutMs: number = TIMEOUT_MS,
): Promise<ListResult> {
  return getList(config, `/artifacts?scope=${scope}`, tokens, fetchFn, timeoutMs);
}

/** Every researcher's readable results that used `inputId` (an asset or a result), one level. */
export function loadUsedBy(
  config: Cfg,
  inputId: string,
  tokens: TokenSource,
  fetchFn: FetchFn,
  timeoutMs: number = TIMEOUT_MS,
): Promise<ListResult> {
  const path = `/artifacts?scope=all&input=${encodeURIComponent(inputId)}`;
  return getList(config, path, tokens, fetchFn, timeoutMs);
}

/** One file of a run: a listing row and its presigned inline link (15 minutes). */
export interface RunFile extends Artifact {
  /** Null when the row carries no https link. */
  url: string | null;
}

export function parseRunFiles(body: unknown): readonly RunFile[] {
  const rows = rec(body, "the response").artifacts;
  if (!Array.isArray(rows)) throw new Error('the response has no "artifacts" list');
  return rows.map((row: unknown, i): RunFile => {
    const url = optStr(rec(row, `artifacts[${i}]`), "url");
    return {
      ...parseArtifact(row, `artifacts[${i}]`),
      url: url !== null && /^https:\/\//.test(url) ? url : null,
    };
  });
}

export type RunResult =
  | { kind: "loaded"; files: readonly RunFile[] }
  | { kind: "error"; message: string }
  | { kind: "ended" };

/**
 * Every researcher's finished results saved by one run (`GET /artifacts?scope=all&run=`), each with
 * its own inline link, in one request. A failure names the HTTP status. Never throws.
 */
export async function loadRun(
  config: Cfg,
  runId: string,
  tokens: TokenSource,
  fetchFn: FetchFn,
  timeoutMs: number = TIMEOUT_MS,
): Promise<RunResult> {
  const path = `/artifacts?scope=all&run=${encodeURIComponent(runId)}`;
  const result = await apiGet(config.apiUrl, path, tokens, fetchFn, timeoutMs, {
    parse: parseRunFiles,
    refusal: async (r) => {
      const text = await serverError(r);
      return `The platform API answered HTTP ${r.status}${text === null ? "" : `: ${text}`}.`;
    },
  });
  return result.kind === "loaded" ? { kind: "loaded", files: result.value } : result;
}

export type RecordResult =
  | { kind: "loaded"; record: ArtifactRecord; url: string }
  | { kind: "error"; message: string }
  | { kind: "ended" };

/**
 * One result's record and a presigned link (valid 15 minutes) that serves it inline, typed: an
 * image or page shows in place. Never throws.
 */
export function loadArtifact(
  config: Cfg,
  artifactId: string,
  tokens: TokenSource,
  fetchFn: FetchFn,
  timeoutMs: number = TIMEOUT_MS,
): Promise<RecordResult> {
  return getRecord(
    config,
    `/artifacts/${encodeURIComponent(artifactId)}`,
    tokens,
    fetchFn,
    timeoutMs,
  );
}

/** As `loadArtifact`, with a link that saves the file (`?download=1`, an attachment). */
export function loadDownload(
  config: Cfg,
  artifactId: string,
  tokens: TokenSource,
  fetchFn: FetchFn,
  timeoutMs: number = TIMEOUT_MS,
): Promise<RecordResult> {
  const path = `/artifacts/${encodeURIComponent(artifactId)}?download=1`;
  return getRecord(config, path, tokens, fetchFn, timeoutMs);
}

async function getRecord(
  config: Cfg,
  path: string,
  tokens: TokenSource,
  fetchFn: FetchFn,
  timeoutMs: number,
): Promise<RecordResult> {
  const result = await apiGet(config.apiUrl, path, tokens, fetchFn, timeoutMs, {
    parse: parseRecord,
    refusal: (r) =>
      r.status === 404
        ? Promise.resolve("This result was not found, or it is not finished yet.")
        : serverError(r),
  });
  return result.kind === "loaded" ? { kind: "loaded", ...result.value } : result;
}

export type ChangeResult =
  | { kind: "done"; artifact: Artifact | null }
  | { kind: "error"; message: string; code: string | null }
  | { kind: "ended" };

async function change(
  method: "POST" | "PATCH" | "DELETE",
  config: Cfg,
  path: string,
  body: unknown,
  tokens: TokenSource,
  fetchFn: FetchFn,
  timeoutMs: number,
): Promise<ChangeResult> {
  let refusal: Refusal = { code: null, text: null };
  const result = await apiSend(method, config.apiUrl, path, body, tokens, fetchFn, timeoutMs, {
    parse: (b) =>
      typeof b === "object" && b !== null && "artifact" in b ? parseArtifact(b.artifact) : null,
    refusal: async (r) => {
      refusal = await serverRefusal(r);
      return refusal.text;
    },
  });
  if (result.kind === "loaded") return { kind: "done", artifact: result.value };
  if (result.kind === "ended") return result;
  return { kind: "error", message: result.message, code: refusal.code };
}

/** PATCH note and name (author only; 403 `not_author` for anyone else). */
export function editArtifact(
  config: Cfg,
  artifactId: string,
  fields: { name: string; note: string | null },
  tokens: TokenSource,
  fetchFn: FetchFn,
  timeoutMs: number = TIMEOUT_MS,
): Promise<ChangeResult> {
  const path = `/artifacts/${encodeURIComponent(artifactId)}`;
  return change("PATCH", config, path, fields, tokens, fetchFn, timeoutMs);
}

/** POST withdraw (author only); the bytes stay and the state becomes WITHDRAWN. */
export function withdrawArtifact(
  config: Cfg,
  artifactId: string,
  tokens: TokenSource,
  fetchFn: FetchFn,
  timeoutMs: number = TIMEOUT_MS,
): Promise<ChangeResult> {
  const path = `/artifacts/${encodeURIComponent(artifactId)}/withdraw`;
  return change("POST", config, path, undefined, tokens, fetchFn, timeoutMs);
}

/** DELETE (author only); refused 409 `has_dependents` while another result used it. */
export function deleteArtifact(
  config: Cfg,
  artifactId: string,
  tokens: TokenSource,
  fetchFn: FetchFn,
  timeoutMs: number = TIMEOUT_MS,
): Promise<ChangeResult> {
  const path = `/artifacts/${encodeURIComponent(artifactId)}`;
  return change("DELETE", config, path, undefined, tokens, fetchFn, timeoutMs);
}

/** The `GET /catalog/studies` body, unparsed, for the results tree's study titles. Never throws. */
export function loadStudiesBody(
  config: Cfg,
  tokens: TokenSource,
  fetchFn: FetchFn,
  timeoutMs: number = TIMEOUT_MS,
): Promise<ApiGetResult<unknown>> {
  return apiGet(config.apiUrl, "/catalog/studies", tokens, fetchFn, timeoutMs, {
    parse: (b) => b,
    refusal: serverError,
  });
}

/** The `sub` claim of an access token, for display only (the API decides who is the author). */
export function tokenSub(token: string | null): string | null {
  const payload = token?.split(".")[1];
  if (payload === undefined || payload === "") return null;
  try {
    const bytes = Uint8Array.from(atob(payload.replace(/-/g, "+").replace(/_/g, "/")), (c) =>
      c.charCodeAt(0),
    );
    const sub: unknown = (JSON.parse(new TextDecoder().decode(bytes)) as { sub?: unknown }).sub;
    return typeof sub === "string" && sub !== "" ? sub : null;
  } catch {
    return null;
  }
}

/** One input of a result as a display row. */
export interface InputRow {
  kind: string;
  id: string;
  version: string | null;
  role: string | null;
  /** In-app path for the input. */
  href: string | null;
}

const KIND_LABEL: Record<string, string> = { table_snapshot: "table", artifact: "result" };

/** The inputs of a record as display rows: files first, then each declared input. */
export function inputRows(record: ArtifactRecord): readonly InputRow[] {
  const files = record.sources.map(
    (s): InputRow => ({
      kind: "file",
      id: s.asset_id,
      version: s.s3_version_id,
      role: null,
      href: `/files/${encodeURIComponent(s.asset_id)}`,
    }),
  );
  const declared = record.inputs.map((i): InputRow => {
    const id = encodeURIComponent(i.id);
    return {
      kind: KIND_LABEL[i.kind] ?? i.kind,
      id: i.id,
      version: i.version === null ? null : String(i.version),
      role: i.role,
      href: i.kind === "asset" ? `/files/${id}` : i.kind === "artifact" ? `/results/${id}` : null,
    };
  });
  return [...files, ...declared];
}

/** A byte count as B, KiB, MiB or GiB. */
export function formatBytes(bytes: number): string {
  const units = ["B", "KiB", "MiB", "GiB"];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return unit === 0 ? `${value} B` : `${value.toFixed(1)} ${units[unit]}`;
}

/** One catalog asset named in research terms (`GET /catalog/assets?ids=`). */
export interface AssetName {
  asset_id: string;
  dataset: string;
  subject: string | null;
  session: string | null;
  datatype: string | null;
  suffix: string | null;
  format: string | null;
  path: string;
  version: string;
  /** Whether no newer version of the same file has been committed since. */
  current: boolean;
}

export const BATCH_IDS = 100;

export function parseAssetNames(body: unknown): readonly AssetName[] {
  const rows = rec(body, "the response").assets;
  if (!Array.isArray(rows)) throw new Error('the response has no "assets" list');
  return rows.map((raw: unknown, i): AssetName => {
    const at = `assets[${i}]`;
    const o = rec(raw, at);
    if (typeof o.current !== "boolean") throw new Error(`${at}.current is not a boolean`);
    return {
      asset_id: str(o, "asset_id", at),
      dataset: str(o, "dataset", at),
      subject: optStr(o, "subject"),
      session: optStr(o, "session"),
      datatype: optStr(o, "datatype"),
      suffix: optStr(o, "suffix"),
      format: optStr(o, "format"),
      path: str(o, "path", at),
      version: str(o, "version", at),
      current: o.current,
    };
  });
}

export type NamesResult =
  | { kind: "loaded"; names: ReadonlyMap<string, AssetName> }
  | { kind: "error"; message: string }
  | { kind: "ended" };

/** Name `ids` in batches of 100 (one call per batch). An id the catalog lacks is absent. */
export async function loadAssetNames(
  config: Cfg,
  ids: readonly string[],
  tokens: TokenSource,
  fetchFn: FetchFn,
  timeoutMs: number = TIMEOUT_MS,
): Promise<NamesResult> {
  const unique = [...new Set(ids)];
  const batches: string[][] = [];
  for (let i = 0; i < unique.length; i += BATCH_IDS) batches.push(unique.slice(i, i + BATCH_IDS));
  const names = new Map<string, AssetName>();
  const results = await Promise.all(
    batches.map((batch) =>
      apiGet(
        config.apiUrl,
        `/catalog/assets?ids=${batch.map(encodeURIComponent).join(",")}`,
        tokens,
        fetchFn,
        timeoutMs,
        { parse: parseAssetNames, refusal: serverError },
      ),
    ),
  );
  for (const r of results) {
    if (r.kind !== "loaded") return r;
    for (const n of r.value) names.set(n.asset_id, n);
  }
  return { kind: "loaded", names };
}
