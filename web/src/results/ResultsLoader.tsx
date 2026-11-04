// Loads the `results` chunk on first use. A chunk that fails to load becomes the reload
// Notice.
import { type ReactNode, useEffect, useState } from "react";
import type { AuthStore } from "../auth/store";
import { Notice } from "../kit/Notice";
import { type ChunkResult, loadChunk, renderChunk } from "../router/lazy";
import type { ResultsRoute } from "./ResultsPlace";

type Results = typeof import("./ResultsPlace");

let pending: Promise<ChunkResult<Results>> | null = null;
function results(): Promise<ChunkResult<Results>> {
  pending ??= loadChunk(() => import("./ResultsPlace")).then((r) => {
    if (r.kind === "stale") pending = null;
    return r;
  });
  return pending;
}

export function ResultsLoader({
  auth,
  route,
}: {
  auth: AuthStore;
  route: ResultsRoute;
}): ReactNode {
  const [chunk, setChunk] = useState<ChunkResult<Results> | null>(null);
  useEffect(() => {
    let live = true;
    void results().then((r) => {
      if (live) setChunk(r);
    });
    return () => {
      live = false;
    };
  }, []);
  if (chunk === null) {
    return (
      <Notice kind="info" testId="results-chunk-loading">
        Loading…
      </Notice>
    );
  }
  return renderChunk(chunk, (m) => <m.ResultsPlace auth={auth} route={route} />);
}
