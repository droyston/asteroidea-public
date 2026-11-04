// One browser sentence per API error code. The API's `detail` is written
// for a developer; the page leads with the sentence here, offers the next step as the primary button,
// and keeps the code, small, for support. A request problem shows the server's detail beside the
// sentence (it names what to change); a platform failure folds it under "Technical detail".
// `states.test.ts` checks this map against every code the API source can answer.

/** The next step a state offers as its primary button. */
export type Next =
  /** Ask again, unchanged. */
  | "retry"
  /** Ask again with a smaller request (fewer units or a shorter window). */
  | "smaller"
  /** Sign in again. */
  | "sign-in"
  /** The work is for curators: say how to reach one. */
  | "curator"
  /** Go back to the Studies list. */
  | "studies"
  /** Copy the details for support: nothing in the page can fix it. */
  | "support";

/** How the server's `detail` sentence shows. */
export type DetailShow = "inline" | "fold";

export interface CodeCopy {
  /** The small capitals line above the title ("Refused · too slow"). */
  readonly eyebrow: string;
  readonly title: string;
  readonly sentence: string;
  readonly next: Next;
  readonly detail: DetailShow;
}

/** The code an API answer without its own code takes from its status (`api_common.py :: CODES`). */
export const STATUS_CODES: Readonly<Record<number, string>> = {
  400: "invalid_request",
  401: "unauthorized",
  403: "forbidden",
  404: "not_found",
  409: "conflict",
  410: "gone",
  413: "too_large",
  422: "unprocessable",
  500: "internal_error",
  502: "upstream_failed",
  503: "unavailable",
  504: "unavailable",
};

/** The code for "the API was not reached" (network down, timeout): no HTTP status at all. */
export const NETWORK = "network";

const request = (title: string, sentence: string): CodeCopy => ({
  eyebrow: "Refused · request",
  title,
  sentence,
  next: "support",
  detail: "inline",
});

const tooBig = (title: string, sentence: string): CodeCopy => ({
  eyebrow: "Refused · too large",
  title,
  sentence,
  next: "smaller",
  detail: "fold",
});

const conflict = (title: string, sentence: string): CodeCopy => ({
  eyebrow: "Refused · conflict",
  title,
  sentence,
  next: "retry",
  detail: "inline",
});

