// Loads the `research` chunk on first use. A chunk that fails to load (gone after a
// deploy, or offline) becomes the reload Notice instead of a blank page.
import { type ReactNode, useEffect, useState } from "react";
import type { AuthStore } from "../auth/store";
import { Notice } from "../kit/Notice";
import { type ChunkResult, loadChunk, renderChunk } from "../router/lazy";
import type { ResearchRoute } from "./research";

type Research = typeof import("./research");

let pending: Promise<ChunkResult<Research>> | null = null;
function research(): Promise<ChunkResult<Research>> {
  pending ??= loadChunk(() => import("./research")).then((r) => {
    if (r.kind === "stale") pending = null;
    return r;
  });
  return pending;
}

export function ResearchLoader({
  auth,
  route,
}: {
  auth: AuthStore;
  route: ResearchRoute;
}): ReactNode {
  const [chunk, setChunk] = useState<ChunkResult<Research> | null>(null);
  useEffect(() => {
    let live = true;
    void research().then((r) => {
      if (live) setChunk(r);
    });
    return () => {
      live = false;
    };
  }, []);
  if (chunk === null) {
    return (
      <Notice kind="info" testId="research-loading">
        Loading…
      </Notice>
    );
  }
  return renderChunk(chunk, (m) => <m.ResearchPage auth={auth} route={route} />);
}
