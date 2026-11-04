// The study page's data and its pure model: the two route bodies
// (`GET /catalog/studies`, `GET /catalog/links`) parsed strictly, and `studyModel`, which turns them
// into what `StudyView` draws. No fetch and no React here.
// The nullable fields (`n_units`, `trials`, `linked_by`, `linked_at`) are the ones the live golden holds.
import {
  dataSessionPath,
  datasetViewPath,
  NO_SESSION,
  NO_STUDY,
  participantPath,
  unlinkedKey,
} from "../router/dataPaths";

export interface NwbAsset {
  readonly asset_id: string;
  readonly n_units: number | null;
}

export interface Session {
  readonly session: string;
  readonly asset_ids: readonly string[];
  readonly nwb: readonly NwbAsset[];
}

export interface LfpSeries {
  readonly asset_id: string;
  readonly path: string;
  readonly channels: number;
  readonly rate_hz: number | null;
}

export interface Subject {
  readonly subject: string;
  readonly species: string | null;
  readonly modalities: readonly string[];
  readonly sessions: readonly Session[];
  readonly units: number;
  readonly trials: number | null;
  readonly lfp: readonly LfpSeries[];
}

export interface Region {
  readonly region: string;
  readonly units: number;
  readonly channels: number;
}

export interface Dataset {
  readonly dataset: string;
  readonly title: string | null;
  readonly tier: string | null;
  readonly source_archive: string | null;
  readonly source_dataset: string | null;
  readonly source_version: string | null;
  readonly license: string | null;
  readonly doi: string | null;
  readonly asset_count: number | null;
  readonly total_bytes: number | null;
  readonly subjects: readonly Subject[];
  readonly regions: readonly Region[];
}

export interface Study {
  readonly study: string;
  readonly title: string;
  readonly authors: string | null;
  readonly citation: string | null;
  readonly doi: string | null;
  readonly datasets: readonly { readonly dataset: string; readonly role: string | null }[];
}

export interface StudiesBody {
  readonly studies: readonly Study[];
  readonly datasets: readonly Dataset[];
  readonly query_ms: number;
}

export interface Link {
  readonly dataset: string;
  readonly subject: string;
  readonly subject_key: string;
  readonly status: string;
  readonly linked_by: string | null;
  readonly linked_at: string | null;
}

export interface LinksBody {
  readonly links: readonly Link[];
}

type Rec = Readonly<Record<string, unknown>>;

function rec(v: unknown, name: string): Rec {
  if (typeof v !== "object" || v === null || Array.isArray(v)) {
    throw new Error(`"${name}" is not an object`);
  }
  return v as Rec;
}

function arr(v: unknown, name: string): readonly unknown[] {
  if (!Array.isArray(v)) throw new Error(`"${name}" is not a list`);
  return v as readonly unknown[];
}

function str(v: unknown, name: string): string {
  if (typeof v !== "string") throw new Error(`"${name}" is not a string`);
  return v;
}

function strOrNull(v: unknown, name: string): string | null {
  return v === null || v === undefined ? null : str(v, name);
}

function numOrNull(v: unknown, name: string): number | null {
  if (v === null || v === undefined) return null;
  if (typeof v !== "number") throw new Error(`"${name}" is not a number`);
  return v;
}

function num(v: unknown, name: string): number {
  return numOrNull(v, name) ?? 0;
}

function list<T>(v: unknown, name: string, each: (row: Rec, i: number) => T): T[] {
  return arr(v ?? [], name).map((row, i) => each(rec(row, `${name}[${i}]`), i));
}

