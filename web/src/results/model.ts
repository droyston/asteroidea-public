// The Results place's pure model: the newest 100 as a dot timeline (mine
// darker), a tree by the data the results used (study, then session, through the batch asset lookup),
// at most 10 counted branches, the open branch's newest 4 and a count, and one result as a chain:
// inputs in research terms with role and "current", the readable run, this result, and what used it.
// A PNG or JPEG previews as an image and an HTML page in a sandboxed frame; every other type says so.
// The "From the same run" strip lists the run's other files, thumbnails for images. The Runs box (and
// the home's Latest results card) groups the All listing by run, newest run first.
// It takes parsed bodies and nothing else, so the artboard harness renders the same view over a fixture.
import { type Artifact, type ArtifactRecord, type AssetName, formatBytes } from "./results";
import { HTML_TYPE, isImage, MONTHS, mediaType, shortAuthor, shortDate } from "./runs";

export {
  isImage,
  MAX_RUNS,
  type RunRow,
  runDetail,
  runsModel,
  shortAuthor,
  shortDate,
} from "./runs";

export const NEWEST = 100;
export const MAX_BRANCHES = 10;
export const BRANCH_ROWS = 4;

/** A study's title and its datasets, from `GET /catalog/studies`. */
export interface StudyName {
  study: string;
  title: string;
  datasets: readonly string[];
}

export function parseStudyNames(body: unknown): readonly StudyName[] {
  if (typeof body !== "object" || body === null) return [];
  const studies = (body as Record<string, unknown>).studies;
  if (!Array.isArray(studies)) return [];
  return studies.flatMap((s: unknown): StudyName[] => {
    if (typeof s !== "object" || s === null) return [];
    const o = s as Record<string, unknown>;
    if (typeof o.study !== "string") return [];
    const datasets = Array.isArray(o.datasets)
      ? o.datasets.flatMap((d: unknown) =>
          typeof d === "object" &&
          d !== null &&
          typeof (d as { dataset?: unknown }).dataset === "string"
            ? [(d as { dataset: string }).dataset]
            : [],
        )
      : [];
    return [{ study: o.study, title: typeof o.title === "string" ? o.title : o.study, datasets }];
  });
}

export interface Dot {
  artifact_id: string;
  name: string;
  /** 0 (oldest of the 100) to 1 (newest). */
  x: number;
  /** 0 to 1, a stable spread so dots on one day do not cover each other. */
  y: number;
  mine: boolean;
  href: string;
}

export interface Timeline {
  dots: readonly Dot[];
  /** Month labels at their x position. */
  ticks: readonly { label: string; x: number }[];
}

export interface Row {
  artifact_id: string;
  name: string;
  author: string;
  date: string;
  pending: boolean;
  selected: boolean;
  href: string;
}

export interface Branch {
  key: string;
  label: string;
  count: number;
  open: boolean;
  sessions: readonly { key: string; label: string; count: number; open: boolean }[];
}

export interface ListModel {
  scope: "mine" | "all";
  mineCount: number;
  total: number;
  timeline: Timeline;
  branches: readonly Branch[];
  /** The open branch (or session) and its newest rows. */
  openLabel: string | null;
  rows: readonly Row[];
  more: number;
}

function spread(id: string): number {
  let h = 0;
  for (const c of id) h = (h * 31 + c.charCodeAt(0)) >>> 0;
  return 0.15 + (h % 1000) / 1430;
}

export function timeline(rows: readonly Artifact[], me: string | null): Timeline {
  const newest = rows.slice(0, NEWEST);
  if (newest.length === 0) return { dots: [], ticks: [] };
  const times = newest.map((r) => r.created_at);
  const lo = Math.min(...times);
  const hi = Math.max(...times);
  const span = hi - lo;
  const at = (t: number) => (span === 0 ? 1 : (t - lo) / span);
  const dots = newest.map((r) => ({
    artifact_id: r.artifact_id,
    name: r.name,
    x: at(r.created_at),
    y: spread(r.artifact_id),
    mine: me !== null && r.author_email === me,
    href: `/results/${encodeURIComponent(r.artifact_id)}`,
  }));
  const ticks: { label: string; x: number }[] = [];
  if (span > 0) {
    const d = new Date(lo * 1000);
    let m = Date.UTC(d.getUTCFullYear(), d.getUTCMonth() + 1, 1) / 1000;
    while (m < hi) {
      ticks.push({ label: MONTHS[new Date(m * 1000).getUTCMonth()] ?? "", x: at(m) });
      const n = new Date(m * 1000);
      m = Date.UTC(n.getUTCFullYear(), n.getUTCMonth() + 1, 1) / 1000;
    }
  }
  return { dots, ticks };
}

