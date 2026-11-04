// Flexible inspection, pure: one control row (normalisation, baseline and response windows,
// bin, linked or per-unit scales, the "computed in your browser" note), a scatter of any unit attribute
// against any other coloured by region, one strip per region on the response axis, and the top 12 by
// |response|. Every number comes from `model.ts :: inspect` over the returned raster; there is no Save.
import type { CSSProperties, ReactNode } from "react";
import { Button } from "../kit/Button";
import {
  ATTRS,
  type Attr,
  attrValue,
  BINS,
  type Inspected,
  type InspectState,
  LOG_ATTRS,
  NORMS,
  ranked,
  responseLabel,
  type UnitPoint,
  type Window,
  windowLabel,
} from "./model";

export interface InspectViewProps {
  readonly subject: string;
  readonly session: string;
  readonly eventName: string;
  readonly nTrials: number;
  readonly window: Window;
  readonly state: InspectState;
  readonly defaults: InspectState;
  readonly data: Inspected;
  readonly heatmapHref: string;
  readonly onChange?: (next: InspectState) => void;
}

const W = 800;
const H = 340;
const PAD = { l: 44, r: 12, t: 10, b: 26 };
const LOG_FLOOR = 0.1;

interface Scale {
  readonly at: (v: number) => number;
  readonly ticks: readonly { v: number; label: string }[];
}

function niceStep(span: number): number {
  const raw = span / 5;
  const p = 10 ** Math.floor(Math.log10(raw));
  const m = raw / p;
  return (m < 1.5 ? 1 : m < 3 ? 2 : m < 7 ? 5 : 10) * p;
}

function fmt(v: number): string {
  const r = Math.round(v * 100) / 100;
  return r < 0 ? `−${Math.abs(r)}` : String(r);
}

/** A linear or log scale over `values` onto [a, b] (b < a for a y axis). */
export function scale(values: readonly number[], log: boolean, a: number, b: number): Scale {
  const vs = log ? values.map((v) => Math.log10(Math.max(LOG_FLOOR, v))) : [...values];
  let lo = Math.min(0, ...vs);
  let hi = Math.max(0, ...vs);
  if (log) {
    lo = Math.floor(Math.min(...vs, 0));
    hi = Math.ceil(Math.max(...vs, 1));
  }
  if (vs.length === 0 || hi === lo) {
    lo -= 1;
    hi += 1;
  }
  const map = (v: number) => a + ((v - lo) / (hi - lo)) * (b - a);
  const at = (v: number) => map(log ? Math.log10(Math.max(LOG_FLOOR, v)) : v);
  const ticks: { v: number; label: string }[] = [];
  if (log) {
    for (let e = lo; e <= hi; e += 1) ticks.push({ v: 10 ** e, label: fmt(10 ** e) });
  } else {
    const step = niceStep(hi - lo);
    for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step)
      ticks.push({ v, label: fmt(v) });
  }
  return { at, ticks };
}

function Seg<T extends string | number>({
  label,
  options,
  value,
  onPick,
  testId,
}: {
  label: string;
  options: readonly (readonly [T, string])[];
  value: T;
  onPick: (v: T) => void;
  testId: string;
}) {
  return (
    <fieldset className="in-seg">
      <legend>{label}</legend>
      {options.map(([v, text]) => (
        <Button
          key={String(v)}
          variant={v === value ? "primary" : "secondary"}
          aria-pressed={v === value}
          onClick={() => onPick(v)}
          data-testid={`${testId}-${v}`}
        >
          {text}
        </Button>
      ))}
    </fieldset>
  );
}

function WindowControl({
  label,
  testId,
  value,
  min,
  max,
  step,
  isDefault,
  onChange,
}: {
  label: string;
  testId: string;
  value: readonly [number, number];
  min: number;
  max: number;
  step: number;
  isDefault: boolean;
  onChange: (w: [number, number]) => void;
}) {
  const snap = (v: number) => Math.round(v / step) * step;
  return (
    <fieldset className="in-window">
      <legend>{label}</legend>
      <span className="in-range">
        <input
          type="range"
          aria-label={`${label} start`}
          data-testid={`${testId}-start`}
          min={min}
          max={max}
          step={step}
          value={value[0]}
          onChange={(e) => {
            const v = snap(Number(e.currentTarget.value));
            if (v < value[1]) onChange([v, value[1]]);
          }}
        />
        <input
          type="range"
          aria-label={`${label} end`}
          data-testid={`${testId}-end`}
          min={min}
          max={max}
          step={step}
          value={value[1]}
          onChange={(e) => {
            const v = snap(Number(e.currentTarget.value));
            if (v > value[0]) onChange([value[0], v]);
          }}
        />
      </span>
      <output data-testid={`${testId}-text`}>
        {windowLabel(value)}
        {isDefault ? " default" : ""}
      </output>
    </fieldset>
  );
}

