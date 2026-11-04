// The file page routes: GET /catalog/assets/{id}/tables/{table}, /events and /download.
// Every fetch goes through catalog.ts `get` (apiGet), so the 401 refresh, the timeout and the refusal
// text are the same as the list pages.
import { type FetchFn, TIMEOUT_MS, type TokenSource } from "../auth/apiGet";
import type { AuthConfig } from "../auth/config";
import { get, type Row } from "./catalog";

export type TableName = "series" | "electrodes" | "units" | "intervals";

export interface TableBody {
  rows: readonly Row[];
  page: number;
  more: boolean;
}

export interface EventsBody {
  intervals: readonly Row[];
  events: readonly Row[];
  intervals_more: boolean;
  events_more: boolean;
  query_ms: number;
}

export interface DownloadBody {
  url: string;
  expires_in: number;
  size_bytes: number | null;
}

function isRecord(v: unknown): v is Row {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

function rowList(body: Row, key: string): Row[] {
  const list = body[key];
  if (!Array.isArray(list)) throw new Error(`the response has no "${key}" list`);
  return (list as unknown[]).map((row, i) => {
    if (!isRecord(row)) throw new Error(`${key}[${i}] is not an object`);
    return row;
  });
}

function flag(body: Row, key: string): boolean {
  const v = body[key];
  if (typeof v !== "boolean") throw new Error(`"${key}" is not a boolean`);
  return v;
}

export function parseTable(raw: unknown): TableBody {
  if (!isRecord(raw)) throw new Error("the response is not an object");
  if (typeof raw.page !== "number") throw new Error('"page" is not a number');
  return { rows: rowList(raw, "rows"), page: raw.page, more: flag(raw, "more") };
}

export function parseEvents(raw: unknown): EventsBody {
  if (!isRecord(raw)) throw new Error("the response is not an object");
  return {
    intervals: rowList(raw, "intervals"),
    events: rowList(raw, "events"),
    intervals_more: flag(raw, "intervals_more"),
    events_more: flag(raw, "events_more"),
    query_ms: typeof raw.query_ms === "number" ? raw.query_ms : 0,
  };
}

export function parseDownload(raw: unknown): DownloadBody {
  if (!isRecord(raw)) throw new Error("the response is not an object");
  if (typeof raw.url !== "string" || !raw.url.startsWith("https://")) {
    throw new Error('"url" is not an https address');
  }
  return {
    url: raw.url,
    expires_in: typeof raw.expires_in === "number" ? raw.expires_in : 0,
    size_bytes: typeof raw.size_bytes === "number" ? raw.size_bytes : null,
  };
}

function assetPath(assetId: string): string {
  return `/catalog/assets/${encodeURIComponent(assetId)}`;
}

export function loadTable(
  config: Pick<AuthConfig, "apiUrl">,
  assetId: string,
  table: TableName,
  page: number,
  tokens: TokenSource,
  fetchFn: FetchFn,
  timeoutMs: number = TIMEOUT_MS,
) {
  const query = page > 0 ? `?page=${page}` : "";
  const path = `${assetPath(assetId)}/tables/${table}${query}`;
  return get(config, path, tokens, fetchFn, timeoutMs, parseTable);
}

export function loadEvents(
  config: Pick<AuthConfig, "apiUrl">,
  assetId: string,
  tokens: TokenSource,
  fetchFn: FetchFn,
  timeoutMs: number = TIMEOUT_MS,
) {
  return get(config, `${assetPath(assetId)}/events`, tokens, fetchFn, timeoutMs, parseEvents);
}

export function loadDownload(
  config: Pick<AuthConfig, "apiUrl">,
  assetId: string,
  tokens: TokenSource,
  fetchFn: FetchFn,
  timeoutMs: number = TIMEOUT_MS,
) {
  return get(config, `${assetPath(assetId)}/download`, tokens, fetchFn, timeoutMs, parseDownload);
}
