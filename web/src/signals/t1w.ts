// This person's T1w for the anatomy panel, resolved the way the
// participant page resolves it: the links route ties the file's subject record to a `subject_key`, the
// person's records come from the studies body, and the T1w is the first non-NWB session of a record with
// an anatomical modality. A T1w in another dataset arrives through a cross-archive link; until every link
// of the person is confirmed, the panel says the link is pending. Then the catalog names the file: the
// first NIfTI among that session's files.
import type { FetchFn, TokenSource } from "../auth/apiGet";
import type { AuthConfig } from "../auth/config";
import { type CatalogResult, get, loadAsset } from "../catalog/catalog";
import { type VolumeHeader, volumeHeader } from "../mri/mri";
import { linksByRecord, linksBySubjectKey } from "../participant/load";
import {
  type PLink,
  parseParticipantLinks,
  participantModel,
  type T1wTarget,
} from "../participant/model";
import { unlinkedKey } from "../router/dataPaths";
import { parseStudies, type StudiesBody } from "../study/model";

/** How many of the session's files are asked for, at most, to find the NIfTI. */
export const MAX_PROBES = 4;

export interface T1wSource {
  target: T1wTarget;
  /** True when the T1w is in another dataset and the link to it is not confirmed. */
  pending: boolean;
}

/** The person's T1w target from the studies body and their links, or null when there is none. */
export function t1wSource(
  dataset: string,
  subject: string,
  studies: StudiesBody,
  links: readonly PLink[],
): T1wSource | null {
  const key = links.find((l) => l.dataset === dataset && l.subject === subject)?.subject_key;
  const mine = key === undefined ? [] : links.filter((l) => l.subject_key === key);
  const model = participantModel(key ?? unlinkedKey(dataset, subject), studies, mine);
  const target = model?.t1w ?? null;
  if (target === null) return null;
  const crossed = target.dataset !== dataset || target.subject !== subject;
  return { target, pending: crossed && !mine.every((l) => l.status === "confirmed") };
}

export interface T1wFile {
  assetId: string;
  dataset: string;
  s3Key: string;
  sizeBytes: number | null;
  header: VolumeHeader;
  pending: boolean;
}

export type T1wLoad = { kind: "none" } | { kind: "found"; file: T1wFile };

type Config = Pick<AuthConfig, "apiUrl">;

/** Reads the studies body and the person's links, then the catalog row of the T1w file. */
export async function loadT1w(
  config: Config,
  dataset: string,
  subject: string,
  tokens: TokenSource,
  fetchFn: FetchFn,
): Promise<CatalogResult<T1wLoad>> {
  const ms = 15_000;
  const [studies, first] = await Promise.all([
    get(config, "/catalog/studies", tokens, fetchFn, ms, parseStudies),
    get(config, linksByRecord(dataset, subject), tokens, fetchFn, ms, parseParticipantLinks),
  ]);
  if (studies.kind !== "loaded") return studies;
  if (first.kind !== "loaded") return first;
  let links = first.value;
  const key = links.find((l) => l.dataset === dataset && l.subject === subject)?.subject_key;
  if (key !== undefined) {
    const whole = await get(
      config,
      linksBySubjectKey(key),
      tokens,
      fetchFn,
      ms,
      parseParticipantLinks,
    );
    if (whole.kind !== "loaded") return whole;
    links = whole.value;
  }
  const source = t1wSource(dataset, subject, studies.value, links);
  if (source === null) return { kind: "loaded", value: { kind: "none" } };
  for (const id of source.target.assetIds.slice(0, MAX_PROBES)) {
    const r = await loadAsset(config, id, tokens, fetchFn, ms);
    if (r.kind !== "loaded") return r;
    const a = r.value.asset;
    if (a.format !== "nifti") continue;
    return {
      kind: "loaded",
      value: {
        kind: "found",
        file: {
          assetId: id,
          dataset: source.target.dataset,
          s3Key: typeof a.s3_key === "string" ? a.s3_key : "",
          sizeBytes: typeof a.size_bytes === "number" ? a.size_bytes : null,
          header: volumeHeader(r.value.headers[0]),
          pending: source.pending,
        },
      },
    };
  }
  return { kind: "loaded", value: { kind: "none" } };
}
