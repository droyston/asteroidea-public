// The Results tab's loader: the results that used any of the session's
// files. A result record carries no session scope, so the list is the
// union over the session's files of `GET /artifacts?scope=all&input=<asset id>` (`loadUsedBy`),
// each result once.
import type { FetchFn, TokenSource } from "../auth/apiGet";
import type { AuthConfig } from "../auth/config";
import { type Artifact, type ListResult, loadUsedBy } from "../results/results";

/** Newest first; the first failing file ends the load with its message. */
export async function loadSessionResults(
  config: Pick<AuthConfig, "apiUrl">,
  assetIds: readonly string[],
  tokens: TokenSource,
  fetchFn: FetchFn,
): Promise<ListResult> {
  const done = await Promise.all(assetIds.map((id) => loadUsedBy(config, id, tokens, fetchFn)));
  const byId = new Map<string, Artifact>();
  for (const r of done) {
    if (r.kind !== "loaded") return r;
    for (const a of r.artifacts) if (!byId.has(a.artifact_id)) byId.set(a.artifact_id, a);
  }
  const artifacts = [...byId.values()].sort(
    (a, b) => b.created_at - a.created_at || a.artifact_id.localeCompare(b.artifact_id),
  );
  return { kind: "loaded", artifacts };
}
