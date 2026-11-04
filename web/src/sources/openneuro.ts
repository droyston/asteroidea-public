// OpenNeuro listing: one directory level per call, by tree id.
import {
  ArchiveError,
  type Cursor,
  type DatasetRef,
  type Fetch,
  type FilePage,
  type SourceAdapter,
} from "./types";

export const FILES_QUERY =
  "query($id:ID!,$tag:String!,$tree:String){snapshot(datasetId:$id,tag:$tag){files(tree:$tree){id filename size directory}}}";

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

function join(dir: string, name: string): string {
  return dir === "" ? name : `${dir}/${name}`;
}

export function openneuroAdapter(fetchFn: Fetch = (u, i) => fetch(u, i)): SourceAdapter {
  return {
    async listFiles(ds: DatasetRef, cursor: Cursor | null): Promise<FilePage> {
      const dir = cursor?.kind === "tree" ? cursor : { kind: "tree" as const, id: null, path: "" };
      const variables: Record<string, string> = { id: ds.id, tag: ds.version };
      if (dir.id !== null) variables.tree = dir.id;
      let res: Response;
      try {
        res = await fetchFn(ds.apiUrl, {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ query: FILES_QUERY, variables }),
        });
      } catch (err) {
        throw new ArchiveError("openneuro", null, err instanceof Error ? err.message : String(err));
      }
      let body: unknown;
      try {
        body = await res.json();
      } catch {
        throw new ArchiveError("openneuro", res.status, res.statusText || "response is not JSON");
      }
      if (isRecord(body) && Array.isArray(body.errors) && body.errors.length > 0) {
        const first: unknown = body.errors[0];
        const message =
          isRecord(first) && typeof first.message === "string" ? first.message : "GraphQL error";
        throw new ArchiveError("openneuro", res.status, message);
      }
      if (!res.ok)
        throw new ArchiveError("openneuro", res.status, res.statusText || "request failed");
      const data = isRecord(body) && isRecord(body.data) ? body.data : null;
      const snapshot = data !== null && isRecord(data.snapshot) ? data.snapshot : null;
      if (snapshot === null || !Array.isArray(snapshot.files)) {
        throw new ArchiveError("openneuro", res.status, `no snapshot ${ds.id} ${ds.version}`);
      }
      const entries = snapshot.files.map((f: unknown, i) => {
        if (!isRecord(f) || typeof f.filename !== "string" || typeof f.id !== "string") {
          throw new ArchiveError("openneuro", res.status, `files[${i}] has no id and filename`);
        }
        const path = join(dir.path, f.filename);
        const isDirectory = f.directory === true;
        return {
          name: f.filename,
          path,
          isDirectory,
          size: isDirectory || typeof f.size !== "number" ? null : f.size,
          open: isDirectory ? ({ kind: "tree", id: f.id, path } as const) : null,
        };
      });
      return { entries, next: null };
    },
  };
}
