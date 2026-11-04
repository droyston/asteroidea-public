// Loads the `Find` chunk on first use. A chunk that fails to load (gone after a deploy, or offline)
// becomes the reload Notice instead of a blank page.
import { type ReactNode, useEffect, useState } from "react";
import { Notice } from "../kit/Notice";
import { type ChunkResult, loadChunk, renderChunk } from "../router/lazy";
import type { FindProps } from "./Find";

type FindChunk = typeof import("./Find");

let pending: Promise<ChunkResult<FindChunk>> | null = null;
function find(): Promise<ChunkResult<FindChunk>> {
  pending ??= loadChunk(() => import("./Find")).then((r) => {
    if (r.kind === "stale") pending = null;
    return r;
  });
  return pending;
}

export function FindLoader(props: FindProps): ReactNode {
  const [chunk, setChunk] = useState<ChunkResult<FindChunk> | null>(null);
  useEffect(() => {
    let live = true;
    void find().then((r) => {
      if (live) setChunk(r);
    });
    return () => {
      live = false;
    };
  }, []);
  if (chunk === null) {
    return (
      <Notice kind="info" testId="find-loading">
        Loading…
      </Notice>
    );
  }
  return renderChunk(chunk, (m) => <m.Find {...props} />);
}
