// The data home's model: everything the page draws, derived from the studies and links
// bodies and nothing else. Pure; `HomeView` only lays it out.
import { regionColour } from "../kit/regions";
import { datasetViewPath } from "../router/dataPaths";
import type { HomeBody, HomeDataset, HomeSubject } from "./data";
import { EMPTY_QUERY, FACETS, type FacetId, type HomeQuery } from "./facets";

/** The default matrix columns, in display order; other modalities follow. */
const DEFAULT_MODALITIES: readonly { id: string; label: string }[] = [
  { id: "units", label: "Units" },
  { id: "lfp", label: "LFP" },
  { id: "ieeg", label: "iEEG" },
  { id: "anat", label: "Anat MRI" },
  { id: "func", label: "fMRI" },
  { id: "task", label: "Task" },
];
const EPHYS = new Set(["units", "lfp", "ieeg"]);
const MRI = new Set(["anat", "func"]);
const OTHER = "other:";

export interface Column {
  readonly id: string;
  readonly label: string;
}

export interface Release {
  readonly dataset: string;
  readonly href: string;
  /** "DANDI 000623 · 0.240227.2023 · ephys": the full version string. */
  readonly text: string;
  readonly license: string | null;
}

export interface RegionSegment {
  readonly label: string;
  readonly units: number;
  /** Share of the bar, 0 to 1. */
  readonly share: number;
  /** `var(--color-region-…)`: the colour never stands without `label`. */
  readonly cssVar: string;
}

export interface StudyRow {
  readonly id: string;
  readonly title: string;
  readonly href: string;
  /** "Homo sapiens · 17 participants". */
  readonly meta: string;
  /** Generated from the counts, never authored. */
  readonly oneLiner: string;
  readonly releases: readonly Release[];
  /** Participants with the modality, by column id; absent when none. */
  readonly cells: Readonly<Record<string, number>>;
  readonly regions: readonly RegionSegment[];
  readonly regionSummary: string;
}

export interface RailEntry {
  readonly key: string;
  readonly name: string;
  readonly detail: string;
  /** `dashed` a pending link, `solid` a confirmed one, `none` both recordings in one dataset. */
  readonly line: "dashed" | "solid" | "none";
}

export interface SandboxFold {
  readonly count: number;
  /** "conc-…" with its count, most first. */
  readonly prefixes: readonly { readonly prefix: string; readonly count: number }[];
  readonly datasets: readonly string[];
}

export interface FacetOption {
  /** What the URL holds: a modality id, a trimmed region label, a species name, an archive id. */
  readonly value: string;
  readonly label: string;
  /** Curated studies that have it. */
  readonly count: number;
  /** A region's colour: it never stands without `label`. */
  readonly cssVar?: string;
}

export interface FacetGroup {
  readonly id: FacetId;
  readonly label: string;
  readonly options: readonly FacetOption[];
}

export interface HomeModel {
  readonly tiers: { readonly curated: number; readonly mine: number; readonly sandbox: number };
  readonly summary: string;
  /** Every option of every facet, counted over all curated studies. */
  readonly facets: readonly FacetGroup[];
  readonly columns: readonly Column[];
  readonly rows: readonly StudyRow[];
  readonly rail: readonly RailEntry[];
  readonly mine: readonly HomeDataset[];
  readonly others: readonly HomeDataset[];
  readonly sandbox: SandboxFold;
}

export function studyPath(study: string): string {
  return `/s/${encodeURIComponent(study)}`;
}

export function modalityLabel(id: string): string {
  const known = DEFAULT_MODALITIES.find((m) => m.id === id);
  if (known !== undefined) return known.label;
  return id.startsWith(OTHER) ? `Other · ${id.slice(OTHER.length)}` : id;
}

export function archiveLabel(archive: string | null): string | null {
  if (archive === null) return null;
  if (archive === "dandi") return "DANDI";
  if (archive === "openneuro") return "OpenNeuro";
  return archive;
}

function roleLabel(role: string): string {
  if (role === "fmri") return "fMRI";
  if (role === "mri") return "MRI";
  return role;
}

function plural(n: number, one: string, many: string): string {
  return `${n} ${n === 1 ? one : many}`;
}

