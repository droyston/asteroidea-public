// The data home's two reads: `GET /catalog/studies` (studies, their datasets, subjects
// and regions) and `GET /catalog/links` (participant links with status). Each goes through `apiGet`
// (via `catalog.ts :: get`); a refusal shows the server's own sentence. The parsers keep only the fields
// the home draws and refuse a body whose shape is wrong, so a drifting API shows as a message, not a
// blank page.
import type { FetchFn, TokenSource } from "../auth/apiGet";
import { TIMEOUT_MS } from "../auth/apiGet";
import type { AuthConfig } from "../auth/config";
import { type CatalogResult, get } from "../catalog/catalog";

export interface HomeRegion {
  readonly region: string;
  readonly units: number;
  readonly channels: number;
}

export interface HomeSubject {
  readonly subject: string;
  readonly species: string | null;
  /** An open set: `units`, `lfp`, `ieeg`, `anat`, `func`, `task`, any other name, or `other:<name>`. */
  readonly modalities: readonly string[];
  readonly units: number;
}

export interface HomeDataset {
  readonly dataset: string;
  readonly title: string | null;
  /** `curated`, `upload` or `sandbox`. */
  readonly tier: string;
  readonly mine: boolean;
  readonly source_archive: string | null;
  readonly source_dataset: string | null;
  readonly source_version: string | null;
  readonly license: string | null;
  readonly created_at: string | null;
  readonly subjects: readonly HomeSubject[];
  readonly regions: readonly HomeRegion[];
}

export interface HomeStudy {
  readonly study: string;
  readonly title: string;
  readonly datasets: readonly { readonly dataset: string; readonly role: string }[];
}

export interface HomeLink {
  readonly dataset: string;
  readonly subject: string;
  readonly subject_key: string;
  /** `pending` or `confirmed`; anything else reads as pending. */
  readonly status: string;
}

export interface HomeBody {
  readonly studies: readonly HomeStudy[];
  readonly datasets: readonly HomeDataset[];
  readonly links: readonly HomeLink[];
  readonly query_ms: number;
}

type Rec = Record<string, unknown>;

function rec(v: unknown, name: string): Rec {
  if (typeof v !== "object" || v === null || Array.isArray(v)) {
    throw new Error(`${name} is not an object`);
  }
  return v as Rec;
}

function arr(v: unknown, name: string): unknown[] {
  if (!Array.isArray(v)) throw new Error(`${name} is not a list`);
  return v;
}

function str(v: unknown, name: string): string {
  if (typeof v !== "string") throw new Error(`${name} is not a string`);
  return v;
}

function strOrNull(v: unknown, name: string): string | null {
  return v === null || v === undefined ? null : str(v, name);
}

function num(v: unknown, name: string): number {
  if (typeof v !== "number") throw new Error(`${name} is not a number`);
  return v;
}

function numOr0(v: unknown, name: string): number {
  return v === null || v === undefined ? 0 : num(v, name);
}

function subject(v: unknown, at: string): HomeSubject {
  const r = rec(v, at);
  return {
    subject: str(r.subject, `${at}.subject`),
    species: strOrNull(r.species, `${at}.species`),
    modalities: arr(r.modalities, `${at}.modalities`).map((m, i) =>
      str(m, `${at}.modalities[${i}]`),
    ),
    units: numOr0(r.units, `${at}.units`),
  };
}

function dataset(v: unknown, at: string): HomeDataset {
  const r = rec(v, at);
  return {
    dataset: str(r.dataset, `${at}.dataset`),
    title: strOrNull(r.title, `${at}.title`),
    tier: str(r.tier, `${at}.tier`),
    mine: r.mine === true,
    source_archive: strOrNull(r.source_archive, `${at}.source_archive`),
    source_dataset: strOrNull(r.source_dataset, `${at}.source_dataset`),
    source_version: strOrNull(r.source_version, `${at}.source_version`),
    license: strOrNull(r.license, `${at}.license`),
    created_at: strOrNull(r.created_at, `${at}.created_at`),
    subjects: arr(r.subjects, `${at}.subjects`).map((s, i) => subject(s, `${at}.subjects[${i}]`)),
    regions: arr(r.regions, `${at}.regions`).map((g, i) => {
      const x = rec(g, `${at}.regions[${i}]`);
      return {
        region: str(x.region, `${at}.regions[${i}].region`),
        units: numOr0(x.units, `${at}.regions[${i}].units`),
        channels: numOr0(x.channels, `${at}.regions[${i}].channels`),
      };
    }),
  };
}

function study(v: unknown, at: string): HomeStudy {
  const r = rec(v, at);
  return {
    study: str(r.study, `${at}.study`),
    title: str(r.title, `${at}.title`),
    datasets: arr(r.datasets, `${at}.datasets`).map((d, i) => {
      const x = rec(d, `${at}.datasets[${i}]`);
      return {
        dataset: str(x.dataset, `${at}.datasets[${i}].dataset`),
        role: strOrNull(x.role, `${at}.datasets[${i}].role`) ?? "",
      };
    }),
  };
}

/** The two bodies as the home draws them. */
export function parseHome(studiesBody: unknown, linksBody: unknown): HomeBody {
  const s = rec(studiesBody, "the studies response");
  const l = rec(linksBody, "the links response");
  return {
    studies: arr(s.studies, "studies").map((x, i) => study(x, `studies[${i}]`)),
    datasets: arr(s.datasets, "datasets").map((x, i) => dataset(x, `datasets[${i}]`)),
    links: arr(l.links, "links").map((x, i) => {
      const r = rec(x, `links[${i}]`);
      return {
        dataset: str(r.dataset, `links[${i}].dataset`),
        subject: str(r.subject, `links[${i}].subject`),
        subject_key: str(r.subject_key, `links[${i}].subject_key`),
        status: strOrNull(r.status, `links[${i}].status`) ?? "pending",
      };
    }),
    query_ms: numOr0(s.query_ms, "query_ms"),
  };
}

/** Read both routes together; the first refusal (or an ended session) is the result. */
export async function loadHome(
  config: Pick<AuthConfig, "apiUrl">,
  tokens: TokenSource,
  fetchFn: FetchFn,
  timeoutMs: number = TIMEOUT_MS,
): Promise<CatalogResult<HomeBody>> {
  const [studies, links] = await Promise.all([
    get(config, "/catalog/studies", tokens, fetchFn, timeoutMs, (b) => b),
    get(config, "/catalog/links", tokens, fetchFn, timeoutMs, (b) => b),
  ]);
  if (studies.kind !== "loaded") return studies;
  if (links.kind !== "loaded") return links;
  try {
    return { kind: "loaded", value: parseHome(studies.value, links.value) };
  } catch (err) {
    const why = err instanceof Error ? err.message : String(err);
    return {
      kind: "error",
      message: `The platform API sent a response this page cannot read (${why}). Tell your administrator.`,
      status503: false,
      code: null,
    };
  }
}
