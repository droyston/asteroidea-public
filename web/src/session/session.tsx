// The session page (the lazy `session` chunk): resolves the URL's session to its NWB
// file with the most units, waits on the events, the units table and the rates at the display
// parameters, and draws `SessionView`. It marks the view ready once the heatmap has drawn, or once a
// plain sentence (no NWB, no events to align to, a refusal) stands in its place. Each of the three
// comes from the stored session summary when it holds that part for the file, so a session whose
// summary holds every part opens with no live request. The exploratory tuning screen for the chosen
// type comes from the stored `tuning-<type>-<attribute>` part when there is one, else from one live
// `/compute/tuning` request; the view marks ready once the screen has settled too. `?view=inspect`
// draws flexible inspection (the lazy `inspect` chunk) over the same file and units instead.
import { type ReactNode, useCallback, useEffect, useMemo, useState } from "react";
import { TIMEOUT_MS } from "../auth/apiGet";
import type { AuthStore } from "../auth/store";
import type { CatalogResult } from "../catalog/catalog";
import { filePath } from "../catalog/depth";
import { doFetch } from "../catalog/loadable";
import { Notice } from "../kit/Notice";
import { useSearch } from "../research/shared";
import { dataCrumbs, dataSubjectPath, NO_SESSION } from "../router/dataPaths";
import { type ChunkResult, loadChunk, renderChunk } from "../router/lazy";
import { appRouter } from "../router/router";
import { Ready, Spine } from "../shell/marks";
import type { EventType } from "../synthesis/session";
import { loadTuning, parseTuning } from "../synthesis/tuning";
import { useLoad } from "../useLoad";
import {
  alignFromSearch,
  buildHeatmap,
  chooseStored,
  chooseStoredEvents,
  chooseStoredTuning,
  chooseStoredUnits,
  inspectQuery,
  layoutFromSearch,
  loadEvents,
  loadRates,
  loadRatesStoredOrLive,
  loadStoredOrLive,
  loadStudies,
  loadSummary,
  loadUnitLabels,
  type NwbAsset,
  parseSessionEvents,
  pickAsset,
  pickableTypes,
  type SessionEvents,
  type StoredOrLive,
  type SummaryAnswer,
  searchWithLayout,
  sessionAssets,
  storedUnitLabels,
  type TuningState,
  tuningAttribute,
  tuningMarks,
  typeLabel,
  type UnitLabel,
  unitSelection,
} from "./model";
import { type HeatBody, SessionView } from "./SessionView";
import "./session.css";

export interface SessionRoute {
  study: string;
  dataset: string;
  subject: string;
  /** The route segment: a session label, or `_` for none. */
  session: string;
}

type Meta =
  | {
      kind: "ready";
      asset: NwbAsset;
      events: SessionEvents;
      labels: UnitLabel[];
      /** The stored summary, or null when missing or unreadable (the page computes live). */
      summary: SummaryAnswer | null;
    }
  | { kind: "absent"; assets: NwbAsset[] | null }
  | { kind: "refused"; asset: NwbAsset; failure: Failure };

type Failure = Extract<CatalogResult<unknown>, { kind: "error" }>;

function failure(message: string): Failure {
  return { kind: "error", message, status503: false, code: null };
}

/** The value of a stored-or-live load, whichever source it came from. */
function plainValue<T>(r: CatalogResult<StoredOrLive<T>>): CatalogResult<T> {
  return r.kind === "loaded" ? { kind: "loaded", value: r.value.value } : r;
}

/** Seconds since `active` last became true, ticking every 100 ms while it stays true. */
function useElapsed(active: boolean): number {
  const [since, setSince] = useState(() => performance.now());
  const [now, setNow] = useState(since);
  useEffect(() => {
    if (!active) return;
    const start = performance.now();
    setSince(start);
    setNow(start);
    const id = window.setInterval(() => setNow(performance.now()), 100);
    return () => window.clearInterval(id);
  }, [active]);
  return Math.max(0, (now - since) / 1000);
}

function Waiting({ testId, what, seconds }: { testId: string; what: string; seconds: number }) {
  return (
    <Notice kind="info" testId={testId}>
      {what}… <span data-testid="session-elapsed">{seconds.toFixed(1)} s</span>
    </Notice>
  );
}