function AttrSelect({
  label,
  value,
  onPick,
  testId,
}: {
  label: string;
  value: Attr;
  onPick: (a: Attr) => void;
  testId: string;
}) {
  return (
    <label className="in-select">
      <span>{label}</span>
      <select
        data-testid={testId}
        value={value}
        onChange={(e) => {
          const a = ATTRS.find(([k]) => k === e.currentTarget.value)?.[0];
          if (a !== undefined) onPick(a);
        }}
      >
        {ATTRS.map(([k, text]) => (
          <option key={k} value={k}>
            {text}
            {LOG_ATTRS.has(k) ? ", log" : ""}
          </option>
        ))}
      </select>
    </label>
  );
}

function attrLabel(a: Attr, s: InspectState): string {
  if (a === "response") return responseLabel(s);
  const text = ATTRS.find(([k]) => k === a)?.[1] ?? a;
  return LOG_ATTRS.has(a) ? `${text}, log` : text;
}

function Scatter({ data, state }: { data: Inspected; state: InspectState }) {
  const pts = data.points.flatMap((p) => {
    const x = attrValue(p, state.x);
    const y = attrValue(p, state.y);
    return x === null || y === null ? [] : [{ p, x, y }];
  });
  const sx = scale(
    pts.map((q) => q.x),
    LOG_ATTRS.has(state.x),
    PAD.l,
    W - PAD.r,
  );
  const sy = scale(
    pts.map((q) => q.y),
    LOG_ATTRS.has(state.y),
    H - PAD.b,
    PAD.t,
  );
  return (
    <svg
      className="in-scatter"
      viewBox={`0 0 ${W} ${H}`}
      role="img"
      aria-label={`${attrLabel(state.y, state)} against ${attrLabel(state.x, state)}, one dot per unit`}
    >
      {sy.ticks.map((t) => (
        <g key={`y${t.v}`} className="in-tick">
          <line x1={PAD.l} x2={W - PAD.r} y1={sy.at(t.v)} y2={sy.at(t.v)} />
          <text x={PAD.l - 4} y={sy.at(t.v) + 3} textAnchor="end">
            {t.label}
          </text>
        </g>
      ))}
      {sx.ticks.map((t) => (
        <text
          key={`x${t.v}`}
          className="in-tick-x"
          x={sx.at(t.v)}
          y={H - PAD.b + 14}
          textAnchor="middle"
        >
          {t.label}
        </text>
      ))}
      <text className="in-axis-label" x={W - PAD.r} y={H - 2} textAnchor="end">
        {attrLabel(state.x, state)}
      </text>
      <text className="in-axis-label" x={PAD.l + 4} y={PAD.t + 10}>
        {attrLabel(state.y, state)}
      </text>
      {pts.map(({ p, x, y }) => (
        <circle
          key={p.unit}
          data-ab="scatter-dot"
          data-unit={p.unit}
          cx={sx.at(x)}
          cy={sy.at(y)}
          r={4.5}
          style={{ fill: p.cssVar } as CSSProperties}
        >
          <title>{`unit ${p.unit}, ${p.location}: ${fmt(y)} against ${fmt(x)}`}</title>
        </circle>
      ))}
    </svg>
  );
}

const SW = 800;
const ROW = 20;

function Strips({ data, state }: { data: Inspected; state: InspectState }) {
  const values = data.points.flatMap((p) => (p.response === null ? [] : [p.response]));
  const sx = scale(values, false, 150, SW - 12);
  const h = data.regions.length * ROW + 16;
  return (
    <section className="in-strips" aria-label="Response by region">
      <h3 className="in-kicker">{responseLabel(state)} by region · each dot a unit · same axis</h3>
      <svg
        viewBox={`0 0 ${SW} ${h}`}
        role="img"
        aria-label="One strip per region, one dot per unit"
      >
        <line
          x1={sx.at(0)}
          x2={sx.at(0)}
          y1={0}
          y2={data.regions.length * ROW}
          className="in-strip-zero"
        />
        {data.regions.map((r, i) => {
          const y = i * ROW + ROW / 2;
          return (
            <g
              key={r.key}
              data-ab="strip"
              data-region={r.key}
              style={{ "--in-region": r.cssVar } as CSSProperties}
            >
              <text x={0} y={y + 4} className="in-strip-label">
                {r.label}
              </text>
              <line x1={150} x2={SW - 12} y1={y} y2={y} className="in-strip-axis" />
              {data.points
                .filter((p) => p.regionKey === r.key && p.response !== null)
                .map((p) => (
                  <circle
                    key={p.unit}
                    data-ab="strip-dot"
                    cx={sx.at(p.response ?? 0)}
                    cy={y}
                    r={4}
                  />
                ))}
            </g>
          );
        })}
        {sx.ticks.map((t) => (
          <text key={t.v} className="in-tick-x" x={sx.at(t.v)} y={h - 3} textAnchor="middle">
            {t.label}
          </text>
        ))}
      </svg>
    </section>
  );
}

