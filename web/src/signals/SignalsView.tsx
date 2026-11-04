// Signal inspection: what the signals view draws from loaded
// data. On top, every channel of one series over the whole recording as one channel x time image of
// the chosen band's power, grouped and coloured by region; a drag on it (the brush) picks the window,
// a click picks the channel. Below, the window's spectrogram on the overview's log frequency axis and
// the file's shared dB range, and the trace, on one time axis: wheel to zoom, drag to pan, the arrow
// keys to step. Under them, the event lanes and the Labels lane on the same axis; on the right,
// the anatomy panel and the labels list the parent passes as `aside`. The parent fetches; this file only
// draws and reports what the user picked.
import {
  type KeyboardEvent,
  type PointerEvent,
  type ReactNode,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { Button } from "../kit/Button";
import { Notice } from "../kit/Notice";
import {
  edgeLabel,
  edgeTicks,
  type LfpSeries,
  panWindow,
  type SpectrogramBody,
  type TimeWindow,
  windowFromDrag,
  zoomWindow,
} from "../lfp/lfp";
import { halfStep, type Overview, totalSeconds } from "../lfp/overview";
import { formatTick, invertScale, linearScale, niceStep, niceTicks } from "../plots/axis";
import { drawHeatmap, type Grid, renderHeatmap, stopsFrom, valueRange } from "../plots/heatmap";
import { drawTrace, traceDomain } from "../plots/trace";
import { ColourBar, Frame, LEFT, RIGHT, rootStyle, TOP } from "./Frame";
import { type LabelsView, Lanes, type LanesState } from "./Lanes";
import {
  BANDS,
  type Band,
  bandIndices,
  bandPowerDb,
  type ChannelLayout,
  logBinned,
  overviewGrid,
  sharedRange,
} from "./model";

export const OVERVIEW_H = 192;
const SPEC_H = 150;
const TRACE_H = 70;
const KEY_ZOOM = 0.5;
const KEY_PAN = 0.5;
const WHEEL_ZOOM = 1.25;
/** A wheel or drag settles this long before the window is asked for. */
const SETTLE_MS = 300;
/** A pointer that moves less than this many pixels is a click (pick a channel), not a brush. */
const CLICK_PX = 4;
/** Above this width the region list sits beside the overview; below it, above. */
const SIDE_LEGEND_MIN = 640;
export const LEGEND_W = 168;

export type OverviewState =
  | { kind: "loading" }
  | { kind: "preparing"; onRetry: () => void }
  | { kind: "skipped"; reason: string }
  | { kind: "error"; message: string; onRetry: (() => void) | null }
  | { kind: "loaded"; overview: Overview };

export type WindowState =
  | { kind: "loading"; last: { body: SpectrogramBody; win: TimeWindow } | null }
  | { kind: "error"; message: string; onRetry: (() => void) | null }
  | { kind: "loaded"; body: SpectrogramBody };

export interface SignalsViewProps {
  list: readonly LfpSeries[];
  chosen: string;
  band: Band;
  overview: OverviewState;
  layout: ChannelLayout | null;
  channel: number;
  win: TimeWindow | null;
  spectrogram: WindowState | null;
  width: number;
  onSeries: (path: string) => void;
  onBand: (id: string) => void;
  onChannel: (channel: number) => void;
  onWindow: (next: TimeWindow | null) => void;
  /** The event lanes under the window. */
  lanes: LanesState;
  /** The Labels lane; null leaves it out. */
  labels: LabelsView | null;
  /** The right column (anatomy, labels list); null leaves it out. */
  aside: ReactNode;
}

function channelName(layout: ChannelLayout | null, c: number): string {
  const region = layout?.regionOf(c) ?? null;
  return region === null ? `Channel ${c}` : `Channel ${c} · ${region}`;
}

function fmtSeconds(s: number): string {
  return String(Math.round(s * 10) / 10);
}

// --- the region list ---------------------------------------------------------------------------------

function Regions({ layout, side }: { layout: ChannelLayout; side: boolean }) {
  return (
    <div
      className={side ? "signals-regions signals-regions-side" : "signals-regions"}
      style={side ? { width: LEGEND_W, height: OVERVIEW_H, marginTop: TOP } : undefined}
      data-testid="signals-regions"
    >
      <ul>
        {layout.groups.map((g) => (
          <li
            key={g.label}
            data-ab="region-group"
            data-ab-known={g.token === null ? "false" : "true"}
            data-testid="signals-region"
            style={side ? { flexGrow: g.channels.length, flexBasis: 0, minHeight: 0 } : undefined}
          >
            <span
              className="signals-swatch"
              style={{ background: g.token === null ? "var(--color-muted)" : `var(${g.token})` }}
              aria-hidden="true"
            />
            <span className="signals-region-label">
              {g.label} · {g.channels.length}
            </span>
          </li>
        ))}
      </ul>
      {layout.mapped ? null : (
        <p className="meta" data-testid="signals-regions-unknown">
          The catalog does not say which electrodes this series' channels are, so channels are in
          recording order without regions.
        </p>
      )}
    </div>
  );
}

// --- the overview ------------------------------------------------------------------------------------

function OverviewPlot({
  overview,
  layout,
  band,
  channel,
  win,
  width,
  onSelect,
  onChannel,
  ticks,
}: {
  ticks: readonly { a: number; b: number; id: string }[];
  overview: Overview;
  layout: ChannelLayout;
  band: Band;
  channel: number;
  win: TimeWindow | null;
  width: number;
  onSelect: (next: TimeWindow | null) => void;
  onChannel: (channel: number) => void;
}) {
  const total = totalSeconds(overview);
  const plotW = width - LEFT - RIGHT;
  const box = useRef<HTMLDivElement>(null);
  const [drag, setDrag] = useState<{ a: number; b: number; x0: number; x1: number } | null>(null);
  const { nTimes, times, edges } = overview;
  const bands = useMemo(() => bandIndices(edges, band.lo, band.hi), [edges, band]);
  const values = useMemo(() => bandPowerDb(overview, bands), [overview, bands]);
  const grid = useMemo<Grid>(
    () => overviewGrid(values, nTimes, layout.order),
    [values, nTimes, layout],
  );
  const range = useMemo(() => valueRange(grid), [grid]);
  const hs = halfStep(times);
  const draw = useCallback(
    (ctx: CanvasRenderingContext2D, cw: number, ch: number) => {
      const xs = linearScale([0, total], [0, cw]);
      const x0 = xs((times[0] ?? 0) - hs);
      const x1 = xs((times[nTimes - 1] ?? 0) + hs);
      drawHeatmap(ctx, renderHeatmap(grid, range, stopsFrom(rootStyle)), {
        x: x0,
        y: 0,
        w: x1 - x0,
        h: ch,
      });
    },
    [grid, range, total, times, hs, nTimes],
  );
  const data = useMemo(
    () => ({
      order: layout.order,
      bands,
      rows: layout.order.map((c) => Array.from(values.subarray(c * nTimes, (c + 1) * nTimes))),
    }),
    [layout, bands, values, nTimes],
  );
  const n = layout.order.length;
  const rowH = n === 0 ? OVERVIEW_H : OVERVIEW_H / n;
  const xs = linearScale([0, total], [LEFT, LEFT + plotW]);
  const seconds = (clientX: number) => {
    const rect = box.current?.getBoundingClientRect();
    if (rect === undefined) return 0;
    const x = ((clientX - rect.left) / rect.width) * width;
    return Math.min(total, Math.max(0, invertScale([0, total], [LEFT, width - RIGHT])(x)));
  };
  const rowAt = (clientY: number): number | null => {
    const rect = box.current?.getBoundingClientRect();
    if (rect === undefined) return null;
    const i = Math.floor((clientY - rect.top - TOP) / rowH);
    return i >= 0 && i < n ? (layout.order[i] ?? null) : null;
  };
  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    const next = (w: TimeWindow | null | undefined) => {
      e.preventDefault();
      onSelect(w ?? null);
    };
    const base = win ?? zoomWindow(null, total, KEY_ZOOM);
    if (e.key === "ArrowLeft" && base !== null) next(panWindow(base, total, -KEY_PAN));
    else if (e.key === "ArrowRight" && base !== null) next(panWindow(base, total, KEY_PAN));
    else if (e.key === "+" || e.key === "=") next(zoomWindow(win, total, KEY_ZOOM));
    else if (e.key === "-") next(zoomWindow(win, total, 1 / KEY_ZOOM));
    else if (e.key === "Escape" || e.key === "Home") next(null);
  };
  const shown = drag === null ? null : { a: Math.min(drag.a, drag.b), b: Math.max(drag.a, drag.b) };
  const picked = layout.order.indexOf(channel);
  let y = TOP;
  const bars = layout.groups.map((g) => {
    const h = g.channels.length * rowH;
    const bar = { g, y, h };
    y += h;
    return bar;
  });
  return (
    <div
      ref={box}
      className="lfp-overview"
      style={{ width }}
      role="slider"
      aria-valuemin={0}
      aria-valuemax={Math.round(total)}
      aria-valuenow={Math.round(win?.start ?? 0)}
      aria-valuetext={
        win === null
          ? "No window selected"
          : `Window from ${win.start} to ${win.start + win.dur} seconds`
      }
      aria-label="Every channel over the whole recording. Drag to select a window, click a row to pick its channel. Arrow keys move the window, plus and minus zoom, Escape clears it."
      tabIndex={0}
      data-testid="lfp-overview-area"
      onKeyDown={onKeyDown}
      onPointerDown={(e: PointerEvent<HTMLDivElement>) => {
        e.currentTarget.setPointerCapture(e.pointerId);
        const t = seconds(e.clientX);
        setDrag({ a: t, b: t, x0: e.clientX, x1: e.clientX });
      }}
      onPointerMove={(e) => {
        if (drag !== null) setDrag({ ...drag, b: seconds(e.clientX), x1: e.clientX });
      }}
      onPointerUp={(e) => {
        if (drag === null) return;
        setDrag(null);
        if (Math.abs(e.clientX - drag.x0) < CLICK_PX) {
          const c = rowAt(e.clientY);
          if (c !== null) onChannel(c);
          return;
        }
        const next = windowFromDrag(drag.a, seconds(e.clientX), total);
        if (next !== null) onSelect(next);
      }}
      onPointerCancel={() => setDrag(null)}
    >
      <Frame
        width={width}
        plotH={OVERVIEW_H}
        xDomain={[0, total]}
        yTicks={[]}
        yTitle=""
        showXAxis
        draw={draw}
        data={data}
        rows={n}
        testId="lfp-overview"
        label={`${band.label} power of all ${n} channels over the whole recording`}
      >
        {bars.map(({ g, y: top, h }) => (
          <g key={g.label}>
            <rect
              x={LEFT - 10}
              y={top}
              width={6}
              height={Math.max(1, h)}
              style={{ fill: g.token === null ? "var(--color-muted)" : `var(${g.token})` }}
            />
            {top > TOP ? (
              <line x1={LEFT - 10} x2={width - RIGHT} y1={top} y2={top} className="signals-sep" />
            ) : null}
          </g>
        ))}
        {picked < 0 ? null : (
          <rect
            x={LEFT - 3}
            y={TOP + picked * rowH}
            width={3}
            height={Math.max(2, rowH)}
            className="signals-pick"
            data-testid="signals-channel-mark"
          />
        )}
        {ticks.map((t) => (
          <rect
            key={t.id}
            x={xs(Math.max(0, t.a))}
            y={TOP + OVERVIEW_H - 4}
            width={Math.max(1, xs(Math.min(total, t.b)) - xs(Math.max(0, t.a)))}
            height={4}
            className="signals-label-tick"
            data-ab="label-tick"
          />
        ))}
        {win === null ? null : (
          <rect
            x={xs(win.start)}
            y={TOP}
            width={Math.max(1, xs(win.start + win.dur) - xs(win.start))}
            height={OVERVIEW_H}
            className="lfp-window"
            data-testid="lfp-window-mark"
            data-ab="brush"
          />
        )}
        {shown === null ? null : (
          <rect
            x={xs(shown.a)}
            y={TOP}
            width={xs(shown.b) - xs(shown.a)}
            height={OVERVIEW_H}
            className="lfp-draft"
          />
        )}
      </Frame>
      <ColourBar range={range} testId="lfp-overview-colourbar" label={`dB, ${band.label}`} />
    </div>
  );
}

