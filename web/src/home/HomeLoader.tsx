// Loads the `home` chunk on first use. A chunk that fails to load (gone after a deploy, or offline)
// becomes the reload Notice instead of a blank page.
import { type ReactNode, useEffect, useState } from "react";
import type { AuthStore } from "../auth/store";
import { Notice } from "../kit/Notice";
import { type ChunkResult, loadChunk, renderChunk } from "../router/lazy";

type Home = typeof import("./home");

let pending: Promise<ChunkResult<Home>> | null = null;
function home(): Promise<ChunkResult<Home>> {
  pending ??= loadChunk(() => import("./home")).then((r) => {
    if (r.kind === "stale") pending = null;
    return r;
  });
  return pending;
}

export function HomeLoader({ auth }: { auth: AuthStore }): ReactNode {
  const [chunk, setChunk] = useState<ChunkResult<Home> | null>(null);
  useEffect(() => {
    let live = true;
    void home().then((r) => {
      if (live) setChunk(r);
    });
    return () => {
      live = false;
    };
  }, []);
  if (chunk === null) {
    return (
      <Notice kind="info" testId="home-loading">
        Loading…
      </Notice>
    );
  }
  return renderChunk(chunk, (m) => <m.HomePage auth={auth} />);
}
