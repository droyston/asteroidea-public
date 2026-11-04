// The Units tab: pick units, an alignment and a window; the page
// asks `GET /compute/rates` and draws, per unit, a raster and a rate (or z) curve with a line at 0. The
// web never bins: the curve is the response's `rate_hz` / `z` as sent. All state lives in the URL.
import { type FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import type { AuthStore } from "../auth/store";
import { loadEvents } from "../catalog/file";
import { doFetch } from "../catalog/loadable";
import { labelAlignOptions, loadLabels } from "../events/labels";
import { Button } from "../kit/Button";
import { Notice } from "../kit/Notice";
import { formatTick, linearScale, niceStep, niceTicks } from "../plots/axis";
import { curvePath, curvePoints, valueDomain } from "../plots/curve";
import { rasterPath } from "../plots/raster";
import { useSearch } from "../research/shared";
import { appRouter } from "../router/router";
import { LoadState } from "../states/LoadState";
import { useLoad } from "../useLoad";
import {
  type AlignOption,
  type FileRef,
  intervalOptions,
  loadAllUnits,
  loadExtraOptions,
  loadRates,
  MAX_UNITS,
  parseUnitsState,
  type RatesBody,
  type RatesUnit,
  type UnitsState,
  unitsSearch,
} from "./units";

const W = 720;
const LEFT = 48;
const RIGHT = 8;
const RASTER_H = 90;
const CURVE_H = 110;
const AXIS_H = 22;

function num(v: unknown): string {
  return typeof v === "number" ? String(v) : "—";
}

function UnitPicker({
  auth,
  assetId,
  selected,
  onChange,
}: {
  auth: AuthStore;
  assetId: string;
  selected: readonly number[];
  onChange: (units: number[]) => void;
}) {
  const run = useCallback(() => loadAllUnits(auth.config, assetId, auth, doFetch), [auth, assetId]);
  const { state, reload } = useLoad(auth, run);
  return (
    <LoadState
      state={state}
      noun="units"
      path={`/catalog/assets/${assetId}/tables/units`}
      onRetry={reload}
    >
      {(rows) => (
        <fieldset className="unit-picker" data-testid="unit-picker">
          <legend>
            Units ({selected.length} of {MAX_UNITS} selected)
          </legend>
          {rows.length === 0 ? <p>This file has no units.</p> : null}
          {rows.map((row) => {
            const index = typeof row.unit_index === "number" ? row.unit_index : -1;
            const on = selected.includes(index);
            const location = typeof row.location === "string" ? row.location : "no location";
            return (
              <label key={index} className="unit-choice">
                <input
                  type="checkbox"
                  checked={on}
                  disabled={!on && selected.length >= MAX_UNITS}
                  onChange={() =>
                    onChange(
                      on
                        ? selected.filter((u) => u !== index)
                        : [...selected, index].sort((a, b) => a - b),
                    )
                  }
                />
                <span>
                  Unit {index} · {location} · {num(row.spike_count)} spikes
                </span>
              </label>
            );
          })}
        </fieldset>
      )}
    </LoadState>
  );
}

/** The alignment choices, and whether any of the three lookups behind them is still running. */
function useAlignOptions(
  auth: AuthStore,
  file: FileRef,
): { options: AlignOption[]; loading: boolean } {
  const run = useCallback(
    () => loadEvents(auth.config, file.assetId, auth, doFetch),
    [auth, file.assetId],
  );
  const { state } = useLoad(auth, run);
  const [extra, setExtra] = useState<AlignOption[]>([]);
  const [extraDone, setExtraDone] = useState(false);
  const [labelsDone, setLabelsDone] = useState(false);
  const { assetId, dataset, subject, session } = file;
  useEffect(() => {
    let live = true;
    setExtraDone(false);
    void loadExtraOptions(auth.config, { assetId, dataset, subject, session }, auth, doFetch).then(
      (o) => {
        if (!live) return;
        setExtra(o);
        setExtraDone(true);
      },
    );
    return () => {
      live = false;
    };
  }, [auth, assetId, dataset, subject, session]);
  const [labelOptions, setLabelOptions] = useState<AlignOption[]>([]);
  useEffect(() => {
    let live = true;
    setLabelsDone(false);
    void loadLabels(auth.config, assetId, auth, doFetch).then((r) => {
      if (!live) return;
      // A failed lookup offers nothing, like the other extra options.
      if (r.kind === "loaded") setLabelOptions(labelAlignOptions(r.value));
      setLabelsDone(true);
    });
    return () => {
      live = false;
    };
  }, [auth, assetId]);
  const options = useMemo(
    () => [
      ...(state.kind === "loaded" ? intervalOptions(state.value.intervals) : []),
      ...extra,
      ...labelOptions,
    ],
    [state, extra, labelOptions],
  );
  return { options, loading: state.kind === "loading" || !extraDone || !labelsDone };
}

function Controls({
  state,
  options,
  loading,
  onChange,
}: {
  state: UnitsState;
  options: readonly AlignOption[];
  loading: boolean;
  onChange: (next: UnitsState) => void;
}) {
  const [draft, setDraft] = useState({
    pre: String(state.pre),
    post: String(state.post),
    bin: String(state.bin),
  });
  useEffect(() => {
    setDraft({ pre: String(state.pre), post: String(state.post), bin: String(state.bin) });
  }, [state.pre, state.post, state.bin]);
  const known = options.some((o) => o.value === state.align);
  const submit = (e: FormEvent) => {
    e.preventDefault();
    const pre = Number(draft.pre);
    const post = Number(draft.post);
    const bin = Number(draft.bin);
    if ([pre, post, bin].every((n) => Number.isFinite(n) && n >= 0)) {
      onChange({ ...state, pre, post, bin });
    }
  };
  const field = (name: "pre" | "post" | "bin", label: string) => (
    <label>
      {label}
      <input
        type="number"
        step="any"
        min="0"
        value={draft[name]}
        data-testid={`units-${name}`}
        onChange={(e) => setDraft({ ...draft, [name]: e.target.value })}
      />
    </label>
  );
  return (
    <form className="units-controls" onSubmit={submit} data-testid="units-controls">
      <label>
        Align to
        <select
          value={state.align}
          data-testid="units-align"
          disabled={loading}
          aria-busy={loading}
          onChange={(e) => onChange({ ...state, align: e.target.value })}
        >
          <option value="">{loading ? "Loading alignments…" : "Choose…"}</option>
          {known || state.align === "" ? null : <option value={state.align}>{state.align}</option>}
          {options.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
      </label>
      {field("pre", "Before (s)")}
      {field("post", "After (s)")}
      {field("bin", "Bin (s)")}
      <label className="units-check">
        <input
          type="checkbox"
          checked={state.z}
          data-testid="units-z"
          onChange={(e) => onChange({ ...state, z: e.target.checked })}
        />
        Z-score
      </label>
      <label className="units-check">
        <input
          type="checkbox"
          checked={state.raster}
          onChange={(e) => onChange({ ...state, raster: e.target.checked })}
        />
        Raster
      </label>
      <Button type="submit">Update</Button>
    </form>
  );
}

function UnitPlot({ body, unit, z }: { body: RatesBody; unit: RatesUnit; z: boolean }) {
  const first = body.edges[0] ?? 0;
  const last = body.edges[body.edges.length - 1] ?? first + 1;
  const xs = linearScale([first, last], [LEFT, W - RIGHT]);
  const showZ = z && unit.z !== null;
  const series = showZ && unit.z !== null ? unit.z : unit.rate_hz;
  const name = showZ ? "z" : "rate_hz";
  const ys0 = valueDomain(series, !showZ);
  const rasterH = unit.raster === null ? 0 : RASTER_H;
  const curveTop = rasterH === 0 ? 0 : rasterH + 8;
  const ys = linearScale(ys0, [curveTop + CURVE_H, curveTop]);
  const points = curvePoints(body.edges, series, xs, ys);
  const height = curveTop + CURVE_H + AXIS_H;
  const step = niceStep(last - first, 6);
  const ticks = niceTicks([first, last], 6);
  const yStep = niceStep(ys0[1] - ys0[0], 3);
  const yTicks = niceTicks(ys0, 3);
  const zeroX = xs(0);
  return (
    <figure className="unit-plot" data-testid={`unit-plot-${unit.unit_index}`}>
      <figcaption>Unit {unit.unit_index}</figcaption>
      {z && unit.z === null ? (
        <p className="meta" data-testid={`unit-noz-${unit.unit_index}`}>
          no z-score: flat baseline
        </p>
      ) : null}
      <svg
        viewBox={`0 0 ${W} ${height}`}
        width="100%"
        role="img"
        aria-label={`Unit ${unit.unit_index}: ${showZ ? "z-score" : "firing rate"} against time from alignment`}
      >
        <title>{`Unit ${unit.unit_index}`}</title>
        {ticks.map((t) => (
          <g key={t}>
            <line x1={xs(t)} x2={xs(t)} y1={0} y2={curveTop + CURVE_H} className="timeline-grid" />
            <text x={xs(t)} y={height - 6} textAnchor="middle" className="timeline-tick">
              {formatTick(t, step)}
            </text>
          </g>
        ))}
        {unit.raster === null ? null : (
          <path
            d={rasterPath(unit.raster.trial, unit.raster.t, body.n_trials, xs, RASTER_H)}
            className="raster-ticks"
            data-testid={`raster-${unit.unit_index}`}
            data-spikes={unit.raster.t.length}
          />
        )}
        {yTicks.map((t) => (
          <text key={t} x={LEFT - 4} y={ys(t) + 3} textAnchor="end" className="timeline-tick">
            {formatTick(t, yStep)}
          </text>
        ))}
        <path
          d={curvePath(points)}
          className="rate-curve"
          data-testid={`curve-${unit.unit_index}`}
          data-series={name}
          data-values={JSON.stringify(points.map((p) => p.value))}
        />
        <line
          x1={zeroX}
          x2={zeroX}
          y1={0}
          y2={curveTop + CURVE_H}
          className="zero-line"
          data-testid={`zero-${unit.unit_index}`}
        />
      </svg>
      <p className="meta">{showZ ? "z-score" : "Hz"} per bin, time in seconds from the alignment</p>
    </figure>
  );
}

function Summary({ body }: { body: RatesBody }) {
  const missing = body.missing_frames;
  return (
    <p className="meta" data-testid="rates-summary">
      <span data-testid="rates-n-trials">{body.n_trials} trials</span> ·{" "}
      <span data-testid="rates-source">source: {body.source}</span> ·{" "}
      <span data-testid="rates-compute-ms">compute {body.compute_ms} ms</span> · query{" "}
      {body.query_ms} ms
      {missing !== null && missing.length > 0 ? (
        <>
          {" "}
          ·{" "}
          <span data-testid="rates-missing">
            {missing.length} frames outside the series: {missing.slice(0, 10).join(", ")}
            {missing.length > 10 ? ", …" : ""}
          </span>
        </>
      ) : null}
    </p>
  );
}

function RatesView({
  auth,
  assetId,
  state,
  onChange,
}: {
  auth: AuthStore;
  assetId: string;
  state: UnitsState;
  onChange: (next: UnitsState) => void;
}) {
  const run = useCallback(
    () => loadRates(auth.config, assetId, state, auth, doFetch),
    [auth, assetId, state],
  );
  const { state: result, reload } = useLoad(auth, run);
  return (
    <LoadState
      state={result}
      noun="firing rates"
      path="/compute/rates"
      what={`Computing firing rates for ${state.units.length} units`}
      onRetry={reload}
      request={{ units: state.units.length }}
      onSmaller={(next) =>
        onChange({ ...state, units: state.units.slice(0, next.units ?? state.units.length) })
      }
      notebookCode={`nwb = asteroidea.open_nwb("${assetId}")`}
    >
      {(body) => (
        <div data-testid="rates-result">
          <Summary body={body} />
          {body.units.map((u) => (
            <UnitPlot key={u.unit_index} body={body} unit={u} z={state.z} />
          ))}
        </div>
      )}
    </LoadState>
  );
}

export function UnitsTab({ auth, file }: { auth: AuthStore; file: FileRef }) {
  const search = useSearch();
  const state = useMemo(() => parseUnitsState(search), [search]);
  const { options, loading } = useAlignOptions(auth, file);
  const set = (next: UnitsState) =>
    appRouter().navigate(`/files/${encodeURIComponent(file.assetId)}${unitsSearch(search, next)}`, {
      replace: true,
    });
  const ready = state.units.length > 0 && state.align !== "";
  return (
    <div className="units-tab" data-testid="units-tab">
      <UnitPicker
        auth={auth}
        assetId={file.assetId}
        selected={state.units}
        onChange={(units) => set({ ...state, units })}
      />
      <Controls state={state} options={options} loading={loading} onChange={set} />
      {ready ? (
        <RatesView auth={auth} assetId={file.assetId} state={state} onChange={set} />
      ) : (
        <Notice kind="info" testId="units-hint">
          Pick at least one unit and an alignment to draw firing rates.
        </Notice>
      )}
    </div>
  );
}