function release(d: HomeDataset | undefined, id: string, role: string): Release {
  if (d === undefined) return { dataset: id, href: datasetViewPath(id), text: id, license: null };
  const archive = archiveLabel(d.source_archive);
  const name =
    archive !== null && d.source_dataset !== null ? `${archive} ${d.source_dataset}` : d.dataset;
  const text = [name, d.source_version, role === "" ? null : roleLabel(role)]
    .filter((p) => p !== null)
    .join(" · ");
  return { dataset: d.dataset, href: datasetViewPath(d.dataset), text, license: d.license };
}

/** Prefix of a generated dataset id: trailing hex-with-a-digit segments (stamps, hashes) dropped. */
export function idPrefix(id: string): string {
  const parts = id.split("-");
  while (parts.length > 1 && /^[0-9a-f]*\d[0-9a-f]*$/.test(parts[parts.length - 1] ?? "")) {
    parts.pop();
  }
  return parts.join("-");
}

function sandboxFold(datasets: readonly HomeDataset[]): SandboxFold {
  const ids = datasets.filter((d) => d.tier === "sandbox").map((d) => d.dataset);
  const counts = new Map<string, number>();
  for (const id of ids) counts.set(idPrefix(id), (counts.get(idPrefix(id)) ?? 0) + 1);
  const prefixes = [...counts]
    .map(([prefix, count]) => ({ prefix, count }))
    .sort((a, b) => b.count - a.count || a.prefix.localeCompare(b.prefix));
  return { count: ids.length, prefixes, datasets: [...ids].sort() };
}

function hasAny(modalities: ReadonlySet<string>, of: ReadonlySet<string>): boolean {
  for (const m of modalities) if (of.has(m)) return true;
  return false;
}

interface Member {
  readonly dataset: HomeDataset;
  readonly subject: HomeSubject;
}

function labels(members: readonly Member[], of: ReadonlySet<string>): string {
  const seen = new Set<string>();
  for (const m of members) for (const x of m.subject.modalities) if (of.has(x)) seen.add(x);
  return [...seen].map(modalityLabel).join(" + ");
}

function side(members: readonly Member[], of: ReadonlySet<string>): string {
  const m = members.filter((x) => hasAny(new Set(x.subject.modalities), of));
  const archive = archiveLabel(m[0]?.dataset.source_archive ?? null) ?? m[0]?.dataset.dataset ?? "";
  return `${labels(m, of)} (${archive})`;
}

function bareName(subject: string): string {
  return subject.replace(/^sub-/, "");
}

/** Per study: the people, merged only across confirmed links (a pending link is an unconfirmed claim). */
function personIds(body: HomeBody) {
  const confirmed = new Map<string, string>();
  for (const l of body.links) {
    if (l.status === "confirmed") confirmed.set(`${l.dataset}\u0000${l.subject}`, l.subject_key);
  }
  return (dataset: string, subject: string): string =>
    confirmed.get(`${dataset}\u0000${subject}`) ?? `${dataset}\u0000${subject}`;
}

function railFor(
  studyTitle: string,
  members: readonly Member[],
  body: HomeBody,
  datasetIds: ReadonlySet<string>,
): RailEntry[] {
  const keyOf = new Map<string, string>();
  const statuses = new Map<string, string[]>();
  for (const l of body.links) {
    if (!datasetIds.has(l.dataset)) continue;
    keyOf.set(`${l.dataset}\u0000${l.subject}`, l.subject_key);
    statuses.set(l.subject_key, [...(statuses.get(l.subject_key) ?? []), l.status]);
  }
  const groups = new Map<string, Member[]>();
  for (const m of members) {
    const key = keyOf.get(`${m.dataset.dataset}\u0000${m.subject.subject}`);
    const id = key === undefined ? `${m.dataset.dataset}\u0000${m.subject.subject}` : `link:${key}`;
    groups.set(id, [...(groups.get(id) ?? []), m]);
  }
  const entries: RailEntry[] = [];
  for (const [id, group] of groups) {
    const all = new Set(group.flatMap((m) => m.subject.modalities));
    if (!hasAny(all, EPHYS) || !hasAny(all, MRI)) continue;
    const key = id.startsWith("link:") ? id.slice(5) : id;
    const st = statuses.get(key);
    const ephysMember =
      group.find((m) => hasAny(new Set(m.subject.modalities), EPHYS)) ?? (group[0] as Member);
    const name = bareName(ephysMember.subject.subject);
    if (st === undefined) {
      entries.push({
        key,
        name,
        detail: `${studyTitle} · ${labels(group, new Set([...EPHYS, ...MRI]))} (${archiveLabel(ephysMember.dataset.source_archive) ?? ephysMember.dataset.dataset}) · same dataset`,
        line: "none",
      });
      continue;
    }
    entries.push({
      key,
      name,
      detail: `${studyTitle} · ${side(group, EPHYS)} ⟷ ${side(group, MRI)}`,
      line: st.every((s) => s === "confirmed") ? "solid" : "dashed",
    });
  }
  return entries;
}

