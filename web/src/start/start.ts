// GET /get-started: the short install command.
// Fetch only, with the same 401 handling as GET /uploads.
import { apiGet, type FetchFn, serverError, TIMEOUT_MS, type TokenSource } from "../auth/apiGet";
import type { AuthConfig } from "../auth/config";

export interface GetStarted {
  /** The command text: the uv hint line, then the `uvx ... asteroidea-setup <app>` line. */
  install: string;
}

export type GetStartedResult =
  | { kind: "loaded"; body: GetStarted }
  | { kind: "error"; message: string }
  | { kind: "ended" };

export type GetStartedState = { kind: "loading" } | GetStartedResult;

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

export function parseGetStarted(body: unknown): GetStarted {
  if (!isRecord(body)) throw new Error("the response is not an object");
  if (typeof body.install !== "string" || body.install === "") {
    throw new Error('the response has no "install" text');
  }
  return { install: body.install };
}

/**
 * Load the get-started body. A 401 triggers one refresh and one retry; a second 401 ends the
 * session. A 403 or 503 carries the server's own `error` text, shown verbatim. Never throws.
 */
export async function loadGetStarted(
  config: Pick<AuthConfig, "apiUrl">,
  tokens: TokenSource,
  fetchFn: FetchFn,
  timeoutMs: number = TIMEOUT_MS,
): Promise<GetStartedResult> {
  const result = await apiGet(config.apiUrl, "/get-started", tokens, fetchFn, timeoutMs, {
    parse: parseGetStarted,
    refusal: async (response) => {
      if (response.status !== 403 && response.status !== 503) return null;
      const text = await serverError(response);
      return text ?? `The platform API answered HTTP ${response.status} with no message.`;
    },
  });
  return result.kind === "loaded" ? { kind: "loaded", body: result.value } : result;
}
