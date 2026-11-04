// The session heatmap, pure: the header, the onset strip of the chosen
// event type, the event-type picker, the heatmap grouped by side-kept region with each region's
// colour edge and label, the Δ column, and the right rail with the baseline box, the colour scale and
// the totals. The heatmap cells are a canvas the browser paints from the server's z (`HeatCanvas`);
// everything a reader counts (rows, groups, bars, the null-z rows, the tuning marks, totals) is
// markup. The View toggle swaps the heatmap for one PSTH per unit (small multiples, `?view=multiples`).
import { type CSSProperties, type ReactNode, useLayoutEffect, useRef } from "react";
import { Button } from "../kit/Button";
import { parseHex, type Rgb, rampColour } from "../plots/heatmap";
import { themeStore } from "../shell/theme";
import { CopyCode } from "../snippets/CopyCode";
import type { EventType } from "../synthesis/session";
import { Multiples } from "./Multiples";
import {
  BASELINE_TEXT,
  type Defaults,
  type Group,
  type Heatmap,
  type HeatRow,
  NULL_Z_TEXT,
  rowPx,
  type SessionLayout,
  signed,
  THRESHOLD,
  type TuningMarks,
  type TuningState,
  typeLabel,
  windowText,
  Z_RANGE,
} from "./model";
import { MarkColumn, TuningSection } from "./Tuning";

/** The diverging scale, fire less (blue) to fire more (red). */
const DIVERGING = [
  "--color-div-7",
  "--color-div-6",
  "--color-div-5",
  "--color-div-4",
  "--color-div-3",
  "--color-div-2",
  "--color-div-1",
] as const;

const DELTA_W = 96;
const DELTA_MID = 48;
const DELTA_SCALE = DELTA_MID / Z_RANGE;

export interface HeatBody {
  readonly heat: Heatmap;
  readonly defaults: Defaults;
  readonly nTrials: number;
  /** The stored summary's `built_at` when the rates came from it; null when computed live. */
  readonly storedAt?: string | null;
}

export interface SessionViewProps {
  readonly dataset: string;
  readonly subject: string;
  /** The session label as the URL carries it. */
  readonly session: string;
  readonly assetId: string;
  readonly fileHref: string;
  /** Units in the file's units table. */
  readonly unitsInFile: number;
  readonly types: readonly EventType[];
  readonly eventType: string;
  readonly onPick?: (name: string) => void;
  /** The heatmap once the rates have drawn; null while they load or after a refusal. */
  readonly body: HeatBody | null;
  /** What stands in the heatmap's place while `body` is null (the wait, a refusal). */
  readonly status?: ReactNode;
  /** The exploratory tuning screen for the chosen type; absent draws no marks and no section. */
  readonly tuning?: TuningState;
  readonly layout?: SessionLayout;
  readonly onLayout?: (layout: SessionLayout) => void;
  /** The flexible inspection page for these units. */
  readonly inspectHref?: string;
}

function readStops(): Rgb[] {
  const style = getComputedStyle(document.documentElement);
  return DIVERGING.map((t) => parseHex(style.getPropertyValue(t)) ?? [0, 0, 0]);
}

/** Paint one group's rows: one pixel per bin and unit, scaled by CSS; a null-z row stays empty. */
function paint(canvas: HTMLCanvasElement, rows: readonly HeatRow[], bins: number): void {
  const ctx = canvas.getContext("2d");
  if (ctx === null) return;
  const stops = readStops();
  const image = ctx.createImageData(bins, rows.length);
  for (const [y, row] of rows.entries()) {
    if (row.z === null) continue;
    for (let x = 0; x < bins; x += 1) {
      const v = row.z[x];
      if (v === null || v === undefined || !Number.isFinite(v)) continue;
      const [r, g, b] = rampColour(stops, (v + Z_RANGE) / (2 * Z_RANGE));
      const o = (y * bins + x) * 4;
      image.data[o] = r;
      image.data[o + 1] = g;
      image.data[o + 2] = b;
      image.data[o + 3] = 255;
    }
  }
  ctx.putImageData(image, 0, 0);
}