function parseSubject(r: Rec, at: string): Subject {
  return {
    subject: str(r.subject, `${at}.subject`),
    species: strOrNull(r.species, `${at}.species`),
    modalities: arr(r.modalities ?? [], `${at}.modalities`).map((m) => str(m, `${at}.modalities`)),
    sessions: list(r.sessions, `${at}.sessions`, (s) => ({
      session: str(s.session, `${at}.sessions.session`),
      asset_ids: arr(s.asset_ids ?? [], `${at}.sessions.asset_ids`).map((a) =>
        str(a, `${at}.sessions.asset_ids`),
      ),
      nwb: list(s.nwb, `${at}.sessions.nwb`, (n) => ({
        asset_id: str(n.asset_id, `${at}.sessions.nwb.asset_id`),
        n_units: numOrNull(n.n_units, `${at}.sessions.nwb.n_units`),
      })),
    })),
    units: num(r.units, `${at}.units`),
    trials: numOrNull(r.trials, `${at}.trials`),
    lfp: list(r.lfp, `${at}.lfp`, (l) => ({
      asset_id: str(l.asset_id, `${at}.lfp.asset_id`),
      path: str(l.path, `${at}.lfp.path`),
      channels: num(l.channels, `${at}.lfp.channels`),
      rate_hz: numOrNull(l.rate_hz, `${at}.lfp.rate_hz`),
    })),
  };
}

export function parseStudies(raw: unknown): StudiesBody {
  const body = rec(raw, "body");
  return {
    studies: list(body.studies, "studies", (s, i) => ({
      study: str(s.study, `studies[${i}].study`),
      title: str(s.title, `studies[${i}].title`),
      authors: strOrNull(s.authors, "authors"),
      citation: strOrNull(s.citation, "citation"),
      doi: strOrNull(s.doi, "doi"),
      datasets: list(s.datasets, "study datasets", (d) => ({
        dataset: str(d.dataset, "study dataset"),
        role: strOrNull(d.role, "role"),
      })),
    })),
    datasets: list(body.datasets, "datasets", (d, i) => ({
      dataset: str(d.dataset, `datasets[${i}].dataset`),
      title: strOrNull(d.title, "title"),
      tier: strOrNull(d.tier, "tier"),
      source_archive: strOrNull(d.source_archive, "source_archive"),
      source_dataset: strOrNull(d.source_dataset, "source_dataset"),
      source_version: strOrNull(d.source_version, "source_version"),
      license: strOrNull(d.license, "license"),
      doi: strOrNull(d.doi, "doi"),
      asset_count: numOrNull(d.asset_count, "asset_count"),
      total_bytes: numOrNull(d.total_bytes, "total_bytes"),
      subjects: list(d.subjects, `datasets[${i}].subjects`, (s, j) =>
        parseSubject(s, `datasets[${i}].subjects[${j}]`),
      ),
      regions: list(d.regions, "regions", (g) => ({
        region: str(g.region, "region"),
        units: num(g.units, "region units"),
        channels: num(g.channels, "region channels"),
      })),
    })),
    query_ms: num(body.query_ms, "query_ms"),
  };
}

export function parseLinks(raw: unknown): LinksBody {
  const body = rec(raw, "body");
  return {
    links: list(body.links, "links", (l, i) => ({
      dataset: str(l.dataset, `links[${i}].dataset`),
      subject: str(l.subject, `links[${i}].subject`),
      subject_key: str(l.subject_key, `links[${i}].subject_key`),
      status: str(l.status, `links[${i}].status`),
      linked_by: strOrNull(l.linked_by, "linked_by"),
      linked_at: strOrNull(l.linked_at, "linked_at"),
    })),
  };
}

// ---- the model ----

export type Target =
  | { readonly kind: "study"; readonly study: string }
  | { readonly kind: "dataset"; readonly dataset: string };

export interface Column {
  readonly key: string;
  readonly label: string;
}

export interface CellModel {
  /** The key of the column this cell sits under. */
  readonly column: string;
  readonly present: boolean;
  /** A count, a tick, or a dash when absent. */
  readonly text: string;
}

export interface SessionCell {
  readonly label: string;
  /** `session` only for a session with an NWB of units; any other session goes to its file. */
  readonly to: "session" | "file" | "none";
  readonly href: string | null;
}

export interface LinkCell {
  readonly confirmed: boolean;
  readonly status: string;
  /** The other records of the same person: "sub-p41cs in openneuro-ds004798". */
  readonly counterparts: readonly string[];
  readonly linkedBy: string | null;
  readonly date: string | null;
}

