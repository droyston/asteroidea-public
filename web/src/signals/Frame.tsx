// The plot frame of the signals view, moved from the LFP tab it replaces: a canvas for the pixels with
// an SVG over it for the axes, ticks and overlays. The canvas repaints when the theme changes, since
// the plots read colour tokens. The numbers drawn sit on the canvas element as `astData` for tests.
import {
  type ReactNode,
  type RefObject,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import { formatTick, linearScale, niceStep, niceTicks } from "../plots/axis";
import { themeStore } from "../shell/theme";

export const LEFT = 52;
export const RIGHT = 12;
export const TOP = 6;
export const AXIS_H = 24;
const MIN_WIDTH = 280;
const MAX_WIDTH = 1100;

export function rootStyle(token: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(token);
}

/** The width of `ref`'s box, kept current; plots draw at this width so their text stays readable. */
export function useWidth(ref: RefObject<HTMLElement | null>): number {
  const [width, setWidth] = useState(720);
  useLayoutEffect(() => {
    const el = ref.current;
    if (el === null) return;
    const measure = () =>
      setWidth(Math.min(MAX_WIDTH, Math.max(MIN_WIDTH, Math.floor(el.clientWidth))));
    measure();
    const watcher = new ResizeObserver(measure);
    watcher.observe(el);
    return () => watcher.disconnect();
  }, [ref]);
  return width;
}

export interface YTick {
  y: number;
  label: string;
}

export interface FrameProps {
  width: number;
  plotH: number;
  /** The time range the x axis shows, seconds. */
  xDomain: readonly [number, number];
  yTicks: readonly YTick[];
  yTitle: string;
  showXAxis: boolean;
  /** Paints the canvas; `cw` by `ch` are its pixels. */
  draw: (ctx: CanvasRenderingContext2D, cw: number, ch: number) => void;
  /** The numbers drawn, kept on the canvas element for tests (`element.astData`). */
  data: unknown;
  testId: string;
  label: string;
  /** Rows the canvas draws, for the artboard's stand-in count. */
  rows: number;
  /** Drawn in the SVG under the axes text, in the frame's own coordinates. */
  children?: ReactNode;
}

export function Frame(p: FrameProps) {
  const plotW = p.width - LEFT - RIGHT;
  const height = TOP + p.plotH + (p.showXAxis ? AXIS_H : 4);
  const ref = useRef<HTMLCanvasElement>(null);
  const dpr =
    typeof window === "undefined" ? 1 : Math.max(1, Math.min(2, window.devicePixelRatio || 1));
  const { draw, data } = p;
  const pw = Math.round(plotW * dpr);
  const ph = Math.round(p.plotH * dpr);
  useEffect(() => {
    const canvas = ref.current;
    const ctx = canvas?.getContext("2d");
    if (canvas === null || canvas === undefined || !ctx) return;
    // A new size clears the canvas: the effect runs again on a size change and paints after it.
    canvas.width = pw;
    canvas.height = ph;
    const paint = () => {
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      draw(ctx, canvas.width, canvas.height);
    };
    paint();
    Object.assign(canvas, { astData: data });
    return themeStore().subscribe(paint);
  }, [draw, data, pw, ph]);
  const xs = linearScale(p.xDomain, [LEFT, LEFT + plotW]);
  const count = Math.max(2, Math.floor(plotW / 90));
  const step = niceStep(p.xDomain[1] - p.xDomain[0], count);
  const xTicks = niceTicks(p.xDomain, count);
  return (
    <div className="lfp-frame" style={{ width: p.width, height }}>
      <canvas
        ref={ref}
        className="lfp-canvas"
        data-testid={p.testId}
        data-ab="canvas"
        data-ab-standin={p.label}
        data-ab-rows={p.rows}
        width={pw}
        height={ph}
        style={{ left: LEFT, top: TOP, width: plotW, height: p.plotH }}
        role="img"
        aria-label={p.label}
      />
      <svg
        className="lfp-axes"
        width={p.width}
        height={height}
        viewBox={`0 0 ${p.width} ${height}`}
        aria-hidden="true"
      >
        <rect x={LEFT} y={TOP} width={plotW} height={p.plotH} className="lfp-box" fill="none" />
        {p.children}
        {xTicks.map((t) => (
          <g key={t}>
            <line
              x1={xs(t)}
              x2={xs(t)}
              y1={TOP + p.plotH}
              y2={TOP + p.plotH + 3}
              className="lfp-tickmark"
            />
            {p.showXAxis ? (
              <text x={xs(t)} y={TOP + p.plotH + 16} textAnchor="middle" className="timeline-tick">
                {formatTick(t, step)}
              </text>
            ) : null}
          </g>
        ))}
        {p.yTicks.map((t) => (
          <g key={`${t.label}@${t.y}`}>
            <line x1={LEFT - 3} x2={LEFT} y1={TOP + t.y} y2={TOP + t.y} className="lfp-tickmark" />
            <text x={LEFT - 6} y={TOP + t.y + 3} textAnchor="end" className="timeline-tick">
              {t.label}
            </text>
          </g>
        ))}
        {p.yTitle === "" ? null : (
          <text
            x={10}
            y={TOP + p.plotH / 2}
            textAnchor="middle"
            className="timeline-tick"
            transform={`rotate(-90 10 ${TOP + p.plotH / 2})`}
          >
            {p.yTitle}
          </text>
        )}
      </svg>
    </div>
  );
}

export function ColourBar({
  range,
  testId,
  label,
}: {
  range: readonly [number, number];
  testId: string;
  label: string;
}) {
  const ticks = niceTicks(range, 4);
  const step = niceStep(range[1] - range[0], 4);
  const at = linearScale(range, [0, 100]);
  return (
    <div className="lfp-colourbar" data-testid={testId}>
      <div className="lfp-ramp" />
      <div className="lfp-ramp-ticks">
        {ticks.map((t) => (
          <span key={t} style={{ left: `${at(t)}%` }}>
            {formatTick(t, step)}
          </span>
        ))}
      </div>
      <span className="meta">{label}</span>
    </div>
  );
}
