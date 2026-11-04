// The authenticated GET shared by GET /uploads and GET /get-started: bearer token, timeout, one
// refresh on a 401, and the visible messages for every failure. Never throws.
import { errorText } from "../errors";

export interface TokenSource {
  getAccessToken(): string | null;
  /** True when a new access token is now available; false when the session cannot be renewed. */
  refresh(): Promise<boolean>;
}

export const TIMEOUT_MS = 30_000;

export type FetchFn = (url: string, init: RequestInit) => Promise<Response>;

export type ApiGetResult<T> =
  | { kind: "loaded"; value: T }
  | { kind: "error"; message: string }
  | { kind: "ended" };

export function unreachable(err: unknown, timeoutMs: number): string {
  if (err instanceof Error && (err.name === "TimeoutError" || err.name === "AbortError")) {
    return `The platform API did not answer within ${timeoutMs / 1000} seconds. Try again.`;
  }
  return `Could not reach the platform API (${errorText(err)}). Check your network, then try again.`;
}

export interface ApiGetOptions<T> {
  /** Turn the parsed JSON body into the value; a throw becomes the "cannot read" message. */
  parse: (body: unknown) => T;
  /** The message for a non-401 failure this endpoint words itself; null falls to the generic text. */
  refusal: (response: Response) => Promise<string | null>;
}

/**
 * GET `${apiUrl}${path}` with the bearer token. A 401 triggers exactly one refresh and one retry; a
 * second 401, or a refresh that fails, ends the session.
 */
export async function apiGet<T>(
  apiUrl: string,
  path: string,
  tokens: TokenSource,
  fetchFn: FetchFn,
  timeoutMs: number,
  options: ApiGetOptions<T>,
): Promise<ApiGetResult<T>> {
  let refreshed = false;
  for (;;) {
    const token = tokens.getAccessToken();
    if (token === null) return { kind: "ended" };
    let response: Response;
    try {
      response = await fetchFn(`${apiUrl}${path}`, {
        headers: { authorization: `Bearer ${token}` },
        signal: AbortSignal.timeout(timeoutMs),
      });
    } catch (err) {
      return { kind: "error", message: unreachable(err, timeoutMs) };
    }
    if (response.status === 401) {
      if (refreshed) return { kind: "ended" };
      refreshed = true;
      try {
        if (!(await tokens.refresh())) return { kind: "ended" };
      } catch (err) {
        return { kind: "error", message: unreachable(err, timeoutMs) };
      }
      continue;
    }
    if (!response.ok) {
      const message = await options.refusal(response);
      if (message !== null) return { kind: "error", message };
      return {
        kind: "error",
        message: `The platform API answered HTTP ${response.status}. Try again; if it keeps failing, tell your administrator.`,
      };
    }
    try {
      return { kind: "loaded", value: options.parse(await response.json()) };
    } catch (err) {
      return {
        kind: "error",
        message: `The platform API sent a response this page cannot read (${errorText(err)}). Tell your administrator.`,
      };
    }
  }
}

/** A refusal body read: the short stable `code` to branch on and the `text` to show. */
export interface Refusal {
  code: string | null;
  text: string | null;
}

function nonEmpty(value: unknown): string | null {
  return typeof value === "string" && value !== "" ? value : null;
}

/**
 * The server's refusal `{"error": "<code>", "detail": "<sentence>"}`. A body from an API older than
 * the uniform body has only `{"error": "<sentence>"}`: its text is that sentence and its code null.
 */
export async function serverRefusal(response: Response): Promise<Refusal> {
  try {
    const body: unknown = await response.json();
    if (typeof body === "object" && body !== null) {
      const error = "error" in body ? nonEmpty(body.error) : null;
      const detail = "detail" in body ? nonEmpty(body.detail) : null;
      if (detail !== null) return { code: error, text: detail };
      return { code: null, text: error };
    }
  } catch {
    // no readable body
  }
  return { code: null, text: null };
}

/** The server's refusal sentence (`detail`, else an older API's `error` text), or null. */
export async function serverError(response: Response): Promise<string | null> {
  return (await serverRefusal(response)).text;
}

export type ApiSendResult<T> =
  | { kind: "loaded"; value: T }
  | { kind: "error"; message: string; status: number | null }
  | { kind: "ended" };

export interface ApiSendOptions<T> {
  /** Turn the parsed JSON body (null for an empty body such as a 204) into the value. */
  parse: (body: unknown) => T;
  /** The message for a non-401 failure this endpoint words itself; null falls to the generic text. */
  refusal: (response: Response) => Promise<string | null>;
}

/**
 * Send `method` `${apiUrl}${path}` with a JSON body (or none), the bearer token, the same timeout and
 * the same single refresh on a 401 as `apiGet`. An error carries the HTTP status (null when the API was
 * not reached) so the caller can tell 403 from 409. Never throws.
 */
export async function apiSend<T>(
  method: "POST" | "PATCH" | "DELETE",
  apiUrl: string,
  path: string,
  body: unknown,
  tokens: TokenSource,
  fetchFn: FetchFn,
  timeoutMs: number,
  options: ApiSendOptions<T>,
): Promise<ApiSendResult<T>> {
  let refreshed = false;
  for (;;) {
    const token = tokens.getAccessToken();
    if (token === null) return { kind: "ended" };
    const headers: Record<string, string> = { authorization: `Bearer ${token}` };
    if (body !== undefined) headers["content-type"] = "application/json";
    let response: Response;
    try {
      response = await fetchFn(`${apiUrl}${path}`, {
        method,
        headers,
        ...(body === undefined ? {} : { body: JSON.stringify(body) }),
        signal: AbortSignal.timeout(timeoutMs),
      });
    } catch (err) {
      return { kind: "error", message: unreachable(err, timeoutMs), status: null };
    }
    if (response.status === 401) {
      if (refreshed) return { kind: "ended" };
      refreshed = true;
      try {
        if (!(await tokens.refresh())) return { kind: "ended" };
      } catch (err) {
        return { kind: "error", message: unreachable(err, timeoutMs), status: null };
      }
      continue;
    }
    if (!response.ok) {
      const message = await options.refusal(response);
      return {
        kind: "error",
        message:
          message ??
          `The platform API answered HTTP ${response.status}. Try again; if it keeps failing, tell your administrator.`,
        status: response.status,
      };
    }
    try {
      const parsed: unknown = response.status === 204 ? null : await response.json();
      return { kind: "loaded", value: options.parse(parsed) };
    } catch (err) {
      return {
        kind: "error",
        message: `The platform API sent a response this page cannot read (${errorText(err)}). Tell your administrator.`,
        status: response.status,
      };
    }
  }
}
