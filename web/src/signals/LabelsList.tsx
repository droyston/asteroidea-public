// The labels list of the signals view: never a long list. Up to 2,000 labels per file, so it
// shows the 3 nearest the window and a count of the rest; every label is also a tick under the
// whole-recording overview. A row opens the label in the Labels lane (moving the window to it).

import { authorText } from "../events/labels";
import type { AxisLabel } from "../events/lanes";
import { nearestLabels, seconds } from "../events/lanes";
import { Notice } from "../kit/Notice";
import type { Domain } from "../plots/axis";

export const NEAREST = 3;

export function LabelsList({
  labels,
  around,
  selectedId,
  loading,
  error,
  onRetry,
  onPick,
}: {
  labels: readonly AxisLabel[];
  /** The window, or the whole recording when none is open. */
  around: Domain;
  selectedId: string | null;
  loading: boolean;
  error: string | null;
  onRetry: () => void;
  onPick: (l: AxisLabel) => void;
}) {
  const near = nearestLabels(labels, around, NEAREST);
  const mine = labels.filter((l) => l.mine).length;
  const rest = labels.length - near.length;
  return (
    <section className="signals-labels" aria-label="Labels on this file" data-testid="labels-list">
      <div className="signals-aside-head">
        <h3 className="signals-head">Labels on this file</h3>
        <span className="meta" data-testid="labels-count">
          {labels.length} · {mine} yours
        </span>
      </div>
      {error === null ? null : (
        <Notice
          kind="error"
          testId="labels-error"
          action={{ label: "Try again", onClick: onRetry }}
        >
          {error}
        </Notice>
      )}
      {loading ? <p className="meta">Loading labels…</p> : null}
      {!loading && error === null && labels.length === 0 ? (
        <p className="meta" data-testid="labels-empty">
          No labels on this file yet. Drag in the Labels lane under a window to mark a span.
        </p>
      ) : null}
      <ul className="signals-label-rows">
        {near.map((l) => (
          <li key={l.label_id} data-ab="label-row">
            <button
              type="button"
              className="signals-label-row"
              aria-pressed={l.label_id === selectedId}
              data-testid={`label-item-${l.label_id}`}
              onClick={() => onPick(l)}
            >
              <strong>{l.name}</strong>
              <span>
                {seconds(l.a)}–{seconds(l.b)} s
              </span>
              <span className="meta">{authorText(l)}</span>
            </button>
          </li>
        ))}
      </ul>
      {rest > 0 ? (
        <p className="meta" data-testid="labels-more">
          {rest} more across the recording
        </p>
      ) : null}
      {labels.length > 0 ? (
        <p className="meta">
          All {labels.length} also appear as ticks under the whole-recording overview. Only the
          author can move or delete a label.
        </p>
      ) : null}
    </section>
  );
}
