// The files of one picked dataset, opened under it in the picks rail: the
// archive's own listing through `sources/*`, one archive page or folder at a time, narrowed by path
// and type, ten rows at a time with counts. A checked file is picked under the dataset; a dataset
// with no file checked stays "whole dataset".
import { useEffect, useMemo, useState } from "react";
import { Button } from "../kit/Button";
import { Notice } from "../kit/Notice";
import { adapterFor } from "../sources/registry";
import type { Cursor, DatasetRef, FilePage } from "../sources/types";
import { FOLDER, narrow, shortBytes, typeCounts, windowOf } from "./files";

export interface PickFilesProps {
  dataset: DatasetRef;
  picked: ReadonlySet<string>;
  onToggle: (path: string) => void;
}

type Listing =
  | { kind: "loading" }
  | { kind: "loaded"; page: FilePage }
  | { kind: "error"; message: string };

export function PickFiles({ dataset, picked, onToggle }: PickFilesProps) {
  // Each step down (a DANDI page, an OpenNeuro folder) pushes the cursor that lists it.
  const [trail, setTrail] = useState<(Cursor | null)[]>([null]);
  const [listing, setListing] = useState<Listing>({ kind: "loading" });
  const [text, setText] = useState("");
  const [type, setType] = useState("");
  const [row, setRow] = useState(0);
  const cursor = trail[trail.length - 1] ?? null;

  useEffect(() => {
    let live = true;
    setListing({ kind: "loading" });
    adapterFor(dataset.source)
      .listFiles(dataset, cursor)
      .then(
        (page) => {
          if (live) setListing({ kind: "loaded", page });
        },
        (err: unknown) => {
          if (live) {
            setListing({
              kind: "error",
              message: err instanceof Error ? err.message : String(err),
            });
          }
        },
      );
    return () => {
      live = false;
    };
  }, [dataset, cursor]);

  const entries = listing.kind === "loaded" ? listing.page.entries : [];
  const types = useMemo(() => typeCounts(entries), [entries]);
  const shown = useMemo(() => narrow(entries, text, type), [entries, text, type]);
  const win = windowOf(shown, row);
  const go = (next: (Cursor | null)[]) => {
    setTrail(next);
    setRow(0);
    setType("");
  };
  const where = cursor?.kind === "tree" ? cursor.path : "";
  const name = `${dataset.source === "dandi" ? "DANDI" : "OpenNeuro"} ${dataset.id}`;

  return (
    <div className="find-files" data-testid="pick-files">
      <div className="find-files-narrow">
        <input
          type="search"
          aria-label={`Narrow ${name} files by path`}
          placeholder="narrow by path"
          value={text}
          onChange={(e) => {
            setText(e.target.value);
            setRow(0);
          }}
        />
        <select
          aria-label={`Narrow ${name} files by type`}
          value={type}
          onChange={(e) => {
            setType(e.target.value);
            setRow(0);
          }}
        >
          <option value="">any type ({entries.length})</option>
          {types.map(([t, n]) => (
            <option key={t} value={t}>
              {t} ({n})
            </option>
          ))}
        </select>
      </div>
      {trail.length > 1 ? (
        <p className="find-files-where">
          <Button variant="quiet" onClick={() => go(trail.slice(0, -1))}>
            Back
          </Button>{" "}
          {where === "" ? `archive page ${trail.length}` : `${where}/`}
        </p>
      ) : null}
      {listing.kind === "loading" ? (
        <p className="find-files-count" role="status">
          Listing files from {dataset.source === "dandi" ? "DANDI" : "OpenNeuro"}…
        </p>
      ) : null}
      {listing.kind === "error" ? (
        <Notice kind="error" testId="pick-files-error">
          Files could not be listed: {listing.message}
        </Notice>
      ) : null}
      {listing.kind === "loaded" ? (
        <>
          <p className="find-files-count" data-testid="pick-files-count">
            {win.total === 0
              ? `No file matches of ${entries.length} listed here.`
              : `${win.first}–${win.last} of ${win.total} matching · ${entries.length} listed here${listing.page.next === null ? "" : " · more on the next archive page"}`}
          </p>
          <ul className="find-files-list">
            {win.rows.map((e) =>
              e.isDirectory ? (
                <li key={e.path}>
                  <Button
                    variant="quiet"
                    onClick={() => go([...trail, e.open])}
                    aria-label={`Open folder ${e.path}`}
                  >
                    {e.name}/
                  </Button>
                  <small>{FOLDER}</small>
                </li>
              ) : (
                <li key={e.path}>
                  <label>
                    <input
                      type="checkbox"
                      checked={picked.has(e.path)}
                      onChange={() => onToggle(e.path)}
                      aria-label={`Pick ${e.path}`}
                    />
                    <span className="find-files-path" title={e.path}>
                      {e.path}
                    </span>
                  </label>
                  <small>{shortBytes(e.size)}</small>
                </li>
              ),
            )}
          </ul>
          <div className="find-files-pager">
            <Button variant="quiet" disabled={win.page === 0} onClick={() => setRow(win.page - 1)}>
              Previous 10
            </Button>
            <Button
              variant="quiet"
              disabled={win.page >= win.pages - 1}
              onClick={() => setRow(win.page + 1)}
            >
              Next 10
            </Button>
            {listing.page.next === null ? null : (
              <Button variant="quiet" onClick={() => go([...trail, listing.page.next])}>
                Next archive page
              </Button>
            )}
          </div>
        </>
      ) : null}
    </div>
  );
}
