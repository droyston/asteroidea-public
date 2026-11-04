// The participant page's loader: `GET /catalog/studies` and `GET /catalog/links` for
// the person, then (best effort) `GET /catalog/datasets/{dataset}` per release for session start times.
// A segment of the form `<dataset>:<subject>` asks the links route for that subject; when a link names
// it, the person is that link's `subject_key` and the links are read again for the whole person.
import { type FetchFn, TIMEOUT_MS, type TokenSource } from "../auth/apiGet";
import type { AuthConfig } from "../auth/config";
import { type CatalogResult, get } from "../catalog/catalog";
import { loadDataset } from "../catalog/depth";
import { parseUnlinkedKey } from "../router/dataPaths";
import { parseStudies, type StudiesBody } from "../study/model";
import {
  type Extras,
  type ParticipantModel,
  type PLink,
  parseParticipantLinks,
  participantModel,
  sessionKeyOf,
  sexAgeOf,
} from "./model";

type Config = Pick<AuthConfig, "apiUrl">;

export interface ParticipantLoad {
  /** Null when no record of this person is in the catalog. */
  readonly model: ParticipantModel | null;
}

export function linksBySubjectKey(key: string): string {
  return `/catalog/links?${new URLSearchParams({ subject_key: key }).toString()}`;
}

export function linksByRecord(dataset: string, subject: string): string {
  return `/catalog/links?${new URLSearchParams({ dataset, subject }).toString()}`;
}

function readLinks(
  config: Config,
  path: string,
  tokens: TokenSource,
  fetchFn: FetchFn,
  ms: number,
): Promise<CatalogResult<PLink[]>> {
  return get(config, path, tokens, fetchFn, ms, parseParticipantLinks);
}

export async function loadParticipant(
  config: Config,
  param: string,
  tokens: TokenSource,
  fetchFn: FetchFn,
  ms: number = TIMEOUT_MS,
): Promise<CatalogResult<ParticipantLoad>> {
  const one = parseUnlinkedKey(param);
  let raw: unknown = null;
  const studiesRun = get<StudiesBody>(config, "/catalog/studies", tokens, fetchFn, ms, (body) => {
    raw = body;
    return parseStudies(body);
  });
  const firstPath =
    one === null ? linksBySubjectKey(param) : linksByRecord(one.dataset, one.subject);
  const [studies, first] = await Promise.all([
    studiesRun,
    readLinks(config, firstPath, tokens, fetchFn, ms),
  ]);
  if (studies.kind !== "loaded") return studies;
  if (first.kind !== "loaded") return first;

  let links = first.value;
  // A segment that is a `subject_key` names the person; the other form takes it from the link found.
  const named =
    one === null
      ? links
      : links.filter((l) => l.dataset === one.dataset && l.subject === one.subject);
  const key = one === null ? param : named[0]?.subject_key;
  if (one !== null && key !== undefined) {
    // A subject that a link names is one person: read all of that person's links.
    const whole = await readLinks(config, linksBySubjectKey(key), tokens, fetchFn, ms);
    if (whole.kind !== "loaded") return whole;
    links = whole.value;
  }
  // The route filters; a body that holds other people's links is cut to this one's.
  const person = key ?? param;
  links = links.filter((l) => l.subject_key === person);

  const base = participantModel(param, studies.value, links);
  if (base === null) return { kind: "loaded", value: { model: null } };

  const dates: Record<string, string> = {};
  await Promise.all(
    [...new Set(base.records.map((r) => r.dataset))].map(async (dataset) => {
      const d = await loadDataset(config, dataset, tokens, fetchFn, ms);
      if (d.kind !== "loaded") return;
      for (const s of d.value.sessions) {
        if (s.start_time !== null) {
          dates[sessionKeyOf(dataset, s.subject, s.bids_session ?? "")] = s.start_time;
        }
      }
    }),
  );
  const extras: Extras = { sexAge: sexAgeOf(raw), dates };
  return {
    kind: "loaded",
    value: { model: participantModel(param, studies.value, links, extras) },
  };
}
