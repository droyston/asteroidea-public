// The study page's loader: `GET /catalog/studies` and `GET /catalog/links?study=`,
// each through the catalog `get` (bearer token, one refresh, the server's own refusal sentence). A
// study page asks for both at once. A dataset page asks for the studies body first, because only a
// dataset that belongs to a study has links to fetch (the route has no dataset-only filter).
import { type FetchFn, TIMEOUT_MS, type TokenSource } from "../auth/apiGet";
import type { AuthConfig } from "../auth/config";
import { type CatalogResult, get } from "../catalog/catalog";
import {
  type LinksBody,
  parseLinks,
  parseStudies,
  type StudiesBody,
  type StudyModel,
  studyModel,
  type Target,
} from "./model";

export interface StudyLoad {
  /** Null when the study or dataset is not in the catalog. */
  readonly model: StudyModel | null;
  readonly queryMs: number;
}

type Config = Pick<AuthConfig, "apiUrl">;

export function linksPath(study: string): string {
  return `/catalog/links?study=${encodeURIComponent(study)}`;
}

function loadLinks(
  config: Config,
  study: string,
  tokens: TokenSource,
  fetchFn: FetchFn,
  timeoutMs: number,
): Promise<CatalogResult<LinksBody>> {
  return get(config, linksPath(study), tokens, fetchFn, timeoutMs, parseLinks);
}

export async function loadStudy(
  config: Config,
  target: Target,
  tokens: TokenSource,
  fetchFn: FetchFn,
  timeoutMs: number = TIMEOUT_MS,
): Promise<CatalogResult<StudyLoad>> {
  const studiesRun = get<StudiesBody>(
    config,
    "/catalog/studies",
    tokens,
    fetchFn,
    timeoutMs,
    parseStudies,
  );
  if (target.kind === "study") {
    const [studies, links] = await Promise.all([
      studiesRun,
      loadLinks(config, target.study, tokens, fetchFn, timeoutMs),
    ]);
    if (studies.kind === "ended" || links.kind === "ended") return { kind: "ended" };
    if (studies.kind === "error") return studies;
    if (links.kind === "error") return links;
    return {
      kind: "loaded",
      value: {
        model: studyModel(target, studies.value, links.value.links),
        queryMs: studies.value.query_ms,
      },
    };
  }
  const studies = await studiesRun;
  if (studies.kind !== "loaded") return studies;
  const home = studies.value.studies.find((s) =>
    s.datasets.some((d) => d.dataset === target.dataset),
  );
  let links: LinksBody = { links: [] };
  if (home !== undefined) {
    const got = await loadLinks(config, home.study, tokens, fetchFn, timeoutMs);
    if (got.kind !== "loaded") return got;
    links = got.value;
  }
  return {
    kind: "loaded",
    value: {
      model: studyModel(target, studies.value, links.links),
      queryMs: studies.value.query_ms,
    },
  };
}
