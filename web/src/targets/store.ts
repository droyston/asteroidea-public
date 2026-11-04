// Pick store over localStorage key `asteroidea.targets/1`. A write failure keeps the
// list in memory and sets a visible error; export still works.
import { errorText } from "../errors";
import { type DatasetPick, datasetKey } from "./groups";
import { parseTargets, TARGETS_SCHEMA, type Target, targetKey } from "./schema";

export const STORAGE_KEY = TARGETS_SCHEMA;

export interface KeyValueStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
}

export interface StoreState {
  targets: readonly Target[];
  error: string | null;
}

export type AddResult = { ok: true } | { ok: false; reason: "already picked" };

export class TargetsStore {
  private state: StoreState;
  private readonly listeners = new Set<() => void>();

  constructor(private readonly storage: KeyValueStorage | null) {
    this.state = this.read();
  }

  private read(): StoreState {
    if (this.storage === null) return { targets: [], error: null };
    try {
      const text = this.storage.getItem(STORAGE_KEY);
      if (text === null) return { targets: [], error: null };
      return { targets: parseTargets(JSON.parse(text)).targets, error: null };
    } catch (err) {
      return { targets: [], error: `Stored picks ignored: ${errorText(err)}` };
    }
  }

  readonly getSnapshot = (): StoreState => this.state;

  readonly subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };

  private commit(targets: readonly Target[], now = new Date()): void {
    let error: string | null = null;
    if (this.storage === null) {
      error =
        "Picks are not saved: localStorage is unavailable (they stay in memory; Export still works)";
    } else {
      try {
        const doc = { schema: TARGETS_SCHEMA, exported_at: now.toISOString(), targets };
        this.storage.setItem(STORAGE_KEY, JSON.stringify(doc));
      } catch (err) {
        error = `Picks not saved (they stay in memory; Export still works): ${errorText(err)}`;
      }
    }
    this.state = { targets, error };
    for (const l of this.listeners) l();
  }

  add(target: Target): AddResult {
    const key = targetKey(target);
    if (this.state.targets.some((t) => targetKey(t) === key)) {
      return { ok: false, reason: "already picked" };
    }
    this.commit([...this.state.targets, target]);
    return { ok: true };
  }

  setNote(index: number, note: string): void {
    this.commit(this.state.targets.map((t, i) => (i === index ? { ...t, note } : t)));
  }

  remove(index: number): void {
    this.commit(this.state.targets.filter((_, i) => i !== index));
  }

  /** Pick or unpick one file under a dataset; the dataset's own entry stays as it is. */
  toggleFile(dataset: DatasetPick, path: string): void {
    const key = targetKey({ ...dataset, path });
    const has = this.state.targets.some((t) => targetKey(t) === key);
    this.commit(
      has
        ? this.state.targets.filter((t) => targetKey(t) !== key)
        : [...this.state.targets, { ...dataset, path, note: "" }],
    );
  }

  /** Remove a dataset with every file picked under it. */
  removeDataset(dataset: DatasetPick): void {
    const key = datasetKey(dataset);
    this.commit(this.state.targets.filter((t) => datasetKey(t) !== key));
  }

  clear(): void {
    this.commit([]);
  }

  replace(targets: readonly Target[]): void {
    this.commit(targets);
  }
}

export function browserStorage(): KeyValueStorage | null {
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}
