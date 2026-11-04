// The Image tab's pure parts: the size and dimension refusals, the `mm=` URL state, the
// window/level arithmetic and the whole-file fetch with progress. Nothing here imports NiiVue.

/** Compressed limit: 64 MiB, the size of the file in the bucket. */
export const MAX_COMPRESSED_BYTES = 64 * 2 ** 20;
/** Decompressed limit: 512 MiB, voxels x bytes per voxel from the catalog header. */
export const MAX_DECOMPRESSED_BYTES = 512 * 2 ** 20;

/** What the catalog `asset_headers` row says about a NIfTI file (all optional there). */
export interface VolumeHeader {
  shape: readonly number[] | null;
  datatype: string | null;
}

export function volumeHeader(row: Readonly<Record<string, unknown>> | undefined): VolumeHeader {
  const shape = row?.nifti_shape;
  const dtype = row?.nifti_datatype;
  const dims =
    Array.isArray(shape) && shape.every((n) => typeof n === "number" && Number.isFinite(n))
      ? (shape as number[])
      : null;
  return { shape: dims, datatype: typeof dtype === "string" ? dtype : null };
}

/** Bytes per voxel from a numpy dtype name such as `int16`, `float32` or `complex64`; null when unknown. */
export function bytesPerVoxel(datatype: string | null): number | null {
  const bits = /(\d+)$/.exec(datatype ?? "")?.[1];
  if (bits === undefined) return null;
  const n = Number(bits) / 8;
  return Number.isInteger(n) && n > 0 ? n : null;
}

function mib(bytes: number): string {
  return `${(bytes / 2 ** 20).toFixed(1)} MiB`;
}

/** Why this volume is not opened, or null when it may be. Decided from the catalog alone, before any download. */
export function refusal(header: VolumeHeader, sizeBytes: number | null): string | null {
  const shape = header.shape;
  if (shape !== null && shape.length > 3 && shape.slice(3).some((n) => n > 1)) {
    return `This file is 4D (shape ${shape.join(" x ")}). Functional runs are not viewable yet.`;
  }
  if (sizeBytes !== null && sizeBytes > MAX_COMPRESSED_BYTES) {
    return `This file is ${mib(sizeBytes)} compressed; the viewer opens files up to ${mib(MAX_COMPRESSED_BYTES)}.`;
  }
  const per = bytesPerVoxel(header.datatype);
  if (shape !== null && per !== null) {
    const bytes = shape.reduce((a, n) => a * n, 1) * per;
    if (bytes > MAX_DECOMPRESSED_BYTES) {
      return `This file is ${mib(bytes)} decompressed; the viewer opens files up to ${mib(MAX_DECOMPRESSED_BYTES)}.`;
    }
  }
  return null;
}

/** An intensity for the readout: an integer as is, any other value to 4 significant digits. */
export function formatIntensity(v: number): string {
  if (!Number.isFinite(v) || Number.isInteger(v)) return String(v);
  return String(Number(v.toPrecision(4)));
}

/** `mm=x,y,z` from a search string, or null when absent or not three finite numbers. */
export function parseMm(search: string): [number, number, number] | null {
  const raw = new URLSearchParams(search).get("mm");
  if (raw === null) return null;
  const parts = raw.split(",").map((s) => (s.trim() === "" ? Number.NaN : Number(s)));
  const [x, y, z] = parts;
  if (parts.length !== 3 || x === undefined || y === undefined || z === undefined) return null;
  return [x, y, z].every(Number.isFinite) ? [x, y, z] : null;
}

/** `search` with `mm=` set to the position (two decimals, other parameters kept); null removes it. */
export function mmSearch(search: string, mm: readonly number[] | null): string {
  const q = new URLSearchParams(search);
  q.delete("mm");
  if (mm !== null) q.set("mm", mm.map((n) => (Math.round(n * 100) / 100).toFixed(2)).join(","));
  const text = q.toString();
  return text === "" ? "" : `?${text}`;
}

/** Window and level (centre, width) from a display range. */
export function toWindowLevel(min: number, max: number): { level: number; window: number } {
  return { level: (min + max) / 2, window: max - min };
}

/** Display range from window and level; the window never drops below `floor`. */
export function fromWindowLevel(level: number, window: number, floor = 0): [number, number] {
  const half = Math.max(window, floor) / 2;
  return [level - half, level + half];
}

export interface Progress {
  loaded: number;
  /** From Content-Length; null when the response has none (or CORS hides it). */
  total: number | null;
}

export type VolumeFetch =
  | { kind: "loaded"; buffer: ArrayBuffer }
  | { kind: "refused"; message: string }
  | { kind: "error"; message: string };

/**
 * One plain GET of the presigned URL (no credentials, no headers): the body is read as a stream so the
 * caller can show progress. A Content-Length over the compressed limit stops the read at once.
 */
export async function fetchVolume(
  url: string,
  fetchFn: (url: string, init: RequestInit) => Promise<Response>,
  onProgress: (p: Progress) => void,
  signal?: AbortSignal,
): Promise<VolumeFetch> {
  let res: Response;
  try {
    res = await fetchFn(url, { credentials: "omit", signal: signal ?? null });
  } catch (err) {
    return { kind: "error", message: `The download failed: ${String(err)}` };
  }
  if (!res.ok) return { kind: "error", message: `The download failed (HTTP ${res.status}).` };
  const header = Number(res.headers.get("content-length"));
  const total = Number.isFinite(header) && header > 0 ? header : null;
  if (total !== null && total > MAX_COMPRESSED_BYTES) {
    void res.body?.cancel();
    return {
      kind: "refused",
      message: `This file is ${mib(total)} compressed; the viewer opens files up to ${mib(MAX_COMPRESSED_BYTES)}.`,
    };
  }
  const reader = res.body?.getReader();
  if (reader === undefined) return { kind: "error", message: "The download has no body." };
  const parts: Uint8Array[] = [];
  let loaded = 0;
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      parts.push(value);
      loaded += value.length;
      if (loaded > MAX_COMPRESSED_BYTES) {
        void reader.cancel();
        return {
          kind: "refused",
          message: `This file is over ${mib(MAX_COMPRESSED_BYTES)} compressed.`,
        };
      }
      onProgress({ loaded, total });
    }
  } catch (err) {
    return { kind: "error", message: `The download failed: ${String(err)}` };
  }
  const out = new Uint8Array(loaded);
  let at = 0;
  for (const part of parts) {
    out.set(part, at);
    at += part.length;
  }
  return { kind: "loaded", buffer: out.buffer };
}