export const FROM_RESULTS = "From other results";
export const ACROSS_STUDIES = "Across studies";
export const ACROSS_SESSIONS = "Across participants";
export const OTHER_DATA = "Other data";

/** Where one result sits in the tree: a branch (study) and a session within it. */
export function place(
  row: Artifact,
  names: ReadonlyMap<string, AssetName>,
  studyOf: ReadonlyMap<string, StudyName>,
): { branch: string; session: string | null } {
  const found = row.source_asset_ids.flatMap((id) => {
    const n = names.get(id);
    return n === undefined ? [] : [n];
  });
  if (found.length === 0) return { branch: FROM_RESULTS, session: null };
  const branches = new Set(found.map((n) => studyOf.get(n.dataset)?.title ?? n.dataset));
  if (branches.size > 1) return { branch: ACROSS_STUDIES, session: null };
  const sessions = new Set(found.map(sessionLabel));
  const [branch] = branches;
  const [session] = sessions;
  return {
    branch: branch ?? FROM_RESULTS,
    session: sessions.size > 1 ? ACROSS_SESSIONS : (session ?? null),
  };
}

/** "CS62 · ses-1", or the subject alone, or the dataset path when neither is known. */
export function sessionLabel(n: AssetName): string {
  if (n.subject === null) return n.path;
  return n.session === null ? n.subject : `${n.subject} · ses-${n.session}`;
}

export interface ListInput {
  scope: "mine" | "all";
  rows: readonly Artifact[];
  mineCount: number;
  me: string | null;
  names: ReadonlyMap<string, AssetName>;
  studies: readonly StudyName[];
  /** The open branch and session keys, or null for the branch of `selected`, else the largest. */
  open: { branch: string; session: string | null } | null;
  selected: string | null;
}

function counted(keys: readonly string[]): [string, number][] {
  const counts = new Map<string, number>();
  for (const k of keys) counts.set(k, (counts.get(k) ?? 0) + 1);
  return [...counts].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
}

export function listModel(input: ListInput): ListModel {
  const rows = input.rows.slice(0, NEWEST);
  const studyOf = new Map<string, StudyName>();
  for (const s of input.studies) for (const d of s.datasets) studyOf.set(d, s);
  const placed = rows.map((r) => ({ row: r, at: place(r, input.names, studyOf) }));
  let top = counted(placed.map((p) => p.at.branch));
  if (top.length > MAX_BRANCHES) {
    const keep = new Set(top.slice(0, MAX_BRANCHES - 1).map(([k]) => k));
    for (const p of placed)
      if (!keep.has(p.at.branch)) p.at = { branch: OTHER_DATA, session: null };
    top = counted(placed.map((p) => p.at.branch));
  }
  const chosen = placed.find((p) => p.row.artifact_id === input.selected)?.at;
  const open = input.open ?? chosen ?? { branch: top[0]?.[0] ?? "", session: null };
  const branches = top.map(([key, count]): Branch => {
    const inBranch = placed.filter((p) => p.at.branch === key);
    const sessions = counted(
      inBranch.flatMap((p) => (p.at.session === null ? [] : [p.at.session])),
    );
    return {
      key,
      label: key,
      count,
      open: key === open.branch,
      sessions: sessions.map(([s, n]) => ({
        key: s,
        label: s,
        count: n,
        open: key === open.branch && s === open.session,
      })),
    };
  });
  const members = placed
    .filter(
      (p) =>
        p.at.branch === open.branch && (open.session === null || p.at.session === open.session),
    )
    .map((p) => p.row);
  const shown = members.slice(0, BRANCH_ROWS);
  const sel = input.selected;
  if (sel !== null && !shown.some((r) => r.artifact_id === sel)) {
    const s = members.find((r) => r.artifact_id === sel);
    if (s !== undefined) shown.splice(BRANCH_ROWS - 1, 1, s);
  }
  return {
    scope: input.scope,
    mineCount: input.mineCount,
    total: rows.length,
    timeline: timeline(rows, input.me),
    branches,
    openLabel:
      branches.length === 0 ? null : open.session === null ? open.branch : `${open.session}`,
    rows: shown.map((r) => ({
      artifact_id: r.artifact_id,
      name: r.name,
      author: shortAuthor(r.author_email ?? (input.scope === "mine" ? input.me : null)),
      date: r.state === "PENDING" ? "saving…" : shortDate(r.created_at),
      pending: r.state === "PENDING",
      selected: r.artifact_id === sel,
      href: `/results/${encodeURIComponent(r.artifact_id)}`,
    })),
    more: members.length - shown.length,
  };
}

