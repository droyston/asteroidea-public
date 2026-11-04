// The home's Latest results card, in its own lazy chunk: one `GET /artifacts?scope=all`,
// the newest run as one link to its first figure, labelled as the Results place's Runs box labels it.
// It loads after the home has drawn and never holds the home's ready mark; it shows its own loading,
// error (with the HTTP status) and empty states. It names no subject: that would need a further
// `GET /catalog/assets?ids=`.
import { useEffect, useState } from "react";
import type { FetchFn, TokenSource } from "../auth/apiGet";
import { TIMEOUT_MS } from "../auth/apiGet";
import type { AuthConfig } from "../auth/config";
import { get } from "../catalog/catalog";
import { doFetch } from "../catalog/loadable";
import { parseArtifacts } from "../results/rows";
import { type RunRow, runDetail, runsModel } from "../results/runs";

export type LatestState =
  | { kind: "loading" }
  | { kind: "loaded"; run: RunRow | null }
  | { kind: "error"; message: string };

/** The newest run of the All listing; an error names the HTTP status, or says the API was not reached. */
export async function loadLatestRun(
  config: Pick<AuthConfig, "apiUrl">,
  tokens: TokenSource,
  fetchFn: FetchFn,
  me: string | null,
  timeoutMs: number = TIMEOUT_MS,
): Promise<Exclude<LatestState, { kind: "loading" }>> {
  const listed = await get(
    config,
    "/artifacts?scope=all",
    tokens,
    fetchFn,
    timeoutMs,
    parseArtifacts,
  );
  if (listed.kind === "loaded") {
    return { kind: "loaded", run: runsModel(listed.value, new Map(), me, 1)[0] ?? null };
  }
  if (listed.kind === "ended") return { kind: "error", message: "the session has ended" };
  const status = listed.status ?? null;
  return {
    kind: "error",
    message:
      status === null
        ? "the platform API was not reached"
        : `the platform API answered HTTP ${status}`,
  };
}

/** The card for one state: a link to the newest run, or a line saying why there is none. */
export function LatestCard({ state }: { state: LatestState }) {
  let body = null;
  if (state.kind === "loading") {
    body = <p className="home-latest-status">Listing the latest results…</p>;
  } else if (state.kind === "error") {
    body = (
      <p className="home-latest-status" data-testid="home-latest-error">
        The latest results could not be listed: {state.message}.
      </p>
    );
  } else if (state.run === null) {
    body = <p className="home-latest-status">No saved runs yet.</p>;
  } else {
    body = (
      <a href={state.run.href} className="home-latest-link" data-testid="home-latest-link">
        <strong>{state.run.analysis}</strong>
        <span>{runDetail(state.run)}</span>
      </a>
    );
  }
  return (
    <section
      className="home-latest"
      aria-label="Latest results"
      aria-busy={state.kind === "loading"}
      data-testid="home-latest"
      data-state={state.kind}
    >
      <small className="home-label">Latest results</small>
      {body}
    </section>
  );
}

/** Load once on mount and draw the card; the home never waits for it. */
export function LatestResults({ tokens, config, me }: LatestProps) {
  const [state, setState] = useState<LatestState>({ kind: "loading" });
  useEffect(() => {
    let live = true;
    void loadLatestRun(config, tokens, doFetch, me).then((s) => {
      if (live) setState(s);
    });
    return () => {
      live = false;
    };
  }, [config, tokens, me]);
  return <LatestCard state={state} />;
}

export interface LatestProps {
  tokens: TokenSource;
  config: Pick<AuthConfig, "apiUrl">;
  me: string | null;
}
