// The labels controller of a file (moved from the labels panel for the signals
// view): every researcher's labels from `/files/{asset_id}/labels`, the one
// selected, a drafted span being named, and the calls that create, rename and delete. Times here are on
// the trials clock, as the routes store them; the view converts. Only the author's labels can change:
// the server enforces it and the view hides the controls for others. A 403 or 409 keeps the server's
// text; a 409 offers Reload.
import { useCallback, useEffect, useState } from "react";
import type { ApiSendResult } from "../auth/apiGet";
import type { AuthStore } from "../auth/store";
import { doFetch } from "../catalog/loadable";
import { useLoad } from "../useLoad";
import {
  createLabel,
  deleteLabel,
  type Label,
  loadLabels,
  round,
  updateLabel,
  validateDraft,
} from "./labels";

export interface Refusal {
  message: string;
  status: number | null;
}

export interface Span {
  start: number;
  stop: number;
}

export interface LabelsCtl {
  labels: readonly Label[];
  /** Set after a failed load. */
  error: string | null;
  loading: boolean;
  selected: Label | null;
  /** A span drafted by a drag, not yet named. */
  draft: Span | null;
  refusal: Refusal | null;
  busy: boolean;
  reload: () => void;
  select: (id: string | null) => void;
  startDraft: (span: Span) => void;
  cancel: () => void;
  /** Names the draft (POST). */
  create: (name: string) => Promise<void>;
  /** Changes the selected label's name and span (PATCH with all four fields and the version). */
  change: (name: string, span: Span) => Promise<void>;
  remove: () => Promise<void>;
}

function sorted(list: readonly Label[]): Label[] {
  return [...list].sort((a, b) => a.start_s - b.start_s || a.label_id.localeCompare(b.label_id));
}

export function useLabels(auth: AuthStore, assetId: string): LabelsCtl {
  const run = useCallback(() => loadLabels(auth.config, assetId, auth, doFetch), [auth, assetId]);
  const { state, reload: reloadLoad } = useLoad(auth, run);
  const [labels, setLabels] = useState<Label[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [draft, setDraft] = useState<Span | null>(null);
  const [refusal, setRefusal] = useState<Refusal | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (state.kind === "loaded") setLabels(sorted(state.value));
  }, [state]);

  const selected = labels.find((l) => l.label_id === selectedId) ?? null;
  const settle = <T>(r: ApiSendResult<T>, ok: (value: T) => void) => {
    setBusy(false);
    if (r.kind === "loaded") {
      setRefusal(null);
      ok(r.value);
    } else if (r.kind === "error") setRefusal({ message: r.message, status: r.status });
    else auth.expire();
  };
  const check = (name: string, note: string, span: Span) => {
    const v = validateDraft({
      name,
      note,
      start: String(round(span.start)),
      stop: String(round(span.stop)),
    });
    if (!v.ok) setRefusal({ message: v.message, status: null });
    return v.ok ? v.fields : null;
  };

  return {
    labels,
    error: state.kind === "error" ? state.message : null,
    loading: state.kind === "loading",
    selected,
    draft,
    refusal,
    busy,
    reload: () => {
      setRefusal(null);
      setSelectedId(null);
      setDraft(null);
      reloadLoad();
    },
    select: (id) => {
      setSelectedId(id);
      setDraft(null);
      setRefusal(null);
    },
    startDraft: (span) => {
      setDraft({ start: Math.max(0, round(span.start)), stop: Math.max(0, round(span.stop)) });
      setSelectedId(null);
      setRefusal(null);
    },
    cancel: () => {
      setSelectedId(null);
      setDraft(null);
      setRefusal(null);
    },
    create: async (name) => {
      if (draft === null) return;
      const fields = check(name, "", draft);
      if (fields === null) return;
      setBusy(true);
      const r = await createLabel(auth.config, assetId, fields, auth, doFetch);
      settle(r, (next) => {
        setLabels((all) => sorted([...all, next]));
        setDraft(null);
        setSelectedId(null);
      });
    },
    change: async (name, span) => {
      if (selected === null) return;
      const fields = check(name, selected.note, span);
      if (fields === null) return;
      setBusy(true);
      const r = await updateLabel(auth.config, assetId, selected, fields, auth, doFetch);
      settle(r, (next) =>
        setLabels((all) => sorted(all.map((l) => (l.label_id === next.label_id ? next : l)))),
      );
    },
    remove: async () => {
      if (selected === null) return;
      setBusy(true);
      const r = await deleteLabel(auth.config, assetId, selected.label_id, auth, doFetch);
      settle(r, () => {
        setLabels((all) => all.filter((l) => l.label_id !== selected.label_id));
        setSelectedId(null);
      });
    },
  };
}
