// The Find page's model: the species by modality matrix over the whole index,
// the facets with their distribution bars, the best fits, the filters and their form in the URL, and the
// join of index ids to the catalog. Pure functions over `Dataset[]`: the view and the artboard share them.
import type { CatalogDataset } from "../catalog/catalog";
import type { Dataset } from "../index/schema";
import { keysFor, type Range } from "../table/facets";

export type Column = "units" | "lfp" | "ieeg" | "anat" | "func" | "beh";
export const COLUMNS: readonly { key: Column; label: string }[] = [
  { key: "units", label: "Units" },
  { key: "lfp", label: "LFP" },
  { key: "ieeg", label: "iEEG" },
  { key: "anat", label: "Anat MRI" },
  { key: "func", label: "fMRI" },
  { key: "beh", label: "Behaviour" },
];

export type SpeciesRow = "human" | "mouse" | "rat" | "macaque" | "other";
export const SPECIES: readonly { key: SpeciesRow; label: string }[] = [
  { key: "human", label: "Human" },
  { key: "mouse", label: "Mouse" },
  { key: "rat", label: "Rat" },
  { key: "macaque", label: "Macaque" },
  { key: "other", label: "Other / unstated" },
];

export type Facet = "source" | "standard" | "licence";
export const FACETS: readonly { key: Facet; label: string }[] = [
  { key: "source", label: "Archive" },
  { key: "standard", label: "Standard" },
  { key: "licence", label: "Licence" },
];

export interface Bin {
  label: string;
  min: number | null;
  max: number | null;
}
export const SUBJECT_BINS: readonly Bin[] = [
  { label: "0-1", min: 0, max: 1 },
  { label: "2-4", min: 2, max: 4 },
  { label: "5-19", min: 5, max: 19 },
  { label: "20-99", min: 20, max: 99 },
  { label: "100+", min: 100, max: null },
];
const MB = 1e6;
const GB = 1e9;
const TB = 1e12;
export const SIZE_BINS: readonly Bin[] = [
  { label: "<100 MB", min: 0, max: 100 * MB - 1 },
  { label: "100 MB-1 GB", min: 100 * MB, max: GB - 1 },
  { label: "1-10 GB", min: GB, max: 10 * GB - 1 },
  { label: "10-100 GB", min: 10 * GB, max: 100 * GB - 1 },
  { label: "100 GB-1 TB", min: 100 * GB, max: TB - 1 },
  { label: "1 TB+", min: TB, max: null },
];

export interface FindFilters {
  text: string;
  species: ReadonlySet<SpeciesRow>;
  has: ReadonlySet<Column>;
  source: ReadonlySet<string>;
  standard: ReadonlySet<string>;
  licence: ReadonlySet<string>;
  subjects: Range;
  bytes: Range;
}

export function emptyFind(): FindFilters {
  return {
    text: "",
    species: new Set(),
    has: new Set(),
    source: new Set(),
    standard: new Set(),
    licence: new Set(),
    subjects: { min: null, max: null },
    bytes: { min: null, max: null },
  };
}

const TAXON_ROW: Readonly<Record<string, SpeciesRow>> = {
  "NCBITaxon:9606": "human",
  "NCBITaxon:10090": "mouse",
  "NCBITaxon:10116": "rat",
  "NCBITaxon:9544": "macaque",
};

function rowOfLabel(label: string): SpeciesRow {
  const t = label.toLowerCase();
  if (/human|homo sapiens/.test(t)) return "human";
  if (/mouse|mus musculus/.test(t)) return "mouse";
  if (/\brat\b|rattus/.test(t)) return "rat";
  if (/macaca|macaque|rhesus/.test(t)) return "macaque";
  return "other";
}

/** The matrix rows a dataset belongs to: by taxon when stated, else by its label; unstated is "other". */
export function speciesRows(d: Dataset): Set<SpeciesRow> {
  if (d.species_status === "not_stated" || d.species.length === 0) return new Set(["other"]);
  return new Set(
    d.species.map(
      (s) => (s.taxon === null ? undefined : TAXON_ROW[s.taxon]) ?? rowOfLabel(s.label),
    ),
  );
}