interface Attrs {
  readonly modality: ReadonlySet<string>;
  readonly region: ReadonlySet<string>;
  readonly species: ReadonlySet<string>;
  readonly archive: ReadonlySet<string>;
}

function attrsOf(datasets: readonly (HomeDataset | undefined)[]): Attrs {
  const modality = new Set<string>();
  const region = new Set<string>();
  const species = new Set<string>();
  const archive = new Set<string>();
  for (const d of datasets) {
    if (d === undefined) continue;
    if (d.source_archive !== null) archive.add(d.source_archive);
    for (const r of d.regions) region.add(r.region.trim());
    for (const s of d.subjects) {
      if (s.species !== null) species.add(s.species);
      for (const m of s.modalities) modality.add(m);
    }
  }
  return { modality, region, species, archive };
}

/** A study stays when, for every facet with a choice, it has at least one of the chosen values. */
function matches(a: Attrs, query: HomeQuery): boolean {
  return FACETS.every((f) => query[f].length === 0 || query[f].some((v) => a[f].has(v)));
}

function facetGroups(all: readonly Attrs[]): FacetGroup[] {
  const tally = (f: FacetId): Map<string, number> => {
    const n = new Map<string, number>();
    for (const a of all) for (const v of a[f]) n.set(v, (n.get(v) ?? 0) + 1);
    return n;
  };
  const byCount = (n: Map<string, number>, label: (v: string) => string): FacetOption[] =>
    [...n]
      .map(([value, count]) => ({ value, label: label(value), count }))
      .sort((x, y) => y.count - x.count || x.label.localeCompare(y.label));
  const modality = tally("modality");
  const ids = [...modality.keys()];
  const rank = (id: string): number => {
    const i = DEFAULT_MODALITIES.findIndex((m) => m.id === id);
    if (i !== -1) return i;
    return id.startsWith(OTHER) ? DEFAULT_MODALITIES.length + 1 : DEFAULT_MODALITIES.length;
  };
  ids.sort((a, b) => rank(a) - rank(b) || a.localeCompare(b));
  return [
    {
      id: "modality",
      label: "Modality",
      options: ids.map((value) => ({
        value,
        label: modalityLabel(value),
        count: modality.get(value) ?? 0,
      })),
    },
    {
      id: "region",
      label: "Region",
      options: byCount(tally("region"), (v) => v).map((o) => ({
        ...o,
        cssVar: regionColour(o.value).cssVar,
      })),
    },
    {
      id: "species",
      label: "Species",
      options: byCount(tally("species"), (v) => v),
    },
    {
      id: "archive",
      label: "Archive",
      options: byCount(tally("archive"), (v) => archiveLabel(v) ?? v),
    },
  ];
}

/**
 * The home's model. `query` narrows the study rows, and with them the matrix columns, the summary and
 * the region bars; the tier counts, the facet options and their counts stay those of every study, and
 * so does the MRI-and-ephys rail.
 */
