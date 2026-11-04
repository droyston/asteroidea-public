// The Source files drawer's data: a dataset's files are never listed on the
// study page. The drawer reads the existing `GET /catalog/datasets/{dataset}/assets` pages (200 rows,
// `more`) on demand and groups the rows loaded so far by recording: one participant's files of one
// datatype in one session. Pure; the drawer component fetches and draws.
import type { FetchFn, TokenSource } from "../auth/apiGet";
import type { AuthConfig } from "../auth/config";
import { type CatalogAssetRow, loadAssets } from "../catalog/catalog";

export interface RecordingFile {
  readonly assetId: string;
  readonly path: string;
  readonly format: string | null;
  readonly bytes: number | null;
}

export interface RecordingGroup {
  /** Stable across pages: a later page that holds more of the same recording extends its group. */
  readonly key: string;
  readonly subject: string;
  readonly session: string;
  readonly datatype: string;
  readonly files: readonly RecordingFile[];
  /** Sum of the known sizes; null when no file in the group reports one. */
  readonly bytes: number | null;
}

const NONE = "\u0000";

/** Group asset rows by (subject, session, datatype), ordered by those three. */
export function recordingGroups(rows: readonly CatalogAssetRow[]): RecordingGroup[] {
  const groups = new Map<string, { head: [string, string, string]; files: RecordingFile[] }>();
  for (const r of rows) {
    const head: [string, string, string] = [
      r.subject ?? "(no participant)",
      r.bids_session === null || r.bids_session === "" ? "(no session)" : r.bids_session,
      r.datatype ?? "other",
    ];
    const key = head.join(NONE);
    const g = groups.get(key) ?? { head, files: [] };
    g.files.push({ assetId: r.asset_id, path: r.path, format: r.format, bytes: r.size_bytes });
    groups.set(key, g);
  }
  return [...groups]
    .map(([key, g]) => {
      const sized = g.files.filter((f) => f.bytes !== null);
      return {
        key,
        subject: g.head[0],
        session: g.head[1],
        datatype: g.head[2],
        files: g.files,
        bytes: sized.length === 0 ? null : sized.reduce((n, f) => n + (f.bytes ?? 0), 0),
      };
    })
    .sort(
      (a, b) =>
        a.subject.localeCompare(b.subject) ||
        a.session.localeCompare(b.session) ||
        a.datatype.localeCompare(b.datatype),
    );
}

/** One 200-row page of a release's files; page 0 is the first. */
export function loadSourcePage(
  config: Pick<AuthConfig, "apiUrl">,
  dataset: string,
  page: number,
  tokens: TokenSource,
  fetchFn: FetchFn,
) {
  return loadAssets(config, dataset, { subject: "", format: "", page }, tokens, fetchFn);
}