const MOD_COLUMN: Readonly<Record<string, Column>> = {
  ieeg: "ieeg",
  mri: "anat",
  mri_structural: "anat",
  mri_functional: "func",
  behavior: "beh",
  beh: "beh",
};

/** The matrix columns a dataset has. */
export function columnsOf(d: Dataset): Set<Column> {
  const out = new Set<Column>();
  if (d.has_units) out.add("units");
  if (d.has_lfp) out.add("lfp");
  for (const m of d.modalities) {
    const c = MOD_COLUMN[m];
    if (c !== undefined) out.add(c);
  }
  return out;
}

function inRange(value: number | null, r: Range): boolean {
  if (r.min === null && r.max === null) return true;
  if (value === null) return false;
  return (r.min === null || value >= r.min) && (r.max === null || value <= r.max);
}

type Skip = "species" | "has" | Facet | "subjects" | "bytes";

function needleMatch(d: Dataset, text: string): boolean {
  const n = text.trim().toLowerCase();
  return n === "" || d.title.toLowerCase().includes(n) || d.id.toLowerCase().includes(n);
}

/** True when `d` passes every filter except the one named in `skip`. */
export function matchesFind(d: Dataset, f: FindFilters, skip?: Skip): boolean {
  if (!needleMatch(d, f.text)) return false;
  if (skip !== "species" && f.species.size > 0) {
    const mine = speciesRows(d);
    if (![...f.species].some((s) => mine.has(s))) return false;
  }
  if (skip !== "has" && f.has.size > 0) {
    const mine = columnsOf(d);
    if (![...f.has].every((c) => mine.has(c))) return false;
  }
  if (skip !== "subjects" && !inRange(d.subjects, f.subjects)) return false;
  if (skip !== "bytes" && !inRange(d.bytes, f.bytes)) return false;
  for (const facet of ["source", "standard", "licence"] as const) {
    if (skip === facet || f[facet].size === 0) continue;
    if (!keysFor(facet, d).some((k) => f[facet].has(k))) return false;
  }
  return true;
}

export function applyFind(rows: readonly Dataset[], f: FindFilters): Dataset[] {
  return rows.filter((d) => matchesFind(d, f));
}

/** Counts per `"<species>|<column>"` over the whole index (a dataset counts in each row and column it has). */
export function matrix(rows: readonly Dataset[]): Map<string, number> {
  const out = new Map<string, number>();
  for (const d of rows) {
    const cols = columnsOf(d);
    for (const s of speciesRows(d)) {
      for (const c of cols) out.set(`${s}|${c}`, (out.get(`${s}|${c}`) ?? 0) + 1);
    }
  }
  return out;
}

export interface FacetCount {
  key: string;
  count: number;
}

/** The values of one facet with counts over the rows passing every other filter; chosen values stay listed. */
export function facetCounts(rows: readonly Dataset[], f: FindFilters, facet: Facet): FacetCount[] {
  const counts = new Map<string, number>();
  for (const key of f[facet]) counts.set(key, 0);
  for (const d of rows) {
    if (!matchesFind(d, f, facet)) continue;
    for (const key of new Set(keysFor(facet, d))) counts.set(key, (counts.get(key) ?? 0) + 1);
  }
  return [...counts.entries()]
    .map(([key, count]) => ({ key, count }))
    .sort((a, b) => b.count - a.count || a.key.localeCompare(b.key));
}

/** Counts per column over the rows passing every other filter (the "Contains" facet). */
export function containsCounts(rows: readonly Dataset[], f: FindFilters): Map<Column, number> {
  const out = new Map<Column, number>();
  for (const d of rows) {
    if (!matchesFind(d, f, "has")) continue;
    for (const c of columnsOf(d)) out.set(c, (out.get(c) ?? 0) + 1);
  }
  return out;
}

/** Counts per species row over the rows passing every other filter. */
export function speciesCounts(rows: readonly Dataset[], f: FindFilters): Map<SpeciesRow, number> {
  const out = new Map<SpeciesRow, number>();
  for (const d of rows) {
    if (!matchesFind(d, f, "species")) continue;
    for (const s of speciesRows(d)) out.set(s, (out.get(s) ?? 0) + 1);
  }
  return out;
}

