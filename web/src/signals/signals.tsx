// The `signals` chunk: the file page's signals view. It loads the file's
// series and electrodes tables, the chosen series' whole-recording overview (`GET /compute/overview`,
// one series per load) and, once a window is picked, that channel's window (`GET /compute/spectrogram`),
// and hands them to `SignalsView`. All state lives in the URL: `series=`, `channel=`, `start=`, `dur=`
// as the LFP tab kept them, plus `band=`. It also draws the event lanes (`GET /compute/events`), the
// labels (`/files/{asset_id}/labels`) and the anatomy panel; a file with no LFP series, and a BIDS
// events file, get the lanes and labels over the whole span of their events.
import {
  type ReactNode,
  type RefObject,
  useCallback,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import type { AuthStore } from "../auth/store";
import type { CatalogResult, Row } from "../catalog/catalog";
import { loadEvents, loadTable } from "../catalog/file";
import { doFetch } from "../catalog/loadable";
import {
  axisLabels,
  type Clock,
  catalogLanes,
  computeLanes,
  type EventLane,
  fromAxis,
  IDENTITY,
  lanesDomain,
  loadLaneEvents,
  shiftLanes,
  toAxis,
  windowAround,
} from "../events/lanes";
import { type LabelsCtl, useLabels } from "../events/useLabels";
import { Notice } from "../kit/Notice";
import type { LfpSeries } from "../lfp/lfp";
import {
  DEFAULTS,
  type LfpState,
  lfpSearch,
  lfpSeries,
  loadAllSeries,
  loadOverview,
  loadSpectrogram,
  overviewNotBuilt,
  parseLfpState,
  type SpectrogramBody,
  type TimeWindow,
  windowOf,
} from "../lfp/lfp";
import { totalSeconds } from "../lfp/overview";
import { useSearch } from "../research/shared";
import { appRouter } from "../router/router";
import { LoadState } from "../states/LoadState";
import { useLoad } from "../useLoad";
import { Anatomy } from "./Anatomy";
import { LabelsList } from "./LabelsList";
import { type LabelsView, Lanes, type LanesState } from "./Lanes";
import { bandSearch, channelLayout, parseBand, skippedReason } from "./model";
import { type OverviewState, SignalsView, type WindowState } from "./SignalsView";
import "./signals.css";

const MAX_PAGES = 10;
/** At this width or more, the anatomy and labels column sits on the right. */
export const ASIDE_MIN = 1000;
export const ASIDE_W = 340;
const GAP = 24;
const MAX_PLOT = 1100;
const MIN_PLOT = 280;

function plainFetch(url: string, init: RequestInit): Promise<Response> {
  return fetch(url, init);
}

/** The full width of `ref`'s box, kept current. */
function useFullWidth(ref: RefObject<HTMLElement | null>): number {
  const [width, setWidth] = useState(720);
  useLayoutEffect(() => {
    const el = ref.current;
    if (el === null) return;
    const measure = () => setWidth(Math.floor(el.clientWidth));
    measure();
    const watcher = new ResizeObserver(measure);
    watcher.observe(el);
    return () => watcher.disconnect();
  }, [ref]);
  return width;
}

/** The plot width, and whether the right column fits beside the plots. */
export function columns(full: number): { plot: number; side: boolean } {
  const side = full >= ASIDE_MIN;
  const main = side ? full - ASIDE_W - GAP : full;
  return { plot: Math.min(MAX_PLOT, Math.max(MIN_PLOT, main)), side };
}

/** The labels controller as the lanes draw it: on the axis of `clock`, or blocked with the reason. */
function labelsView(ctl: LabelsCtl, clock: Clock, blocked: string | null): LabelsView {
  return {
    labels: axisLabels(ctl.labels, clock),
    selectedId: ctl.selected?.label_id ?? null,
    draft:
      ctl.draft === null
        ? null
        : { a: toAxis(clock, ctl.draft.start), b: toAxis(clock, ctl.draft.stop) },
    blocked,
    busy: ctl.busy,
    refusal: ctl.refusal,
    onDraft: (a, b) => ctl.startDraft({ start: fromAxis(clock, a), stop: fromAxis(clock, b) }),
    onSelect: ctl.select,
    onCancel: ctl.cancel,
    onCreate: (name) => {
      void ctl.create(name);
    },
    onChange: (name, a, b) => {
      void ctl.change(name, { start: fromAxis(clock, a), stop: fromAxis(clock, b) });
    },
    onRemove: () => {
      void ctl.remove();
    },
    onReload: ctl.reload,
  };
}

interface LanesBody {
  /** Built on the trials clock. */
  lanes: EventLane[];
  offset: number;
  notes: string[];
}

type LanesLoad =
  | { kind: "loading" }
  | { kind: "error"; message: string; onRetry: () => void }
  | ({ kind: "loaded" } & LanesBody);

/** The lanes from `GET /compute/events` (an NWB file) or the catalog's events rows (a BIDS events file). */
function useLanes(auth: AuthStore, assetId: string, source: "compute" | "catalog"): LanesLoad {
  const run = useCallback(async (): Promise<CatalogResult<LanesBody>> => {
    if (source === "compute") {
      const r = await loadLaneEvents(auth.config, assetId, auth, doFetch);
      if (r.kind !== "loaded") return r;
      return {
        kind: "loaded",
        value: { lanes: computeLanes(r.value, IDENTITY), offset: r.value.offset, notes: [] },
      };
    }
    const r = await loadEvents(auth.config, assetId, auth, doFetch);
    if (r.kind !== "loaded") return r;
    const notes: string[] = [];
    if (r.value.intervals_more) {
      notes.push("Showing the first 20,000 intervals; the file has more.");
    }
    if (r.value.events_more) notes.push("Showing the first 20,000 events; the file has more.");
    return {
      kind: "loaded",
      value: { lanes: catalogLanes(r.value.intervals, r.value.events), offset: 0, notes },
    };
  }, [auth, assetId, source]);
  const { state, reload } = useLoad(auth, run);
  if (state.kind === "loaded") return { kind: "loaded", ...state.value };
  if (state.kind === "error") return { kind: "error", message: state.message, onRetry: reload };
  return { kind: "loading" };
}

/** The lanes moved from the trials clock onto the view's axis. */
function useLanesState(load: LanesLoad, clock: Clock): LanesState {
  const lanes = load.kind === "loaded" ? load.lanes : null;
  const { offset, startingTime } = clock;
  const shifted = useMemo(
    () => (lanes === null ? null : shiftLanes(lanes, { offset, startingTime })),
    [lanes, offset, startingTime],
  );
  if (load.kind !== "loaded" || shifted === null)
    return load.kind === "loaded" ? { kind: "loading" } : load;
  return { kind: "loaded", lanes: shifted };
}

const BLOCKED_LOADING =
  "Labels can be drafted once the events have loaded: they set the recording's clock.";
const BLOCKED_ERROR =
  "Labels cannot be drafted here: the events did not load, so the clock that places a label is unknown.";

function blockedBy(load: LanesLoad): string | null {
  if (load.kind === "loaded") return null;
  return load.kind === "loading" ? BLOCKED_LOADING : BLOCKED_ERROR;
}

export interface FileWho {
  dataset: string | null;
  subject: string | null;
}

function Aside({
  auth,
  who,
  ctl,
  clock,
  around,
  onPick,
}: {
  auth: AuthStore;
  who: FileWho;
  ctl: LabelsCtl;
  clock: Clock;
  around: readonly [number, number];
  onPick: (id: string, a: number, b: number) => void;
}): ReactNode {
  return (
    <>
      <Anatomy auth={auth} dataset={who.dataset} subject={who.subject} />
      <LabelsList
        labels={axisLabels(ctl.labels, clock)}
        around={around}
        selectedId={ctl.selected?.label_id ?? null}
        loading={ctl.loading}
        error={ctl.error}
        onRetry={ctl.reload}
        onPick={(l) => onPick(l.label_id, l.a, l.b)}
      />
    </>
  );
}

async function loadElectrodes(auth: AuthStore, assetId: string): Promise<CatalogResult<Row[]>> {
  const rows: Row[] = [];
  for (let page = 0; page < MAX_PAGES; page += 1) {
    const r = await loadTable(auth.config, assetId, "electrodes", page, auth, doFetch);
    if (r.kind !== "loaded") return r;
    rows.push(...r.value.rows);
    if (!r.value.more) break;
  }
  return { kind: "loaded", value: rows };
}

function useWindow(
  auth: AuthStore,
  assetId: string,
  series: string,
  channel: number,
  win: TimeWindow | null,
): WindowState | null {
  const run = useCallback(
    (): Promise<CatalogResult<SpectrogramBody> | { kind: "idle" }> =>
      win === null
        ? Promise.resolve({ kind: "idle" as const })
        : loadSpectrogram(auth.config, assetId, series, channel, win, auth, doFetch),
    [auth, assetId, series, channel, win],
  );
  const { state, reload } = useLoad(auth, run);
  // The last window drawn stays on screen, moved to the new time axis, while the next one loads.
  const last = useRef<{ body: SpectrogramBody; win: TimeWindow } | null>(null);
  if (win === null) return null;
  if (state.kind === "loaded") {
    last.current = { body: state.value, win };
    return { kind: "loaded", body: state.value };
  }
  if (state.kind === "error") {
    return { kind: "error", message: state.message, onRetry: state.status503 ? reload : null };
  }
  return { kind: "loading", last: last.current };
}

function SignalsBody({
  auth,
  assetId,
  list,
  who,
}: {
  auth: AuthStore;
  assetId: string;
  list: readonly LfpSeries[];
  who: FileWho;
}) {
  const search = useSearch();
  const state = useMemo(() => parseLfpState(search), [search]);
  const band = useMemo(() => parseBand(search), [search]);
  const chosen = state.series === "" ? (list[0]?.path ?? "") : state.series;
  const { start, dur } = state;
  // The window object changes only when its numbers do, so the window is not asked for again.
  const win = useMemo(() => windowOf({ ...DEFAULTS, start, dur }), [start, dur]);
  const go = (next: string) =>
    appRouter().navigate(`/files/${encodeURIComponent(assetId)}${next}`, { replace: true });
  const set = (next: LfpState) => go(lfpSearch(search, next));
  const runOverview = useCallback(
    () => loadOverview(auth.config, assetId, chosen, auth, doFetch, plainFetch),
    [auth, assetId, chosen],
  );
  const { state: ov, reload } = useLoad(auth, runOverview);
  const runElectrodes = useCallback(() => loadElectrodes(auth, assetId), [auth, assetId]);
  const { state: el } = useLoad(auth, runElectrodes);
  const holder = useRef<HTMLDivElement>(null);
  const { plot: width, side } = columns(useFullWidth(holder));
  const events = useLanes(auth, assetId, "compute");
  const ctl = useLabels(auth, assetId);
  const overview = ov.kind === "loaded" ? ov.value : null;
  const entry = list.find((s) => s.path === chosen);
  const channels = overview?.nChannels ?? entry?.channels ?? 1;
  const channel = Math.min(state.channel, Math.max(0, channels - 1));
  const electrodes = el.kind === "loaded" ? el.value : null;
  const layout = useMemo(
    () =>
      overview === null || el.kind === "loading"
        ? null
        : channelLayout(overview.nChannels, electrodes),
    [overview, el.kind, electrodes],
  );
  const spectrogram = useWindow(auth, assetId, chosen, channel, win);
  const clock: Clock = {
    offset: events.kind === "loaded" ? events.offset : 0,
    startingTime: overview?.startingTime ?? 0,
  };
  const lanesView = useLanesState(events, clock);
  const total = overview === null ? null : totalSeconds(overview);
  const onWindow = (next: TimeWindow | null) =>
    set({ ...state, start: next?.start ?? null, dur: next?.dur ?? null });
  const around: readonly [number, number] =
    win === null ? [0, total ?? 0] : [win.start, win.start + win.dur];
  let overviewState: OverviewState;
  if (ov.kind === "loading" || ov.kind === "ended") overviewState = { kind: "loading" };
  else if (ov.kind === "loaded") overviewState = { kind: "loaded", overview: ov.value };
  else if (overviewNotBuilt(ov)) overviewState = { kind: "preparing", onRetry: reload };
  else {
    const reason = skippedReason(ov.message);
    overviewState =
      reason === null
        ? { kind: "error", message: ov.message, onRetry: ov.status503 ? reload : null }
        : { kind: "skipped", reason };
  }
  return (
    <div className={side ? "signals-holder signals-side" : "signals-holder"} ref={holder}>
      <SignalsView
        list={list}
        chosen={chosen}
        band={band}
        overview={overviewState}
        layout={layout}
        channel={channel}
        win={win}
        spectrogram={spectrogram}
        width={width}
        onSeries={(path) => set({ ...DEFAULTS, series: path })}
        onBand={(id) => go(bandSearch(search, id))}
        onChannel={(c) => set({ ...state, channel: c })}
        onWindow={onWindow}
        lanes={lanesView}
        labels={labelsView(ctl, clock, blockedBy(events))}
        aside={
          <Aside
            auth={auth}
            who={who}
            ctl={ctl}
            clock={clock}
            around={around}
            onPick={(id, a, b) => {
              ctl.select(id);
              if (total !== null && (win === null || b < win.start || a > win.start + win.dur)) {
                onWindow(windowAround(a, b, total));
              }
            }}
          />
        }
      />
    </div>
  );
}

/** Lanes and labels over the whole span of the file's events: no LFP series, or a BIDS events file. */
function EventsOnly({
  auth,
  assetId,
  source,
  who,
  children,
}: {
  auth: AuthStore;
  assetId: string;
  source: "compute" | "catalog";
  who: FileWho;
  children?: ReactNode;
}) {
  const holder = useRef<HTMLDivElement>(null);
  const { plot: width, side } = columns(useFullWidth(holder));
  const events = useLanes(auth, assetId, source);
  const ctl = useLabels(auth, assetId);
  const lanes = events.kind === "loaded" ? events.lanes : null;
  const lanesView = useLanesState(events, IDENTITY);
  const domain = useMemo((): readonly [number, number] => {
    const spans: EventLane[] = [
      ...(lanes ?? []),
      {
        name: "",
        items: ctl.labels.map((l) => ({ start: l.start_s, stop: l.stop_s, title: "" })),
      },
    ];
    const d = lanesDomain(spans);
    return [Math.max(0, d[0] - 1), d[1] + 1];
  }, [lanes, ctl.labels]);
  return (
    <div className={side ? "signals-holder signals-side" : "signals-holder"} ref={holder}>
      <section className="signals" data-testid="events-view" aria-label="Events and labels">
        <div className="signals-layout">
          <div className="signals-main">
            {children}
            <h3 className="signals-head">Events and labels over the whole file</h3>
            {events.kind === "loaded"
              ? events.notes.map((n) => (
                  <Notice key={n} kind="info" testId="lanes-more">
                    {n}
                  </Notice>
                ))
              : null}
            <Lanes
              state={lanesView}
              domain={domain}
              width={width}
              labels={labelsView(ctl, IDENTITY, blockedBy(events))}
            />
          </div>
          <aside className="signals-aside">
            <Aside
              auth={auth}
              who={who}
              ctl={ctl}
              clock={IDENTITY}
              around={domain}
              onPick={(id) => ctl.select(id)}
            />
          </aside>
        </div>
      </section>
    </div>
  );
}

/** A BIDS events file: its events and labels on one axis. */
export function EventsFile({
  auth,
  assetId,
  who,
}: {
  auth: AuthStore;
  assetId: string;
  who: FileWho;
}) {
  return <EventsOnly auth={auth} assetId={assetId} source="catalog" who={who} />;
}

export function Signals({
  auth,
  assetId,
  who,
}: {
  auth: AuthStore;
  assetId: string;
  who: FileWho;
}) {
  const run = useCallback(
    () => loadAllSeries(auth.config, assetId, auth, doFetch),
    [auth, assetId],
  );
  const { state, reload } = useLoad(auth, run);
  return (
    <LoadState
      state={state}
      noun="series"
      path={`/catalog/assets/${assetId}/tables/series`}
      onRetry={reload}
    >
      {(rows) => {
        const list = lfpSeries(rows);
        return list.length === 0 ? (
          <EventsOnly auth={auth} assetId={assetId} source="compute" who={who}>
            <Notice kind="info" testId="lfp-none">
              This file has no LFP series.
            </Notice>
          </EventsOnly>
        ) : (
          <SignalsBody auth={auth} assetId={assetId} list={list} who={who} />
        );
      }}
    </LoadState>
  );
}
