// Pick list `asteroidea.targets/1`. Exists only in TypeScript. Exactly five keys per
// entry; the guard rejects any other key, so a new field needs a new major version.

export const TARGETS_SCHEMA = "asteroidea.targets/1";
export const TARGET_KEYS = ["source", "dataset_id", "version", "path", "note"] as const;

export type TargetSource = "dandi" | "openneuro";

export interface Target {
  source: TargetSource;
  dataset_id: string;
  version: string;
  path: string | null;
  note: string;
}

export interface TargetsDoc {
  schema: typeof TARGETS_SCHEMA;
  exported_at: string;
  targets: Target[];
}

export class TargetsError extends Error {
  override name = "TargetsError";
}

function describe(v: unknown): string {
  return v === undefined ? "undefined" : JSON.stringify(v);
}

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

/** Validate one entry; throws a TargetsError naming the entry index, key and value found. */
export function parseTarget(raw: unknown, index: number): Target {
  const at = `targets[${index}]`;
  if (!isRecord(raw)) throw new TargetsError(`${at}: expected an object, got ${describe(raw)}`);
  for (const key of Object.keys(raw)) {
    if (!(TARGET_KEYS as readonly string[]).includes(key)) {
      throw new TargetsError(
        `${at}: unexpected key ${JSON.stringify(key)} (value ${describe(raw[key])})`,
      );
    }
  }
  const { source, dataset_id, version, path, note } = raw;
  if (source !== "dandi" && source !== "openneuro") {
    throw new TargetsError(
      `${at}: key "source" expected "dandi" or "openneuro", got ${describe(source)}`,
    );
  }
  if (typeof dataset_id !== "string" || dataset_id === "") {
    throw new TargetsError(
      `${at}: key "dataset_id" expected a non-empty string, got ${describe(dataset_id)}`,
    );
  }
  if (typeof version !== "string" || version === "") {
    throw new TargetsError(
      `${at}: key "version" expected a non-empty string, got ${describe(version)}`,
    );
  }
  if (path !== null && (typeof path !== "string" || path === "")) {
    throw new TargetsError(
      `${at}: key "path" expected null or a non-empty string, got ${describe(path)}`,
    );
  }
  if (typeof note !== "string") {
    throw new TargetsError(`${at}: key "note" expected a string, got ${describe(note)}`);
  }
  return { source, dataset_id, version, path, note };
}

/** Validate a whole document; the first problem rejects it. */
export function parseTargets(raw: unknown): TargetsDoc {
  if (!isRecord(raw)) throw new TargetsError(`expected an object, got ${describe(raw)}`);
  if (raw.schema !== TARGETS_SCHEMA) {
    throw new TargetsError(
      `unsupported schema string ${describe(raw.schema)}, expected ${JSON.stringify(TARGETS_SCHEMA)}`,
    );
  }
  if (typeof raw.exported_at !== "string") {
    throw new TargetsError(`key "exported_at" expected a string, got ${describe(raw.exported_at)}`);
  }
  if (!Array.isArray(raw.targets)) {
    throw new TargetsError(`key "targets" expected an array, got ${describe(raw.targets)}`);
  }
  return {
    schema: TARGETS_SCHEMA,
    exported_at: raw.exported_at,
    targets: raw.targets.map((t, i) => parseTarget(t, i)),
  };
}

/** Identity of a pick: the same (source, dataset_id, version, path) twice is "already picked". */
export function targetKey(t: Pick<Target, "source" | "dataset_id" | "version" | "path">): string {
  return JSON.stringify([t.source, t.dataset_id, t.version, t.path]);
}