function Ranked({ data, state }: { data: Inspected; state: InspectState }) {
  const top = ranked(data.points);
  const max = Math.max(1e-9, ...top.map((p) => Math.abs(p.response ?? 0)));
  const signed = (v: number) => (v < 0 ? `−${Math.abs(v).toFixed(1)}` : `+${v.toFixed(1)}`);
  return (
    <aside className="in-rail" aria-label="Ranked units">
      <header className="in-rank-head">
        <small className="in-kicker">Ranked by |{responseLabel(state)}|</small>
        <small>
          top {top.length} of {data.points.length}
        </small>
      </header>
      <ol className="in-ranked">
        {top.map((p: UnitPoint) => (
          <li
            key={p.unit}
            data-ab="ranked-row"
            data-unit={p.unit}
            style={{ "--in-region": p.cssVar } as CSSProperties}
          >
            <span className="in-rank-unit">{p.unit}</span>
            <span className="in-rank-region">{p.regionLabel}</span>
            <span className="in-rank-bar">
              <i
                className={(p.response ?? 0) >= 0 ? "in-up" : "in-down"}
                style={{ width: `${(100 * Math.abs(p.response ?? 0)) / max}%` }}
              />
            </span>
            <span className="in-rank-value">{signed(p.response ?? 0)}</span>
          </li>
        ))}
      </ol>
      {data.undefinedUnits.length === 0 ? null : (
        <p className="in-muted" data-testid="inspect-undefined">
          Undefined under {responseLabel(state)}, left out: unit
          {data.undefinedUnits.length === 1 ? "" : "s"} {data.undefinedUnits.join(", ")}
          {state.norm === "z" ? " (no baseline variance)" : " (a zero baseline rate)"}.
        </p>
      )}
    </aside>
  );
}

export function InspectView(p: InspectViewProps): ReactNode {
  const s = p.state;
  const set = (next: Partial<InspectState>) => p.onChange?.({ ...s, ...next });
  const binOptions = BINS.map((b) => [b, `${Math.round(b * 1000)} ms`] as const);
  return (
    <section className="in-page" data-testid="inspect-page" aria-label="Inspect units">
      <header className="in-head">
        <h2>
          Inspect units <span className="in-muted">{p.subject}</span>{" "}
          <span className="in-muted">{p.session}</span>
        </h2>
        <p className="meta">
          {p.data.points.length} units · aligned to {p.eventName} · {p.nTrials} events ·{" "}
          <a href={p.heatmapHref} data-testid="inspect-heatmap-link">
            back to the heatmap
          </a>
        </p>
      </header>
      <div className="in-controls" data-ab="control-row">
        <Seg
          label="Normalise"
          options={NORMS}
          value={s.norm}
          onPick={(norm) => set({ norm })}
          testId="norm"
        />
        <WindowControl
          label="Baseline"
          testId="baseline"
          value={s.base}
          min={-p.window.pre}
          max={0}
          step={s.bin}
          isDefault={s.base[0] === p.defaults.base[0] && s.base[1] === p.defaults.base[1]}
          onChange={(base) => set({ base })}
        />
        <WindowControl
          label="Response"
          testId="response"
          value={s.resp}
          min={0}
          max={p.window.post}
          step={s.bin}
          isDefault={s.resp[0] === p.defaults.resp[0] && s.resp[1] === p.defaults.resp[1]}
          onChange={(resp) => set({ resp })}
        />
        <Seg
          label="Bin"
          options={binOptions}
          value={s.bin}
          onPick={(bin) => set({ bin })}
          testId="bin"
        />
        <Seg
          label="Scales"
          options={[
            ["linked", "Linked"],
            ["unit", "Per unit"],
          ]}
          value={s.scales}
          onPick={(scales) => set({ scales })}
          testId="scales"
        />
        <p className="in-note" data-ab="local-note" data-testid="inspect-note">
          Computed in your browser · nothing is saved
        </p>
      </div>
      <div className="in-main">
        <div className="in-plotbar">
          <AttrSelect label="Plot" value={s.y} onPick={(y) => set({ y })} testId="inspect-y" />
          <AttrSelect label="against" value={s.x} onPick={(x) => set({ x })} testId="inspect-x" />
          <span className="in-muted">colour: region</span>
          <span className="in-muted in-count">{p.data.points.length} units</span>
        </div>
        <Scatter data={p.data} state={s} />
        <Strips data={p.data} state={s} />
      </div>
      <Ranked data={p.data} state={s} />
    </section>
  );
}
