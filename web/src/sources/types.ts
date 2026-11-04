// Source interface, TypeScript side: listing only, no checksums, no in-file reads.
import type { SourceName } from "../index/schema";

export type Fetch = (input: string, init?: RequestInit) => Promise<Response>;

/** What an adapter needs to know about a dataset; taken from an index record. */
export interface DatasetRef {
  source: SourceName;
  id: string;
  version: string;
  apiUrl: string;
}

/** Where a listing call continues: a DANDI page URL, or an OpenNeuro directory (tree id, path). */
export type Cursor =
  | { kind: "page"; url: string }
  | { kind: "tree"; id: string | null; path: string };

export interface FileEntry {
  name: string;
  path: string;
  isDirectory: boolean;
  size: number | null;
  /** For a directory: the cursor that lists it. */
  open: Cursor | null;
}

export interface FilePage {
  entries: FileEntry[];
  next: Cursor | null;
}

export interface SourceAdapter {
  listFiles(ds: DatasetRef, cursor: Cursor | null): Promise<FilePage>;
}

/** A failed archive call: the archive, its HTTP status (null when none) and its message. */
export class ArchiveError extends Error {
  override name = "ArchiveError";
  constructor(
    readonly archive: SourceName,
    readonly status: number | null,
    detail: string,
  ) {
    super(`${archive}: ${status === null ? "no response" : `HTTP ${status}`}: ${detail}`);
  }
}