// --- one result as a chain ---

export interface InputCard {
  /** Unique within one chain. */
  key: string;
  /** RECORDING, EVENTS, RESULT, FILE … */
  kind: string;
  role: string | null;
  title: string;
  /** "version 3HL4kq…" or the author and date of a result input. */
  detail: string | null;
  /** True, false, or null when it is not a catalog file or the catalog does not know it. */
  current: boolean | null;
  href: string | null;
}

export interface RunLine {
  label: string;
  value: string;
}

export interface ChainModel {
  artifact_id: string;
  name: string;
  state: string;
  author: string;
  mine: boolean;
  when: string;
  size: string;
  note: string | null;
  inputs: readonly InputCard[];
  run: { title: string; lines: readonly RunLine[] } | null;
  usedBy: readonly { artifact_id: string; name: string; detail: string; href: string }[];
  /** Why Delete is refused, or null when the author may delete. */
  deleteRefusal: string | null;
  /** How the preview draws this result's type. */
  preview: PreviewKind;
}

/** How a result previews: an image, a page in a sandboxed frame, or a sentence. */
export type PreviewKind = { kind: "image" } | { kind: "html" } | { kind: "none"; text: string };

/** The preview's sentence for a type it does not draw. */
export function previewFallback(contentType: string | null): string {
  return `Preview not built for ${mediaType(contentType) ?? "this type"}; download it`;
}

export function previewKind(contentType: string | null): PreviewKind {
  if (isImage(contentType)) return { kind: "image" };
  if (mediaType(contentType) === HTML_TYPE) return { kind: "html" };
  return { kind: "none", text: previewFallback(contentType) };
}

/** One file of the "From the same run" strip. */
export interface StripItem {
  artifact_id: string;
  name: string;
  href: string;
  /** The result this page shows. */
  current: boolean;
  /** A presigned image link for a thumbnail, or null for a name chip. */
  thumb: string | null;
}

/**
 * The strip of one run's files, sorted by name, the current one marked; null when the run saved
 * nothing else (a strip of one says nothing).
 */
export function stripModel(
  rows: readonly Artifact[],
  currentId: string,
  thumbs: ReadonlyMap<string, string>,
): readonly StripItem[] | null {
  const items = [...rows]
    .sort((a, b) => a.name.localeCompare(b.name) || a.artifact_id.localeCompare(b.artifact_id))
    .map((a) => ({
      artifact_id: a.artifact_id,
      name: a.name,
      href: `/results/${encodeURIComponent(a.artifact_id)}`,
      current: a.artifact_id === currentId,
      thumb: thumbs.get(a.artifact_id) ?? null,
    }));
  return items.some((i) => !i.current) ? items : null;
}

const FUNCTION_TITLE: Readonly<Record<string, string>> = {
  rates: "Firing rates",
  tuning: "Tuning screen",
  spectrogram: "LFP spectrogram",
  events: "Alignment events",
};

const PARAM_LABEL: Readonly<Record<string, string>> = {
  event: "Aligned to",
  attribute: "Attribute",
  bin_s: "Bin",
  window_s: "Window",
  pre_s: "Before",
  post_s: "After",
  baseline_s: "Baseline",
};

function paramValue(key: string, v: unknown): string {
  if (typeof v === "number" && key.endsWith("_s")) {
    return v < 1 ? `${Math.round(v * 1000)} ms` : `${v} s`;
  }
  if (typeof v === "string") return v.replace(/_/g, " ");
  return JSON.stringify(v);
}

function short(version: string): string {
  return version.length > 6 ? `${version.slice(0, 6)}…` : version;
}

function fileKind(n: AssetName): string {
  if (n.suffix === "events") return "EVENTS";
  if (n.format === "nwb") return "RECORDING";
  if (n.datatype === "anat") return "ANATOMY";
  return (n.datatype ?? n.format ?? "file").toUpperCase();
}

function fileLabel(n: AssetName): string {
  if (n.format === "nwb") return "NWB ephys";
  return n.path.split("/").pop() ?? n.path;
}