// --- the window --------------------------------------------------------------------------------------

function WindowPlots({
  body,
  view,
  edges,
  range,
  width,
}: {
  body: SpectrogramBody;
  view: TimeWindow;
  edges: ArrayLike<number>;
  range: readonly [number, number];
  width: number;
}) {
  const xDomain = useMemo<readonly [number, number]>(
    () => [view.start, view.start + view.dur],
    [view.start, view.dur],
  );
  const { times, trace } = body;
  const binned = useMemo(() => logBinned(body, edges), [body, edges]);
  const nBands = binned.length;
  const grid = useMemo<Grid>(
    () => ({ nx: times.length, ny: nBands, at: (ix, iy) => binned[iy]?.[ix] ?? Number.NaN }),
    [times, nBands, binned],
  );
  const hs = halfStep(times);
  const draw = useCallback(
    (ctx: CanvasRenderingContext2D, cw: number, ch: number) => {
      const xs = linearScale(xDomain, [0, cw]);
      const x0 = xs((times[0] ?? 0) - hs);
      const x1 = xs((times[times.length - 1] ?? 0) + hs);
      drawHeatmap(ctx, renderHeatmap(grid, range, stopsFrom(rootStyle)), {
        x: x0,
        y: 0,
        w: x1 - x0,
        h: ch,
      });
    },
    [grid, range, times, hs, xDomain],
  );
  const fTicks = edgeTicks(edges, Math.max(1, Math.round(nBands / 4))).map((t) => ({
    y: SPEC_H - (t.index / Math.max(1, nBands)) * SPEC_H,
    label: t.label,
  }));
  const tDomain = useMemo(() => traceDomain(trace.values), [trace.values]);
  const tScale = linearScale(tDomain, [TRACE_H, 0]);
  const tStep = niceStep(tDomain[1] - tDomain[0], 3);
  const tTicks = niceTicks(tDomain, 3).map((v) => ({ y: tScale(v), label: formatTick(v, tStep) }));
  const drawTraceCanvas = useCallback(
    (ctx: CanvasRenderingContext2D, cw: number, ch: number) => {
      const sx = linearScale(xDomain, [0, cw]);
      const sy = linearScale(tDomain, [ch, 0]);
      ctx.save();
      drawTrace(
        ctx,
        trace.times,
        trace.values,
        sx,
        sy,
        { x: 0, y: 0, w: cw, h: ch },
        rootStyle("--color-accent").trim() || "#0969da",
      );
      ctx.restore();
    },
    [trace, tDomain, xDomain],
  );
  return (
    <div data-testid="lfp-window-plots">
      <Frame
        width={width}
        plotH={SPEC_H}
        xDomain={xDomain}
        yTicks={fTicks}
        yTitle="Hz (log)"
        showXAxis={false}
        draw={draw}
        data={binned}
        rows={nBands}
        testId="lfp-spectrogram"
        label="Spectrogram of the selected window, log frequency"
      />
      <Frame
        width={width}
        plotH={TRACE_H}
        xDomain={xDomain}
        yTicks={tTicks}
        yTitle="Signal"
        showXAxis
        draw={drawTraceCanvas}
        data={trace.values}
        rows={1}
        testId="lfp-trace"
        label="Minimum and maximum of the signal over the selected window"
      />
      <ColourBar
        range={range}
        testId="lfp-spectrogram-colourbar"
        label="dB, the file's shared range"
      />
    </div>
  );
}

