// Loads the `session` chunk (the session heatmap) on first use. A chunk that fails to
// load becomes the reload Notice.
import { type ReactNode, useEffect, useState } from "react";
import type { AuthStore } from "../auth/store";
import { Notice } from "../kit/Notice";
import { type ChunkResult, loadChunk, renderChunk } from "../router/lazy";
import type { SessionRoute } from "./session";

type Session = typeof import("./session");

let pending: Promise<ChunkResult<Session>> | null = null;
function session(): Promise<ChunkResult<Session>> {
  pending ??= loadChunk(() => import("./session")).then((r) => {
    if (r.kind === "stale") pending = null;
    return r;
  });
  return pending;
}

export function SessionLoader({
  auth,
  route,
}: {
  auth: AuthStore;
  route: SessionRoute;
}): ReactNode {
  const [chunk, setChunk] = useState<ChunkResult<Session> | null>(null);
  useEffect(() => {
    let live = true;
    void session().then((r) => {
      if (live) setChunk(r);
    });
    return () => {
      live = false;
    };
  }, []);
  if (chunk === null) {
    return (
      <Notice kind="info" testId="session-chunk-loading">
        Loading…
      </Notice>
    );
  }
  return renderChunk(chunk, (m) => (
    <m.SessionPage
      key={`${route.study}/${route.dataset}/${route.subject}/${route.session}`}
      auth={auth}
      route={route}
    />
  ));
}