export function homeModel(body: HomeBody, query: HomeQuery = EMPTY_QUERY): HomeModel {
  const byId = new Map(body.datasets.map((d) => [d.dataset, d]));
  const person = personIds(body);
  const inStudy = new Set(body.studies.flatMap((s) => s.datasets.map((d) => d.dataset)));
  const present = new Set<string>();
  const rows: StudyRow[] = [];
  const rail: RailEntry[] = [];
  const allPeople = new Set<string>();
  const allRegions = new Set<string>();
  const speciesSeen = new Set<string>();
  let datasetCount = 0;

  const groups: {
    id: string;
    title: string;
    href: string;
    refs: { dataset: string; role: string }[];
  }[] = body.studies.map((s) => ({
    id: s.study,
    title: s.title,
    href: studyPath(s.study),
    refs: s.datasets.map((d) => ({ ...d })),
  }));
  for (const d of body.datasets) {
    if (d.tier === "curated" && !inStudy.has(d.dataset)) {
      groups.push({
        id: `dataset:${d.dataset}`,
        title: d.title ?? d.dataset,
        href: datasetViewPath(d.dataset),
        refs: [{ dataset: d.dataset, role: "" }],
      });
    }
  }

  for (const g of groups) {
    const datasets = g.refs.map((r) => byId.get(r.dataset));
    const members: Member[] = [];
    for (const d of datasets) {
      if (d !== undefined) for (const s of d.subjects) members.push({ dataset: d, subject: s });
    }
    rail.push(...railFor(g.title, members, body, new Set(g.refs.map((r) => r.dataset))));
    if (!matches(attrsOf(datasets), query)) continue;
    const people = new Set(members.map((m) => person(m.dataset.dataset, m.subject.subject)));
    const withModality = new Map<string, Set<string>>();
    for (const m of members) {
      const p = person(m.dataset.dataset, m.subject.subject);
      for (const x of m.subject.modalities) {
        withModality.set(x, (withModality.get(x) ?? new Set()).add(p));
        present.add(x);
      }
    }
    for (const p of people) allPeople.add(`${g.id}\u0000${p}`);
    datasetCount += g.refs.length;
    const species = [...new Set(members.map((m) => m.subject.species).filter((s) => s !== null))];
    for (const s of species) speciesSeen.add(s);

    const weights = new Map<string, number>();
    const unitsOf = new Map<string, number>();
    for (const d of datasets) {
      for (const r of d?.regions ?? []) {
        unitsOf.set(r.region, (unitsOf.get(r.region) ?? 0) + r.units);
        weights.set(r.region, (weights.get(r.region) ?? 0) + (r.units > 0 ? r.units : r.channels));
        allRegions.add(r.region);
      }
    }
    const total = [...weights.values()].reduce((a, b) => a + b, 0);
    const regions = [...weights]
      .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
      .map(([label, w]) => ({
        label: label.trim(),
        units: unitsOf.get(label) ?? 0,
        share: total === 0 ? 0 : w / total,
        cssVar: regionColour(label).cssVar,
      }));
    const unitTotal = [...unitsOf.values()].reduce((a, b) => a + b, 0);
    const regionSummary =
      regions.length === 0
        ? "No regions recorded"
        : `${plural(regions.length, "region", "regions")} · ${plural(unitTotal, "unit", "units")}`;

    const cells: Record<string, number> = {};
    for (const [id, set] of withModality) cells[id] = set.size;
    const names = [...withModality.keys()].map(modalityLabel);
    const oneLiner = [
      plural(g.refs.length, "dataset", "datasets"),
      names.length === 0 ? "no recordings yet" : names.join(", "),
      regions.length === 0 ? null : plural(regions.length, "region", "regions"),
    ]
      .filter((p) => p !== null)
      .join(" · ");

    rows.push({
      id: g.id,
      title: g.title,
      href: g.href,
      meta: [...species, plural(people.size, "participant", "participants")].join(" · "),
      oneLiner,
      releases: g.refs.map((r) => release(byId.get(r.dataset), r.dataset, r.role)),
      cells,
      regions,
      regionSummary,
    });
  }

  const named = DEFAULT_MODALITIES.filter((m) => present.has(m.id));
  const extra = [...present].filter((id) => !DEFAULT_MODALITIES.some((m) => m.id === id));
  const columns: Column[] = [
    ...named,
    ...extra.filter((id) => !id.startsWith(OTHER)).sort(),
    ...extra.filter((id) => id.startsWith(OTHER)).sort(),
  ].map((m) => (typeof m === "string" ? { id: m, label: modalityLabel(m) } : m));

  const mine = body.datasets.filter((d) => d.tier === "upload" && d.mine);
  const others = body.datasets.filter((d) => d.tier === "upload" && !d.mine);
  const sandbox = sandboxFold(body.datasets);
  const summary = [
    plural(datasetCount, "archive dataset", "archive datasets"),
    plural(allPeople.size, "participant", "participants"),
    allRegions.size === 0 ? null : `from ${plural(allRegions.size, "region", "regions")}`,
    speciesSeen.size === 0 ? null : [...speciesSeen].join(" and "),
  ]
    .filter((p) => p !== null)
    .join(" · ");
  return {
    tiers: { curated: groups.length, mine: mine.length, sandbox: sandbox.count },
    summary,
    facets: facetGroups(groups.map((g) => attrsOf(g.refs.map((r) => byId.get(r.dataset))))),
    columns,
    rows,
    rail,
    mine,
    others,
    sandbox,
  };
}