export interface Row {
  readonly subject: string;
  readonly href: string;
  readonly species: string | null;
  readonly cells: readonly CellModel[];
  readonly sessions: readonly SessionCell[];
  readonly link: LinkCell | null;
}

/** Above this many participants a release's matrix turns sideways into a strip. */
export const STRIP_ABOVE = 20;

/**
 * How one release's records nest, counted from the studies body: participants, their sessions, and
 * recording groups (one participant's files of one modality). Thousands of files collapse to these.
 */
export interface Relate {
  readonly participants: number;
  readonly sessions: number;
  readonly recordings: number;
  readonly byModality: readonly {
    readonly key: string;
    readonly label: string;
    readonly n: number;
  }[];
  /** Files and bytes of the whole release, as the catalog counts them. */
  readonly files: number | null;
  readonly bytes: number | null;
}

export interface Release {
  readonly dataset: string;
  readonly href: string;
  readonly role: string | null;
  readonly title: string | null;
  readonly archive: string | null;
  readonly version: string | null;
  readonly license: string | null;
  readonly doi: string | null;
  readonly files: number | null;
  readonly bytes: number | null;
  readonly columns: readonly Column[];
  readonly rows: readonly Row[];
  /** More than `STRIP_ABOVE` participants: drawn as a strip, one column per participant. */
  readonly strip: boolean;
  readonly relate: Relate;
}

export interface Segment {
  readonly text: string;
  readonly strong?: boolean;
}

export interface RegionLine {
  readonly region: string;
  readonly units: number;
  readonly channels: number;
}

export interface Readiness {
  readonly trials: { readonly total: number; readonly participants: number } | null;
  readonly units: { readonly total: number; readonly regions: number } | null;
  readonly lfp: {
    readonly channels: number;
    readonly series: number;
    readonly rates: readonly number[];
  } | null;
  readonly regions: readonly RegionLine[];
}

export interface StudyModel {
  readonly mode: "study" | "dataset";
  readonly title: string;
  readonly slug: string;
  /** In dataset mode, the study the dataset belongs to, if any. */
  readonly partOf: { readonly study: string; readonly title: string } | null;
  readonly authors: string | null;
  readonly citation: string | null;
  readonly doi: string | null;
  readonly paragraph: readonly Segment[];
  readonly releases: readonly Release[];
  readonly readiness: Readiness;
}

const ORDER = ["units", "lfp", "ieeg", "anat", "func", "task"] as const;
const LABEL: Readonly<Record<string, string>> = {
  units: "Units",
  lfp: "LFP",
  ieeg: "iEEG",
  anat: "Anat MRI",
  func: "fMRI",
  task: "Task",
};

export function modalityLabel(key: string): string {
  const known = LABEL[key];
  if (known !== undefined) return known;
  return key.startsWith("other:") ? key.slice("other:".length) : key;
}

function modalityRank(key: string): number {
  const i = (ORDER as readonly string[]).indexOf(key);
  return i === -1 ? ORDER.length : i;
}

/** The modalities present in a release, the default order first, then the others by name. */
export function columnsOf(subjects: readonly Subject[]): Column[] {
  const keys = [...new Set(subjects.flatMap((s) => s.modalities))];
  keys.sort((a, b) => modalityRank(a) - modalityRank(b) || a.localeCompare(b));
  return keys.map((key) => ({ key, label: modalityLabel(key) }));
}

function cellOf(subject: Subject, key: string): CellModel {
  if (!subject.modalities.includes(key)) return { column: key, present: false, text: "–" };
  if (key === "units" && subject.units > 0)
    return { column: key, present: true, text: String(subject.units) };
  if (key === "lfp") {
    const channels = subject.lfp.reduce((n, l) => n + l.channels, 0);
    if (channels > 0) return { column: key, present: true, text: String(channels) };
  }
  if (key === "task" && subject.trials !== null && subject.trials > 0) {
    return { column: key, present: true, text: String(subject.trials) };
  }
  return { column: key, present: true, text: "✓" };
}

