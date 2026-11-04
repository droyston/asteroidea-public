// Client-side facets over the index.
import type { Dataset } from "../index/schema";

export const NOT_STATED = "not_stated";
export const UNMAPPED = "unmapped";

/** One canonical display label per taxon, in the DANDI form. */
export const TAXON_LABELS: Readonly<Record<string, string>> = {
  "NCBITaxon:9606": "Homo sapiens - Human",
  "NCBITaxon:10090": "Mus musculus - House mouse",
  "NCBITaxon:10116": "Rattus norvegicus - Norway rat",
  "NCBITaxon:9544": "Macaca mulatta - Rhesus monkey",
};

export type MultiFacet = "source" | "species" | "modality" | "standard" | "licence";
export const MULTI_FACETS: readonly MultiFacet[] = [
  "source",
  "species",
  "modality",
  "standard",
  "licence",
];

export interface Range {
  min: number | null;
  max: number | null;
}

export interface Filters {
  text: string;
  multi: Record<MultiFacet, ReadonlySet<string>>;
  hasUnits: boolean;
  hasLfp: boolean;
  bytes: Range;
  subjects: Range;
}

export function emptyFilters(): Filters {
  return {
    text: "",
    multi: {
      source: new Set(),
      species: new Set(),
      modality: new Set(),
      standard: new Set(),
      licence: new Set(),
    },
    hasUnits: false,
    hasLfp: false,
    bytes: { min: null, max: null },
    subjects: { min: null, max: null },
  };
}

/** Species facet keys: the taxon when known, else `text:` + lowercased label; `not_stated` alone. */
export function speciesKeys(d: Dataset): string[] {
  if (d.species_status === "not_stated") return [NOT_STATED];
  const keys = d.species.map((s) => s.taxon ?? `text:${s.label.trim().toLowerCase()}`);
  return keys.length > 0 ? [...new Set(keys)] : [NOT_STATED];
}

export function keysFor(facet: MultiFacet, d: Dataset): string[] {
  switch (facet) {
    case "source":
      return [d.source];
    case "species":
      return speciesKeys(d);
    case "modality":
      return d.modalities;
    case "standard":
      return d.standards;
    case "licence":
      return [d.license.spdx ?? UNMAPPED];
  }
}

/** Display labels for species keys, built once from the data (first label seen wins). */
export function speciesLabels(rows: readonly Dataset[]): Map<string, string> {
  const labels = new Map<string, string>([[NOT_STATED, "not stated"]]);
  for (const [taxon, label] of Object.entries(TAXON_LABELS)) labels.set(taxon, label);
  // DANDI labels first so a taxon outside the table shows the DANDI form.
  const ordered = [...rows].sort(
    (a, b) => Number(a.source !== "dandi") - Number(b.source !== "dandi"),
  );
  for (const d of ordered) {
    if (d.species_status === "not_stated") continue;
    for (const s of d.species) {
      const key = s.taxon ?? `text:${s.label.trim().toLowerCase()}`;
      if (!labels.has(key)) labels.set(key, s.label.trim());
    }
  }
  return labels;
}

function inRange(value: number | null, r: Range): boolean {
  if (r.min === null && r.max === null) return true;
  if (value === null) return false;
  return (r.min === null || value >= r.min) && (r.max === null || value <= r.max);
}

function matchesText(d: Dataset, text: string): boolean {
  const needle = text.trim().toLowerCase();
  if (needle === "") return true;
  return d.title.toLowerCase().includes(needle) || d.id.toLowerCase().includes(needle);
}

/** True when `d` passes every filter, ignoring the multi facet named in `skip`. */
export function matches(d: Dataset, f: Filters, skip?: MultiFacet): boolean {
  if (!matchesText(d, f.text)) return false;
  if (f.hasUnits && !d.has_units) return false;
  if (f.hasLfp && !d.has_lfp) return false;
  if (!inRange(d.bytes, f.bytes) || !inRange(d.subjects, f.subjects)) return false;
  for (const facet of MULTI_FACETS) {
    if (facet === skip) continue;
    const chosen = f.multi[facet];
    if (chosen.size > 0 && !keysFor(facet, d).some((k) => chosen.has(k))) return false;
  }
  return true;
}

export function applyFilters(rows: readonly Dataset[], f: Filters): Dataset[] {
  return rows.filter((d) => matches(d, f));
}

export interface FacetValue {
  key: string;
  label: string;
  count: number;
}

/**
 * Values of one multi facet with counts over the rows that pass every other filter.
 * Chosen values are always listed, even at count 0, so they can be unticked.
 */
export function facetValues(
  rows: readonly Dataset[],
  f: Filters,
  facet: MultiFacet,
  labels: ReadonlyMap<string, string>,
): FacetValue[] {
  const counts = new Map<string, number>();
  for (const key of f.multi[facet]) counts.set(key, 0);
  for (const d of rows) {
    if (!matches(d, f, facet)) continue;
    for (const key of new Set(keysFor(facet, d))) counts.set(key, (counts.get(key) ?? 0) + 1);
  }
  return [...counts.entries()]
    .map(([key, count]) => ({
      key,
      count,
      label: facet === "species" ? (labels.get(key) ?? key) : key,
    }))
    .sort((a, b) => b.count - a.count || a.label.localeCompare(b.label));
}

export function toggle(f: Filters, facet: MultiFacet, key: string): Filters {
  const next = new Set(f.multi[facet]);
  if (next.has(key)) next.delete(key);
  else next.add(key);
  return { ...f, multi: { ...f.multi, [facet]: next } };
}

export function formatBytes(n: number | null): string {
  if (n === null) return "—";
  const units = ["B", "KB", "MB", "GB", "TB", "PB"];
  let v = n;
  let u = 0;
  while (v >= 1000 && u < units.length - 1) {
    v /= 1000;
    u += 1;
  }
  return `${v >= 100 || u === 0 ? v.toFixed(0) : v.toFixed(1)} ${units[u]}`;
}