function HeatCanvas({
  rows,
  bins,
  px,
  label,
}: {
  rows: readonly HeatRow[];
  bins: number;
  px: number;
  label: string;
}) {
  const ref = useRef<HTMLCanvasElement>(null);
  useLayoutEffect(() => {
    const canvas = ref.current;
    if (canvas === null) return;
    paint(canvas, rows, bins);
    const scheme = window.matchMedia("(prefers-color-scheme: dark)");
    const repaint = () => paint(canvas, rows, bins);
    scheme.addEventListener("change", repaint);
    // The paint reads colour tokens: it runs again when the page's theme choice changes.
    const off = themeStore().subscribe(repaint);
    return () => {
      scheme.removeEventListener("change", repaint);
      off();
    };
  }, [rows, bins]);
  return (
    <canvas
      ref={ref}
      className="sh-canvas"
      width={bins}
      height={rows.length}
      style={{ height: `${rows.length * px}px` }}
      role="img"
      aria-label={`${label}: z per 50 ms bin, one row per unit`}
      data-ab-standin="heatmap cells: painted in the browser from the server's z"
      data-ab-rows={rows.length}
    />
  );
}

function DeltaColumn({ rows, px }: { rows: readonly HeatRow[]; px: number }) {
  const h = rows.length * px;
  const bar = Math.max(1, px - 2);
  return (
    <svg className="sh-delta" width={DELTA_W} height={h} aria-hidden="true">
      <line x1={DELTA_MID} y1={0} x2={DELTA_MID} y2={h} className="sh-zero" />
      {rows.map((r, i) => {
        const y = i * px;
        if (r.delta === null) {
          return (
            <g key={r.unit} data-ab="unit-row" data-unit={r.unit}>
              <title>{`unit ${r.unit}: ${NULL_Z_TEXT}`}</title>
            </g>
          );
        }
        const len = Math.min(DELTA_MID, Math.abs(r.delta) * DELTA_SCALE);
        const x = r.delta >= 0 ? DELTA_MID : DELTA_MID - len;
        return (
          <g key={r.unit} data-ab="unit-row" data-unit={r.unit}>
            <title>{`unit ${r.unit}, ${r.location}: mean z ${signed(r.delta)}`}</title>
            <rect
              data-ab="delta-bar"
              className={`sh-bar sh-bar-${r.delta >= 0 ? "up" : "down"}`}
              x={x}
              y={y + (px - bar) / 2}
              width={Math.max(0.5, len)}
              height={bar}
            />
          </g>
        );
      })}
    </svg>
  );
}

function GroupBlock({ group, bins, px, edges, marks }: GroupProps) {
  const zeroAt = zeroPercent(edges);
  const readable = px >= 10;
  return (
    <section
      className="sh-group"
      data-ab="region-group"
      data-region={group.key}
      style={{ "--sh-region": group.cssVar } as CSSProperties}
      aria-label={group.label}
    >
      <div className="sh-region">
        <strong>{group.label}</strong>
        <small>
          <span className="sh-up">↑{group.more}</span>{" "}
          <span className="sh-down">↓{group.less}</span> of {group.rows.length}
        </small>
      </div>
      <div className="sh-units" aria-hidden="true">
        {readable
          ? group.rows.map((r) => (
              <span key={r.unit} style={{ height: `${px}px` }}>
                {r.unit}
              </span>
            ))
          : null}
      </div>
      <div className="sh-cells">
        <HeatCanvas rows={group.rows} bins={bins} px={px} label={group.label} />
        <div className="sh-onset" style={{ left: `${zeroAt}%` }} />
        {group.rows.map((r, i) =>
          r.z === null ? (
            <div
              key={r.unit}
              className="sh-null"
              data-ab="null-z-row"
              data-unit={r.unit}
              style={{ top: `${i * px}px`, height: `${px}px`, lineHeight: `${px}px` }}
            >
              <span>
                unit {r.unit}: {NULL_Z_TEXT}
              </span>
            </div>
          ) : null,
        )}
      </div>
      <DeltaColumn rows={group.rows} px={px} />
      <div className="sh-values" aria-hidden="true">
        {readable
          ? group.rows.map((r) => (
              <span key={r.unit} style={{ height: `${px}px` }}>
                {r.delta === null ? "—" : signed(r.delta)}
              </span>
            ))
          : null}
      </div>
      <MarkColumn rows={group.rows} px={px} marks={marks} />
    </section>
  );
}

interface GroupProps {
  group: Group;
  bins: number;
  px: number;
  edges: readonly number[];
  marks: TuningMarks | null;
}

function zeroPercent(edges: readonly number[]): number {
  const first = edges[0] ?? 0;
  const last = edges[edges.length - 1] ?? 1;
  return last === first ? 0 : (100 * (0 - first)) / (last - first);
}

