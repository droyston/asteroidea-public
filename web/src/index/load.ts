import { errorText } from "../errors";
import { type Index, parseIndex } from "./schema";

export const INDEX_URL = "/index.json";

export type LoadState =
  | { kind: "loading" }
  | { kind: "loaded"; index: Index }
  | { kind: "missing"; detail: string }
  | { kind: "error"; message: string };

type Fetch = (url: string) => Promise<Response>;

/**
 * Fetch and validate the harvested index. Never throws: every failure becomes a state that
 * names its cause. A dev or preview server answers a missing public file either with 404 or
 * with the SPA's index.html, so a non-JSON content type also counts as a missing index.
 */
export async function loadIndex(fetchFn: Fetch = (u) => fetch(u)): Promise<LoadState> {
  let response: Response;
  try {
    response = await fetchFn(INDEX_URL);
  } catch (err) {
    return { kind: "error", message: `Could not fetch ${INDEX_URL}: ${errorText(err)}` };
  }
  const type = response.headers.get("content-type") ?? "";
  if (response.status === 404) {
    return { kind: "missing", detail: `GET ${INDEX_URL} returned 404` };
  }
  if (!response.ok) {
    return {
      kind: "error",
      message: `GET ${INDEX_URL} failed: HTTP ${response.status} ${response.statusText}`.trim(),
    };
  }
  if (!type.includes("json")) {
    return {
      kind: "missing",
      detail: `GET ${INDEX_URL} returned ${type || "no content type"}, not JSON`,
    };
  }
  let body: unknown;
  try {
    body = await response.json();
  } catch (err) {
    return { kind: "error", message: `${INDEX_URL} is not valid JSON: ${errorText(err)}` };
  }
  try {
    return { kind: "loaded", index: parseIndex(body) };
  } catch (err) {
    return { kind: "error", message: errorText(err) };
  }
}
