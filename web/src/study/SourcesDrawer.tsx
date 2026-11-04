// The Source files drawer: a release's files, opened on demand and never a
// list on the page. It reads the catalog's assets route in 200-row pages ("Load more" asks the next
// page) and groups what it holds by recording: participant, session, datatype. Each group is folded;
// its files open one level down.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { AuthStore } from "../auth/store";
import type { CatalogAssetRow } from "../catalog/catalog";
import { filePath } from "../catalog/depth";
import { doFetch } from "../catalog/loadable";
import { Dialog } from "../kit/Dialog";
import { Notice } from "../kit/Notice";
import { formatBytes } from "../table/facets";
import { loadSourcePage, type RecordingGroup, recordingGroups } from "./sources";

interface Paged {
  readonly rows: readonly CatalogAssetRow[];
  /** The last page loaded, or -1 before the first. */
  readonly page: number;
  readonly more: boolean;
  readonly busy: boolean;
  readonly error: string | null;
}

const FIRST: Paged = { rows: [], page: -1, more: false, busy: true, error: null };

function plural(n: number, one: string, many: string = `${one}s`): string {
  return `${n} ${n === 1 ? one : many}`;
}

export function GroupList({ groups }: { groups: readonly RecordingGroup[] }) {
  return (
    <ul className="sources-groups" data-testid="sources-groups">
      {groups.map((g) => (
        <li key={g.key} data-ab="recording-group">
          <details>
            <summary>
              <strong>{g.subject}</strong> · {g.session} · {g.datatype}{" "}
              <small>
                {plural(g.files.length, "file")}
                {g.bytes === null ? "" : `, ${formatBytes(g.bytes)}`}
              </small>
            </summary>
            <ul className="sources-files">
              {g.files.map((f) => (
                <li key={f.assetId}>
                  <a href={filePath(f.assetId)}>{f.path}</a>{" "}
                  <small>{f.bytes === null ? "" : formatBytes(f.bytes)}</small>
                </li>
              ))}
            </ul>
          </details>
        </li>
      ))}
    </ul>
  );
}

function Loaded({
  auth,
  dataset,
  files,
}: {
  auth: AuthStore;
  dataset: string;
  /** The release's file count from the studies body, for "n of N". */
  files: number | null;
}) {
  const [state, setState] = useState<Paged>(FIRST);
  const ticket = useRef(0);
  const ask = useCallback(
    (page: number) => {
      ticket.current += 1;
      const mine = ticket.current;
      setState((s) => ({ ...s, busy: true, error: null }));
      void loadSourcePage(auth.config, dataset, page, auth, doFetch).then((r) => {
        if (mine !== ticket.current) return;
        if (r.kind === "ended") {
          auth.expire();
        } else if (r.kind === "error") {
          setState((s) => ({ ...s, busy: false, error: r.message }));
        } else {
          setState((s) => ({
            rows: [...s.rows, ...r.value.assets],
            page,
            more: r.value.more,
            busy: false,
            error: null,
          }));
        }
      });
    },
    [auth, dataset],
  );
  useEffect(() => {
    ask(0);
    return () => {
      ticket.current += 1;
    };
  }, [ask]);
  const groups = useMemo(() => recordingGroups(state.rows), [state.rows]);
  return (
    <div className="sources" data-testid="sources">
      <p className="sources-count" data-testid="sources-count">
        {`${plural(state.rows.length, "file")} read${files === null ? "" : ` of ${files}`}, in ${plural(groups.length, "recording group")}.`}
      </p>
      {state.error === null ? null : (
        <Notice
          kind="error"
          testId="sources-error"
          action={{ label: "Try again", onClick: () => ask(state.page + 1) }}
        >
          {state.error}
        </Notice>
      )}
      <GroupList groups={groups} />
      {state.busy ? (
        <p className="meta" data-testid="sources-loading">
          Loading…
        </p>
      ) : null}
      {state.more && !state.busy && state.error === null ? (
        <button
          type="button"
          className="sources-more"
          data-testid="sources-more"
          onClick={() => ask(state.page + 1)}
        >
          Load the next 200 files
        </button>
      ) : null}
    </div>
  );
}

/** The drawer: closed while `dataset` is null; opening it starts the first page's fetch. */
export function SourcesDrawer({
  auth,
  dataset,
  files,
  onClose,
}: {
  auth: AuthStore;
  dataset: string | null;
  files: number | null;
  onClose: () => void;
}) {
  return (
    <Dialog
      open={dataset !== null}
      title={dataset === null ? "Source files" : `Source files of ${dataset}`}
      onClose={onClose}
      testId="sources-drawer"
    >
      {dataset === null ? null : (
        <Loaded key={dataset} auth={auth} dataset={dataset} files={files} />
      )}
      <div className="dialog-actions">
        <button type="button" className="sources-close" onClick={onClose}>
          Close
        </button>
      </div>
    </Dialog>
  );
}