function OnsetStrip({ type }: { type: EventType | undefined }) {
  if (type === undefined) return null;
  const onsets = type.events.map((e) => e.onset_s);
  const end = Math.max(1, ...onsets, ...type.events.map((e) => e.stop_s ?? 0));
  return (
    <div className="sh-strip">
      <span className="sh-strip-label">
        {typeLabel(type.name)} · {type.n_events}
      </span>
      <svg
        className="sh-strip-ticks"
        viewBox="0 0 1000 20"
        preserveAspectRatio="none"
        role="img"
        aria-label={`${type.n_events} ${typeLabel(type.name)} onsets over ${(end / 60).toFixed(1)} min`}
      >
        <line x1={0} y1={19.5} x2={1000} y2={19.5} className="sh-strip-axis" />
        <path
          className="sh-strip-onsets"
          d={onsets.map((t) => `M${((t / end) * 1000).toFixed(2)} 2V19`).join("") || "M0 19"}
        />
      </svg>
      <span className="sh-strip-end">{(end / 60).toFixed(1)} min</span>
    </div>
  );
}

function Picker({
  types,
  eventType,
  onPick,
}: {
  types: readonly EventType[];
  eventType: string;
  onPick?: ((name: string) => void) | undefined;
}) {
  return (
    <fieldset className="sh-picker">
      <legend className="sh-picker-label">Aligned to</legend>
      {types.map((t) => (
        <Button
          key={t.name}
          variant={t.name === eventType ? "primary" : "secondary"}
          aria-pressed={t.name === eventType}
          onClick={() => onPick?.(t.name)}
          data-testid={`event-${t.name}`}
        >
          {typeLabel(t.name)} · {t.n_events}
        </Button>
      ))}
    </fieldset>
  );
}

function Axis({ edges, eventName }: { edges: readonly number[]; eventName: string }) {
  const first = edges[0] ?? 0;
  const last = edges[edges.length - 1] ?? 1;
  const at = (t: number) => `${(100 * (t - first)) / (last - first)}%`;
  const ticks: { t: number; label: string }[] = [];
  for (let t = Math.ceil(first); t <= last; t += 1) {
    ticks.push({ t, label: t === 0 ? eventName : t < 0 ? `−${-t} s` : `+${t} s` });
  }
  return (
    <div className="sh-axis">
      <span className="sh-axis-region">Region</span>
      <span className="sh-axis-unit">Unit</span>
      <div className="sh-axis-time">
        {ticks.map((k) => (
          <span
            key={k.t}
            style={{ left: at(k.t) }}
            className={
              k.t === 0
                ? "sh-axis-zero"
                : k.t === first
                  ? "sh-axis-first"
                  : k.t === last
                    ? "sh-axis-last"
                    : ""
            }
          >
            {k.label}
          </span>
        ))}
      </div>
      <span className="sh-axis-delta">Δ mean z</span>
      <span className="sh-axis-values">z</span>
      <span className="sh-axis-marks" title="passed the exploratory tuning screen">
        T
      </span>
    </div>
  );
}

function LayoutToggle({
  layout,
  onLayout,
}: {
  layout: SessionLayout;
  onLayout?: ((layout: SessionLayout) => void) | undefined;
}) {
  const options: readonly [SessionLayout, string][] = [
    ["heatmap", "Heatmap"],
    ["multiples", "Small multiples"],
  ];
  return (
    <fieldset className="sh-picker sh-layout">
      <legend className="sh-picker-label">View</legend>
      {options.map(([id, label]) => (
        <Button
          key={id}
          variant={id === layout ? "primary" : "secondary"}
          aria-pressed={id === layout}
          onClick={() => onLayout?.(id)}
          data-testid={`view-${id}`}
        >
          {label}
        </Button>
      ))}
    </fieldset>
  );
}

