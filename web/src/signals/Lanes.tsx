// Event lanes and the Labels lane under the window plots, on the
// window's time axis and the plot frame's margins, so a tick sits under the sample it marks. Each lane
// draws a tick per event at the window's resolution, binned into density when two fall in one pixel.
// In the Labels lane a drag drafts a span in place and a Popover names it (Esc cancels); a click picks
// a label. Your own label opens for rename, edge drag and delete; another researcher's reads only.
import { type FormEvent, type PointerEvent, useMemo, useRef, useState } from "react";
import { authorText, NAME_MAX, READ_ONLY_REASON } from "../events/labels";
import type { AxisLabel, EventLane } from "../events/lanes";
import { laneMarks, seconds } from "../events/lanes";
import type { Refusal } from "../events/useLabels";
import { Button } from "../kit/Button";
import { Notice } from "../kit/Notice";
import { Popover } from "../kit/Popover";
import type { Domain } from "../plots/axis";
import { LEFT, RIGHT } from "./Frame";

export const LANE_H = 18;
export const LABEL_H = 24;
const CLICK_PX = 4;
const EDGE_PX = 5;
const NAME_CHARS = 9;

export interface LabelsView {
  labels: readonly AxisLabel[];
  selectedId: string | null;
  /** The drafted span on the axis. */
  draft: { a: number; b: number } | null;
  /** Null when a drag may draft; otherwise why it may not. */
  blocked: string | null;
  busy: boolean;
  refusal: Refusal | null;
  onDraft: (a: number, b: number) => void;
  onSelect: (id: string | null) => void;
  onCancel: () => void;
  onCreate: (name: string) => void;
  onChange: (name: string, a: number, b: number) => void;
  onRemove: () => void;
  onReload: () => void;
}

export type LanesState =
  | { kind: "loading" }
  | { kind: "error"; message: string; onRetry: (() => void) | null }
  | { kind: "loaded"; lanes: readonly EventLane[] };

function short(name: string): string {
  return name.length > NAME_CHARS ? `${name.slice(0, NAME_CHARS - 1)}…` : name;
}

function RefusalNotice({ refusal, onReload }: { refusal: Refusal; onReload: () => void }) {
  return refusal.status === 409 ? (
    <Notice kind="error" testId="label-refusal" action={{ label: "Reload", onClick: onReload }}>
      {refusal.message}
    </Notice>
  ) : (
    <Notice kind="error" testId="label-refusal">
      {refusal.message}
    </Notice>
  );
}

function NameForm({
  initial,
  busy,
  submit,
  hint,
  children,
}: {
  initial: string;
  busy: boolean;
  submit: (name: string) => void;
  hint: string;
  children?: React.ReactNode;
}) {
  const [name, setName] = useState(initial);
  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    submit(name);
  };
  return (
    <form className="label-form" onSubmit={onSubmit} data-testid="label-editor">
      <input
        type="text"
        value={name}
        maxLength={NAME_MAX}
        aria-label="Label name"
        data-autofocus
        data-testid="label-name"
        onChange={(e) => setName(e.target.value)}
      />
      <div className="label-form-foot">
        <span className="meta">{hint}</span>
        <Button type="submit" variant="primary" disabled={busy}>
          Save
        </Button>
      </div>
      {children}
    </form>
  );
}

function SelectedPopover({ l, view }: { l: AxisLabel; view: LabelsView }) {
  const [confirming, setConfirming] = useState(false);
  if (!l.mine) {
    return (
      <div data-testid="label-readonly">
        <p className="meta">{authorText(l)}</p>
        {l.note === "" ? null : <p>{l.note}</p>}
        <p className="meta" data-testid="label-readonly-reason">
          {READ_ONLY_REASON}
        </p>
      </div>
    );
  }
  return (
    <NameForm
      initial={l.name}
      busy={view.busy}
      hint="Yours · drag an edge to move it"
      submit={(name) => view.onChange(name, l.a, l.b)}
    >
      {confirming ? (
        <Notice kind="info" testId="label-confirm">
          Delete this label for every researcher?{" "}
          <Button onClick={view.onRemove} disabled={view.busy}>
            Confirm delete
          </Button>{" "}
          <Button onClick={() => setConfirming(false)}>Keep it</Button>
        </Notice>
      ) : (
        <Button onClick={() => setConfirming(true)} disabled={view.busy}>
          Delete
        </Button>
      )}
    </NameForm>
  );
}

type Drag =
  | { kind: "draft"; x0: number; x1: number }
  | { kind: "edge"; id: string; edge: "a" | "b"; x0: number; x1: number };

