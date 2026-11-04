// The spine row under the top bar: the research objects the reader is inside, each level linking up.
import { useSyncExternalStore } from "react";
import { type SpineCrumb, spineSlot } from "./marks";

/** The trail a view published, else `fallback` (the route's own trail, for views that publish none). */
export function usePublishedSpine(
  fallback: readonly SpineCrumb[] | null,
): readonly SpineCrumb[] | null {
  const published = useSyncExternalStore(spineSlot.subscribe, spineSlot.get);
  return published ?? fallback;
}

export function SpineBar({ trail }: { trail: readonly SpineCrumb[] }) {
  return (
    <nav className="crumbs" aria-label="Breadcrumb">
      {trail.map((c, i) => (
        <span key={c.label}>
          {i > 0 ? " / " : ""}
          {c.to === undefined ? (
            <span aria-current="page">{c.label}</span>
          ) : (
            <a href={c.to}>{c.label}</a>
          )}
        </span>
      ))}
    </nav>
  );
}
