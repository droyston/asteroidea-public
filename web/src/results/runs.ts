// The Runs box on /results and the home's Latest results card: the All listing grouped by
// run, newest run first, each one link to the run's first figure. With the small formatters the
// Results place shares, it stays apart from model.ts so the home chunk carries only this.
import type { Artifact } from "./rows";

/** The catalog asset fields the Runs box reads; `results.ts :: AssetName` has them. */
export interface SubjectName {
  subject: string | null;
}

export const MONTHS = [
  "Jan",
  "Feb",
  "Mar",
  "Apr",
  "May",
  "Jun",
  "Jul",
  "Aug",
  "Sep",
  "Oct",
  "Nov",
  "Dec",
];

/** "14 Oct" in UTC. */
export function shortDate(epochSeconds: number): string {
  const d = new Date(epochSeconds * 1000);
  return `${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]}`;
}

/** An author as their email; the address is shown as it is. */
export function shortAuthor(email: string | null): string {
  return email ?? "—";
}

const IMAGE_TYPES: ReadonlySet<string> = new Set(["image/png", "image/jpeg"]);
export const HTML_TYPE = "text/html";

/** The bare media type: no parameters, lower case. */
export function mediaType(contentType: string | null): string | null {
  const bare = contentType?.split(";")[0]?.trim().toLowerCase();
  return bare === undefined || bare === "" ? null : bare;
}

export function isImage(contentType: string | null): boolean {
  const t = mediaType(contentType);
  return t !== null && IMAGE_TYPES.has(t);
}

export const MAX_RUNS = 5;

/** One run of the Runs box: its files grouped by `run_id`, linked to its first figure. */
export interface RunRow {
  run_id: string;
  /** The analysis that saved it (`run_function`). */
  analysis: string;
  /** The subject its files used; "N subjects" when several; null when none is known. */
  subject: string | null;
  files: number;
  date: string;
  /** "you" for the signed-in researcher's run, else the author's email. */
  author: string;
  mine: boolean;
  /** The run's first image by name, else its first file by name. */
  href: string;
}

function subjectOf(
  files: readonly Artifact[],
  names: ReadonlyMap<string, SubjectName>,
): string | null {
  const subjects = new Set<string>();
  for (const f of files) {
    for (const id of f.source_asset_ids) {
      const s = names.get(id)?.subject;
      if (s !== null && s !== undefined) subjects.add(s);
    }
  }
  if (subjects.size === 0) return null;
  if (subjects.size === 1) return [...subjects][0] ?? null;
  return `${subjects.size} subjects`;
}

/**
 * The newest `limit` runs of `rows` (the All listing, newest first), newest run first. A row with no
 * `run_id` is not a run and is left to the tree. `names` names the subject; an empty map leaves it out.
 */
export function runsModel(
  rows: readonly Artifact[],
  names: ReadonlyMap<string, SubjectName>,
  me: string | null,
  limit = MAX_RUNS,
): readonly RunRow[] {
  const groups = new Map<string, Artifact[]>();
  for (const r of rows) {
    if (r.run_id === null) continue;
    const g = groups.get(r.run_id);
    if (g === undefined) groups.set(r.run_id, [r]);
    else g.push(r);
  }
  const newest = (files: readonly Artifact[]) => Math.max(...files.map((f) => f.created_at));
  return [...groups]
    .map(([run_id, files]) => ({ run_id, files, at: newest(files) }))
    .sort((a, b) => b.at - a.at)
    .slice(0, limit)
    .map(({ run_id, files, at }): RunRow => {
      const byName = [...files].sort(
        (a, b) => a.name.localeCompare(b.name) || a.artifact_id.localeCompare(b.artifact_id),
      );
      const first = byName.find((f) => isImage(f.content_type)) ?? byName[0];
      const email = files.find((f) => f.author_email !== null)?.author_email ?? null;
      const mine = me !== null && email === me;
      return {
        run_id,
        analysis: files.find((f) => f.run_function !== null)?.run_function ?? "Unnamed analysis",
        subject: subjectOf(files, names),
        files: files.length,
        date: shortDate(at),
        author: mine ? "you" : shortAuthor(email),
        mine,
        href: `/results/${encodeURIComponent(first?.artifact_id ?? "")}`,
      };
    });
}

/** "CS62 · 20 files · 28 Oct · you": a run's label after its analysis name. */
export function runDetail(run: RunRow): string {
  const files = `${run.files} file${run.files === 1 ? "" : "s"}`;
  return [run.subject, files, run.date, run.author].filter((p) => p !== null).join(" · ");
}