/** The sentence and next step for a refusal: 413 too large, 503 slow, 422 not NWB, else the server's. */
function Refusal({
  state,
  fileHref,
  retry,
}: {
  state: Failure;
  fileHref: string | null;
  retry: () => void;
}) {
  const file =
    fileHref === null ? null : (
      <>
        {" "}
        <a href={fileHref} data-testid="session-file-link">
          Open the file page
        </a>
        .
      </>
    );
  let sentence: ReactNode;
  let action: { label: string; onClick: () => void } | undefined;
  if (state.code === "too_large") {
    sentence = (
      <>
        The rates for these units are over the server's 5 MiB response cap, so the heatmap cannot
        load. Look at fewer units at a time on the file page's Units tab, or in the SDK.{file}
      </>
    );
  } else if (state.status503 || state.code === "slow") {
    sentence = (
      <>
        The server did not finish the rates within its time limit. Try again: a second request is
        often faster once the file is warm.
      </>
    );
    action = { label: "Try again", onClick: retry };
  } else if (state.code === "not_nwb") {
    sentence = <>This file is not an NWB file, so it has no units or events to show.{file}</>;
  } else {
    sentence = (
      <>
        {state.message}
        {file}
      </>
    );
  }
  return (
    <Notice kind="error" testId="session-refusal" {...(action === undefined ? {} : { action })}>
      {sentence}
      {state.code === "too_large" || state.code === "slow" || state.code === "not_nwb" ? (
        <span className="sh-detail"> ({state.message})</span>
      ) : null}
    </Notice>
  );
}

function Heat({
  auth,
  route,
  asset,
  events,
  labels,
  summary,
}: {
  auth: AuthStore;
  route: SessionRoute;
  asset: NwbAsset;
  events: SessionEvents;
  labels: UnitLabel[];
  summary: SummaryAnswer | null;
}) {
  const types = useMemo(() => pickableTypes(events.types), [events]);
  const [eventType, setEventType] = useState(types[0]?.name ?? "");
  const units = useMemo(() => unitSelection(labels.map((l) => l.unit_index)), [labels]);
  const assetId = asset.asset_id;
  const stored = useMemo(
    () => chooseStored(summary, assetId, eventType),
    [summary, assetId, eventType],
  );
  const run = useCallback(
    () =>
      loadRatesStoredOrLive(
        stored,
        () => loadRates(auth.config, assetId, eventType, units, auth, doFetch, TIMEOUT_MS),
        doFetch,
        TIMEOUT_MS,
      ),
    [auth, assetId, eventType, units, stored],
  );
  const rates = useLoad(auth, run);
  const loading = rates.state.kind === "loading";
  const seconds = useElapsed(loading);
  const body: HeatBody | null = useMemo(() => {
    if (rates.state.kind !== "loaded") return null;
    const r = rates.state.value.rates;
    return {
      heat: buildHeatmap(labels, r.rates, r.defaults.responseWindow),
      defaults: r.defaults,
      nTrials: r.rates.n_trials,
      storedAt: rates.state.value.storedAt,
    };
  }, [rates.state, labels]);
  const chosen = types.find((t) => t.name === eventType);
  const tuning = useTuning(auth, summary, assetId, chosen);
  const search = useSearch();
  const layout = layoutFromSearch(search);
  const base = sessionPath(route);
  const settled = (body !== null && tuning.kind !== "loading") || rates.state.kind === "error";
  return (
    <>
      {settled ? <Ready /> : null}
      <SessionView
        dataset={route.dataset}
        subject={route.subject}
        session={route.session}
        assetId={assetId}
        fileHref={filePath(assetId)}
        unitsInFile={labels.length}
        types={types}
        eventType={eventType}
        onPick={setEventType}
        body={body}
        tuning={tuning}
        layout={layout}
        onLayout={(next) =>
          appRouter().navigate(`${base}${searchWithLayout(search, next)}`, { replace: true })
        }
        inspectHref={`${base}${inspectQuery(eventType)}`}
        status={
          rates.state.kind === "error" ? (
            <Refusal state={rates.state} fileHref={filePath(assetId)} retry={rates.reload} />
          ) : (
            <Waiting
              testId="session-rates-loading"
              what={`Computing the rates for ${chosen?.n_events ?? 0} ${typeLabel(eventType)} events (this takes several seconds)`}
              seconds={seconds}
            />
          )
        }
      />
    </>
  );
}

type Inspect = typeof import("../inspect/inspect");

let inspectChunk: Promise<ChunkResult<Inspect>> | null = null;
function inspectModule(): Promise<ChunkResult<Inspect>> {
  inspectChunk ??= loadChunk(() => import("../inspect/inspect")).then((r) => {
    if (r.kind === "stale") inspectChunk = null;
    return r;
  });
  return inspectChunk;
}

