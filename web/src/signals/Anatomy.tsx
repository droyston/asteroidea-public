// The anatomy panel of the signals view: this person's T1w in three
// planes and a readout cell, with NiiVue loaded only when asked (the volume is megabytes). Until then the
// planes are placeholders. A T1w reached through a link that is not confirmed says "link pending".
import { type ReactNode, useCallback, useState } from "react";
import type { AuthStore } from "../auth/store";
import { doFetch } from "../catalog/loadable";
import { Button } from "../kit/Button";
import { Notice } from "../kit/Notice";
import { refusal } from "../mri/mri";
import { Volume } from "../mri/Volume";
import { formatBytes } from "../table/facets";
import { useLoad } from "../useLoad";
import { loadT1w, type T1wFile } from "./t1w";

export type AnatomyState =
  | { kind: "loading" }
  | { kind: "none" }
  | { kind: "error"; message: string; onRetry: () => void }
  | { kind: "found"; file: T1wFile };

const PLANES = ["axial", "coronal", "sagittal"] as const;

export function AnatomyView({
  state,
  onOpen,
  viewer,
}: {
  state: AnatomyState;
  onOpen: () => void;
  /** The open viewer; null until the T1w is asked for. */
  viewer: ReactNode;
}) {
  const file = state.kind === "found" ? state.file : null;
  const refused = file === null ? null : refusal(file.header, file.sizeBytes);
  return (
    <section className="signals-anatomy" aria-label="Anatomy" data-testid="anatomy">
      <div className="signals-aside-head">
        <h3 className="signals-head">Anatomy · this participant</h3>
        {file === null ? null : (
          <span className="meta" data-testid="anatomy-source">
            T1w from {file.dataset}
            {file.pending ? (
              <>
                {" "}
                <span className="signals-pending" data-testid="anatomy-link-pending">
                  link pending
                </span>
              </>
            ) : null}
          </span>
        )}
      </div>
      {state.kind === "loading" ? <p className="meta">Finding this person's T1w…</p> : null}
      {state.kind === "none" ? (
        <p className="meta" data-testid="anatomy-none">
          The catalog holds no T1w for this participant, in this dataset or through a link.
        </p>
      ) : null}
      {state.kind === "error" ? (
        <Notice
          kind="error"
          testId="anatomy-error"
          action={{ label: "Try again", onClick: state.onRetry }}
        >
          {state.message}
        </Notice>
      ) : null}
      {file !== null && viewer !== null ? viewer : null}
      {file !== null && viewer === null ? (
        <>
          <div className="signals-planes" aria-hidden="true">
            {PLANES.map((p) => (
              <figure key={p} className="signals-plane" data-ab="anatomy-plane">
                <figcaption>{p}</figcaption>
              </figure>
            ))}
            <figure className="signals-plane signals-plane-readout">
              <figcaption>x y z · voxel · value</figcaption>
            </figure>
          </div>
          {refused === null ? (
            <Button onClick={onOpen} data-testid="anatomy-open">
              Load the T1w ({formatBytes(file.sizeBytes)})
            </Button>
          ) : (
            <Notice kind="info" testId="file-image-refused">
              {refused}
            </Notice>
          )}
        </>
      ) : null}
      <p className="meta">
        The viewer loads on demand
        {file === null ? "" : ` (${formatBytes(file.sizeBytes)})`}. Electrodes cannot be placed: no
        coordinates in this release.
      </p>
    </section>
  );
}

export function Anatomy({
  auth,
  dataset,
  subject,
}: {
  auth: AuthStore;
  dataset: string | null;
  subject: string | null;
}) {
  const run = useCallback(
    () =>
      dataset === null || subject === null
        ? Promise.resolve({ kind: "loaded" as const, value: { kind: "none" as const } })
        : loadT1w(auth.config, dataset, subject, auth, doFetch),
    [auth, dataset, subject],
  );
  const { state, reload } = useLoad(auth, run);
  const [open, setOpen] = useState(false);
  let view: AnatomyState;
  if (state.kind === "loaded") view = state.value;
  else if (state.kind === "error")
    view = { kind: "error", message: state.message, onRetry: reload };
  else view = { kind: "loading" };
  const file = view.kind === "found" ? view.file : null;
  return (
    <AnatomyView
      state={view}
      onOpen={() => setOpen(true)}
      viewer={
        open && file !== null ? (
          <Volume
            auth={auth}
            assetId={file.assetId}
            header={file.header}
            sizeBytes={file.sizeBytes}
            s3Key={file.s3Key}
            compact
          />
        ) : null
      }
    />
  );
}
