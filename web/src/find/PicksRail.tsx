// The picks rail (restyled from the old PickPanel): the count, the picked
// datasets with an editable note and Remove, Export list, Import list (whole-file validation), Clear.
// A picked dataset is a parent: "Files" opens its archive listing under it, and each file
// checked there is picked under the dataset; with none checked the pick is the whole dataset. A
// destructive step asks through the kit Dialog, not window.confirm. A curator gets the import control
// and the run in `extra`; anyone else gets "Ask a curator to import", which exports the pick list for
// a curator to load.
import { type ReactNode, useState } from "react";
import { Button } from "../kit/Button";
import { Dialog } from "../kit/Dialog";
import { Notice } from "../kit/Notice";
import type { DatasetRef } from "../sources/types";
import { downloadExport } from "../targets/export";
import {
  type DatasetPick,
  groupPicks,
  groupScope,
  type PickGroup,
  pickCount,
} from "../targets/groups";
import { parseImport, replaceMessage } from "../targets/import";
import type { Target } from "../targets/schema";
import type { StoreState, TargetsStore } from "../targets/store";
import { PickFiles } from "./PickFiles";

export interface PicksRailProps {
  store: TargetsStore;
  state: StoreState;
  notice: string | null;
  /** The archive listing for a picked dataset; null when the index does not hold it. */
  refFor?: (d: DatasetPick) => DatasetRef | null;
  /** The curator's import and run; absent for everyone else, who get "Ask a curator to import". */
  extra?: ReactNode;
}

type Ask = { kind: "clear" } | { kind: "replace"; targets: Target[]; message: string };

const archive = (d: DatasetPick) => (d.source === "dandi" ? "DANDI" : "OpenNeuro");

export function PicksRail({ store, state, notice, refFor, extra }: PicksRailProps) {
  const [importError, setImportError] = useState<string | null>(null);
  const [ask, setAsk] = useState<Ask | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const { targets } = state;
  const groups = groupPicks(targets);
  const count = groups.length;

  const onImport = async (file: File | undefined) => {
    if (file === undefined) return;
    setImportError(null);
    const result = parseImport(await file.text(), file.name);
    if (!result.ok) {
      setImportError(result.reason);
      return;
    }
    setAsk({
      kind: "replace",
      targets: result.targets,
      message: replaceMessage(count, pickCount(result.targets), file.name),
    });
  };

  const confirm = () => {
    if (ask?.kind === "clear") store.clear();
    if (ask?.kind === "replace") store.replace(ask.targets);
    setAsk(null);
  };

  return (
    <section className="find-picks" aria-label="Picks" data-testid="pick-panel">
      <header>
        <p className="find-label">
          Picks · <span data-testid="pick-count">{count}</span>
        </p>
        <div className="find-picks-links">
          <Button variant="quiet" onClick={() => downloadExport(targets)}>
            Export list
          </Button>
          <label className="find-import">
            Import list
            <input
              type="file"
              accept="application/json,.json"
              aria-label="Import"
              onChange={(e) => {
                const input = e.currentTarget;
                void onImport(input.files?.[0]).finally(() => {
                  input.value = "";
                });
              }}
            />
          </label>
          <Button
            variant="quiet"
            onClick={() => setAsk({ kind: "clear" })}
            disabled={targets.length === 0}
          >
            Clear
          </Button>
        </div>
      </header>
      {notice !== null ? <Notice kind="info">{notice}</Notice> : null}
      {state.error !== null ? <Notice kind="error">{state.error}</Notice> : null}
      {importError !== null ? <Notice kind="error">Import rejected: {importError}</Notice> : null}
      {count === 0 ? (
        <p className="find-picks-empty">No picks yet. Pick a dataset from the list.</p>
      ) : (
        <ol className="find-picks-list">
          {groups.map((g) => (
            <PickRow
              key={g.key}
              group={g}
              store={store}
              open={open === g.key}
              onOpen={() => setOpen(open === g.key ? null : g.key)}
              listing={refFor?.(g.dataset) ?? null}
            />
          ))}
        </ol>
      )}
      <div className="find-picks-import">
        {extra ?? (
          <Button
            variant="primary"
            disabled={targets.length === 0}
            onClick={() => downloadExport(targets)}
          >
            Ask a curator to import
          </Button>
        )}
        {extra === undefined ? (
          <p className="find-picks-hint">Exports your pick list; send the file to a curator.</p>
        ) : null}
      </div>
      <Dialog
        open={ask !== null}
        title={ask?.kind === "clear" ? `Clear all ${count} picks?` : "Replace the picks?"}
        onClose={() => setAsk(null)}
        testId="picks-dialog"
      >
        {ask?.kind === "replace" ? <p>{ask.message}</p> : <p>This empties the pick list.</p>}
        <div className="find-dialog-actions">
          <Button onClick={() => setAsk(null)}>Cancel</Button>
          <Button variant="primary" data-autofocus onClick={confirm}>
            {ask?.kind === "clear" ? "Clear" : "Replace"}
          </Button>
        </div>
      </Dialog>
    </section>
  );
}

function PickRow({
  group,
  store,
  open,
  onOpen,
  listing,
}: {
  group: PickGroup;
  store: TargetsStore;
  open: boolean;
  onOpen: () => void;
  listing: DatasetRef | null;
}) {
  const d = group.dataset;
  const id = `${d.dataset_id}`;
  const picked = new Set(group.files.map((f) => f.target.path ?? ""));
  return (
    <li data-testid="pick-entry" className="find-pick">
      <div className="find-pick-head">
        <span className="what" title={`${d.source} ${d.dataset_id} ${d.version}`}>
          {archive(d)} {d.dataset_id} · <span data-testid="pick-scope">{groupScope(group)}</span>
        </span>
        {group.entry === null ? null : (
          <input
            type="text"
            aria-label={`Note for ${id} (whole dataset)`}
            placeholder="add note"
            value={group.entry.target.note}
            onChange={(e) => store.setNote(group.entry?.index ?? -1, e.target.value)}
          />
        )}
        <Button variant="quiet" aria-expanded={open} aria-label={`Files of ${id}`} onClick={onOpen}>
          Files
        </Button>
        <Button
          variant="quiet"
          aria-label={`Remove ${id} (whole dataset)`}
          onClick={() => store.removeDataset(d)}
        >
          Remove
        </Button>
      </div>
      {group.files.length > 0 ? (
        <ul className="find-pick-files">
          {group.files.map((f) => (
            <li key={f.target.path} data-testid="pick-file">
              <span className="find-files-path" title={f.target.path ?? ""}>
                {f.target.path}
              </span>
              <input
                type="text"
                aria-label={`Note for ${id} ${f.target.path}`}
                placeholder="add note"
                value={f.target.note}
                onChange={(e) => store.setNote(f.index, e.target.value)}
              />
              <Button
                variant="quiet"
                aria-label={`Remove ${id} ${f.target.path}`}
                onClick={() => store.toggleFile(d, f.target.path ?? "")}
              >
                Remove
              </Button>
            </li>
          ))}
        </ul>
      ) : null}
      {open ? (
        listing === null ? (
          <p className="find-files-count">
            {archive(d)} {d.dataset_id} {d.version} is not in the archive index, so its files cannot
            be listed here.
          </p>
        ) : (
          <PickFiles
            dataset={listing}
            picked={picked}
            onToggle={(path) => store.toggleFile(d, path)}
          />
        )
      ) : null}
    </li>
  );
}
