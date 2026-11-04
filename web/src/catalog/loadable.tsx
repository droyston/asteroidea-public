// Shared catalog helpers: the fetch the views use and the query-time badge. The load-state renderer
// is `states/LoadState.tsx`.

type Fetch = typeof fetch;
export const doFetch: Fetch = (url, init) => fetch(url, init);

export function QueryTime({ ms }: { ms: number }) {
  return (
    <span className="query-ms" data-testid="catalog-query-ms">
      {ms} ms
    </span>
  );
}
