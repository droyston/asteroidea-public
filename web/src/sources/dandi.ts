// DANDI listing: the flat asset list, 100 per page.
import {
  ArchiveError,
  type Cursor,
  type DatasetRef,
  type Fetch,
  type FilePage,
  type SourceAdapter,
} from "./types";

export const DANDI_PAGE_SIZE = 100;

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

async function failure(res: Response): Promise<ArchiveError> {
  let detail = res.statusText;
  try {
    const body: unknown = await res.json();
    if (isRecord(body) && typeof body.detail === "string") detail = body.detail;
  } catch {
    // body was not JSON: keep the status text
  }
  return new ArchiveError("dandi", res.status, detail || "no message");
}

export function dandiAdapter(fetchFn: Fetch = (u, i) => fetch(u, i)): SourceAdapter {
  return {
    async listFiles(ds: DatasetRef, cursor: Cursor | null): Promise<FilePage> {
      const url =
        cursor?.kind === "page"
          ? cursor.url
          : `${ds.apiUrl.replace(/\/?$/, "/")}assets/?page_size=${DANDI_PAGE_SIZE}`;
      let res: Response;
      try {
        res = await fetchFn(url);
      } catch (err) {
        throw new ArchiveError("dandi", null, err instanceof Error ? err.message : String(err));
      }
      if (!res.ok) throw await failure(res);
      const body: unknown = await res.json();
      if (!isRecord(body) || !Array.isArray(body.results)) {
        throw new ArchiveError("dandi", res.status, "response has no results array");
      }
      const entries = body.results.map((r: unknown, i) => {
        if (!isRecord(r) || typeof r.path !== "string") {
          throw new ArchiveError("dandi", res.status, `results[${i}] has no string path`);
        }
        const name = r.path.split("/").pop() ?? r.path;
        return {
          name,
          path: r.path,
          isDirectory: false,
          size: typeof r.size === "number" ? r.size : null,
          open: null,
        };
      });
      const next =
        typeof body.next === "string" ? ({ kind: "page", url: body.next } as const) : null;
      return { entries, next };
    },
  };
}
