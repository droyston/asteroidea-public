// Index schema `asteroidea.dataset-index/2`. The Python writer is
// services/src/asteroidea_services/harvest/model.py; the golden index gates drift.

export const INDEX_SCHEMA = "asteroidea.dataset-index/2";

export type SourceName = "dandi" | "openneuro";
export type SpeciesStatus = "stated" | "not_stated";

export interface Species {
  taxon: string | null;
  label: string;
}

export interface License {
  spdx: string | null;
  raw: string | null;
}

export interface Dataset {
  source: SourceName;
  id: string;
  version: string;
  version_ref: string | null;
  version_modified: string | null;
  title: string;
  species: Species[];
  species_status: SpeciesStatus;
  modalities: string[];
  variables: string[];
  has_units: boolean;
  has_lfp: boolean;
  has_ephys_series: boolean;
  approach: string[];
  techniques: string[];
  subjects: number | null;
  bytes: number | null;
  files: number | null;
  license: License;
  standards: string[];
  urls: { landing: string; api: string };
  published_at: string | null;
  deid: { affirmed_defaced: boolean | null };
  harvested_at: string;
}

export interface Failure {
  id: string;
  version: string | null;
  url: string;
  error_type: string;
  message: string;
  attempts: number;
  stale: boolean;
}

export interface SourceSummary {
  listed: number;
  ok: number;
  seconds: number;
  failed: Failure[];
  null_nodes?: number;
}

export interface Index {
  schema: typeof INDEX_SCHEMA;
  harvested_at: string;
  harvester: { package: string; version: string };
  sources: Partial<Record<SourceName, SourceSummary>>;
  datasets: Dataset[];
}

export class SchemaError extends Error {
  override name = "SchemaError";
}

type Obj = Record<string, unknown>;

function describe(value: unknown): string {
  if (value === undefined) return "undefined";
  if (value === null) return "null";
  if (Array.isArray(value)) return "array";
  if (typeof value === "string") return `string ${JSON.stringify(value.slice(0, 60))}`;
  return typeof value;
}

class Checker {
  constructor(private readonly where: string) {}

  fail(field: string, expected: string, value: unknown): never {
    throw new SchemaError(
      `${this.where}: field ${field} expected ${expected}, got ${describe(value)}`,
    );
  }

  obj(value: unknown, field: string): Obj {
    if (typeof value !== "object" || value === null || Array.isArray(value)) {
      this.fail(field, "object", value);
    }
    return value as Obj;
  }

  str(o: Obj, key: string, prefix = ""): string {
    const v = o[key];
    if (typeof v !== "string") this.fail(prefix + key, "string", v);
    return v;
  }

  strOrNull(o: Obj, key: string, prefix = ""): string | null {
    const v = o[key];
    if (v !== null && typeof v !== "string") this.fail(prefix + key, "string or null", v);
    return v;
  }

  bool(o: Obj, key: string): boolean {
    const v = o[key];
    if (typeof v !== "boolean") this.fail(key, "boolean", v);
    return v;
  }

  boolIn(o: Obj, key: string, prefix: string): boolean {
    const v = o[key];
    if (typeof v !== "boolean") this.fail(prefix + key, "boolean", v);
    return v;
  }

  boolOrNull(o: Obj, key: string, prefix = ""): boolean | null {
    const v = o[key];
    if (v !== null && typeof v !== "boolean") this.fail(prefix + key, "boolean or null", v);
    return v;
  }

  num(o: Obj, key: string, prefix = ""): number {
    const v = o[key];
    if (typeof v !== "number" || !Number.isFinite(v)) this.fail(prefix + key, "number", v);
    return v;
  }

  countOrNull(o: Obj, key: string): number | null {
    const v = o[key];
    if (v === null) return null;
    if (typeof v !== "number" || !Number.isInteger(v) || v < 0) {
      this.fail(key, "non-negative integer or null", v);
    }
    return v;
  }

  arr(o: Obj, key: string): unknown[] {
    const v = o[key];
    if (!Array.isArray(v)) this.fail(key, "array", v);
    return v;
  }

  strings(o: Obj, key: string): string[] {
    const v = o[key];
    if (!Array.isArray(v)) this.fail(key, "array of strings", v);
    v.forEach((item, i) => {
      if (typeof item !== "string") this.fail(`${key}[${i}]`, "string", item);
    });
    return v as string[];
  }

