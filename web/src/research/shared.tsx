// Helpers shared by the research pages: the tab-in-URL hook, the trail for the shell's spine and the dash for a null.
import { useSyncExternalStore } from "react";
import { searchWithTab, tabFromSearch } from "../kit/Tabs";
import type { Crumb } from "../router/dataPaths";
import { appRouter } from "../router/router";
import type { SpineCrumb } from "../shell/marks";

export function useSearch(): string {
  const router = appRouter();
  return useSyncExternalStore(router.subscribe, () => window.location.search);
}

/** Tab state kept in the URL query (`tab=`). */
export function useTab(path: string, ids: readonly string[]): [string, (id: string) => void] {
  const search = useSearch();
  const selected = tabFromSearch(search, ids);
  const select = (id: string) =>
    appRouter().navigate(`${path}${searchWithTab(search, id, ids[0] ?? "")}`, { replace: true });
  return [selected, select];
}

/** A `dataCrumbs` trail as the `Spine` props (shell/marks.tsx). */
export function toTrail(crumbs: readonly Crumb[]): SpineCrumb[] {
  return crumbs.map((c) =>
    c.href === undefined ? { label: c.label } : { label: c.label, to: c.href },
  );
}

export function none(v: string | null): string {
  return v ?? "—";
}
