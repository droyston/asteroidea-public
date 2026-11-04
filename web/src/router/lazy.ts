// Route chunk loading. A deploy replaces the hashed chunk files, so a tab
// opened before it can fail to `import()` a route chunk. The failure becomes a Notice with a Reload
// button instead of a blank page. The `research`, `imports` and `results` chunks use it (ResearchLoader, ImportsLoader, ResultsLoader).
import { createElement, type ReactNode } from "react";
import { Notice } from "../kit/Notice";

export type ChunkResult<T> = { kind: "loaded"; module: T } | { kind: "stale" };

export const STALE_MESSAGE = "The app was updated. Reload to continue.";

/** Run a dynamic import; a rejection (chunk gone after a deploy, or offline) becomes `stale`. */
export async function loadChunk<T>(load: () => Promise<T>): Promise<ChunkResult<T>> {
  try {
    return { kind: "loaded", module: await load() };
  } catch {
    return { kind: "stale" };
  }
}

export function reloadPage(): void {
  location.reload();
}

export function StaleChunkNotice() {
  return createElement(
    Notice,
    { kind: "error", testId: "chunk-stale", action: { label: "Reload", onClick: reloadPage } },
    STALE_MESSAGE,
  );
}

/** Render the loaded module, or the reload Notice when the chunk failed. */
export function renderChunk<T>(
  result: ChunkResult<T>,
  render: (module: T) => ReactNode,
): ReactNode {
  return result.kind === "loaded" ? render(result.module) : createElement(StaleChunkNotice);
}
