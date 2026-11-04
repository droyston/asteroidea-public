// Loads the `imports` chunk (the curator's half of the picks rail) on first use. A chunk that fails to
// load becomes the reload Notice.
import { type ReactNode, useEffect, useState } from "react";
import { Notice } from "../kit/Notice";
import { type ChunkResult, loadChunk, renderChunk } from "../router/lazy";
import type { CuratorRailProps } from "./ImportRun";

type Imports = typeof import("./ImportRun");

let pending: Promise<ChunkResult<Imports>> | null = null;
function imports(): Promise<ChunkResult<Imports>> {
  pending ??= loadChunk(() => import("./ImportRun")).then((r) => {
    if (r.kind === "stale") pending = null;
    return r;
  });
  return pending;
}

/** The curator's import button and run; the chunk loads when a curator's rail first shows. */
export function CuratorRailLoader(props: CuratorRailProps): ReactNode {
  const [chunk, setChunk] = useState<ChunkResult<Imports> | null>(null);
  useEffect(() => {
    let live = true;
    void imports().then((r) => {
      if (live) setChunk(r);
    });
    return () => {
      live = false;
    };
  }, []);
  if (chunk === null) {
    return (
      <Notice kind="info" testId="imports-chunk-loading">
        Loading…
      </Notice>
    );
  }
  return renderChunk(chunk, (m) => <m.CuratorRail {...props} />);
}
