// Shared researcher labels: the wire shapes of
// `/files/{asset_id}/labels`, the four calls, the editor's validation and the Units align options.
// The API returns `mine`, never the author. PATCH sends all four fields plus the `version` the editor
// loaded; a 403 is "not the author" and a 409 is "stale", both worded by the server.
import {
  type ApiSendResult,
  apiSend,
  type FetchFn,
  serverError,
  TIMEOUT_MS,
  type TokenSource,
} from "../auth/apiGet";
import type { AuthConfig } from "../auth/config";
import { type CatalogResult, get } from "../catalog/catalog";

export const NAME_MAX = 64;
export const NOTE_MAX_BYTES = 1024;

export interface Label {
  label_id: string;
  name: string;
  note: string;
  start_s: number;
  stop_s: number;
  mine: boolean;
  version: number;
}

/** What POST sends; PATCH sends the same four fields plus `version`. */
export interface LabelFields {
  name: string;
  note: string;
  start_s: number;
  stop_s: number;
}

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

export function parseLabel(raw: unknown): Label {
  if (!isRecord(raw)) throw new Error("a label is not an object");
  const { label_id, name, note, start_s, stop_s, mine, version } = raw;
  if (typeof label_id !== "string") throw new Error('"label_id" is not text');
  if (typeof name !== "string") throw new Error('"name" is not text');
  if (typeof start_s !== "number" || typeof stop_s !== "number") {
    throw new Error('"start_s" or "stop_s" is not a number');
  }
  if (typeof mine !== "boolean") throw new Error('"mine" is not a boolean');
  if (typeof version !== "number") throw new Error('"version" is not a number');
  return {
    label_id,
    name,
    note: typeof note === "string" ? note : "",
    start_s,
    stop_s,
    mine,
    version,
  };
}

export function parseLabels(raw: unknown): Label[] {
  if (!isRecord(raw) || !Array.isArray(raw.labels)) {
    throw new Error('the response has no "labels" list');
  }
  return (raw.labels as unknown[]).map(parseLabel);
}

export function labelsPath(assetId: string): string {
  return `/files/${encodeURIComponent(assetId)}/labels`;
}

function labelPath(assetId: string, labelId: string): string {
  return `${labelsPath(assetId)}/${encodeURIComponent(labelId)}`;
}

type Config = Pick<AuthConfig, "apiUrl">;

export function loadLabels(
  config: Config,
  assetId: string,
  tokens: TokenSource,
  fetchFn: FetchFn,
  timeoutMs: number = TIMEOUT_MS,
): Promise<CatalogResult<Label[]>> {
  return get(config, labelsPath(assetId), tokens, fetchFn, timeoutMs, parseLabels);
}

// 500 is here so the uniform 500's sentence, which names the request id, is shown.
const REFUSALS = new Set([400, 403, 404, 409, 413, 422, 500, 502, 503]);
const refusal = async (response: Response): Promise<string | null> =>
  REFUSALS.has(response.status) ? await serverError(response) : null;

export function createLabel(
  config: Config,
  assetId: string,
  fields: LabelFields,
  tokens: TokenSource,
  fetchFn: FetchFn,
  timeoutMs: number = TIMEOUT_MS,
): Promise<ApiSendResult<Label>> {
  const path = labelsPath(assetId);
  return apiSend("POST", config.apiUrl, path, fields, tokens, fetchFn, timeoutMs, {
    refusal,
    parse: parseLabel,
  });
}

export function updateLabel(
  config: Config,
  assetId: string,
  label: Pick<Label, "label_id" | "version">,
  fields: LabelFields,
  tokens: TokenSource,
  fetchFn: FetchFn,
  timeoutMs: number = TIMEOUT_MS,
): Promise<ApiSendResult<Label>> {
  const path = labelPath(assetId, label.label_id);
  const body = { ...fields, version: label.version };
  return apiSend("PATCH", config.apiUrl, path, body, tokens, fetchFn, timeoutMs, {
    refusal,
    parse: parseLabel,
  });
}

export function deleteLabel(
  config: Config,
  assetId: string,
  labelId: string,
  tokens: TokenSource,
  fetchFn: FetchFn,
  timeoutMs: number = TIMEOUT_MS,
): Promise<ApiSendResult<null>> {
  const path = labelPath(assetId, labelId);
  return apiSend("DELETE", config.apiUrl, path, undefined, tokens, fetchFn, timeoutMs, {
    refusal,
    parse: () => null,
  });
}

/** How a label's author reads: the API says only whether it is yours. */
export function authorText(label: Pick<Label, "mine">): string {
  return label.mine ? "you" : "another researcher";
}

export const READ_ONLY_REASON = "Only the author can change or delete a label.";

export interface Draft {
  name: string;
  note: string;
  start: string;
  stop: string;
}

/** Time to millisecond precision, so a dragged span does not carry a dozen digits. */
export function round(seconds: number): number {
  return Math.round(seconds * 1000) / 1000;
}

export function draftOf(label: {
  name?: string;
  note?: string;
  start_s: number;
  stop_s: number;
}): Draft {
  return {
    name: label.name ?? "",
    note: label.note ?? "",
    start: String(round(label.start_s)),
    stop: String(round(label.stop_s)),
  };
}

/** The fields to send, or the message that says what to fix. */
export function validateDraft(
  d: Draft,
): { ok: true; fields: LabelFields } | { ok: false; message: string } {
  if (d.name.trim() === "" || [...d.name].length > NAME_MAX) {
    return { ok: false, message: `Name the label with 1 to ${NAME_MAX} characters.` };
  }
  if (new TextEncoder().encode(d.note).length > NOTE_MAX_BYTES) {
    return { ok: false, message: `The note can be at most ${NOTE_MAX_BYTES} bytes.` };
  }
  const start = d.start.trim() === "" ? Number.NaN : Number(d.start);
  const stop = d.stop.trim() === "" ? Number.NaN : Number(d.stop);
  if (!Number.isFinite(start) || !Number.isFinite(stop) || start < 0 || stop < 0) {
    return { ok: false, message: "Start and stop must be seconds, 0 or more." };
  }
  if (stop < start) return { ok: false, message: "Stop must not be before start." };
  return { ok: true, fields: { name: d.name, note: d.note, start_s: start, stop_s: stop } };
}

export interface LabelAlignOption {
  value: string;
  label: string;
}

/** `label:<id>` for each label, then `label_name:<name>` for each distinct name. */
export function labelAlignOptions(labels: readonly Label[]): LabelAlignOption[] {
  const out: LabelAlignOption[] = labels.map((l) => ({
    value: `label:${l.label_id}`,
    label: `Label: ${l.name}`,
  }));
  for (const n of new Set(labels.map((l) => l.name))) {
    out.push({ value: `label_name:${n}`, label: `All labels named ${n}` });
  }
  return out;
}
