// One GET /artifacts listing row and its parser, apart from results.ts so the home's Latest
// results card parses the listing without the Results place's fetchers (the home chunk's budget).
// Shapes follow ingest/.../artifacts.py.

export interface Artifact {
  artifact_id: string;
  name: string;
  size: number;
  state: string;
  created_at: number;
  /** Present on the All listing and on `input` rows. */
  author_email: string | null;
  /** The catalog assets the result used (the server sends the first 100); [] when absent. */
  source_asset_ids: readonly string[];
  /** How many assets it used in all. */
  source_asset_count: number;
  /** The record's media type, or null when the row does not carry one. */
  content_type: string | null;
  /** The run that saved it; null for a version 1 record, which has no run. */
  run_id: string | null;
  /** The analysis that saved it (the record's `run.function`); null for a version 1 record. */
  run_function: string | null;
}

export function rec(v: unknown, what: string): Record<string, unknown> {
  if (typeof v !== "object" || v === null || Array.isArray(v)) {
    throw new Error(`${what} is not an object`);
  }
  return v as Record<string, unknown>;
}

export function str(o: Record<string, unknown>, key: string, what: string): string {
  const v = o[key];
  if (typeof v !== "string") throw new Error(`${what}.${key} is not a string`);
  return v;
}

export function optStr(o: Record<string, unknown>, key: string): string | null {
  const v = o[key];
  return typeof v === "string" && v !== "" ? v : null;
}

export function items(o: Record<string, unknown>, key: string): unknown[] {
  const v = o[key];
  return Array.isArray(v) ? v : [];
}

export function parseArtifact(raw: unknown, at = "artifact"): Artifact {
  const o = rec(raw, at);
  const { size, created_at } = o;
  if (typeof size !== "number" || typeof created_at !== "number") {
    throw new Error(`${at} does not match the GET /artifacts contract`);
  }
  return {
    artifact_id: str(o, "artifact_id", at),
    name: str(o, "name", at),
    size,
    state: str(o, "state", at),
    created_at,
    author_email: optStr(o, "author_email"),
    source_asset_ids: items(o, "source_asset_ids").filter(
      (v): v is string => typeof v === "string",
    ),
    source_asset_count: typeof o.source_asset_count === "number" ? o.source_asset_count : 0,
    content_type: optStr(o, "content_type"),
    run_id: optStr(o, "run_id"),
    run_function: optStr(o, "run_function"),
  };
}

export function parseArtifacts(body: unknown): readonly Artifact[] {
  const rows = rec(body, "the response").artifacts;
  if (!Array.isArray(rows)) throw new Error('the response has no "artifacts" list');
  return rows.map((row: unknown, i) => parseArtifact(row, `artifacts[${i}]`));
}