/** Rows per bin over the rows passing every other filter; a row with no value is in no bin. */
export function histogram(
  rows: readonly Dataset[],
  f: FindFilters,
  which: "subjects" | "bytes",
  bins: readonly Bin[],
): number[] {
  const out = bins.map(() => 0);
  for (const d of rows) {
    if (!matchesFind(d, f, which)) continue;
    const v = d[which];
    if (v === null) continue;
    const i = bins.findIndex((b) => inRange(v, { min: b.min, max: b.max }));
    if (i >= 0) out[i] = (out[i] ?? 0) + 1;
  }
  return out;
}

function fitScore(d: Dataset, f: FindFilters): number {
  const n = f.text.trim().toLowerCase();
  let s = 0;
  if (n !== "" && d.title.toLowerCase().startsWith(n)) s += 3;
  else if (n !== "" && d.title.toLowerCase().includes(n)) s += 2;
  if (d.has_units) s += 2;
  if (d.has_lfp) s += 1;
  if (d.species_status === "stated") s += 1;
  return s;
}

/** The `n` best fits among the matches: text in the title, then recorded units and LFP, then more participants. */
export function bestFit(rows: readonly Dataset[], f: FindFilters, n: number): Dataset[] {
  return applyFind(rows, f)
    .sort(
      (a, b) =>
        fitScore(b, f) - fitScore(a, f) ||
        (b.subjects ?? 0) - (a.subjects ?? 0) ||
        a.id.localeCompare(b.id),
    )
    .slice(0, n);
}

/** `"<archive>:<id>"` of an index dataset, the join key to the catalog's source fields. */
export const joinKey = (d: Pick<Dataset, "source" | "id">): string => `${d.source}:${d.id}`;

/** Index id to catalog dataset id, over the catalog's `source_archive` and `source_dataset`. */
export function catalogJoin(datasets: readonly CatalogDataset[]): Map<string, string> {
  const out = new Map<string, string>();
  for (const c of datasets) {
    if (c.source_archive !== null && c.source_dataset !== null) {
      out.set(`${c.source_archive}:${c.source_dataset}`, c.dataset);
    }
  }
  return out;
}

const SPECIES_KEYS = new Set<string>(SPECIES.map((s) => s.key));
const COLUMN_KEYS = new Set<string>(COLUMNS.map((c) => c.key));

function rangeText(r: Range): string {
  return r.min === null && r.max === null ? "" : `${r.min ?? ""}-${r.max ?? ""}`;
}

function parseRange(text: string | null): Range {
  const m = /^(\d*)-(\d*)$/.exec(text ?? "");
  if (m === null || (m[1] === "" && m[2] === "")) return { min: null, max: null };
  return { min: m[1] === "" ? null : Number(m[1]), max: m[2] === "" ? null : Number(m[2]) };
}

function list(p: URLSearchParams, key: string): string[] {
  return (p.get(key) ?? "").split(",").filter((s) => s !== "");
}

/** The filters a query string names; an unknown key or value is ignored. */
export function filtersFromSearch(search: string): FindFilters {
  const p = new URLSearchParams(search);
  return {
    text: p.get("q") ?? "",
    species: new Set(list(p, "sp").filter((s): s is SpeciesRow => SPECIES_KEYS.has(s))),
    has: new Set(list(p, "has").filter((s): s is Column => COLUMN_KEYS.has(s))),
    source: new Set(list(p, "src")),
    standard: new Set(list(p, "std")),
    licence: new Set(list(p, "lic")),
    subjects: parseRange(p.get("subj")),
    bytes: parseRange(p.get("size")),
  };
}

/** The query string (with its `?`, or "") that names the filters. */
export function filtersToSearch(f: FindFilters): string {
  const p = new URLSearchParams();
  if (f.text.trim() !== "") p.set("q", f.text);
  const put = (key: string, set: ReadonlySet<string>) => {
    if (set.size > 0) p.set(key, [...set].join(","));
  };
  put("sp", f.species);
  put("has", f.has);
  put("src", f.source);
  put("std", f.standard);
  put("lic", f.licence);
  if (rangeText(f.subjects) !== "") p.set("subj", rangeText(f.subjects));
  if (rangeText(f.bytes) !== "") p.set("size", rangeText(f.bytes));
  const text = p.toString();
  return text === "" ? "" : `?${text}`;
}
