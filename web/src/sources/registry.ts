import type { Dataset, SourceName } from "../index/schema";
import { dandiAdapter } from "./dandi";
import { openneuroAdapter } from "./openneuro";
import type { DatasetRef, SourceAdapter } from "./types";

const adapters: Record<SourceName, SourceAdapter> = {
  dandi: dandiAdapter(),
  openneuro: openneuroAdapter(),
};

/** A third archive adds one module and one entry here. */
export function adapterFor(source: SourceName): SourceAdapter {
  return adapters[source];
}

export function refOf(d: Dataset): DatasetRef {
  return { source: d.source, id: d.id, version: d.version, apiUrl: d.urls.api };
}
