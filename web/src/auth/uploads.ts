// GET /uploads and the panel's states.
import { apiGet, type FetchFn, TIMEOUT_MS, type TokenSource } from "./apiGet";
import type { AuthConfig } from "./config";

export { TIMEOUT_MS, type TokenSource };

export type UploadState = "OPEN" | "FINALIZING" | "ACCEPTED" | "COMMITTED" | "INCOMPLETE";

export interface Upload {
  session_id: string;
  dataset: string;
  relpath: string;
  state: string;
  created_at: number;
  asset_id: string | null;
}

export type UploadsResult =
  | { kind: "loaded"; uploads: readonly Upload[] }
  | { kind: "error"; message: string }
  | { kind: "ended" };

export type UploadsState = { kind: "loading" } | UploadsResult;

export function parseUploads(body: unknown): readonly Upload[] {
  if (
    typeof body !== "object" ||
    body === null ||
    !("uploads" in body) ||
    !Array.isArray(body.uploads)
  ) {
    throw new Error('the response has no "uploads" list');
  }
  return body.uploads.map((row: unknown, i: number): Upload => {
    if (typeof row !== "object" || row === null) throw new Error(`uploads[${i}] is not an object`);
    const r = row as Record<string, unknown>;
    const { session_id, dataset, relpath, state, created_at, asset_id } = r;
    if (
      typeof session_id !== "string" ||
      typeof dataset !== "string" ||
      typeof relpath !== "string" ||
      typeof state !== "string" ||
      typeof created_at !== "number" ||
      !(typeof asset_id === "string" || asset_id === null || asset_id === undefined)
    ) {
      throw new Error(`uploads[${i}] does not match the GET /uploads contract`);
    }
    return { session_id, dataset, relpath, state, created_at, asset_id: asset_id ?? null };
  });
}

/**
 * Load the caller's uploads. A 401 triggers exactly one refresh and one retry; a second 401, or a
 * refresh that fails, ends the session. Never throws.
 */
export async function loadUploads(
  config: Pick<AuthConfig, "apiUrl">,
  tokens: TokenSource,
  fetchFn: FetchFn,
  timeoutMs: number = TIMEOUT_MS,
): Promise<UploadsResult> {
  const result = await apiGet(config.apiUrl, "/uploads", tokens, fetchFn, timeoutMs, {
    parse: parseUploads,
    refusal: (response) =>
      Promise.resolve(
        response.status === 403
          ? "Your account is not in the researchers group, so it cannot list uploads. Ask your administrator to add you."
          : null,
      ),
  });
  return result.kind === "loaded" ? { kind: "loaded", uploads: result.value } : result;
}
