// The data home's view state, held in the URL query: the tier and four facets
// (modality, region, species, archive). A view is a link, and Back walks through the choices. Pure:
// `parseQuery` and `queryString` are the two halves; the page reads `location.search` and navigates.
// Within a facet the values widen (any of them); across facets they narrow (all of them).

export type Tier = "curated" | "mine" | "sandbox";
export const TIERS: readonly Tier[] = ["curated", "mine", "sandbox"];

export const FACETS = ["modality", "region", "species", "archive"] as const;
export type FacetId = (typeof FACETS)[number];

export interface HomeQuery {
  readonly tier: Tier;
  readonly modality: readonly string[];
  readonly region: readonly string[];
  readonly species: readonly string[];
  readonly archive: readonly string[];
}

export const EMPTY_QUERY: HomeQuery = {
  tier: "curated",
  modality: [],
  region: [],
  species: [],
  archive: [],
};

function isTier(v: string | null): v is Tier {
  return TIERS.some((t) => t === v);
}

function values(q: URLSearchParams, name: FacetId): string[] {
  return [...new Set(q.getAll(name).filter((v) => v !== ""))].sort();
}

/** The query in `search` (`location.search`); a tier or parameter it does not know is ignored. */
export function parseQuery(search: string): HomeQuery {
  const q = new URLSearchParams(search);
  const tier = q.get("tier");
  return {
    tier: isTier(tier) ? tier : "curated",
    modality: values(q, "modality"),
    region: values(q, "region"),
    species: values(q, "species"),
    archive: values(q, "archive"),
  };
}

/** `?tier=…&modality=…` in one canonical order; the default view is the empty string. */
export function queryString(query: HomeQuery): string {
  const q = new URLSearchParams();
  if (query.tier !== "curated") q.set("tier", query.tier);
  for (const f of FACETS) for (const v of [...new Set(query[f])].sort()) q.append(f, v);
  const text = q.toString();
  return text === "" ? "" : `?${text}`;
}

/** The query with `value` of `facet` chosen, or unchosen when it already was. */
export function toggleFacet(query: HomeQuery, facet: FacetId, value: string): HomeQuery {
  const have = query[facet];
  const next = have.includes(value) ? have.filter((v) => v !== value) : [...have, value].sort();
  return { ...query, [facet]: next };
}

export function isNarrowed(query: HomeQuery): boolean {
  return FACETS.some((f) => query[f].length > 0);
}

/** The query with every facet cleared and the tier kept. */
export function clearFacets(query: HomeQuery): HomeQuery {
  return { ...EMPTY_QUERY, tier: query.tier };
}