  oneOf<T extends string>(o: Obj, key: string, allowed: readonly T[]): T {
    const v = o[key];
    if (typeof v !== "string" || !(allowed as readonly string[]).includes(v)) {
      this.fail(key, allowed.map((a) => JSON.stringify(a)).join(" | "), v);
    }
    return v as T;
  }
}

const SOURCES = ["dandi", "openneuro"] as const;
const STATUSES = ["stated", "not_stated"] as const;

function parseDataset(value: unknown, i: number): Dataset {
  const o = new Checker(`index record ${i}`).obj(value, "record");
  const named =
    typeof o.source === "string" && typeof o.id === "string" ? ` (${o.source} ${o.id})` : "";
  const c = new Checker(`index record ${i}${named}`);

  const species = c.arr(o, "species").map((s, j) => {
    const so = c.obj(s, `species[${j}]`);
    return {
      taxon: c.strOrNull(so, "taxon", `species[${j}].`),
      label: c.str(so, "label", `species[${j}].`),
    };
  });
  const license = c.obj(o.license, "license");
  const urls = c.obj(o.urls, "urls");
  const deid = c.obj(o.deid, "deid");

  return {
    source: c.oneOf(o, "source", SOURCES),
    id: c.str(o, "id"),
    version: c.str(o, "version"),
    version_ref: c.strOrNull(o, "version_ref"),
    version_modified: c.strOrNull(o, "version_modified"),
    title: c.str(o, "title"),
    species,
    species_status: c.oneOf(o, "species_status", STATUSES),
    modalities: c.strings(o, "modalities"),
    variables: c.strings(o, "variables"),
    has_units: c.bool(o, "has_units"),
    has_lfp: c.bool(o, "has_lfp"),
    has_ephys_series: c.bool(o, "has_ephys_series"),
    approach: c.strings(o, "approach"),
    techniques: c.strings(o, "techniques"),
    subjects: c.countOrNull(o, "subjects"),
    bytes: c.countOrNull(o, "bytes"),
    files: c.countOrNull(o, "files"),
    license: {
      spdx: c.strOrNull(license, "spdx", "license."),
      raw: c.strOrNull(license, "raw", "license."),
    },
    standards: c.strings(o, "standards"),
    urls: { landing: c.str(urls, "landing", "urls."), api: c.str(urls, "api", "urls.") },
    published_at: c.strOrNull(o, "published_at"),
    deid: { affirmed_defaced: c.boolOrNull(deid, "affirmed_defaced", "deid.") },
    harvested_at: c.str(o, "harvested_at"),
  };
}

function parseSummary(value: unknown, name: string): SourceSummary {
  const c = new Checker(`index sources.${name}`);
  const o = c.obj(value, name);
  const failed = c.arr(o, "failed").map((f, j) => {
    const fo = c.obj(f, `failed[${j}]`);
    const p = `failed[${j}].`;
    return {
      id: c.str(fo, "id", p),
      version: c.strOrNull(fo, "version", p),
      url: c.str(fo, "url", p),
      error_type: c.str(fo, "error_type", p),
      message: c.str(fo, "message", p),
      attempts: c.num(fo, "attempts", p),
      stale: c.boolIn(fo, "stale", p),
    };
  });
  const summary: SourceSummary = {
    listed: c.num(o, "listed"),
    ok: c.num(o, "ok"),
    seconds: c.num(o, "seconds"),
    failed,
  };
  if (o.null_nodes !== undefined) summary.null_nodes = c.num(o, "null_nodes");
  return summary;
}

/** Validate a parsed index document; throws SchemaError naming the record, field and value. */
export function parseIndex(value: unknown, file = "index.json"): Index {
  const c = new Checker(file);
  const o = c.obj(value, "document");
  if (o.schema !== INDEX_SCHEMA) {
    throw new SchemaError(
      `${file}: unsupported schema ${describe(o.schema)}; this page reads ${INDEX_SCHEMA}`,
    );
  }
  const harvester = c.obj(o.harvester, "harvester");
  const sourcesRaw = c.obj(o.sources, "sources");
  const sources: Partial<Record<SourceName, SourceSummary>> = {};
  for (const name of SOURCES) {
    if (sourcesRaw[name] !== undefined) sources[name] = parseSummary(sourcesRaw[name], name);
  }
  const datasets = c.arr(o, "datasets").map(parseDataset);
  return {
    schema: INDEX_SCHEMA,
    harvested_at: c.str(o, "harvested_at"),
    harvester: {
      package: c.str(harvester, "package", "harvester."),
      version: c.str(harvester, "version", "harvester."),
    },
    sources,
    datasets,
  };
}