export function Lanes({
  state,
  domain,
  width,
  labels,
}: {
  state: LanesState;
  domain: Domain;
  width: number;
  labels: LabelsView | null;
}) {
  const plotW = Math.max(1, width - LEFT - RIGHT);
  const lanes = state.kind === "loaded" ? state.lanes : [];
  const marks = useMemo(
    () => lanes.map((l) => laneMarks(l.items, domain, plotW)),
    [lanes, domain, plotW],
  );
  const [drag, setDrag] = useState<Drag | null>(null);
  const svg = useRef<SVGSVGElement>(null);
  const span = domain[1] - domain[0];
  const x = (t: number) => LEFT + ((t - domain[0]) / span) * plotW;
  const t = (px: number) => domain[0] + ((px - LEFT) / plotW) * span;
  const clampPx = (px: number) => Math.min(LEFT + plotW, Math.max(LEFT, px));
  const labelsTop = lanes.length * LANE_H;
  const height = labelsTop + (labels === null ? 0 : LABEL_H);
  // The SVG may be drawn narrower than `width` (max-width: 100%): pointer pixels are scaled back.
  const local = (e: PointerEvent<SVGSVGElement>) => {
    const rect = svg.current?.getBoundingClientRect();
    if (rect === undefined || rect.width === 0) return clampPx(0);
    return clampPx(((e.clientX - rect.left) / rect.width) * width);
  };
  const shown =
    labels === null ? [] : labels.labels.filter((l) => l.b >= domain[0] && l.a <= domain[1]);
  const selected = labels?.labels.find((l) => l.label_id === labels.selectedId) ?? null;

  const onPointerDown = (e: PointerEvent<SVGSVGElement>) => {
    if (labels === null || e.button !== 0) return;
    const rect = e.currentTarget.getBoundingClientRect();
    if (e.clientY - rect.top < labelsTop) return;
    e.stopPropagation();
    e.currentTarget.setPointerCapture(e.pointerId);
    const px = local(e);
    if (selected?.mine === true) {
      if (Math.abs(px - x(selected.a)) <= EDGE_PX) {
        setDrag({ kind: "edge", id: selected.label_id, edge: "a", x0: px, x1: px });
        return;
      }
      if (Math.abs(px - x(selected.b)) <= EDGE_PX) {
        setDrag({ kind: "edge", id: selected.label_id, edge: "b", x0: px, x1: px });
        return;
      }
    }
    setDrag({ kind: "draft", x0: px, x1: px });
  };
  const onPointerMove = (e: PointerEvent<SVGSVGElement>) => {
    if (drag !== null) setDrag({ ...drag, x1: local(e) });
  };
  const onPointerUp = (e: PointerEvent<SVGSVGElement>) => {
    if (drag === null || labels === null) return;
    const px = local(e);
    setDrag(null);
    if (Math.abs(px - drag.x0) < CLICK_PX) {
      const at = t(px);
      const hit = shown.find((l) => l.a <= at && at <= l.b);
      labels.onSelect(hit?.label_id ?? null);
      return;
    }
    if (drag.kind === "edge" && selected !== null) {
      const moved = t(px);
      const a = drag.edge === "a" ? Math.min(moved, selected.b) : selected.a;
      const b = drag.edge === "b" ? Math.max(moved, selected.a) : selected.b;
      labels.onChange(selected.name, a, b);
      return;
    }
    if (labels.blocked !== null) return;
    labels.onDraft(t(Math.min(drag.x0, px)), t(Math.max(drag.x0, px)));
  };

  const ghost = (() => {
    if (drag === null) return null;
    if (drag.kind === "draft")
      return { x0: Math.min(drag.x0, drag.x1), x1: Math.max(drag.x0, drag.x1) };
    if (selected === null) return null;
    const a = drag.edge === "a" ? Math.min(drag.x1, x(selected.b)) : x(selected.a);
    const b = drag.edge === "b" ? Math.max(drag.x1, x(selected.a)) : x(selected.b);
    return { x0: a, x1: b };
  })();
  const draft = labels?.draft ?? null;
  const pop = (() => {
    if (labels === null) return null;
    if (draft !== null) {
      return {
        key: `new:${draft.a}:${draft.b}`,
        title: `New label · ${seconds(draft.a)}–${seconds(draft.b)} s`,
        x: (clampPx(x(draft.a)) + clampPx(x(draft.b))) / 2,
        body: (
          <NameForm
            initial=""
            busy={labels.busy}
            hint="Shared with everyone"
            submit={labels.onCreate}
          />
        ),
      };
    }
    if (selected !== null && selected.b >= domain[0] && selected.a <= domain[1]) {
      return {
        key: `${selected.label_id}:${selected.version}`,
        title: `${selected.mine ? "Your label" : selected.name} · ${seconds(selected.a)}–${seconds(selected.b)} s`,
        x: (clampPx(x(selected.a)) + clampPx(x(selected.b))) / 2,
        body: <SelectedPopover l={selected} view={labels} />,
      };
    }
    return null;
  })();

  return (
    <div className="signals-lanes" style={{ width }} data-testid="signals-lanes">
      {state.kind === "loading" ? (
        <p className="meta" data-testid="lanes-loading">
          Loading the events…
        </p>
      ) : null}
      {state.kind === "error" ? (
        <Notice
          kind="error"
          testId="lanes-error"
          {...(state.onRetry === null
            ? {}
            : { action: { label: "Try again", onClick: state.onRetry } })}
        >
          {state.message}
        </Notice>
      ) : null}
      {state.kind === "loaded" && lanes.length === 0 ? (
        <p className="meta" data-testid="lanes-none">
          The recipe resolves no events for this file.
        </p>
      ) : null}
      {labels?.blocked == null ? null : (
        <p className="meta" data-testid="labels-blocked">
          {labels.blocked}
        </p>
      )}
      <div className="signals-lanes-plot">
        <svg
          ref={svg}
          width={width}
          height={height}
          viewBox={`0 0 ${width} ${height}`}
          className="signals-lanes-svg"
          aria-label={
            labels === null
              ? "Event lanes"
              : "Event lanes and the Labels lane. Drag in the Labels lane to draft a label; click one to open it."
          }
          role="img"
          onPointerDown={onPointerDown}
          onPointerMove={onPointerMove}
          onPointerUp={onPointerUp}
          onPointerCancel={() => setDrag(null)}
        >
          {lanes.map((l, i) => {
            const y = i * LANE_H;
            const ms = marks[i] ?? [];
            const max = ms.reduce((n, m) => Math.max(n, m.kind === "bin" ? m.n : 1), 1);
            return (
              <g key={l.name} data-testid={`lane-${l.name}`} data-ab="event-lane">
                <text x={LEFT - 6} y={y + LANE_H / 2 + 4} textAnchor="end" className="lane-name">
                  {short(l.name)}
                  <title>{`${l.name} (${l.items.length})`}</title>
                </text>
                <line
                  x1={LEFT}
                  x2={LEFT + plotW}
                  y1={y + LANE_H - 0.5}
                  y2={y + LANE_H - 0.5}
                  className="lane-rule"
                />
                {ms.map((m) =>
                  m.kind === "span" ? (
                    <rect
                      key={`s${m.x0}`}
                      className="lane-span"
                      x={LEFT + m.x0}
                      y={y + 4}
                      width={m.x1 - m.x0}
                      height={LANE_H - 8}
                    >
                      <title>{m.title}</title>
                    </rect>
                  ) : m.kind === "tick" ? (
                    <line
                      key={`t${m.x}`}
                      className="lane-tick"
                      x1={LEFT + m.x}
                      x2={LEFT + m.x}
                      y1={y + 3}
                      y2={y + LANE_H - 3}
                    >
                      <title>{m.title}</title>
                    </line>
                  ) : (
                    <rect
                      key={`b${m.x}`}
                      className="lane-bin"
                      x={LEFT + m.x}
                      y={y + 3}
                      width={1}
                      height={LANE_H - 6}
                      opacity={0.25 + (0.75 * m.n) / max}
                    >
                      <title>{`${m.n} events`}</title>
                    </rect>
                  ),
                )}
              </g>
            );
          })}
          {labels === null ? null : (
            <g data-testid="lane-labels">
              <text
                x={LEFT - 6}
                y={labelsTop + LABEL_H / 2 + 4}
                textAnchor="end"
                className="lane-name"
              >
                Labels
              </text>
              <rect
                x={LEFT}
                y={labelsTop + 1}
                width={plotW}
                height={LABEL_H - 2}
                className="lane-labels-bg"
              />
              {shown.map((l) => {
                const x0 = clampPx(x(l.a));
                const w = Math.max(2, clampPx(x(l.b)) - x0);
                return (
                  <g key={l.label_id}>
                    <rect
                      className={`label-bar${l.mine ? " label-mine" : ""}${
                        l.label_id === labels.selectedId ? " bar-selected" : ""
                      }`}
                      data-testid={`label-bar-${l.label_id}`}
                      data-ab="label-bar"
                      x={x0}
                      y={labelsTop + 4}
                      width={w}
                      height={LABEL_H - 8}
                    >
                      <title>{`${l.name} · ${authorText(l)}`}</title>
                    </rect>
                    {w > 44 ? (
                      <text x={x0 + 4} y={labelsTop + LABEL_H / 2 + 4} className="label-bar-name">
                        {short(l.name)}
                      </text>
                    ) : null}
                  </g>
                );
              })}
              {draft === null ? null : (
                <rect
                  className="label-pending"
                  data-testid="label-draft"
                  x={clampPx(x(draft.a))}
                  y={labelsTop + 2}
                  width={Math.max(2, clampPx(x(draft.b)) - clampPx(x(draft.a)))}
                  height={LABEL_H - 4}
                />
              )}
              {ghost === null ? null : (
                <rect
                  className="timeline-select"
                  x={ghost.x0}
                  y={labelsTop + 2}
                  width={ghost.x1 - ghost.x0}
                  height={LABEL_H - 4}
                />
              )}
            </g>
          )}
        </svg>
        {labels === null || pop === null ? null : (
          <Popover
            key={pop.key}
            title={pop.title}
            x={pop.x}
            y={height + 4}
            bound={width}
            onClose={labels.onCancel}
            testId="label-popover"
          >
            {pop.body}
            {labels.refusal === null ? null : (
              <RefusalNotice refusal={labels.refusal} onReload={labels.onReload} />
            )}
          </Popover>
        )}
      </div>
      {labels !== null && pop === null && labels.refusal !== null ? (
        <RefusalNotice refusal={labels.refusal} onReload={labels.onReload} />
      ) : null}
    </div>
  );
}
