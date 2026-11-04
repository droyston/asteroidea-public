// Loads the `signals` chunk on first use (the file page's Signals tab, and a BIDS events file's Events
// tab). A chunk that fails to load (gone after a deploy, or offline) becomes the reload Notice instead of
// a blank tab.
import { type ReactNode, useEffect, useState } from "react";
import type { AuthStore } from "../auth/store";
import { Notice } from "../kit/Notice";
import { type ChunkResult, loadChunk, renderChunk } from "../router/lazy";

type Signals = typeof import("./signals");

let pending: Promise<ChunkResult<Signals>> | null = null;
function signals(): Promise<ChunkResult<Signals>> {
  pending ??= loadChunk(() => import("./signals")).then((r) => {
    if (r.kind === "stale") pending = null;
    return r;
  });
  return pending;
}

export function SignalsLoader({
  auth,
  assetId,
  dataset,
  subject,
  view = "signals",
}: {
  auth: AuthStore;
  assetId: string;
  dataset: string | null;
  subject: string | null;
  /** `events`: a BIDS events file's lanes and labels, without the LFP views. */
  view?: "signals" | "events";
}): ReactNode {
  const [chunk, setChunk] = useState<ChunkResult<Signals> | null>(null);
  useEffect(() => {
    let live = true;
    void signals().then((r) => {
      if (live) setChunk(r);
    });
    return () => {
      live = false;
    };
  }, []);
  if (chunk === null) {
    return (
      <Notice kind="info" testId="lfp-loading">
        Loading…
      </Notice>
    );
  }
  const who = { dataset, subject };
  return renderChunk(chunk, (m) =>
    view === "events" ? (
      <m.EventsFile auth={auth} assetId={assetId} who={who} />
    ) : (
      <m.Signals auth={auth} assetId={assetId} who={who} />
    ),
  );
}
