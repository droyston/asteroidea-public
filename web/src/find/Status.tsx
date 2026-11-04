// The index status line (moved from the old Discover view): the harvest time and each archive's counts,
// or the notice naming why the index is not there. `App.tsx` re-exports `Status`.
import type { LoadState } from "../index/load";
import type { Index, SourceName } from "../index/schema";
import { Notice } from "../kit/Notice";

export function Status({ state }: { state: LoadState }) {
  if (state.kind === "loaded") return <Summary index={state.index} />;
  return (
    <div className="status-note">
      <StatusNotice state={state} />
    </div>
  );
}

function StatusNotice({ state }: { state: LoadState }) {
  switch (state.kind) {
    case "loading":
      return <Notice kind="info">Loading index.json…</Notice>;
    case "missing":
      return (
        <Notice kind="error">
          <strong>No index: run make harvest</strong> <span className="detail">{state.detail}</span>
        </Notice>
      );
    case "error":
      return (
        <Notice kind="error">
          <strong>Index error</strong> <span className="detail">{state.message}</span>
        </Notice>
      );
    case "loaded":
      return null;
  }
}

export function Summary({ index }: { index: Index }) {
  const names = Object.keys(index.sources) as SourceName[];
  return (
    <div className="status" role="status" data-testid="index-summary">
      <span>
        Index harvested <time dateTime={index.harvested_at}>{index.harvested_at}</time>
      </span>
      {names.map((name) => {
        const s = index.sources[name];
        if (s === undefined) return null;
        return (
          <details key={name} className="source-summary">
            <summary>
              {name}: {s.ok} ok of {s.listed} listed, {s.failed.length} failed
              {s.null_nodes ? `, ${s.null_nodes} null nodes` : ""}
            </summary>
            {s.failed.length > 0 ? (
              <ul>
                {s.failed.map((f) => (
                  <li key={`${f.id}-${f.version ?? ""}`}>
                    {f.id} {f.version ?? ""}: {f.error_type}: {f.message} ({f.attempts} attempts,{" "}
                    {f.url}){f.stale ? "; stale: showing the record from an earlier harvest" : ""}
                  </li>
                ))}
              </ul>
            ) : null}
          </details>
        );
      })}
    </div>
  );
}
