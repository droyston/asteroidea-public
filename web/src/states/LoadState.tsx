// The load-state renderer every catalog and compute view uses (it replaces
// `catalog/loadable.tsx :: Loadable`): loading with elapsed time against the route's usual time, a
// refusal or failure as its state card, nothing on "ended" (the session-ended dialog covers the
// page), and the children once loaded.
import type { ReactNode } from "react";
import type { CatalogState } from "../catalog/catalog";
import { Loading } from "./Loading";
import { Refusal } from "./Refusal";
import type { Shrinkable, Smaller } from "./smaller";

export interface LoadStateProps<T> {
  state: CatalogState<T>;
  /** What is loading, for the sentences ("studies", "the file"). */
  noun: string;
  /** The request path: it picks the usual time and goes in the support details. */
  path: string;
  onRetry: () => void;
  /** What the loading line says instead of "Loading <noun>". */
  what?: string;
  meanwhile?: string;
  request?: Shrinkable;
  onSmaller?: (next: Smaller) => void;
  notebookCode?: string;
  children: (value: T) => ReactNode;
}

export function LoadState<T>(p: LoadStateProps<T>) {
  const { state } = p;
  switch (state.kind) {
    case "loading":
      return (
        <Loading
          path={p.path}
          what={p.what ?? `Loading ${p.noun}`}
          {...(p.meanwhile === undefined ? {} : { meanwhile: p.meanwhile })}
        />
      );
    case "error":
      return (
        <Refusal
          status={state.status ?? (state.status503 ? 503 : null)}
          code={state.code}
          detail={state.message}
          noun={p.noun}
          path={p.path}
          onRetry={p.onRetry}
          {...(p.request === undefined ? {} : { request: p.request })}
          {...(p.onSmaller === undefined ? {} : { onSmaller: p.onSmaller })}
          {...(p.notebookCode === undefined ? {} : { notebookCode: p.notebookCode })}
          testId="catalog-error"
        />
      );
    case "ended":
      return null;
    case "loaded":
      return p.children(state.value);
  }
}