function sessionCell(dataset: string, subject: string, s: Session, study: string): SessionCell {
  const label = s.session === "" || s.session === NO_SESSION ? "(no session)" : s.session;
  if (s.nwb.some((n) => (n.n_units ?? 0) > 0)) {
    return {
      label,
      to: "session",
      href: dataSessionPath(dataset, subject, s.session, study),
    };
  }
  const asset = s.nwb[0]?.asset_id ?? s.asset_ids[0];
  if (asset === undefined) return { label, to: "none", href: null };
  return { label, to: "file", href: `/files/${encodeURIComponent(asset)}` };
}

function dateOf(stamp: string | null): string | null {
  return stamp === null ? null : stamp.slice(0, 10) || null;
}

function linkCell(link: Link, all: readonly Link[]): LinkCell {
  return {
    confirmed: link.status === "confirmed",
    status: link.status,
    counterparts: all
      .filter(
        (o) =>
          o.subject_key === link.subject_key &&
          !(o.dataset === link.dataset && o.subject === link.subject),
      )
      .map((o) => `${o.subject} in ${o.dataset}`),
    linkedBy: link.linked_by,
    date: dateOf(link.linked_at),
  };
}

function release(d: Dataset, role: string | null, links: readonly Link[], study: string): Release {
  const columns = columnsOf(d.subjects);
  const mine = links.filter((l) => l.dataset === d.dataset);
  const rows: Row[] = d.subjects.map((s) => {
    const link = mine.find((l) => l.subject === s.subject);
    return {
      subject: s.subject,
      href: participantPath(link?.subject_key ?? unlinkedKey(d.dataset, s.subject), study),
      species: s.species,
      cells: columns.map((c) => cellOf(s, c.key)),
      sessions: s.sessions.map((x) => sessionCell(d.dataset, s.subject, x, study)),
      link: link === undefined ? null : linkCell(link, links),
    };
  });
  const byModality = columns.map((c) => ({
    key: c.key,
    label: c.label,
    n: rows.filter((r) => r.cells.some((x) => x.column === c.key && x.present)).length,
  }));
  return {
    dataset: d.dataset,
    href: datasetViewPath(d.dataset),
    role,
    title: d.title,
    archive: d.source_archive,
    version: d.source_version,
    license: d.license,
    doi: d.doi,
    files: d.asset_count,
    bytes: d.total_bytes,
    columns,
    rows,
    strip: rows.length > STRIP_ABOVE,
    relate: {
      participants: rows.length,
      sessions: d.subjects.reduce((n, s) => n + s.sessions.length, 0),
      recordings: byModality.reduce((n, m) => n + m.n, 0),
      byModality,
      files: d.asset_count,
      bytes: d.total_bytes,
    },
  };
}

function readinessOf(datasets: readonly Dataset[]): Readiness {
  const subjects = datasets.flatMap((d) => d.subjects);
  const withTrials = subjects.filter((s) => s.trials !== null && s.trials > 0);
  const units = subjects.reduce((n, s) => n + s.units, 0);
  const series = subjects.flatMap((s) => s.lfp);
  const byRegion = new Map<string, RegionLine>();
  for (const g of datasets.flatMap((d) => d.regions)) {
    const prev = byRegion.get(g.region);
    byRegion.set(g.region, {
      region: g.region,
      units: (prev?.units ?? 0) + g.units,
      channels: (prev?.channels ?? 0) + g.channels,
    });
  }
  const regions = [...byRegion.values()].sort(
    (a, b) => b.units - a.units || a.region.localeCompare(b.region),
  );
  const rates = [...new Set(series.flatMap((l) => (l.rate_hz === null ? [] : [l.rate_hz])))].sort(
    (a, b) => a - b,
  );
  return {
    trials:
      withTrials.length === 0
        ? null
        : {
            total: withTrials.reduce((n, s) => n + (s.trials ?? 0), 0),
            participants: withTrials.length,
          },
    units:
      units === 0 ? null : { total: units, regions: regions.filter((r) => r.units > 0).length },
    lfp:
      series.length === 0
        ? null
        : { channels: series.reduce((n, l) => n + l.channels, 0), series: series.length, rates },
    regions,
  };
}