/** The window: wheel to zoom, drag to pan, arrow keys to step; settles before it asks again. */
function WindowArea({
  state,
  win,
  total,
  edges,
  range,
  width,
  onWindow,
  lanes,
  labels,
}: {
  lanes: LanesState;
  labels: LabelsView | null;
  state: WindowState;
  win: TimeWindow;
  total: number;
  edges: ArrayLike<number>;
  range: readonly [number, number];
  width: number;
  onWindow: (next: TimeWindow | null) => void;
}) {
  const [view, setView] = useState<TimeWindow>(win);
  const viewRef = useRef(view);
  viewRef.current = view;
  const area = useRef<HTMLDivElement>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pan = useRef<{ x: number; start: TimeWindow } | null>(null);
  useEffect(() => setView(win), [win]);
  const settle = useCallback(
    (next: TimeWindow) => {
      setView(next);
      if (timer.current !== null) clearTimeout(timer.current);
      timer.current = setTimeout(() => {
        timer.current = null;
        onWindow(next);
      }, SETTLE_MS);
    },
    [onWindow],
  );
  useEffect(
    () => () => {
      if (timer.current !== null) clearTimeout(timer.current);
    },
    [],
  );
  useEffect(() => {
    const el = area.current;
    if (el === null) return;
    // A native listener: React's wheel handler is passive and cannot stop the page scrolling.
    const onWheel = (e: WheelEvent) => {
      if (e.deltaY === 0) return;
      e.preventDefault();
      const z = zoomWindow(viewRef.current, total, e.deltaY > 0 ? WHEEL_ZOOM : 1 / WHEEL_ZOOM);
      if (z !== null) settle(z);
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, [total, settle]);
  const secondsPerPx = view.dur / Math.max(1, width - LEFT - RIGHT);
  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    if ((e.target as HTMLElement).closest(".popover") !== null) return;
    const go = (next: TimeWindow | null) => {
      e.preventDefault();
      if (next !== null) {
        setView(next);
        onWindow(next);
      }
    };
    if (e.key === "ArrowLeft") go(panWindow(view, total, -KEY_PAN));
    else if (e.key === "ArrowRight") go(panWindow(view, total, KEY_PAN));
    else if (e.key === "+" || e.key === "=") go(zoomWindow(view, total, KEY_ZOOM));
    else if (e.key === "-") go(zoomWindow(view, total, 1 / KEY_ZOOM));
  };
  const shown =
    state.kind === "loaded"
      ? state.body
      : state.kind === "loading" && state.last !== null
        ? state.last.body
        : null;
  return (
    <div
      ref={area}
      className="signals-window"
      data-testid="signals-window"
      tabIndex={0}
      role="slider"
      aria-valuemin={0}
      aria-valuemax={Math.round(total)}
      aria-valuenow={Math.round(view.start)}
      aria-valuetext={`Window from ${view.start} to ${view.start + view.dur} seconds`}
      aria-label="The selected window. Scroll to zoom, drag to pan, arrow keys step."
      onKeyDown={onKeyDown}
      onPointerDown={(e: PointerEvent<HTMLDivElement>) => {
        if (e.button !== 0 || (e.target as HTMLElement).closest("button, .popover") !== null)
          return;
        e.currentTarget.setPointerCapture(e.pointerId);
        pan.current = { x: e.clientX, start: view };
      }}
      onPointerMove={(e) => {
        const p = pan.current;
        if (p === null) return;
        const dx = e.clientX - p.x;
        if (Math.abs(dx) < 1) return;
        setView(panWindow(p.start, total, (-dx * secondsPerPx) / p.start.dur));
      }}
      onPointerUp={() => {
        const p = pan.current;
        pan.current = null;
        if (p !== null && viewRef.current.start !== p.start.start) onWindow(viewRef.current);
      }}
      onPointerCancel={() => {
        pan.current = null;
        setView(win);
      }}
    >
      {state.kind === "error" ? (
        <Notice
          kind="error"
          testId="catalog-error"
          {...(state.onRetry === null
            ? {}
            : { action: { label: "Try again", onClick: state.onRetry } })}
        >
          {state.message}
        </Notice>
      ) : null}
      {state.kind === "loading" ? (
        <p className="meta" data-testid="signals-window-loading">
          Loading the window…
        </p>
      ) : null}
      {state.kind === "loaded" ? (
        <p className="meta" data-testid="lfp-summary">
          <span data-testid="lfp-bytes">{state.body.bytesRead} bytes read</span> ·{" "}
          <span data-testid="lfp-requests">{state.body.requests} requests</span> ·{" "}
          <span data-testid="lfp-compute-ms">compute {state.body.computeMs} ms</span>
        </p>
      ) : null}
      {shown === null ? null : (
        <div data-testid={state.kind === "loaded" ? "lfp-window-result" : undefined}>
          <WindowPlots body={shown} view={view} edges={edges} range={range} width={width} />
        </div>
      )}
      <Lanes
        state={lanes}
        domain={[view.start, view.start + view.dur]}
        width={width}
        labels={labels}
      />
    </div>
  );
}

// --- the view ----------------------------------------------------------------------------------------

function Head({ children }: { children: ReactNode }) {
  return <h3 className="signals-head">{children}</h3>;
}

export function SignalsView(p: SignalsViewProps) {
  const ov = p.overview.kind === "loaded" ? p.overview.overview : null;
  const total = ov === null ? null : totalSeconds(ov);
  const side = p.width >= SIDE_LEGEND_MIN;
  const plotWidth = side ? p.width - LEGEND_W : p.width;
  const range = useMemo(() => (ov === null ? null : sharedRange(ov)), [ov]);
  const known = p.list.some((s) => s.path === p.chosen);
  const entry = p.list.find((s) => s.path === p.chosen);
  const nChannels = ov?.nChannels ?? entry?.channels ?? 1;
  const t = total ?? 0;
  const ticks = useMemo(
    () => (p.labels?.labels ?? []).map((l) => ({ a: l.a, b: l.b, id: l.label_id })),
    [p.labels],
  );
  return (
    <section className="signals" data-testid="lfp-tab" aria-label="Signals">
      <div className="signals-layout">
        <div className="signals-main">
          <div className="lfp-controls" data-testid="lfp-controls">
            <label>
              Series
              <select
                value={p.chosen}
                data-testid="lfp-series"
                onChange={(e) => p.onSeries(e.target.value)}
              >
                {known ? null : <option value={p.chosen}>{p.chosen}</option>}
                {p.list.map((s) => (
                  <option key={s.path} value={s.path} data-ab="series-option">
                    {s.path} ({s.channels} channels, {s.rateHz} Hz)
                  </option>
                ))}
              </select>
            </label>
            <label>
              Band
              <select
                value={p.band.id}
                data-testid="signals-band"
                onChange={(e) => p.onBand(e.target.value)}
              >
                {BANDS.map((b) => (
                  <option key={b.id} value={b.id}>
                    {b.label}
                  </option>
                ))}
              </select>
            </label>
            {entry === undefined ? null : (
              <p className="meta" data-testid="signals-facts">
                {nChannels} channels at {entry.rateHz} Hz
                {total === null ? "" : ` · ${fmtSeconds(total)} s`}
              </p>
            )}
          </div>
          {p.overview.kind === "loading" ? (
            <Notice kind="info" testId="lfp-loading">
              Loading the overview…
            </Notice>
          ) : null}
          {p.overview.kind === "preparing" ? (
            <Notice
              kind="info"
              testId="lfp-preparing"
              action={{ label: "Try again", onClick: p.overview.onRetry }}
            >
              This recording's overview is being prepared.
            </Notice>
          ) : null}
          {p.overview.kind === "skipped" ? (
            <Notice kind="info" testId="signals-not-built">
              Overview not built for this series. The builder skipped it: {p.overview.reason}
            </Notice>
          ) : null}
          {p.overview.kind === "error" ? (
            <Notice
              kind="error"
              testId="catalog-error"
              {...(p.overview.onRetry === null
                ? {}
                : { action: { label: "Try again", onClick: p.overview.onRetry } })}
            >
              {p.overview.message}
            </Notice>
          ) : null}
          {ov === null || p.layout === null || range === null || total === null ? null : (
            <>
              <Head>
                Whole recording · all {ov.nChannels} channels · {p.band.label} power
              </Head>
              <p className="meta">
                Drag to choose a window, click a row to pick its channel
                {p.win === null
                  ? "."
                  : ` · current ${fmtSeconds(p.win.start)}–${fmtSeconds(p.win.start + p.win.dur)} s`}
              </p>
              <div className={side ? "signals-top signals-top-side" : "signals-top"}>
                <Regions layout={p.layout} side={side} />
                <OverviewPlot
                  overview={ov}
                  layout={p.layout}
                  band={p.band}
                  channel={p.channel}
                  win={p.win}
                  width={plotWidth}
                  onSelect={p.onWindow}
                  onChannel={p.onChannel}
                  ticks={ticks}
                />
              </div>
              <div
                className="signals-window-head"
                style={side ? { marginLeft: LEGEND_W } : undefined}
              >
                <Head>
                  {p.win === null
                    ? "Window"
                    : `Window ${fmtSeconds(p.win.start)}–${fmtSeconds(p.win.start + p.win.dur)} s`}
                </Head>
                <label>
                  <select
                    value={p.channel}
                    data-testid="lfp-channel"
                    aria-label="Channel"
                    onChange={(e) => p.onChannel(Number(e.target.value))}
                  >
                    {(p.layout.mapped
                      ? p.layout.order
                      : Array.from({ length: nChannels }, (_v, i) => i)
                    ).map((c) => (
                      <option key={c} value={c} data-ab="channel-option">
                        {channelName(p.layout, c)}
                      </option>
                    ))}
                  </select>
                </label>
                <span className="meta">scroll to zoom · drag to pan · ← → step</span>
                <div className="lfp-buttons">
                  <Button onClick={() => p.onWindow(zoomWindow(p.win, t, KEY_ZOOM))}>
                    Zoom in
                  </Button>
                  <Button
                    disabled={p.win === null}
                    onClick={() => p.onWindow(zoomWindow(p.win, t, 1 / KEY_ZOOM))}
                  >
                    Zoom out
                  </Button>
                  <Button
                    disabled={p.win === null}
                    onClick={() => p.win !== null && p.onWindow(panWindow(p.win, t, -KEY_PAN))}
                  >
                    Pan left
                  </Button>
                  <Button
                    disabled={p.win === null}
                    onClick={() => p.win !== null && p.onWindow(panWindow(p.win, t, KEY_PAN))}
                  >
                    Pan right
                  </Button>
                  <Button disabled={p.win === null} onClick={() => p.onWindow(null)}>
                    Whole recording
                  </Button>
                </div>
                <p className="meta" data-testid="signals-axis-note">
                  Log axis {edgeLabel(ov.edges[0] ?? 0)}–{edgeLabel(ov.edges[ov.nBands] ?? 0)} Hz,
                  the overview's bands; colour on the file's shared dB range. Computed in your
                  browser · nothing is saved.
                </p>
              </div>
              <div style={side ? { marginLeft: LEGEND_W } : undefined}>
                {p.win === null || p.spectrogram === null ? (
                  <Notice kind="info" testId="lfp-hint">
                    Drag across the overview to open a window of 1 to 1,200 seconds.
                  </Notice>
                ) : (
                  <WindowArea
                    state={p.spectrogram}
                    win={p.win}
                    total={total}
                    edges={ov.edges}
                    range={range}
                    width={plotWidth}
                    onWindow={p.onWindow}
                    lanes={p.lanes}
                    labels={p.labels}
                  />
                )}
              </div>
            </>
          )}
        </div>
        {p.aside === null ? null : <aside className="signals-aside">{p.aside}</aside>}
      </div>
    </section>
  );
}
