// App-dev notes: the wire shape of `POST /notes`, the page state a note carries
// and the token it is sent with. The server stamps the id, the time and the author; the note carries
// the text, the page address and `view` (the route and its query). The ID token is sent when there is
// one, so the server can store the author's email; without it the access token goes and the server
// stores the `sub` and a flag.
import {
  type ApiSendResult,
  apiSend,
  type FetchFn,
  serverError,
  TIMEOUT_MS,
  type TokenSource,
} from "../auth/apiGet";
import type { AuthConfig } from "../auth/config";

export const TEXT_MAX = 4000;
export const NOTES_PATH = "/notes";

export interface NoteView {
  path: string;
  query: Record<string, string>;
  hash: string;
}

export interface NoteBody {
  text: string;
  url: string;
  view: NoteView;
}

/** The page's route state: the path, each query parameter (the last of a repeated name) and the hash. */
export function viewOf(location: Pick<Location, "pathname" | "search" | "hash">): NoteView {
  return {
    path: location.pathname,
    query: Object.fromEntries(new URLSearchParams(location.search)),
    hash: location.hash,
  };
}

/** The text the Send button may send: non-blank and within the limit. */
export function sendable(text: string): boolean {
  return text.trim() !== "" && text.length <= TEXT_MAX;
}

/** A token source that offers the ID token first (it carries `email`), else the access token. */
export function idFirst(auth: {
  getIdToken(): string | null;
  getAccessToken(): string | null;
  refresh(): Promise<boolean>;
}): TokenSource {
  return {
    getAccessToken: () => auth.getIdToken() ?? auth.getAccessToken(),
    refresh: () => auth.refresh(),
  };
}

export function parseNoteId(raw: unknown): string {
  if (typeof raw === "object" && raw !== null && "note_id" in raw) {
    const id = raw.note_id;
    if (typeof id === "string" && id !== "") return id;
  }
  throw new Error('the response has no "note_id"');
}

// Every refusal the route can send is worded by the server; 500 names the request id.
const REFUSALS = new Set([400, 403, 413, 500, 502, 503]);
const refusal = async (response: Response): Promise<string | null> =>
  REFUSALS.has(response.status) ? await serverError(response) : null;

export function sendNote(
  config: Pick<AuthConfig, "apiUrl">,
  body: NoteBody,
  tokens: TokenSource,
  fetchFn: FetchFn,
  timeoutMs: number = TIMEOUT_MS,
): Promise<ApiSendResult<string>> {
  return apiSend("POST", config.apiUrl, NOTES_PATH, body, tokens, fetchFn, timeoutMs, {
    refusal,
    parse: parseNoteId,
  });
}
