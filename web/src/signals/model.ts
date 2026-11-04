// The signals view's model: pure functions over what `GET /compute/overview`,
// `GET /compute/spectrogram` and the electrodes table return. Every number here is computed in the
// browser for inspection only and never saved: the band selector averages the overview's log bands in
// linear power, the window spectrogram is averaged into the overview's own band edges so both pictures
// share one log frequency axis, and the file's shared dB range is read from the overview.
//
// Channel to region: the catalog's series table does not record which electrodes a series' channels
// are. A channel takes the region of the electrodes row of the same index only when the electrodes
// table has exactly one row per channel of the series; otherwise the view says the regions are not
// known for this series, and draws the channels in recording order.
import type { Row } from "../catalog/catalog";
import { normalizeRegion, regionColour } from "../kit/regions";
import type { SpectrogramBody } from "../lfp/lfp";
import type { Overview } from "../lfp/overview";
import { type Grid, valueRange } from "../plots/heatmap";

// --- bands -------------------------------------------------------------------------------------------

export interface Band {
  id: string;
  label: string;
  lo: number;
  hi: number;
}

/** The band choices: the 4-30 Hz band first, then the classic bands, then everything. */
export const BANDS: readonly Band[] = [
  { id: "4-30", label: "4–30 Hz", lo: 4, hi: 30 },
  { id: "1-4", label: "Delta 1–4 Hz", lo: 1, hi: 4 },
  { id: "4-8", label: "Theta 4–8 Hz", lo: 4, hi: 8 },
  { id: "8-13", label: "Alpha 8–13 Hz", lo: 8, hi: 13 },
  { id: "13-30", label: "Beta 13–30 Hz", lo: 13, hi: 30 },
  { id: "30-80", label: "Gamma 30–80 Hz", lo: 30, hi: 80 },
  { id: "80-250", label: "High gamma 80–250 Hz", lo: 80, hi: 250 },
  { id: "1-250", label: "All 1–250 Hz", lo: 1, hi: 250 },
];

export const DEFAULT_BAND = "4-30";

/** The band named by `band=` in `search`; an unknown or absent value gives the default. */
export function parseBand(search: string): Band {
  const id = new URLSearchParams(search).get("band") ?? DEFAULT_BAND;
  return BANDS.find((b) => b.id === id) ?? (BANDS[0] as Band);
}

/** `search` with `band=` set; the default band is left out of the URL. */
export function bandSearch(search: string, band: string): string {
  const q = new URLSearchParams(search);
  q.delete("band");
  if (band !== DEFAULT_BAND) q.set("band", band);
  const text = q.toString();
  return text === "" ? "" : `?${text}`;
}

/** The overview bands whose geometric centre lies in [lo, hi]. */
export function bandIndices(edges: ArrayLike<number>, lo: number, hi: number): number[] {
  const out: number[] = [];
  for (let b = 0; b + 1 < edges.length; b += 1) {
    const centre = Math.sqrt((edges[b] ?? 0) * (edges[b + 1] ?? 0));
    if (centre >= lo && centre <= hi) out.push(b);
  }
  return out;
}

/**
 * One value per channel and time: the bands' mean power density over [lo, hi], in dB. Each band's dB
 * becomes linear power, is weighted by its width in Hz, summed and divided by the summed widths, then
 * goes back to dB, so the result is in the same units as one band (and as the window spectrogram).
 * Laid out [channel][time].
 */
export function bandPowerDb(o: Overview, bands: readonly number[]): Float32Array {
  const out = new Float32Array(o.nChannels * o.nTimes).fill(Number.NaN);
  const widths = bands.map((b) => (o.edges[b + 1] ?? 0) - (o.edges[b] ?? 0));
  const total = widths.reduce((a, w) => a + w, 0);
  if (bands.length === 0 || !(total > 0)) return out;
  for (let c = 0; c < o.nChannels; c += 1) {
    for (let t = 0; t < o.nTimes; t += 1) {
      const at = (c * o.nTimes + t) * o.nBands;
      let sum = 0;
      for (let i = 0; i < bands.length; i += 1) {
        sum += 10 ** ((o.db[at + (bands[i] ?? 0)] ?? Number.NaN) / 10) * (widths[i] ?? 0);
      }
      out[c * o.nTimes + t] = 10 * Math.log10(sum / total);
    }
  }
  return out;
}

/** The file's dB range: the 2nd to 98th percentile of every overview value (all channels, all bands). */
export function sharedRange(o: Overview): [number, number] {
  return valueRange({
    nx: o.db.length,
    ny: 1,
    at: (ix) => o.db[ix] ?? Number.NaN,
  });
}

// --- channels and regions ----------------------------------------------------------------------------

