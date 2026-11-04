// The participant page's pure model: one person across archives. The
// identity is the set of subject records the links tie to one `subject_key` (a subject no link
// names is a person of one record, keyed `<dataset>:<subject>`), the bridge between them with its status,
// `linked_by`, date and basis, the modality timeline, the ephys cards, the T1w target and the
// regions panel. No fetch and no React here.
//
// A recording is a tick on a lane. The studies route lists a subject's sessions and modalities but not
// which modality belongs to which session, so the lanes follow one rule: an NWB session is one tick on
// the Ephys lane; a session with no NWB is one tick on each BIDS lane the subject has (anat, func, ieeg,
// other:*). A tick sits at its session's start time when every recording has one, else at its rank.
import { normalizeRegion, regionColour } from "../kit/regions";
import { NO_SESSION, parseUnlinkedKey, participantPath } from "../router/dataPaths";
import { modalityLabel, type StudiesBody, type Subject } from "../study/model";

export interface PLink {
  readonly dataset: string;
  readonly subject: string;
  readonly subject_key: string;
  readonly status: string;
  readonly linked_by: string | null;
  readonly linked_at: string | null;
  readonly basis: string | null;
  readonly note: string | null;
}

type Rec = Readonly<Record<string, unknown>>;

function isRec(v: unknown): v is Rec {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

function str(v: unknown, name: string): string {
  if (typeof v !== "string") throw new Error(`"${name}" is not a string`);
  return v;
}

function strOrNull(v: unknown, name: string): string | null {
  return v === null || v === undefined ? null : str(v, name);
}

/** `GET /catalog/links` rows, with the `basis` and `note` the study page does not read. */
export function parseParticipantLinks(raw: unknown): PLink[] {
  if (!isRec(raw)) throw new Error('"body" is not an object');
  const list = raw.links;
  if (!Array.isArray(list)) throw new Error('"links" is not a list');
  return (list as unknown[]).map((row, i) => {
    if (!isRec(row)) throw new Error(`"links[${i}]" is not an object`);
    return {
      dataset: str(row.dataset, `links[${i}].dataset`),
      subject: str(row.subject, `links[${i}].subject`),
      subject_key: str(row.subject_key, `links[${i}].subject_key`),
      status: str(row.status, `links[${i}].status`),
      linked_by: strOrNull(row.linked_by, "linked_by"),
      linked_at: strOrNull(row.linked_at, "linked_at"),
      basis: strOrNull(row.basis, "basis"),
      note: strOrNull(row.note, "note"),
    };
  });
}

export interface SexAge {
  readonly sex: string | null;
  readonly age: string | null;
}

/** `sex` and `age` per `<dataset>/<subject>` from the studies body (the study page's parser drops them). */
export function sexAgeOf(raw: unknown): Map<string, SexAge> {
  const out = new Map<string, SexAge>();
  if (!isRec(raw) || !Array.isArray(raw.datasets)) return out;
  for (const d of raw.datasets as unknown[]) {
    if (!isRec(d) || typeof d.dataset !== "string" || !Array.isArray(d.subjects)) continue;
    for (const s of d.subjects as unknown[]) {
      if (!isRec(s) || typeof s.subject !== "string") continue;
      out.set(`${d.dataset}/${s.subject}`, {
        sex: typeof s.sex === "string" ? s.sex : null,
        age: typeof s.age === "string" || typeof s.age === "number" ? String(s.age) : null,
      });
    }
  }
  return out;
}

export function sessionKeyOf(dataset: string, subject: string, session: string): string {
  return `${dataset}/${subject}/${session === "" ? NO_SESSION : session}`;
}

/** What the page needs beyond the studies and links bodies. Both are best effort. */
export interface Extras {
  readonly sexAge: ReadonlyMap<string, SexAge>;
  /** Session start times by `sessionKeyOf`, from the dataset route. */
  readonly dates: Readonly<Record<string, string>>;
}

export const NO_EXTRAS: Extras = { sexAge: new Map(), dates: {} };

export interface RecordFact {
  readonly dataset: string;
  readonly subject: string;
  readonly archive: string | null;
  readonly version: string | null;
  readonly title: string | null;
  readonly datasetHref: string;
  readonly modalities: readonly string[];
  readonly sessions: number;
  /** The first session label, for the card's second line. */
  readonly firstSession: string | null;
}

export interface Bridge {
  readonly confirmed: boolean;
  readonly status: string;
  readonly linkedBy: string | null;
  readonly date: string | null;
  readonly basis: string | null;
  readonly note: string | null;
}

export interface Tick {
  readonly session: string;
  readonly dataset: string;
  readonly label: string;
  /** 0 to 1 along the axis. */
  readonly frac: number;
}

export interface Lane {
  readonly key: string;
  readonly label: string;
  readonly ticks: readonly Tick[];
}

export interface Axis {
  readonly kind: "time" | "order";
  readonly from: string;
  readonly to: string;
}

export interface EphysSession {
  readonly id: string;
  readonly dataset: string;
  readonly subject: string;
  readonly session: string;
  readonly label: string;
  readonly assetId: string;
  readonly units: number;
  readonly lfpChannels: number;
  readonly lfpRates: readonly number[];
  readonly trials: number | null;
  readonly time: number | null;
}

export interface T1wTarget {
  readonly dataset: string;
  readonly subject: string;
  readonly session: string;
  /** The session's file ids in path order; the page asks the preview route in turn. */
  readonly assetIds: readonly string[];
}

/**
 * The T1w card's state. The preview route is deferred, so a T1w is always "not built": the card
 * names the target's first file (anat sorts first in path order) and links the file page's MRI tab.
 */
export type T1wState =
  | { readonly kind: "missing"; readonly assetId: string }
  | { readonly kind: "none" };

export function t1wState(target: T1wTarget | null): T1wState {
  const id = target?.assetIds[0];
  return id === undefined ? { kind: "none" } : { kind: "missing", assetId: id };
}

export interface RegionRelease {
  readonly region: string;
  readonly channels: number;
}

export interface ParticipantModel {
  readonly key: string;
  readonly heading: string;
  readonly linked: boolean;
  readonly studyKey: string | null;
  readonly studyTitle: string | null;
  readonly species: string | null;
  readonly sex: string | null;
  readonly age: string | null;
  readonly records: readonly RecordFact[];
  readonly bridge: Bridge | null;
  readonly lanes: readonly Lane[];
  readonly axis: Axis;
  readonly ephys: readonly EphysSession[];
  /** Ephys sessions beyond the six shown. */
  readonly ephysHidden: number;
  readonly t1w: T1wTarget | null;
  readonly releaseRegions: readonly RegionRelease[];
}

export const CARDS_SHOWN = 6;

const LANE_ORDER = ["ephys", "ieeg", "anat", "func"] as const;
const LANE_LABEL: Readonly<Record<string, string>> = {
  ephys: "Ephys",
  ieeg: "iEEG",
  anat: "Anatomical MRI",
  func: "Functional MRI",
};

function laneLabel(key: string): string {
  return LANE_LABEL[key] ?? modalityLabel(key);
}

function laneRank(key: string): number {
  const i = (LANE_ORDER as readonly string[]).indexOf(key);
  return i === -1 ? LANE_ORDER.length : i;
}

const BIDS_NOT_LANES = new Set(["units", "lfp", "task"]);

function timeOf(stamp: string | undefined): number | null {
  if (stamp === undefined) return null;
  const t = Date.parse(stamp.includes("T") ? stamp : stamp.replace(" ", "T"));
  return Number.isNaN(t) ? null : t;
}

function dayOf(ms: number): string {
  return new Date(ms).toISOString().slice(0, 10);
}

interface Found {
  readonly dataset: string;
  readonly subject: Subject;
}

function pickRecordLinks(
  param: string,
  links: readonly PLink[],
): { key: string; pairs: { dataset: string; subject: string }[]; rows: readonly PLink[] } | null {
  if (links.length > 0) {
    const key = links[0]?.subject_key ?? param;
    const rows = links.filter((l) => l.subject_key === key);
    const seen = new Set<string>();
    const pairs: { dataset: string; subject: string }[] = [];
    for (const l of rows) {
      const id = `${l.dataset}/${l.subject}`;
      if (!seen.has(id)) {
        seen.add(id);
        pairs.push({ dataset: l.dataset, subject: l.subject });
      }
    }
    return { key, pairs, rows };
  }
  const one = parseUnlinkedKey(param);
  return one === null ? null : { key: param, pairs: [one], rows: [] };
}

function bridgeOf(rows: readonly PLink[], records: number): Bridge | null {
  if (records < 2 || rows.length === 0) return null;
  const confirmed = rows.every((l) => l.status === "confirmed");
  const open = rows.find((l) => l.status !== "confirmed");
  const first = <T>(pick: (l: PLink) => T | null): T | null => {
    for (const l of rows) {
      const v = pick(l);
      if (v !== null) return v;
    }
    return null;
  };
  const stamp = first((l) => l.linked_at);
  return {
    confirmed,
    status: confirmed ? "confirmed" : (open?.status ?? "pending"),
    linkedBy: first((l) => l.linked_by),
    date: stamp === null ? null : stamp.slice(0, 10) || null,
    basis: first((l) => l.basis),
    note: first((l) => l.note),
  };
}

function sessionLabel(session: string): string {
  return session === "" || session === NO_SESSION ? "(no session)" : session;
}

/**
 * The model for the participant segment `param` (a `subject_key`, or `<dataset>:<subject>`), from the
 * studies body, the links the loader read for that person and the extras. Null when no record of the
 * person is in the studies body.
 */
export function participantModel(
  param: string,
  studies: StudiesBody,
  links: readonly PLink[],
  extras: Extras = NO_EXTRAS,
): ParticipantModel | null {
  const picked = pickRecordLinks(param, links);
  if (picked === null) return null;
  const datasets = new Map(studies.datasets.map((d) => [d.dataset, d]));
  const found: Found[] = [];
  for (const p of picked.pairs) {
    const subject = datasets.get(p.dataset)?.subjects.find((s) => s.subject === p.subject);
    if (subject !== undefined) found.push({ dataset: p.dataset, subject });
  }
  if (found.length === 0) return null;

  const records: RecordFact[] = found.map((f) => {
    const d = datasets.get(f.dataset);
    return {
      dataset: f.dataset,
      subject: f.subject.subject,
      archive: d?.source_archive ?? null,
      version: d?.source_version ?? null,
      title: d?.title ?? null,
      datasetHref: `/d/${encodeURIComponent(f.dataset)}`,
      modalities: f.subject.modalities.map(modalityLabel),
      sessions: f.subject.sessions.length,
      firstSession: f.subject.sessions[0]?.session ?? null,
    };
  });

  // Ephys sessions and ticks.
  const ephysAll: EphysSession[] = [];
  const raw: { lane: string; dataset: string; session: string; subject: string }[] = [];
  for (const f of found) {
    const nwbSessions = f.subject.sessions.filter((s) => s.nwb.length > 0).length;
    for (const s of f.subject.sessions) {
      if (s.nwb.length > 0) {
        raw.push({
          lane: "ephys",
          dataset: f.dataset,
          session: s.session,
          subject: f.subject.subject,
        });
        const asset = [...s.nwb].sort(
          (a, b) => (b.n_units ?? 0) - (a.n_units ?? 0) || a.asset_id.localeCompare(b.asset_id),
        )[0];
        if (asset !== undefined && (asset.n_units ?? 0) > 0) {
          const series = f.subject.lfp.filter((l) => s.asset_ids.includes(l.asset_id));
          ephysAll.push({
            id: sessionKeyOf(f.dataset, f.subject.subject, s.session),
            dataset: f.dataset,
            subject: f.subject.subject,
            session: s.session,
            label: sessionLabel(s.session),
            assetId: asset.asset_id,
            units: asset.n_units ?? 0,
            lfpChannels: series.reduce((n, l) => n + l.channels, 0),
            lfpRates: [
              ...new Set(series.flatMap((l) => (l.rate_hz === null ? [] : [l.rate_hz]))),
            ].sort((a, b) => a - b),
            trials: nwbSessions === 1 ? f.subject.trials : null,
            time: timeOf(extras.dates[sessionKeyOf(f.dataset, f.subject.subject, s.session)]),
          });
        }
      } else {
        const lanes = f.subject.modalities.filter((m) => !BIDS_NOT_LANES.has(m));
        for (const lane of lanes.length === 0 ? ["other:files"] : lanes) {
          raw.push({ lane, dataset: f.dataset, session: s.session, subject: f.subject.subject });
        }
      }
    }
  }

  // The axis: start times when every session has one, else the sessions' rank.
  const sessionIds = [...new Set(raw.map((t) => sessionKeyOf(t.dataset, t.subject, t.session)))];
  const times = new Map(sessionIds.map((id) => [id, timeOf(extras.dates[id])]));
  const timed = sessionIds.length > 0 && [...times.values()].every((t) => t !== null);
  const stamps = [...times.values()].flatMap((t) => (t === null ? [] : [t]));
  const lo = Math.min(...stamps);
  const hi = Math.max(...stamps);
  const ranked = [...sessionIds].sort((a, b) => {
    const ta = times.get(a) ?? Number.POSITIVE_INFINITY;
    const tb = times.get(b) ?? Number.POSITIVE_INFINITY;
    return ta - tb || a.localeCompare(b);
  });
  const fracOf = (id: string): number => {
    if (timed) {
      const t = times.get(id) ?? lo;
      return hi === lo ? 0.5 : (t - lo) / (hi - lo);
    }
    return ranked.length < 2 ? 0.5 : ranked.indexOf(id) / (ranked.length - 1);
  };
  const byLane = new Map<string, Tick[]>();
  for (const t of raw) {
    const id = sessionKeyOf(t.dataset, t.subject, t.session);
    const ticks = byLane.get(t.lane) ?? [];
    ticks.push({
      session: t.session,
      dataset: t.dataset,
      label: sessionLabel(t.session),
      frac: fracOf(id),
    });
    byLane.set(t.lane, ticks);
  }
  const lanes: Lane[] = [...byLane.entries()]
    .sort(([a], [b]) => laneRank(a) - laneRank(b) || a.localeCompare(b))
    .map(([key, ticks]) => ({
      key,
      label: laneLabel(key),
      ticks: [...ticks].sort((a, b) => a.frac - b.frac || a.label.localeCompare(b.label)),
    }));

  const ephys = [...ephysAll].sort(
    (a, b) => (b.time ?? 0) - (a.time ?? 0) || a.id.localeCompare(b.id),
  );

  // The T1w target: the first non-NWB session of a record with an anat modality.
  let t1w: T1wTarget | null = null;
  for (const f of found) {
    if (t1w !== null || !f.subject.modalities.includes("anat")) continue;
    const s = f.subject.sessions.find((x) => x.nwb.length === 0 && x.asset_ids.length > 0);
    if (s !== undefined) {
      t1w = {
        dataset: f.dataset,
        subject: f.subject.subject,
        session: s.session,
        assetIds: s.asset_ids,
      };
    }
  }

  // Heading, study, demographics.
  const lead = found.find((f) => f.subject.units > 0) ?? found[0];
  const ids = new Set(found.map((f) => f.dataset));
  const study = studies.studies.find((s) => s.datasets.some((d) => ids.has(d.dataset)));
  const first = (pick: (x: SexAge) => string | null): string | null => {
    for (const f of found) {
      const v = pick(
        extras.sexAge.get(`${f.dataset}/${f.subject.subject}`) ?? { sex: null, age: null },
      );
      if (v !== null) return v;
    }
    return null;
  };
  return {
    key: picked.key,
    heading: lead?.subject.subject ?? picked.key,
    linked: picked.rows.length > 0 && records.length > 1,
    studyKey: study?.study ?? null,
    studyTitle: study?.title ?? null,
    species: found.map((f) => f.subject.species).find((s) => s !== null) ?? null,
    sex: first((x) => x.sex),
    age: first((x) => x.age),
    records,
    bridge: bridgeOf(picked.rows, records.length),
    lanes,
    axis: timed
      ? { kind: "time", from: dayOf(lo), to: dayOf(hi) }
      : { kind: "order", from: "first", to: "last" },
    ephys: ephys.slice(0, CARDS_SHOWN),
    ephysHidden: Math.max(0, ephys.length - CARDS_SHOWN),
    t1w,
    releaseRegions: [...ids].flatMap((d) =>
      (datasets.get(d)?.regions ?? []).map((r) => ({ region: r.region, channels: r.channels })),
    ),
  };
}

/** This model's page address, for links back to it. */
export function modelPath(m: ParticipantModel): string {
  return participantPath(m.key, m.studyKey ?? undefined);
}

// ---- the timeline layout ----

export const MERGE_PX = 6;

export interface Block {
  readonly x: number;
  readonly count: number;
  readonly label: string;
}

/**
 * The lane's ticks at `width` px: a tick closer than `MERGE_PX` to the block before it joins that
 * block, which then reads as a count. A block sits at its first tick.
 */
export function layoutLane(ticks: readonly Tick[], width: number, pad = 8): Block[] {
  const span = Math.max(0, width - 2 * pad);
  const blocks: { x: number; count: number; label: string }[] = [];
  for (const t of ticks) {
    const x = pad + t.frac * span;
    const last = blocks.at(-1);
    if (last !== undefined && x - last.x < MERGE_PX) last.count += 1;
    else blocks.push({ x, count: 1, label: t.label });
  }
  return blocks.map((b) => ({
    x: b.x,
    count: b.count,
    label: b.count === 1 ? b.label : `${b.count} recordings`,
  }));
}

// ---- the regions panel ----

export interface RegionLine {
  readonly key: string;
  readonly label: string;
  readonly cssVar: string;
  readonly units: number;
  /** Electrode channels at this region over the person's releases (the release's count, not the person's); null when the release lists none. */
  readonly channels: number | null;
}

/** Units per region from the unit labels of the person's sessions, with the release's channels beside. */
export function regionLines(
  locations: readonly (string | null)[],
  release: readonly RegionRelease[],
): RegionLine[] {
  const by = new Map<string, { label: string; units: number }>();
  for (const loc of locations) {
    if (loc === null) continue;
    const key = normalizeRegion(loc).key;
    const prev = by.get(key);
    by.set(key, { label: prev?.label ?? loc, units: (prev?.units ?? 0) + 1 });
  }
  const channels = new Map<string, number>();
  for (const r of release) {
    const key = normalizeRegion(r.region).key;
    channels.set(key, (channels.get(key) ?? 0) + r.channels);
  }
  return [...by.entries()]
    .map(([key, v]) => ({
      key,
      label: v.label,
      cssVar: regionColour(v.label).cssVar,
      units: v.units,
      channels: channels.get(key) ?? null,
    }))
    .sort((a, b) => b.units - a.units || a.label.localeCompare(b.label));
}