/** Flexible inspection for the file's units, from the lazy `inspect` chunk. */
function InspectSlot({
  auth,
  route,
  asset,
  types,
  labels,
  eventType,
}: {
  auth: AuthStore;
  route: SessionRoute;
  asset: NwbAsset;
  types: readonly EventType[];
  labels: UnitLabel[];
  eventType: string;
}) {
  const [chunk, setChunk] = useState<ChunkResult<Inspect> | null>(null);
  useEffect(() => {
    let live = true;
    void inspectModule().then((r) => {
      if (live) setChunk(r);
    });
    return () => {
      live = false;
    };
  }, []);
  const units = useMemo(() => unitSelection(labels.map((l) => l.unit_index)), [labels]);
  if (chunk === null) {
    return (
      <Notice kind="info" testId="inspect-chunk-loading">
        Loading…
      </Notice>
    );
  }
  const name = types.find((t) => t.name === eventType)?.name ?? eventType;
  return renderChunk(chunk, (m) => (
    <m.InspectPage
      auth={auth}
      assetId={asset.asset_id}
      eventType={eventType}
      eventName={typeLabel(name)}
      units={units}
      labels={labels}
      subject={route.subject}
      session={route.session}
      base={sessionPath(route)}
    />
  ));
}

/** The page's own path (the route segments, encoded). */
function sessionPath(route: SessionRoute): string {
  const parts = [route.study, route.dataset, route.subject, route.session];
  return `/s/${parts.map(encodeURIComponent).join("/")}`;
}

/**
 * The exploratory tuning screen for the chosen type: the stored part when the summary holds one for
 * this file and type, else one live `/compute/tuning` request for the type's first attribute with 2
 * to 12 levels. A refusal is the server's sentence in the rail; it never blocks the heatmap.
 */
function useTuning(
  auth: AuthStore,
  summary: SummaryAnswer | null,
  assetId: string,
  type: EventType | undefined,
): TuningState {
  const stored = useMemo(
    () => (type === undefined ? null : chooseStoredTuning(summary, assetId, type.name)),
    [summary, assetId, type],
  );
  const attribute = useMemo(
    () => stored?.attribute ?? (type === undefined ? null : tuningAttribute(type)),
    [stored, type],
  );
  const run = useCallback(async (): Promise<CatalogResult<TuningState>> => {
    if (type === undefined || attribute === null) {
      const name = typeLabel(type?.name ?? "");
      return {
        kind: "loaded",
        value: {
          kind: "none",
          reason: `No attribute of the ${name} events has 2 to 12 levels, so there is nothing to screen.`,
        },
      };
    }
    const r = await loadStoredOrLive(
      stored?.choice ?? null,
      parseTuning,
      () => loadTuning(auth.config, assetId, type.name, attribute, auth, doFetch, TIMEOUT_MS),
      doFetch,
      TIMEOUT_MS,
    );
    if (r.kind === "loaded") {
      const marks = tuningMarks(r.value.value, r.value.storedAt);
      return { kind: "loaded", value: { kind: "marks", marks } };
    }
    if (r.kind === "error") {
      const what = attribute.replaceAll("_", " ");
      return {
        kind: "loaded",
        value: { kind: "none", reason: `The screen on ${what} did not run: ${r.message}` },
      };
    }
    return r;
  }, [auth, assetId, type, attribute, stored]);
  const state = useLoad(auth, run).state;
  return state.kind === "loaded" ? state.value : { kind: "loading" };
}

function Plain({ children, testId }: { children: ReactNode; testId: string }) {
  return (
    <section className="sh-plain" data-testid={testId}>
      <Ready />
      <p>{children}</p>
    </section>
  );
}

