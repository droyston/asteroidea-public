// A picked dataset's file listing in the picks rail: narrow the listed
// archive page by path text and by type, then show it ten rows at a time with counts, never a long
// scroll. Pure; `PickFiles` draws it.
import type { FileEntry } from "../sources/types";

export const FILE_ROWS = 10;
export const FOLDER = "folder";

const DOUBLE = [".nii.gz", ".tsv.gz", ".json.gz"];

/** A listing entry's type: "folder", a file extension (".nwb", ".nii.gz"), or "no extension". */
export function fileType(e: Pick<FileEntry, "name" | "isDirectory">): string {
  if (e.isDirectory) return FOLDER;
  const lower = e.name.toLowerCase();
  const double = DOUBLE.find((x) => lower.endsWith(x));
  if (double !== undefined) return double;
  const dot = lower.lastIndexOf(".");
  return dot <= 0 ? "no extension" : lower.slice(dot);
}

/** Each type on the page with its count, most common first. */
export function typeCounts(entries: readonly FileEntry[]): [string, number][] {
  const counts = new Map<string, number>();
  for (const e of entries) counts.set(fileType(e), (counts.get(fileType(e)) ?? 0) + 1);
  return [...counts].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
}

/** The entries whose path holds `text` (any case) and whose type is `type` ("" for any). */
export function narrow(entries: readonly FileEntry[], text: string, type: string): FileEntry[] {
  const needle = text.trim().toLowerCase();
  return entries.filter(
    (e) =>
      (needle === "" || e.path.toLowerCase().includes(needle)) &&
      (type === "" || fileType(e) === type),
  );
}

export interface Window<T> {
  rows: T[];
  /** 1-based first and last row shown; 0 and 0 when there is none. */
  first: number;
  last: number;
  total: number;
  page: number;
  pages: number;
}

/** Page `page` (0-based, clamped) of `rows`, `size` at a time. */
export function windowOf<T>(rows: readonly T[], page: number, size: number = FILE_ROWS): Window<T> {
  const pages = Math.max(1, Math.ceil(rows.length / size));
  const p = Math.min(Math.max(0, page), pages - 1);
  const shown = rows.slice(p * size, p * size + size);
  return {
    rows: shown,
    first: shown.length === 0 ? 0 : p * size + 1,
    last: p * size + shown.length,
    total: rows.length,
    page: p,
    pages,
  };
}

/** A byte count in the rail's short form. */
export function shortBytes(n: number | null): string {
  if (n === null) return "";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let v = n;
  let u = 0;
  while (v >= 1000 && u < units.length - 1) {
    v /= 1000;
    u += 1;
  }
  return `${u === 0 ? v : v.toFixed(1)} ${units[u]}`;
}
