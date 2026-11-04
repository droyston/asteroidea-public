// CATALOG_FIXTURE: the one fixture every artboard surface draws.
// Frozen from a live catalog listing and the rates body recorded for CS62 R2;
// the awkward states the live catalog lacks are listed in `synthetic_datasets` or marked "Synthetic".
// No clock and no randomness: the 152-unit rates body is a fixed formula. `fixture.test.ts` pins the
// counts, so an edit here is a reviewed change. The browser run serves the same files with page.route.

import units160Json from "../../e2e/fixtures/synthesis-units-160.json" with { type: "json" };
import catalogJson from "../../e2e/fixtures/ux/catalog.json" with { type: "json" };
import ratesJson from "../../e2e/fixtures/ux/rates-cs62-scene_cut-display.json" with {
  type: "json",
};
import unitsJson from "../../e2e/fixtures/ux/units-cs62-r2.json" with { type: "json" };

export interface NwbAsset {
  readonly asset_id: string;
  readonly n_units: number;
}

export interface Session {
  readonly session: string;
  readonly asset_ids: readonly string[];
  readonly nwb: readonly NwbAsset[];
}

export interface Subject {
  readonly subject: string;
  readonly species: string | null;
  readonly modalities: readonly string[];
  readonly sessions: readonly Session[];
  readonly units: number;
  readonly trials: number | null;
}

export interface Region {
  readonly region: string;
  readonly units: number;
  readonly channels: number;
}

export interface Dataset {
  readonly dataset: string;
  readonly title: string;
  readonly tier: string;
  readonly ingest_method: string;
  readonly mine: boolean;
  readonly source_archive: string | null;
  readonly source_dataset: string | null;
  readonly source_version: string | null;
  readonly license: string | null;
  readonly doi: string | null;
  readonly asset_count: number | null;
  readonly subject_count: number | null;
  readonly total_bytes: number | null;
  readonly created_at: string;
  readonly subjects: readonly Subject[];
  readonly regions: readonly Region[];
}

export interface Study {
  readonly study: string;
  readonly title: string;
  readonly authors: string | null;
  readonly citation: string | null;
  readonly doi: string | null;
  readonly datasets: readonly { readonly dataset: string; readonly role: string }[];
}

export interface Link {
  readonly dataset: string;
  readonly subject: string;
  readonly subject_key: string;
  readonly status: string;
  readonly linked_by: string | null;
  readonly linked_at: string | null;
}

export interface StudiesBody {
  readonly studies: readonly Study[];
  readonly datasets: readonly Dataset[];
  readonly query_ms: number;
}

export interface RatesUnit {
  readonly unit_index: number;
  readonly rate_hz: readonly number[];
  readonly z: readonly number[] | null;
}

export interface RatesBody {
  readonly edges: readonly number[];
  readonly units: readonly RatesUnit[];
  readonly n_trials: number;
  readonly align: string;
  readonly defaults: { readonly baseline: string; readonly response_window_s: readonly number[] };
  readonly responsive: readonly { readonly unit_index: number; readonly location: string }[];
}

export interface UnitRow {
  readonly unit_index: number;
  readonly location: string;
}

export interface Catalog {
  readonly name: string;
  readonly live_dataset_count: number;
  readonly synthetic_datasets: readonly string[];
  readonly variants: { readonly mine_two: readonly Dataset[] };
  readonly session: {
    readonly study: string;
    readonly dataset: string;
    readonly subject: string;
    readonly session: string;
    readonly asset_id: string;
    readonly event: string;
    readonly n_trials: number;
    readonly null_z_unit: number;
  };
  readonly studies: StudiesBody;
  readonly links: { readonly links: readonly Link[] };
}

export const CATALOG_FIXTURE: Catalog = catalogJson;

/** The recorded body (CS62 R2, scene cuts, display parameters) with one unit's `z` null. */
export const RATES_CS62: RatesBody = ratesJson;

/** The units table for CS62 R2: live unit indices and location labels (from the recorded `responsive`). */
export const UNITS_CS62: { readonly rows: readonly UnitRow[] } = unitsJson;

/** The 152-unit surface: the first 152 rows of `synthesis-units-160.json` (MAX_UNITS). */
export const UNITS_152: { readonly rows: readonly UnitRow[] } = {
  rows: units160Json.rows.slice(0, 152),
};

/** A synthetic rates body of the recorded body's shape for the 152 units: a fixed formula, no randomness. */
export function rates152(): RatesBody {
  const edges = RATES_CS62.edges;
  const bins = edges.length - 1;
  const units = UNITS_152.rows.map((u) => {
    const z = Array.from({ length: bins }, (_, b) => (((u.unit_index * 7 + b * 3) % 11) - 5) / 2.5);
    return { unit_index: u.unit_index, rate_hz: z.map((v) => 5 + v), z };
  });
  return {
    ...RATES_CS62,
    units,
    responsive: UNITS_152.rows.map((u) => ({ unit_index: u.unit_index, location: u.location })),
  };
}

/** The datasets body today's pages read (`GET /catalog/datasets`), derived from the studies body. */
export function datasetsListing(datasets: readonly Dataset[]): unknown {
  return {
    datasets: datasets.map((d) => ({
      dataset: d.dataset,
      title: d.title,
      license: d.license,
      doi: d.doi,
      source_archive: d.source_archive,
      source_dataset: d.source_dataset,
      source_version: d.source_version,
      ingest_method: d.ingest_method,
      asset_count: d.asset_count ?? 0,
      subject_count: d.subject_count ?? 0,
      total_bytes: d.total_bytes ?? 0,
    })),
    query_ms: CATALOG_FIXTURE.studies.query_ms,
  };
}
