// The whole-recording LFP overview: the server sends it as inline JSON or, when large, as a
// presigned GET of the binary `ASTLFPOV` object (ingest/src/asteroidea_ingest/lfp_overview.py `dumps`).
// The binary layout, version 1, all little-endian:
//   bytes 0..7    the magic "ASTLFPOV"
//   bytes 8..11   u32 header length H
//   bytes 12..    H bytes of UTF-8 JSON: version, shape [channels, times, bands], dtype "<f2", edges,
//                 rate_hz, segment_s, starting_time
//   then          times: n_times x f64 (segment centres, seconds from the series' first sample)
//   then          dB: channels x times x bands x f16, C order
// `Overview.db` is a Float32Array in the same order; an f16 widens to f32 exactly.

export const MAGIC = "ASTLFPOV";
export const LEAD_BYTES = MAGIC.length + 4;

export interface Overview {
  nChannels: number;
  nTimes: number;
  nBands: number;
  /** n_times segment centres, seconds from the series' first sample. */
  times: Float64Array;
  /** n_bands + 1 band edges in Hz. */
  edges: Float64Array;
  /** dB, laid out [channel][time][band]. */
  db: Float32Array;
  rateHz: number;
  startingTime: number;
}

/** The f16 bit pattern as a number (IEEE 754 half precision, exact widening). */
export function halfToFloat(h: number): number {
  const sign = h & 0x8000 ? -1 : 1;
  const exp = (h >> 10) & 0x1f;
  const frac = h & 0x3ff;
  if (exp === 0) return sign * frac * 2 ** -24;
  if (exp === 31) return frac === 0 ? sign * Number.POSITIVE_INFINITY : Number.NaN;
  return sign * (1 + frac / 1024) * 2 ** (exp - 15);
}

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

function numberList(v: unknown, name: string): number[] {
  if (!Array.isArray(v) || !v.every((x) => typeof x === "number")) {
    throw new Error(`"${name}" is not a list of numbers`);
  }
  return v as number[];
}

const NOT_OVERVIEW = "not a version-1 LFP overview:";

/** Parse the binary overview; throws an Error naming what is wrong. */
export function parseOverviewBinary(buffer: ArrayBuffer): Overview {
  if (buffer.byteLength < LEAD_BYTES) throw new Error(`${NOT_OVERVIEW} too short`);
  const bytes = new Uint8Array(buffer);
  for (let i = 0; i < MAGIC.length; i += 1) {
    if (bytes[i] !== MAGIC.charCodeAt(i)) throw new Error(`${NOT_OVERVIEW} bad magic`);
  }
  const view = new DataView(buffer);
  const headLen = view.getUint32(MAGIC.length, true);
  if (LEAD_BYTES + headLen > buffer.byteLength) throw new Error(`${NOT_OVERVIEW} short header`);
  let head: unknown;
  try {
    head = JSON.parse(new TextDecoder().decode(bytes.subarray(LEAD_BYTES, LEAD_BYTES + headLen)));
  } catch (err) {
    throw new Error(`${NOT_OVERVIEW} header is not JSON (${String(err)})`);
  }
  if (!isRecord(head) || head.version !== 1 || head.dtype !== "<f2") {
    throw new Error(`${NOT_OVERVIEW} unknown version or dtype`);
  }
  const shape = numberList(head.shape, "shape");
  const edges = numberList(head.edges, "edges");
  const [nChannels, nTimes, nBands] = shape;
  if (
    shape.length !== 3 ||
    nChannels === undefined ||
    nTimes === undefined ||
    nBands === undefined ||
    edges.length !== nBands + 1
  ) {
    throw new Error(`${NOT_OVERVIEW} shape ${JSON.stringify(shape)} with ${edges.length} edges`);
  }
  const bodyAt = LEAD_BYTES + headLen;
  const count = nChannels * nTimes * nBands;
  if (buffer.byteLength - bodyAt !== nTimes * 8 + count * 2) {
    throw new Error(`${NOT_OVERVIEW} ${buffer.byteLength - bodyAt} body bytes for shape`);
  }
  const times = new Float64Array(nTimes);
  for (let i = 0; i < nTimes; i += 1) times[i] = view.getFloat64(bodyAt + i * 8, true);
  const dbAt = bodyAt + nTimes * 8;
  const db = new Float32Array(count);
  for (let i = 0; i < count; i += 1) db[i] = halfToFloat(view.getUint16(dbAt + i * 2, true));
  return {
    nChannels,
    nTimes,
    nBands,
    times,
    edges: Float64Array.from(edges),
    db,
    rateHz: typeof head.rate_hz === "number" ? head.rate_hz : 0,
    startingTime: typeof head.starting_time === "number" ? head.starting_time : 0,
  };
}

/** The inline JSON form (`times_s`, `band_edges_hz`, `channels`, `power_db[channel][time][band]`). */
export function parseOverviewJson(raw: Record<string, unknown>): Overview {
  const times = numberList(raw.times_s, "times_s");
  const edges = numberList(raw.band_edges_hz, "band_edges_hz");
  const channels = numberList(raw.channels, "channels");
  const rows = raw.power_db;
  if (!Array.isArray(rows) || rows.length !== channels.length) {
    throw new Error('"power_db" does not hold one entry per channel');
  }
  const nBands = edges.length - 1;
  const db = new Float32Array(channels.length * times.length * nBands);
  rows.forEach((channel: unknown, c) => {
    if (!Array.isArray(channel) || channel.length !== times.length) {
      throw new Error(`power_db[${c}] does not hold one row per time`);
    }
    channel.forEach((row: unknown, t) => {
      const values = numberList(row, `power_db[${c}][${t}]`);
      if (values.length !== nBands)
        throw new Error(`power_db[${c}][${t}] is not one value per band`);
      db.set(values, (c * times.length + t) * nBands);
    });
  });
  return {
    nChannels: channels.length,
    nTimes: times.length,
    nBands,
    times: Float64Array.from(times),
    edges: Float64Array.from(edges),
    db,
    rateHz: typeof raw.rate_hz === "number" ? raw.rate_hz : 0,
    startingTime: typeof raw.starting_time_s === "number" ? raw.starting_time_s : 0,
  };
}

/** What `/compute/overview` answered: the overview itself, or where to fetch it. */
export type OverviewReply =
  | { kind: "inline"; overview: Overview }
  | { kind: "url"; url: string; expiresIn: number };

export function parseOverviewReply(raw: unknown): OverviewReply {
  if (!isRecord(raw)) throw new Error("the response is not an object");
  if (typeof raw.url === "string") {
    if (!raw.url.startsWith("https://")) throw new Error('"url" is not an https address');
    return {
      kind: "url",
      url: raw.url,
      expiresIn: typeof raw.expires_in === "number" ? raw.expires_in : 0,
    };
  }
  return { kind: "inline", overview: parseOverviewJson(raw) };
}

/** The recording's length: the last segment centre plus half a segment step. */
export function totalSeconds(o: Overview): number {
  if (o.nTimes === 0) return 0;
  const last = o.times[o.nTimes - 1] ?? 0;
  const step = o.nTimes > 1 ? last - (o.times[o.nTimes - 2] ?? 0) : 1;
  return last + step / 2;
}

/** The half-width of one time cell, seconds. */
export function halfStep(times: ArrayLike<number>): number {
  const n = times.length;
  return n > 1 ? ((times[n - 1] ?? 0) - (times[0] ?? 0)) / (n - 1) / 2 : 0.5;
}
