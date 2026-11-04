// The `find` chunk: reads the filters from the URL and writes them back
// (replace, so the back button is not a filter history), holds the pick list (localStorage), reads the
// catalog once for "already in catalog" when signed in, and draws `FindView`. The no-auth build draws
// the same page without the catalog join and with "Ask a curator to import".
import { useCallback, useMemo, useState, useSyncExternalStore } from "react";
import type { AuthStore } from "../auth/store";
import { loadDatasets } from "../catalog/catalog";
import { doFetch } from "../catalog/loadable";
import { CuratorRailLoader } from "../imports/ImportsLoader";
import type { LoadState } from "../index/load";
import type { Dataset } from "../index/schema";
import { appRouter } from "../router/router";
import { Ready } from "../shell/marks";
import { refOf } from "../sources/registry";
import type { DatasetRef } from "../sources/types";
import { type DatasetPick, datasetKey, type PickGroup } from "../targets/groups";
import { browserStorage, TargetsStore } from "../targets/store";
import { useLoad } from "../useLoad";
import { FindView } from "./FindView";
import { catalogJoin, filtersFromSearch, filtersToSearch, joinKey } from "./model";
import { PicksRail } from "./PicksRail";
import { Status } from "./Status";
import "./find.css";

const store = new TargetsStore(browserStorage());

const search = () => window.location.search;

function useCatalogJoin(auth: AuthStore): ReadonlyMap<string, string> | null {
  const run = useCallback(() => loadDatasets(auth.config, auth, doFetch), [auth]);
  const { state } = useLoad(auth, run);
  return useMemo(
    () => (state.kind === "loaded" ? catalogJoin(state.value.datasets) : null),
    [state],
  );
}

function WithCatalog(props: FindProps & { auth: AuthStore }) {
  return <FindPage {...props} catalog={useCatalogJoin(props.auth)} />;
}

export interface FindProps {
  state: LoadState;
  auth: AuthStore | null;
  curator: boolean;
  /** The run open in the rail (`/discover/imports/:run_id`); null elsewhere. */
  runId?: string | null;
  /** "imports" on the curator's Imports tab (`/discover/imports*`). */
  tab?: "index" | "imports";
}

export function Find(props: FindProps) {
  return props.auth === null ? (
    <FindPage {...props} catalog={null} />
  ) : (
    <WithCatalog {...props} auth={props.auth} />
  );
}

function FindPage({
  state,
  auth,
  curator,
  catalog,
  runId = null,
  tab = "index",
}: FindProps & { catalog: ReadonlyMap<string, string> | null }) {
  const router = appRouter();
  const query = useSyncExternalStore(router.subscribe, search);
  const filters = useMemo(() => filtersFromSearch(query), [query]);
  const picks = useSyncExternalStore(store.subscribe, store.getSnapshot);
  const [notice, setNotice] = useState<string | null>(null);
  const picked = useMemo(
    () => new Set(picks.targets.map((t) => joinKey({ source: t.source, id: t.dataset_id }))),
    [picks.targets],
  );

  const onFilters = useCallback(
    (next: Parameters<typeof filtersToSearch>[0]) => {
      router.navigate(`${window.location.pathname}${filtersToSearch(next)}`, { replace: true });
    },
    [router],
  );

  const onPick = (d: Dataset) => {
    const result = store.add({
      source: d.source,
      dataset_id: d.id,
      version: d.version,
      path: null,
      note: "",
    });
    setNotice(result.ok ? null : `${result.reason}: ${d.source} ${d.id} (whole dataset)`);
  };

  // Each indexed dataset's listing reference and file count, by dataset key (stable per index).
  const indexed = useMemo(() => {
    const m = new Map<string, { ref: DatasetRef; files: number | null }>();
    if (state.kind !== "loaded") return m;
    for (const d of state.index.datasets) {
      m.set(datasetKey({ source: d.source, dataset_id: d.id, version: d.version }), {
        ref: refOf(d),
        files: d.files,
      });
    }
    return m;
  }, [state]);
  const refFor = useCallback(
    (d: DatasetPick) => indexed.get(datasetKey(d))?.ref ?? null,
    [indexed],
  );
  const fileCount = useCallback((g: PickGroup) => indexed.get(g.key)?.files ?? null, [indexed]);

  const rail = (
    <PicksRail
      store={store}
      state={picks}
      notice={notice}
      refFor={refFor}
      extra={
        curator && auth !== null ? (
          <CuratorRailLoader
            auth={auth}
            targets={picks.targets}
            fileCount={fileCount}
            runId={runId}
            recent={tab === "imports"}
          />
        ) : undefined
      }
    />
  );

  if (state.kind !== "loaded") {
    return (
      <>
        {state.kind === "loading" ? null : <Ready />}
        <Status state={state} />
      </>
    );
  }
  return (
    <>
      <Ready />
      <FindView
        index={state.index}
        filters={filters}
        onFilters={onFilters}
        picked={picked}
        onPick={onPick}
        catalog={catalog}
        rail={rail}
        curator={curator}
        tab={tab}
        status={<Status state={state} />}
      />
    </>
  );
}