function assetCard(
  id: string,
  role: string | null,
  version: string | null,
  names: ReadonlyMap<string, AssetName>,
): Omit<InputCard, "key"> {
  const n = names.get(id);
  const href = `/files/${encodeURIComponent(id)}`;
  if (n === undefined) {
    return {
      kind: "FILE",
      role,
      title: `asset ${id.slice(0, 8)}…, not in the catalog`,
      detail: version === null ? null : `version ${short(version)}`,
      current: null,
      href,
    };
  }
  return {
    kind: fileKind(n),
    role,
    title: `${sessionLabel(n)} · ${fileLabel(n)}`,
    detail: `version ${short(version ?? n.version)}`,
    current: n.current,
    href,
  };
}

export interface ChainInput {
  record: ArtifactRecord;
  names: ReadonlyMap<string, AssetName>;
  /** Results known from the listings, to name a result input. */
  known: ReadonlyMap<string, Artifact>;
  usedBy: readonly Artifact[];
  /** The caller's token `sub`: the author is the one whose `sub` the record carries. */
  sub: string | null;
}

export function chainModel(input: ChainInput): ChainModel {
  const { record, names } = input;
  const files = record.sources.map((s) => assetCard(s.asset_id, null, s.s3_version_id, names));
  const declared = record.inputs.flatMap((i): Omit<InputCard, "key">[] => {
    if (i.kind === "asset") {
      return [assetCard(i.id, i.role, i.version === null ? null : String(i.version), names)];
    }
    if (i.kind === "artifact") {
      const a = input.known.get(i.id);
      return [
        {
          kind: "RESULT",
          role: i.role,
          title: a?.name ?? `result ${i.id.slice(0, 8)}…`,
          detail:
            a === undefined ? null : `${shortAuthor(a.author_email)} · ${shortDate(a.created_at)}`,
          current: null,
          href: `/results/${encodeURIComponent(i.id)}`,
        },
      ];
    }
    return [];
  });
  const run = record.run;
  const settings = record.inputs.filter((i) => i.kind !== "asset" && i.kind !== "artifact");
  let runModel: ChainModel["run"] = null;
  if (run !== null) {
    const params =
      typeof run.parameters === "object" && run.parameters !== null
        ? Object.entries(run.parameters as Record<string, unknown>)
        : [];
    const fn = run.function.split(".").pop() ?? run.function;
    const event = params.find(([k]) => k === "event")?.[1];
    const base = FUNCTION_TITLE[fn] ?? run.function;
    const lines: RunLine[] = [
      { label: "Function", value: run.function },
      ...params.map(([k, v]) => ({ label: PARAM_LABEL[k] ?? k, value: paramValue(k, v) })),
    ];
    if (run.defaults_version !== null) {
      lines.push({ label: "Defaults", value: `Study defaults ${run.defaults_version}` });
    }
    const sdk = run.sdk_version ?? record.sdk_version;
    if (sdk !== null) lines.push({ label: "SDK", value: sdk });
    for (const s of settings) {
      const label =
        s.kind === "table_snapshot" ? "Catalog" : s.kind === "recipe" ? "Recipe" : "Defaults";
      const what = s.kind === "table_snapshot" ? `${s.id} snapshot` : s.id;
      lines.push({ label, value: s.version === null ? what : `${what} ${s.version}` });
    }
    runModel = {
      title: typeof event === "string" ? `${base} around ${event.replace(/_/g, " ")}` : base,
      lines,
    };
  }
  // A result never uses itself; a listing that answers more than its dependents still reads right.
  const usedBy = input.usedBy
    .filter((a) => a.artifact_id !== record.artifact_id)
    .map((a) => ({
      artifact_id: a.artifact_id,
      name: a.name,
      detail: `${shortAuthor(a.author_email)} · ${shortDate(a.created_at)}`,
      href: `/results/${encodeURIComponent(a.artifact_id)}`,
    }));
  return {
    artifact_id: record.artifact_id,
    name: record.name,
    state: record.state,
    author: record.author_email ?? "an unknown author",
    mine: input.sub !== null && input.sub === record.author_sub,
    when: `${shortDate(record.created_at)} ${new Date(record.created_at * 1000).getUTCFullYear()}`,
    size: formatBytes(record.size),
    note: record.note,
    inputs: [...files, ...declared].map((c, n) => ({ ...c, key: `${n}` })),
    run: runModel,
    usedBy,
    deleteRefusal:
      usedBy.length > 0
        ? `Delete is refused while another result uses it (${usedBy.length}). Withdraw it instead.`
        : null,
    preview: previewKind(record.content_type),
  };
}
