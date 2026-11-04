// The catalog routes: GET /catalog/datasets, GET /catalog/datasets/{dataset}/assets and
// GET /catalog/assets/{asset_id}. Every fetch goes through apiGet; a refusal shows the server's own
// refusal sentence (`detail`) verbatim and carries its code.
import { apiGet, type FetchFn, serverRefusal, TIMEOUT_MS, type TokenSource } from "../auth/apiGet";
import type { AuthConfig } from "../auth/config";

export interface CatalogDataset {
  dataset: string;
  title: string | null;
  license: string | null;
  doi: string | null;
  source_archive: string | null;
  source_dataset: string | null;
  source_version: string | null;
  ingest_method: string | null;
  asset_count: number;
  subject_count: number;
  total_bytes: number;
}

export interface CatalogAssetRow {
  asset_id: string;
  path: string;
  subject: string | null;
  bids_session: string | null;
  datatype: string | null;
  format: string | null;
  size_bytes: number | null;
  committed_at: string | null;
}

export type Row = Record<string, unknown>;

export interface DatasetsBody {
  datasets: readonly CatalogDataset[];
  query_ms: number;
}

export interface AssetsBody {
  assets: readonly CatalogAssetRow[];
  page: number;
  more: boolean;
  query_ms: number;
}

export interface AssetBody {
  asset: Row;
  headers: readonly Row[];
}

export interface AssetFilters {
  subject: string;
  format: string;
  page: number;
}

export type CatalogResult<T> =
  | { kind: "loaded"; value: T }
  | {
      kind: "error";
      message: string;
      status503: boolean;
      code: string | null;
      /** The HTTP status; null when the API was not reached; absent from older callers. */
      status?: number | null;
    }
  | { kind: "ended" };

export type CatalogState<T> = { kind: "loading" } | CatalogResult<T>;

function isRecord(v: unknown): v is Row {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

function str(v: unknown, name: string): string | null {
  if (v === null || v === undefined) return null;
  if (typeof v !== "string") throw new Error(`"${name}" is not a string`);
  return v;
}

function num(v: unknown, name: string): number {
  if (typeof v !== "number") throw new Error(`"${name}" is not a number`);
  return v;
}

function list(body: unknown, key: string): { items: Row[]; body: Row } {
  if (!isRecord(body) || !Array.isArray(body[key]))
    throw new Error(`the response has no "${key}" list`);
  const items = (body[key] as unknown[]).map((row, i) => {
    if (!isRecord(row)) throw new Error(`${key}[${i}] is not an object`);
    return row;
  });
  return { items, body };
}

export function parseDatasets(raw: unknown): DatasetsBody {
  const { items, body } = list(raw, "datasets");
  return {
    datasets: items.map((r) => ({
      dataset: str(r.dataset, "dataset") ?? "",
      title: str(r.title, "title"),
      license: str(r.license, "license"),
      doi: str(r.doi, "doi"),
      source_archive: str(r.source_archive, "source_archive"),
      source_dataset: str(r.source_dataset, "source_dataset"),
      source_version: str(r.source_version, "source_version"),
      ingest_method: str(r.ingest_method, "ingest_method"),
      asset_count: num(r.asset_count ?? 0, "asset_count"),
      subject_count: num(r.subject_count ?? 0, "subject_count"),
      total_bytes: num(r.total_bytes ?? 0, "total_bytes"),
    })),
    query_ms: num(body.query_ms, "query_ms"),
  };
}

export function parseAssets(raw: unknown): AssetsBody {
  const { items, body } = list(raw, "assets");
  if (typeof body.more !== "boolean") throw new Error('"more" is not a boolean');
  return {
    assets: items.map((r) => ({
      asset_id: str(r.asset_id, "asset_id") ?? "",
      path: str(r.path, "path") ?? "",
      subject: str(r.subject, "subject"),
      bids_session: str(r.bids_session, "bids_session"),
      datatype: str(r.datatype, "datatype"),
      format: str(r.format, "format"),
      size_bytes:
        r.size_bytes === null || r.size_bytes === undefined
          ? null
          : num(r.size_bytes, "size_bytes"),
      committed_at: str(r.committed_at, "committed_at"),
    })),
    page: num(body.page, "page"),
    more: body.more,
    query_ms: num(body.query_ms, "query_ms"),
  };
}

export function parseAsset(raw: unknown): AssetBody {
  const { items, body } = list(raw, "headers");
  if (!isRecord(body.asset)) throw new Error('the response has no "asset" object');
  return { asset: body.asset, headers: items };
}

/** The query string for the assets route: an empty filter and page 0 are left out. */
export function assetsQuery(f: AssetFilters): string {
  const q = new URLSearchParams();
  if (f.subject.trim() !== "") q.set("subject", f.subject.trim());
  if (f.format.trim() !== "") q.set("format", f.format.trim());
  if (f.page > 0) q.set("page", String(f.page));
  const text = q.toString();
  return text === "" ? "" : `?${text}`;
}

// 500 is here so the uniform 500's sentence, which names the request id, is shown.
const REFUSALS = new Set([400, 403, 404, 413, 422, 500, 502, 503]);

export async function get<T>(
  config: Pick<AuthConfig, "apiUrl">,
  path: string,
  tokens: TokenSource,
  fetchFn: FetchFn,
  timeoutMs: number,
  parse: (body: unknown) => T,
): Promise<CatalogResult<T>> {
  let slow = false;
  let code: string | null = null;
  let status: number | null = null;
  const result = await apiGet(config.apiUrl, path, tokens, fetchFn, timeoutMs, {
    parse,
    refusal: async (response) => {
      status = response.status;
      if (!REFUSALS.has(response.status)) return null;
      slow = response.status === 503;
      const refused = await serverRefusal(response);
      code = refused.code;
      return refused.text ?? `The platform API answered HTTP ${response.status} with no message.`;
    },
  });
  if (result.kind === "error") return { ...result, status503: slow, code, status };
  return result;
}

export function loadDatasets(
  config: Pick<AuthConfig, "apiUrl">,
  tokens: TokenSource,
  fetchFn: FetchFn,
  timeoutMs: number = TIMEOUT_MS,
) {
  return get(config, "/catalog/datasets", tokens, fetchFn, timeoutMs, parseDatasets);
}

export function loadAssets(
  config: Pick<AuthConfig, "apiUrl">,
  dataset: string,
  filters: AssetFilters,
  tokens: TokenSource,
  fetchFn: FetchFn,
  timeoutMs: number = TIMEOUT_MS,
) {
  const path = `/catalog/datasets/${encodeURIComponent(dataset)}/assets${assetsQuery(filters)}`;
  return get(config, path, tokens, fetchFn, timeoutMs, parseAssets);
}

export function loadAsset(
  config: Pick<AuthConfig, "apiUrl">,
  assetId: string,
  tokens: TokenSource,
  fetchFn: FetchFn,
  timeoutMs: number = TIMEOUT_MS,
) {
  const path = `/catalog/assets/${encodeURIComponent(assetId)}`;
  return get(config, path, tokens, fetchFn, timeoutMs, parseAsset);
}