function plural(n: number, one: string, many: string = `${one}s`): string {
  return `${n} ${n === 1 ? one : many}`;
}

function paragraphOf(
  datasets: readonly Dataset[],
  columns: readonly string[],
  r: Readiness,
  sessions: number,
): Segment[] {
  const participants = datasets.reduce((n, d) => n + d.subjects.length, 0);
  const releases = datasets.map((d) =>
    [d.dataset, d.source_version].filter((p) => p !== null && p !== "").join(" "),
  );
  const out: Segment[] = [
    {
      text: `${plural(participants, "participant record")} and ${plural(sessions, "session")} in `,
    },
    { text: plural(datasets.length, "release"), strong: true },
    { text: `: ${releases.join(" and ")}. ` },
  ];
  if (r.units !== null) {
    out.push({ text: "Micro-wires recorded " });
    out.push({ text: plural(r.units.total, "single unit"), strong: true });
    out.push({ text: ` in ${plural(r.units.regions, "region")}` });
    const names = r.regions.filter((g) => g.units > 0).map((g) => g.region);
    if (names.length > 0) out.push({ text: ` (${names.join(", ")})` });
    out.push({ text: ". " });
  }
  if (r.trials !== null) {
    out.push({
      text: `${plural(r.trials.total, "trial")} across ${plural(r.trials.participants, "participant")}. `,
    });
  }
  if (columns.length > 0) out.push({ text: `Modalities: ${columns.join(", ")}.` });
  return out;
}

function sessionsOf(datasets: readonly Dataset[]): number {
  return datasets.reduce((n, d) => n + d.subjects.reduce((m, s) => m + s.sessions.length, 0), 0);
}

/**
 * The model for a study (`/s/:study`) or one dataset (`/d/:dataset`). Null when the study or the
 * dataset is not in the body. `links` may hold more than this page needs; only links of its datasets
 * count. A dataset page shows links only through the study the dataset belongs to.
 */
export function studyModel(
  target: Target,
  body: StudiesBody,
  links: readonly Link[],
): StudyModel | null {
  const byId = new Map(body.datasets.map((d) => [d.dataset, d]));
  if (target.kind === "study") {
    const s = body.studies.find((x) => x.study === target.study);
    if (s === undefined) return null;
    const refs = s.datasets.filter((r) => byId.has(r.dataset));
    const datasets = refs.flatMap((r) => byId.get(r.dataset) ?? []);
    const ids = new Set(datasets.map((d) => d.dataset));
    const mine = links.filter((l) => ids.has(l.dataset));
    return build("study", s.study, s.title, s, datasets, refs, mine, null);
  }
  const d = byId.get(target.dataset);
  if (d === undefined) return null;
  const home = body.studies.find((s) => s.datasets.some((r) => r.dataset === d.dataset));
  const role = home?.datasets.find((r) => r.dataset === d.dataset)?.role ?? null;
  const mine = home === undefined ? [] : links;
  return build(
    "dataset",
    d.dataset,
    d.title ?? d.dataset,
    null,
    [d],
    [{ dataset: d.dataset, role }],
    mine,
    home === undefined ? null : { study: home.study, title: home.title },
  );
}

function build(
  mode: "study" | "dataset",
  slug: string,
  title: string,
  meta: Study | null,
  datasets: readonly Dataset[],
  refs: readonly { readonly dataset: string; readonly role: string | null }[],
  links: readonly Link[],
  partOf: StudyModel["partOf"],
): StudyModel {
  const readiness = readinessOf(datasets);
  const studyKey = mode === "study" ? slug : NO_STUDY;
  const releases = datasets.map((d) =>
    release(d, refs.find((r) => r.dataset === d.dataset)?.role ?? null, links, studyKey),
  );
  const labels = [...new Set(releases.flatMap((r) => r.columns.map((c) => c.label)))];
  return {
    mode,
    title,
    slug,
    partOf,
    authors: meta?.authors ?? null,
    citation: meta?.citation ?? null,
    doi: meta?.doi ?? null,
    paragraph: paragraphOf(datasets, labels, readiness, sessionsOf(datasets)),
    releases,
    readiness,
  };
}