export const CODE_COPY: Readonly<Record<string, CodeCopy>> = {
  // --- too much work: offer a smaller request ---
  slow: {
    eyebrow: "Refused · too slow",
    title: "That request is too big to compute here",
    sentence:
      "The server stopped before it finished. A smaller request fits, or run the full request in your notebook.",
    next: "smaller",
    detail: "fold",
  },
  too_large: tooBig(
    "That answer is too large to send",
    "The result would be bigger than the server can send in one answer. A smaller request fits, or run the full request in your notebook.",
  ),
  body_estimate_too_large: tooBig(
    "That answer would be too large to send",
    "The raster for this request would be bigger than the server can send. A smaller request fits, or turn the raster off.",
  ),
  too_many_units: tooBig(
    "Too many units for one request",
    "The server computes at most 152 units at a time. Ask for fewer units, or loop over them in your notebook.",
  ),
  too_many: tooBig(
    "Too many links match",
    "More links match than the server lists at once. Narrow it to one study, subject or dataset.",
  ),
  // --- the platform failed: try again, detail folded ---
  internal_error: {
    eyebrow: "Error · platform",
    title: "Something failed on our side",
    sentence:
      "The platform could not finish this request. Nothing you did caused it. Try again; if it keeps failing, copy the details for support.",
    next: "retry",
    detail: "fold",
  },
  upstream_failed: {
    eyebrow: "Error · catalog unavailable",
    title: "The catalog didn't answer",
    sentence:
      "We couldn't load this just now. Nothing you did caused this. Links you have open still work once it's back.",
    next: "retry",
    detail: "fold",
  },
  unavailable: {
    eyebrow: "Error · unavailable",
    title: "The platform is busy",
    sentence:
      "The platform did not answer in time. Nothing you did caused this. Try again in a moment.",
    next: "retry",
    detail: "fold",
  },
  summary_unreadable: {
    eyebrow: "Error · stored summary",
    title: "The stored summary could not be read",
    sentence:
      "The precomputed summary for this session is damaged or missing a part. Try again; if it keeps failing, copy the details for support.",
    next: "retry",
    detail: "fold",
  },
  [NETWORK]: {
    eyebrow: "Error · no connection",
    title: "The platform could not be reached",
    sentence: "Your browser got no answer. Check your network, then try again.",
    next: "retry",
    detail: "fold",
  },
  // --- access ---
  unauthorized: {
    eyebrow: "Session ended",
    title: "Sign in again to carry on",
    sentence: "Your sign-in has run out. You will come back to this page.",
    next: "sign-in",
    detail: "fold",
  },
  id_token_required: {
    eyebrow: "Session ended",
    title: "Sign in again to carry on",
    sentence:
      "The platform needs your email from a fresh sign-in. You will come back to this page.",
    next: "sign-in",
    detail: "fold",
  },
  forbidden: {
    eyebrow: "Refused · access",
    title: "Your account cannot do this",
    sentence:
      "Your account is not in the group this needs. Ask a curator or your administrator to add it.",
    next: "curator",
    detail: "inline",
  },
  not_author: {
    eyebrow: "Refused · not yours",
    title: "Only its author can change this",
    sentence: "This result is another researcher's. You can read it and use it, not change it.",
    next: "support",
    detail: "fold",
  },
  dataset_not_yours: {
    eyebrow: "Refused · not yours",
    title: "Another researcher owns that dataset name",
    sentence: "Pick another dataset name for your upload.",
    next: "support",
    detail: "inline",
  },
  // --- not there ---
  not_found: {
    eyebrow: "Not found",
    title: "That is not in the catalog",
    sentence: "It may have been removed, or the link is wrong. Start again from the Studies list.",
    next: "studies",
    detail: "inline",
  },
  gone: {
    eyebrow: "Not found",
    title: "That is no longer here",
    sentence: "It expired or was removed. Start again from the Studies list.",
    next: "studies",
    detail: "inline",
  },
  overview_not_built: {
    eyebrow: "Not ready",
    title: "This recording's overview is being prepared",
    sentence: "The overview is built after import. Try again in a few minutes.",
    next: "retry",
    detail: "fold",
  },
  index_not_built: {
    eyebrow: "Not ready",
    title: "This recording is still being indexed",
    sentence: "The chunk index is built after import. Try again in a few minutes.",
    next: "retry",
    detail: "fold",
  },
  session_expired: {
    eyebrow: "Refused · expired",
    title: "That upload session expired",
    sentence: "Upload sessions last a limited time. Start the upload again.",
    next: "support",
    detail: "fold",
  },
  // --- the request itself: the server's detail says what to change ---
  invalid_request: request(
    "The request was not accepted",
    "The server could not use this request as it is.",
  ),
  invalid_ids: request("Those file ids were not accepted", "Ask for 1 to 100 valid file ids."),
  invalid_session: request(
    "That session name was not accepted",
    "The session name in the link is not one the catalog uses.",
  ),
  invalid_scope: request(
    "That listing was not accepted",
    "Results are listed as yours or everyone's.",
  ),
  invalid_input: request(
    "That filter was not accepted",
    "The file or result to filter by is empty or too long.",
  ),
  invalid_run: request(
    "That run filter was not accepted",
    "The run to list is empty or too long, or it was sent with a file filter.",
  ),
  unprocessable: request(
    "The server could not use that file",
    "The file is not in a form this view reads.",
  ),
  not_nwb: request("That file is not an NWB file", "This view reads NWB recordings only."),
  malformed_range: request(
    "That unit range was not accepted",
    "Units are all, a range such as 0-43, or a comma list.",
  ),
  malformed_align: request(
    "That alignment was not accepted",
    "An event filter is written attribute=value.",
  ),
  recipe_unresolved: request(
    "That alignment does not match this file",
    "The events or intervals named are not in this file.",
  ),
  screen_unresolved: request(
    "That screen does not match this file",
    "The events or attributes named are not in this file.",
  ),
  no_such_attribute: request(
    "That attribute is not in this file",
    "Pick an attribute the file's events carry.",
  ),
  no_such_event_type: request(
    "That event type is not in this file",
    "Pick an event type the file lists.",
  ),
  no_stimulus_file: request(
    "This file has no stimulus file to align on",
    "Pick another alignment for this file.",
  ),
  not_average: request(
    "That event type cannot be averaged",
    "Pick an event type marked for averaging.",
  ),
  too_few_events_per_level: request(
    "Too few events at some levels",
    "Each level of the attribute needs more events to compare. Pick another attribute.",
  ),
  defaults_window: request(
    "That window does not fit this alignment",
    "Set the window before and after the event yourself.",
  ),
  // --- conflicts: the state changed under the request ---
  conflict: conflict("That changed while you were working", "Reload and try again."),
  raw_key_exists: conflict(
    "A file is already at that path",
    "Upload to another path, or remove the old file first.",
  ),
  upload_pending: conflict(
    "The upload has not arrived yet",
    "Finish uploading the file, then try again.",
  ),
  upload_incomplete: conflict(
    "The upload did not arrive whole",
    "The file did not match its size or checksum and was removed. Upload it again.",
  ),
  has_dependents: conflict(
    "Other results use this one",
    "It cannot be deleted while others use it. Withdraw it instead.",
  ),
};

/** The `session_<state>` codes of `POST /sessions/{id}/finalize` share one sentence. */
const SESSION_STATE: CodeCopy = conflict(
  "That upload session cannot be finished now",
  "Its state does not allow it. Check the upload and try again.",
);

export interface RefusalInput {
  /** The HTTP status, or null when the API was not reached. */
  readonly status: number | null;
  /** The body's `error` code, or null for an older body or a gateway `{message}`. */
  readonly code: string | null;
}

/** The code a refusal is known by: its own, else its status's, else "network". */
export function codeOf(r: RefusalInput): string {
  if (r.code !== null && r.code !== "") return r.code;
  if (r.status === null) return NETWORK;
  return STATUS_CODES[r.status] ?? (r.status >= 500 ? "internal_error" : "invalid_request");
}

/** The copy for a refusal; an unknown code falls back on its status's copy. */
export function copyFor(r: RefusalInput): CodeCopy & { readonly code: string } {
  const code = codeOf(r);
  const known = CODE_COPY[code];
  if (known !== undefined) return { ...known, code };
  if (code.startsWith("session_")) return { ...SESSION_STATE, code };
  const fallback = CODE_COPY[codeOf({ status: r.status, code: null })] ?? CODE_COPY.internal_error;
  return { ...(fallback as CodeCopy), code };
}
