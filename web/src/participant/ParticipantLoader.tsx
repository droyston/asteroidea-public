// Loads the `participant` chunk on first use. A chunk that fails to load (gone after a deploy, or
// offline) becomes the reload Notice instead of a blank page.
import { type ReactNode, useEffect, useState } from "react";
import type { AuthStore } from "../auth/store";
import { Notice } from "../kit/Notice";
import { type ChunkResult, loadChunk, renderChunk } from "../router/lazy";

type Participant = typeof import("./participant");

let pending: Promise<ChunkResult<Participant>> | null = null;
function participant(): Promise<ChunkResult<Participant>> {
  pending ??= loadChunk(() => import("./participant")).then((r) => {
    if (r.kind === "stale") pending = null;
    return r;
  });
  return pending;
}

export function ParticipantLoader({
  auth,
  participant: key,
  study,
}: {
  auth: AuthStore;
  participant: string;
  study: string;
}): ReactNode {
  const [chunk, setChunk] = useState<ChunkResult<Participant> | null>(null);
  useEffect(() => {
    let live = true;
    void participant().then((r) => {
      if (live) setChunk(r);
    });
    return () => {
      live = false;
    };
  }, []);
  if (chunk === null) {
    return (
      <Notice kind="info" testId="participant-loading">
        Loading…
      </Notice>
    );
  }
  return renderChunk(chunk, (m) => (
    <m.ParticipantPage key={`${study}/${key}`} auth={auth} participant={key} />
  ));
}
