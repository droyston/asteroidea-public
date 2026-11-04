// The pick list read as two levels: a picked dataset is a parent and the
// files picked under it are its children. The stored and exported document stays the flat
// `asteroidea.targets/1` list, so every older export reads unchanged: a dataset-only list is all
// parents, and the older per-file list (files with no `path: null` entry) still groups under its
// dataset. A dataset with no file entries means the whole dataset; one with file entries means
// those files only.
import type { Target, TargetSource } from "./schema";

/** The dataset a pick belongs to. */
export interface DatasetPick {
  source: TargetSource;
  dataset_id: string;
  version: string;
}

export interface PickFile {
  target: Target;
  /** Position in the flat list (for `TargetsStore.setNote`). */
  index: number;
}

export interface PickGroup {
  key: string;
  dataset: DatasetPick;
  /** The `path: null` entry, which carries the dataset's note; null for an older per-file list. */
  entry: PickFile | null;
  files: PickFile[];
}

/** Identity of a dataset across its parent and file entries. */
export function datasetKey(d: DatasetPick): string {
  return JSON.stringify([d.source, d.dataset_id, d.version]);
}

/** The flat list as groups, in the order each dataset first appears. */
export function groupPicks(targets: readonly Target[]): PickGroup[] {
  const groups = new Map<string, PickGroup>();
  targets.forEach((target, index) => {
    const dataset = {
      source: target.source,
      dataset_id: target.dataset_id,
      version: target.version,
    };
    const key = datasetKey(dataset);
    let group = groups.get(key);
    if (group === undefined) {
      group = { key, dataset, entry: null, files: [] };
      groups.set(key, group);
    }
    if (target.path === null) group.entry ??= { target, index };
    else group.files.push({ target, index });
  });
  return [...groups.values()];
}

/** The number of datasets picked: what the rail counts. */
export function pickCount(targets: readonly Target[]): number {
  return groupPicks(targets).length;
}

/** "whole dataset", or the file count. */
export function groupScope(group: PickGroup): string {
  const n = group.files.length;
  if (n === 0) return "whole dataset";
  return n === 1 ? "1 file" : `${n} files`;
}