export interface ChannelGroup {
  /** The region label as the author wrote it, or "region not known". */
  label: string;
  /** The custom property of the region colour, or null when the region is not known. */
  token: string | null;
  /** Channel indices of the series, in recording order. */
  channels: number[];
}

export interface ChannelLayout {
  /** True when each channel's region came from the electrodes row of the same index. */
  mapped: boolean;
  groups: ChannelGroup[];
  /** Channels top to bottom as drawn: group by group. */
  order: number[];
  /** A channel's region label, or null when not known. */
  regionOf: (channel: number) => string | null;
}

export const UNKNOWN_REGION = "region not known";

function locationOf(row: Row | undefined): string | null {
  const v = row?.location;
  return typeof v === "string" && v.trim() !== "" ? v.trim() : null;
}

/**
 * Group the series' channels by region, groups in first-seen order. `electrodes` are the file's
 * electrodes rows (any order; matched by `electrode_index`).
 */
export function channelLayout(nChannels: number, electrodes: readonly Row[] | null): ChannelLayout {
  const byIndex = new Map<number, Row>();
  for (const r of electrodes ?? []) {
    if (typeof r.electrode_index === "number") byIndex.set(r.electrode_index, r);
  }
  const mapped =
    electrodes !== null && electrodes.length === nChannels && byIndex.size === nChannels;
  const all = Array.from({ length: nChannels }, (_v, i) => i);
  if (!mapped) {
    return {
      mapped: false,
      groups: [{ label: UNKNOWN_REGION, token: null, channels: all }],
      order: all,
      regionOf: () => null,
    };
  }
  const groups = new Map<string, ChannelGroup>();
  const labels: (string | null)[] = all.map((c) => locationOf(byIndex.get(c)));
  for (const c of all) {
    const label = labels[c] ?? null;
    const key = label === null ? "" : normalizeRegion(label).key;
    let g = groups.get(key);
    if (g === undefined) {
      g = {
        label: label ?? UNKNOWN_REGION,
        token: label === null ? null : regionColour(label).token,
        channels: [],
      };
      groups.set(key, g);
    }
    g.channels.push(c);
  }
  const list = [...groups.values()];
  return {
    mapped: true,
    groups: list,
    order: list.flatMap((g) => g.channels),
    regionOf: (c) => labels[c] ?? null,
  };
}

/** The overview grid as drawn: one row per channel in `order` (row 0 at the top), one column per time. */
export function overviewGrid(values: Float32Array, nTimes: number, order: readonly number[]): Grid {
  const n = order.length;
  return {
    nx: nTimes,
    ny: n,
    // `renderHeatmap` puts grid row 0 at the bottom: the first channel in `order` is the last grid row.
    at: (ix, iy) => values[(order[n - 1 - iy] ?? 0) * nTimes + ix] ?? Number.NaN,
  };
}

// --- the window spectrogram on the overview's log axis -----------------------------------------------

/**
 * The window spectrogram (linear frequency bins, dB) averaged into the overview's bands, as the
 * builder averages the overview (`analysis.band_power`): a band takes the bins with lo <= f < hi (the
 * last band includes hi), averaged in linear power; a band with no bin takes the bin nearest its
 * geometric centre. Returns dB laid out [band][time].
 */
export function logBinned(body: SpectrogramBody, edges: ArrayLike<number>): number[][] {
  const { freqs, power, times } = body;
  const nBands = edges.length - 1;
  const out: number[][] = [];
  for (let b = 0; b < nBands; b += 1) {
    const lo = edges[b] ?? 0;
    const hi = edges[b + 1] ?? 0;
    let members: number[] = [];
    freqs.forEach((f, i) => {
      if (f >= lo && (b === nBands - 1 ? f <= hi : f < hi)) members.push(i);
    });
    if (members.length === 0 && freqs.length > 0) {
      const centre = Math.sqrt(lo * hi);
      let best = 0;
      freqs.forEach((f, i) => {
        if (Math.abs(f - centre) < Math.abs((freqs[best] ?? 0) - centre)) best = i;
      });
      members = [best];
    }
    const row: number[] = [];
    for (let t = 0; t < times.length; t += 1) {
      let sum = 0;
      for (const i of members) sum += 10 ** ((power[i]?.[t] ?? Number.NaN) / 10);
      row.push(members.length === 0 ? Number.NaN : 10 * Math.log10(sum / members.length));
    }
    out.push(row);
  }
  return out;
}

// --- the overview's "not built" answers --------------------------------------------------------------

const SKIPPED = /^(?:series not indexed|the series has no uniform rate): (.+)$/s;

/** The builder's reason when the overview route says it skipped this series, else null. */
export function skippedReason(message: string): string | null {
  const m = SKIPPED.exec(message.trim());
  return m?.[1] ?? null;
}
