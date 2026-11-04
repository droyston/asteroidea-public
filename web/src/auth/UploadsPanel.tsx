// "My uploads" page: shown only when signed in. UploadsView is presentational so the
// states render in a plain Node test; UploadsPanel owns the fetch.
import { useCallback } from "react";
import "./uploads.css";
import { Button } from "../kit/Button";
import { Notice } from "../kit/Notice";
import { useLoad } from "../useLoad";
import type { AuthStore } from "./store";
import { loadUploads, type Upload, type UploadsState } from "./uploads";

export function formatCreated(epochSeconds: number): string {
  const d = new Date(epochSeconds * 1000);
  return Number.isNaN(d.getTime())
    ? String(epochSeconds)
    : `${d.toISOString().slice(0, 19).replace("T", " ")} UTC`;
}

const STATE_WORDS: Readonly<Record<string, string>> = {
  OPEN: "Uploading",
  FINALIZING: "Finishing",
  ACCEPTED: "Received, entering the catalog",
  COMMITTED: "In the catalog",
  INCOMPLETE: "Did not finish: upload the file again",
};

/** Plain words for an upload state; an unknown state shows as the server wrote it. */
export function stateWords(state: string): string {
  return STATE_WORDS[state] ?? state;
}

export const SDK_UPLOAD_CALL = `import asteroidea

asteroidea.upload("sub-01/anat/sub-01_T1w.nii", dataset="my-dataset")`;

export interface UploadsViewProps {
  state: UploadsState;
  onRetry: () => void;
}

export function UploadsView({ state, onRetry }: UploadsViewProps) {
  switch (state.kind) {
    case "loading":
      return (
        <p className="uploads-note" role="status" data-testid="uploads-loading">
          Loading your uploads…
        </p>
      );
    case "error":
      return (
        <Notice
          kind="error"
          testId="uploads-error"
          action={{ label: "Try again", onClick: onRetry }}
        >
          {state.message}
        </Notice>
      );
    case "ended":
      return null;
    case "loaded":
      return state.uploads.length === 0 ? (
        <p className="uploads-empty" data-testid="uploads-empty">
          No uploads of yours yet. Upload a file with the SDK call below and it appears here.
        </p>
      ) : (
        <UploadsTable uploads={state.uploads} />
      );
  }
}

function UploadsTable({ uploads }: { uploads: readonly Upload[] }) {
  return (
    <div className="table-wrap">
      <table className="table uploads-table" aria-label="Your uploads" data-testid="uploads-table">
        <thead>
          <tr>
            <th>Dataset</th>
            <th>File</th>
            <th>State</th>
            <th>Started</th>
          </tr>
        </thead>
        <tbody>
          {uploads.map((u) => (
            <tr key={u.session_id}>
              <td className="uploads-dataset">{u.dataset}</td>
              <td className="what">{u.relpath}</td>
              <td title={u.state}>{stateWords(u.state)}</td>
              <td>
                <time dateTime={new Date(u.created_at * 1000).toISOString()}>
                  {formatCreated(u.created_at)}
                </time>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function UploadsPanel({ auth }: { auth: AuthStore }) {
  const run = useCallback(
    () => loadUploads(auth.config, auth, (url, init) => fetch(url, init)),
    [auth],
  );
  const { state, reload: load } = useLoad(auth, run);

  return (
    <section className="uploads-page" aria-label="My uploads" data-testid="uploads-panel">
      <header className="uploads-head">
        <h2>My uploads</h2>
        <Button variant="quiet" onClick={load} disabled={state.kind === "loading"}>
          Refresh
        </Button>
      </header>
      <UploadsView state={state} onRetry={load} />
      <section className="uploads-sdk" aria-label="Upload from Python">
        <h3>Upload a file</h3>
        <p>From Python, after you have run the Setup command:</p>
        <pre className="codeblock" data-testid="uploads-sdk">
          <code>{SDK_UPLOAD_CALL}</code>
        </pre>
      </section>
    </section>
  );
}