export function SessionPage({ auth, route }: { auth: AuthStore; route: SessionRoute }) {
  const run = useCallback(async (): Promise<CatalogResult<Meta>> => {
    // The stored summary is asked for alongside the studies; its failure never blocks. The events
    // and the units come from its parts when it holds them for the file, else from the live routes.
    const summaryAsk = loadSummary(
      auth.config,
      route.dataset,
      route.subject,
      route.session,
      auth,
      doFetch,
      TIMEOUT_MS,
    );
    const studies = await loadStudies(auth.config, auth, doFetch, TIMEOUT_MS);
    if (studies.kind !== "loaded") return studies;
    let assets: NwbAsset[] | null;
    try {
      assets = sessionAssets(studies.value, route.dataset, route.subject, route.session);
    } catch (err) {
      return failure(
        `The studies listing sent a response this page cannot read (${err instanceof Error ? err.message : String(err)}). Tell your administrator.`,
      );
    }
    const asset = pickAsset(assets ?? []);
    if (asset === null || asset.n_units === 0)
      return { kind: "loaded", value: { kind: "absent", assets } };
    const summaryResult = await summaryAsk;
    if (summaryResult.kind === "error")
      console.warn("the stored summary could not be read; computing live", summaryResult.message);
    const summary = summaryResult.kind === "loaded" ? summaryResult.value : null;
    const [events, labels] = await Promise.all([
      loadStoredOrLive(
        chooseStoredEvents(summary, asset.asset_id),
        parseSessionEvents,
        () => loadEvents(auth.config, asset.asset_id, auth, doFetch, TIMEOUT_MS),
        doFetch,
        TIMEOUT_MS,
      ).then(plainValue),
      loadStoredOrLive(
        chooseStoredUnits(summary, asset.asset_id),
        storedUnitLabels,
        () => loadUnitLabels(auth.config, asset.asset_id, auth, doFetch),
        doFetch,
        TIMEOUT_MS,
      ).then(plainValue),
    ]);
    for (const r of [events, labels]) {
      if (r.kind === "ended") return r;
      if (r.kind === "error")
        return { kind: "loaded", value: { kind: "refused", asset, failure: r } };
    }
    if (events.kind !== "loaded" || labels.kind !== "loaded") return { kind: "ended" };
    return {
      kind: "loaded",
      value: {
        kind: "ready",
        asset,
        events: events.value,
        labels: labels.value,
        summary,
      },
    };
  }, [auth, route.dataset, route.subject, route.session]);
  const meta = useLoad(auth, run);
  const seconds = useElapsed(meta.state.kind === "loading");
  const session = route.session === NO_SESSION ? null : route.session;
  const subjectHref = dataSubjectPath(route.dataset, route.subject);
  const search = useSearch();
  const inspecting = layoutFromSearch(search) === "inspect";
  const crumbs = dataCrumbs({ dataset: route.dataset, subject: route.subject, session }).map((c) =>
    c.href === undefined ? { label: c.label } : { label: c.label, to: c.href },
  );
  const trail = inspecting ? [...crumbs, { label: "Inspect units" }] : crumbs;
  let content: ReactNode;
  const s = meta.state;
  if (s.kind === "loading") {
    content = (
      <Waiting
        testId="session-loading"
        what="Finding the session's file, its events and its units"
        seconds={seconds}
      />
    );
  } else if (s.kind === "error") {
    content = (
      <>
        <Ready />
        <Refusal state={s} fileHref={null} retry={meta.reload} />
      </>
    );
  } else if (s.kind === "ended") {
    content = null;
  } else if (s.value.kind === "refused") {
    content = (
      <>
        <Ready />
        <Refusal
          state={s.value.failure}
          fileHref={filePath(s.value.asset.asset_id)}
          retry={meta.reload}
        />
      </>
    );
  } else if (s.value.kind === "absent") {
    const zero = s.value.assets?.find((a) => a.n_units === 0);
    content = (
      <Plain testId="session-no-units">
        {s.value.assets === null
          ? "This session is not in the catalog's studies listing, so there is no heatmap to draw. "
          : zero === undefined
            ? "This session has no NWB file, so it has no units to draw. "
            : "This session's NWB file has no units, so there is no heatmap to draw. "}
        {zero === undefined ? (
          <a href={subjectHref}>Open the participant page</a>
        ) : (
          <a href={filePath(zero.asset_id)} data-testid="session-file-link">
            Open the file page
          </a>
        )}
        .
      </Plain>
    );
  } else {
    const v = s.value;
    content =
      pickableTypes(v.events.types).length === 0 ? (
        <Plain testId="session-no-events">
          {v.events.recipeDefault
            ? `${v.events.dataset} has no event recipe, and this file's tables hold no events, so there is nothing to align its ${v.labels.length} units to. `
            : `The recipe resolved no events for this file, so there is nothing to align its ${v.labels.length} units to. `}
          <a href={filePath(v.asset.asset_id)} data-testid="session-file-link">
            Open the file page
          </a>{" "}
          for its units, spikes and tables.
        </Plain>
      ) : inspecting ? (
        <InspectSlot
          auth={auth}
          route={route}
          asset={v.asset}
          types={pickableTypes(v.events.types)}
          labels={v.labels}
          eventType={alignFromSearch(search, pickableTypes(v.events.types))}
        />
      ) : (
        <Heat
          auth={auth}
          route={route}
          asset={v.asset}
          events={v.events}
          labels={v.labels}
          summary={v.summary}
        />
      );
  }
  return (
    <>
      <Spine trail={trail} />
      {content}
    </>
  );
}
