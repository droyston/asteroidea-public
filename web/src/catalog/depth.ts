// The catalog depth routes: GET /catalog/datasets/{dataset} (subjects, sessions and formats of
// one dataset) and the subject-filtered assets route. Every fetch goes through catalog.ts `get`
// (apiGet), so the 401 refresh, the timeout and the refusal text are the same as the list pages.
import { type FetchFn, TIMEOUT_MS, type TokenSource } from "../auth/apiGet";
import type { AuthConfig } from "../auth/config";
import { dataSubjectPath } from "../router/dataPaths";
import { get, loadAssets } from "./catalog";

export interface SubjectRow {
  subject: string;
  species: string | null;
  sex: string | null;
  age: string | null;
}

export interface SessionRow {
  subject: string;
  bids_session: string | null;
  start_time: string | null;
  description: string | null;
}

export interface FormatRow {
  format: string | null;
  count: number;
  bytes: number | null;
}

/** The header facts of one dataset (the GET /catalog/datasets/{dataset} `facts` object). */
export interface DatasetFacts {
  dataset: string;
  title: string | null;
  license: string | null;
  doi: string | null;
  source_archive: string | null;
  source_dataset: string | null;
  source_version: string | null;
  asset_count: number;
  subject_count: number;
  total_bytes: number;
}

export interface DatasetBody {
  dataset: string;
  facts: DatasetFacts;
  subjects: readonly SubjectRow[];
  sessions: readonly SessionRow[];
  formats: readonly FormatRow[];
  query_ms: number;
}

type Rec = Record<string, unknown>;

function isRecord(v: unknown): v is Rec {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

/** A cell as text: null stays null, a number or string is shown as written, anything else fails. */
function text(v: unknown, name: string): string | null {
  if (v === null || v === undefined) return null;
  if (typeof v === "string") return v;
  if (typeof v === "number" || typeof v === "boolean") return String(v);
  throw new Error(`"${name}" is not text`);
}

function whole(v: unknown, name: string): number {
  if (typeof v !== "number") throw new Error(`"${name}" is not a number`);
  return v;
}

function rows(body: Rec, key: string): Rec[] {
  const list = body[key];
  if (!Array.isArray(list)) throw new Error(`the response has no "${key}" list`);
  return (list as unknown[]).map((row, i) => {
    if (!isRecord(row)) throw new Error(`${key}[${i}] is not an object`);
    return row;
  });
}

function parseFacts(raw: unknown): DatasetFacts {
  if (!isRecord(raw)) throw new Error('the response has no "facts" object');
  return {
    dataset: text(raw.dataset, "dataset") ?? "",
    title: text(raw.title, "title"),
    license: text(raw.license, "license"),
    doi: text(raw.doi, "doi"),
    source_archive: text(raw.source_archive, "source_archive"),
    source_dataset: text(raw.source_dataset, "source_dataset"),
    source_version: text(raw.source_version, "source_version"),
    asset_count: whole(raw.asset_count ?? 0, "asset_count"),
    subject_count: whole(raw.subject_count ?? 0, "subject_count"),
    total_bytes: whole(raw.total_bytes ?? 0, "total_bytes"),
  };
}

export function parseDataset(raw: unknown): DatasetBody {
  if (!isRecord(raw)) throw new Error("the response is not an object");
  return {
    dataset: text(raw.dataset, "dataset") ?? "",
    facts: parseFacts(raw.facts),
    subjects: rows(raw, "subjects").map((r) => ({
      subject: text(r.subject, "subject") ?? "",
      species: text(r.species, "species"),
      sex: text(r.sex, "sex"),
      age: text(r.age, "age"),
    })),
    sessions: rows(raw, "sessions").map((r) => ({
      subject: text(r.subject, "subject") ?? "",
      bids_session: text(r.bids_session, "bids_session"),
      start_time: text(r.start_time, "start_time"),
      description: text(r.description, "description"),
    })),
    formats: rows(raw, "formats").map((r) => ({
      format: text(r.format, "format"),
      count: whole(r.count ?? 0, "count"),
      bytes: r.bytes === null || r.bytes === undefined ? null : whole(r.bytes, "bytes"),
    })),
    query_ms: whole(raw.query_ms, "query_ms"),
  };
}

export function loadDataset(
  config: Pick<AuthConfig, "apiUrl">,
  dataset: string,
  tokens: TokenSource,
  fetchFn: FetchFn,
  timeoutMs: number = TIMEOUT_MS,
) {
  const path = `/catalog/datasets/${encodeURIComponent(dataset)}`;
  return get(config, path, tokens, fetchFn, timeoutMs, parseDataset);
}

/** One subject's files: the assets route with `subject=` (the shape and paging of catalog.ts). */
export function loadSubjectFiles(
  config: Pick<AuthConfig, "apiUrl">,
  dataset: string,
  subject: string,
  page: number,
  tokens: TokenSource,
  fetchFn: FetchFn,
  timeoutMs: number = TIMEOUT_MS,
) {
  return loadAssets(config, dataset, { subject, format: "", page }, tokens, fetchFn, timeoutMs);
}

/** Where a file's own page lives. */
export function filePath(assetId: string): string {
  return `/files/${encodeURIComponent(assetId)}`;
}

export function subjectPath(dataset: string, subject: string): string {
  return dataSubjectPath(dataset, subject);
}