function Rail({
  body,
  eventName,
  tuning,
  inspectHref,
}: {
  body: HeatBody | null;
  eventName: string;
  tuning: TuningState | undefined;
  inspectHref: string | undefined;
}) {
  const d = body?.defaults;
  const t = body?.heat.totals;
  return (
    <aside className="sh-rail" aria-label="Baseline and totals">
      <section className="sh-baseline" data-ab="baseline-box" aria-label="Baseline">
        <small className="sh-kicker">Baseline</small>
        <h3>{BASELINE_TEXT}</h3>
        <p>
          Each unit against its own rate in the second before the {eventName}, as z
          {body === null ? "" : ` over ${body.nTrials} events`}; 50 ms bins.
          {inspectHref === undefined ? null : (
            <>
              {" "}
              Change it on{" "}
              <a href={inspectHref} data-testid="session-inspect-link">
                Inspect
              </a>
              .
            </>
          )}
        </p>
        <p className="sh-default">
          Analysis default
          {d === undefined
            ? ": loading"
            : ` (${d.block} block): baseline ${d.baseline}, response window ${windowText(d.responseWindow)}`}
          .
        </p>
        {body?.storedAt == null ? null : (
          <p className="sh-default" data-testid="session-from-summary">
            From the stored summary built {body.storedAt}.
          </p>
        )}
      </section>
      <div className="sh-scale" aria-hidden="true">
        <div className="sh-scale-bar" />
        <div className="sh-scale-labels">
          <span>−{Z_RANGE} fires less</span>
          <span>0</span>
          <span>fires more +{Z_RANGE}</span>
        </div>
      </div>
      {t === undefined || d === undefined ? null : (
        <div className="sh-totals" data-ab="totals">
          <p className="sh-big">
            <span>
              <strong className="sh-up">{t.more}</strong>
              <small>fire more</small>
            </span>
            <span>
              <strong className="sh-down">{t.less}</strong>
              <small>fire less</small>
            </span>
            <span>
              <strong>{t.flat}</strong>
              <small>|z| &lt; {THRESHOLD}</small>
            </span>
          </p>
          <p className="sh-rule">
            Fire more: mean z ≥ +{THRESHOLD} over {windowText(d.responseWindow)} after the{" "}
            {eventName}; fire less: mean z ≤ −{THRESHOLD}. The window is the {d.block} defaults
            block's response window.
          </p>
          {t.nullUnits.length === 0 ? null : (
            <p className="sh-nulls">
              {NULL_Z_TEXT}, counted in neither direction: unit{t.nullUnits.length === 1 ? "" : "s"}{" "}
              {t.nullUnits.join(", ")}
            </p>
          )}
        </div>
      )}
      {tuning === undefined || body === null ? null : (
        <TuningSection groups={body.heat.groups} tuning={tuning} />
      )}
    </aside>
  );
}

export function SessionView(p: SessionViewProps) {
  const chosen = p.types.find((t) => t.name === p.eventType);
  const eventName = typeLabel(p.eventType);
  const heat = p.body?.heat;
  const px = rowPx(heat?.units ?? 0);
  const bins = (heat?.edges.length ?? 1) - 1;
  const capped = heat !== undefined && heat.units < p.unitsInFile;
  const marks = p.tuning?.kind === "marks" ? p.tuning.marks : null;
  const layout = p.layout ?? "heatmap";
  return (
    <section className="sh-page" data-testid="session-page" aria-label="Session heatmap">
      <header className="sh-head">
        <h2>
          {p.subject} <span className="sh-session">{p.session}</span>
        </h2>
        <CopyCode
          page="session"
          values={{ dataset: p.dataset, subject: p.subject, session: p.session }}
        />
        <p className="meta">
          {heat === undefined
            ? `${p.unitsInFile} units`
            : `${heat.units} units in ${heat.groups.length} regions`}{" "}
          · {p.dataset} · file <a href={p.fileHref}>{p.assetId}</a>
          {capped ? (
            <span data-testid="session-capped">
              {" "}
              · showing the first {heat.units} of {p.unitsInFile} units (the server's bound per
              request)
            </span>
          ) : null}
        </p>
      </header>
      <div className="sh-main">
        <OnsetStrip type={chosen} />
        <div className="sh-controls">
          <Picker types={p.types} eventType={p.eventType} onPick={p.onPick} />
          <LayoutToggle layout={layout} onLayout={p.onLayout} />
        </div>
        {heat === undefined ? (
          p.status
        ) : layout === "multiples" ? (
          <Multiples heat={heat} screened={marks?.screened ?? new Set()} />
        ) : (
          <div className="sh-heat" data-testid="session-heatmap">
            <Axis edges={heat.edges} eventName={eventName} />
            {heat.groups.map((g) => (
              <GroupBlock
                key={g.key}
                group={g}
                bins={bins}
                px={px}
                edges={heat.edges}
                marks={marks}
              />
            ))}
          </div>
        )}
      </div>
      <Rail body={p.body} eventName={eventName} tuning={p.tuning} inspectHref={p.inspectHref} />
    </section>
  );
}
