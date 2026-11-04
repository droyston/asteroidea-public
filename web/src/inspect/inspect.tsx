// The flexible inspection page (the lazy `inspect` chunk), shown at `?view=inspect` on the session
// page: one `/compute/rates?...&raster=1` request at the display window for the chosen event type,
// then every control (normalisation, windows, bin, scales, axes) recomputes in the browser and is
// kept in the URL; none sends a request. It marks the view ready once the rates have drawn or a
// refusal stands in their place.
import { type ReactNode, useCallback, useMemo } from "react";
import { TIMEOUT_MS } from "../auth/apiGet";
import type { AuthStore } from "../auth/store";
import { get } from "../catalog/catalog";
import { doFetch } from "../catalog/loadable";
import { Notice } from "../kit/Notice";
import { useSearch } from "../research/shared";
import { appRouter } from "../router/router";
import { POST_S, PRE_S, parseSessionRates, ratesPath, type UnitLabel } from "../session/model";
import { Ready } from "../shell/marks";
import { useLoad } from "../useLoad";
import { InspectView } from "./InspectView";
import { defaultState, type InspectState, inspect, inspectSearch, parseInspect } from "./model";
import "./inspect.css";

export interface InspectPageProps {
  readonly auth: AuthStore;
  readonly assetId: string;
  readonly eventType: string;
  readonly eventName: string;
  readonly units: string;
  readonly labels: readonly UnitLabel[];
  readonly subject: string;
  readonly session: string;
  /** The session page's path (no query). */
  readonly base: string;
}

const WINDOW = { pre: PRE_S, post: POST_S };

export function InspectPage(p: InspectPageProps): ReactNode {
  const { auth, assetId, eventType, units } = p;
  const run = useCallback(
    () =>
      get(
        auth.config,
        ratesPath(assetId, eventType, units, true),
        auth,
        doFetch,
        TIMEOUT_MS,
        parseSessionRates,
      ),
    [auth, assetId, eventType, units],
  );
  const rates = useLoad(auth, run);
  const search = useSearch();
  const where = useMemo(() => new Map(p.labels.map((l) => [l.unit_index, l.location])), [p.labels]);
  const loaded = rates.state.kind === "loaded" ? rates.state.value : null;
  const defaults = useMemo(
    () => defaultState(WINDOW, loaded?.defaults.responseWindow ?? [0, 1]),
    [loaded],
  );
  const state = useMemo(() => parseInspect(search, defaults, WINDOW), [search, defaults]);
  const data = useMemo(
    () => (loaded === null ? null : inspect(loaded.rates, where, WINDOW, state)),
    [loaded, where, state],
  );
  const onChange = (next: InspectState) =>
    appRouter().navigate(`${p.base}${inspectSearch(search, next, defaults)}`, { replace: true });
  if (rates.state.kind === "error") {
    return (
      <>
        <Ready />
        <Notice
          kind="error"
          testId="inspect-refusal"
          action={{ label: "Try again", onClick: rates.reload }}
        >
          The rates with per-trial spikes could not load: {rates.state.message}
        </Notice>
      </>
    );
  }
  if (loaded === null || data === null) {
    return (
      <Notice kind="info" testId="inspect-loading">
        Loading every unit's spikes around the {p.eventName} events…
      </Notice>
    );
  }
  return (
    <>
      <Ready />
      <InspectView
        subject={p.subject}
        session={p.session}
        eventName={p.eventName}
        nTrials={loaded.rates.n_trials}
        window={WINDOW}
        state={state}
        defaults={defaults}
        data={data}
        heatmapHref={p.base}
        onChange={onChange}
      />
    </>
  );
}
