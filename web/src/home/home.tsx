// The `home` chunk: the loader over the studies and links routes, and the page that
// marks itself ready once both have drawn. `HomeView` is the pure view. The tier and the facet choices
// live in the URL query: each choice is a history entry, so a view is a link and Back undoes it.
// The Latest results card is its own lazy chunk, loaded after the home draws; the ready mark never
// waits for it.
import { useCallback, useEffect, useMemo, useState, useSyncExternalStore } from "react";
import type { AuthStore } from "../auth/store";
import { doFetch } from "../catalog/loadable";
import { type ChunkResult, loadChunk } from "../router/lazy";
import { appRouter } from "../router/router";
import { Ready } from "../shell/marks";
import { tokenEmail } from "../shell/user";
import { LoadState } from "../states/LoadState";
import { useLoad } from "../useLoad";
import { loadHome } from "./data";
import { type HomeQuery, parseQuery, queryString } from "./facets";
import { HomeView } from "./HomeView";
import "./home.css";

/** The home's query, read from `location.search`; it follows Back, Forward and `navigate`. */
function useHomeQuery(): [HomeQuery, (query: HomeQuery) => void] {
  const router = appRouter();
  const search = useSyncExternalStore(router.subscribe, () => window.location.search);
  const query = useMemo(() => parseQuery(search), [search]);
  const set = useCallback((next: HomeQuery) => router.navigate(`/${queryString(next)}`), [router]);
  return [query, set];
}

type Latest = typeof import("./latest");

/** The Latest results card's chunk, imported once the home has drawn; a stale chunk draws nothing. */
function LatestLoader({ auth }: { auth: AuthStore }) {
  const [chunk, setChunk] = useState<ChunkResult<Latest> | null>(null);
  useEffect(() => {
    let live = true;
    void loadChunk(() => import("./latest")).then((r) => {
      if (live) setChunk(r);
    });
    return () => {
      live = false;
    };
  }, []);
  if (chunk === null || chunk.kind !== "loaded") return null;
  const { LatestResults } = chunk.module;
  return <LatestResults tokens={auth} config={auth.config} me={tokenEmail(auth.getIdToken())} />;
}

export function HomePage({ auth }: { auth: AuthStore }) {
  const run = useCallback(() => loadHome(auth.config, auth, doFetch), [auth]);
  const { state, reload } = useLoad(auth, run);
  const [query, setQuery] = useHomeQuery();
  return (
    <LoadState state={state} noun="the studies list" path="/catalog/studies" onRetry={reload}>
      {(body) => (
        <>
          <Ready />
          <HomeView
            body={body}
            query={query}
            onQuery={setQuery}
            latest={<LatestLoader auth={auth} />}
          />
        </>
      )}
    </LoadState>
  );
}
