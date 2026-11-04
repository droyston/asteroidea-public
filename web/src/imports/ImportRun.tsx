// The curator's half of the picks rail, the lazy `imports` chunk: the
// picks become imports (`POST /imports`, one per picked dataset, the files picked under it as the
// subset, none for the whole dataset; the 50-file cap stops a run here with a sentence), the run as a
// 10 × 5 grid of squares (one per file, state by colour token and by a text label), a plain sentence
// per failed file, and "Open the new study" when the run lands. On the Imports tab with no run open,
// the curator's last runs. The run polls every 5 s until DONE, REFUSED or FAILED.
import { useCallback, useEffect, useRef, useState } from "react";
import { TIMEOUT_MS } from "../auth/apiGet";
import type { AuthStore } from "../auth/store";
import { get } from "../catalog/catalog";
import { Button } from "../kit/Button";
import { Notice } from "../kit/Notice";
import { appRouter } from "../router/router";
import { type PickGroup, pickCount } from "../targets/groups";
import type { Target } from "../targets/schema";
import { useLoad } from "../useLoad";
import {
  failureSentence,
  isTerminal,
  landingFor,
  loadRun,
  loadRuns,
  MAX_RUN_FILES,
  POLL_MS,
  parseStudyDatasets,
  pickedImports,
  type Run,
  runProgress,
  SQUARE_LABEL,
  type SquareState,
  squareState,
  startImport,
} from "./imports";

const fetchFn = (url: string, init: RequestInit) => fetch(url, init);

export const RUN_COLUMNS = 10;
const RECENT = 5;

export interface CuratorRailProps {
  auth: AuthStore;
  targets: readonly Target[];
  /** A picked dataset's file count from the index; null when unknown. */
  fileCount: (g: PickGroup) => number | null;
  /** The run open in the rail (`/discover/imports/:run_id`), or null. */
  runId: string | null;
  /** The Imports tab with no run open: list the curator's last runs. */
  recent: boolean;
}

export function runPath(runId: string): string {
  return `/discover/imports/${encodeURIComponent(runId)}`;
}

export function CuratorRail({ auth, targets, fileCount, runId, recent }: CuratorRailProps) {
  return (
    <>
      <ImportPicked auth={auth} targets={targets} fileCount={fileCount} />
      {runId === null ? null : <RunPanel key={runId} auth={auth} runId={runId} />}
      {runId === null && recent ? <RecentRuns auth={auth} /> : null}
    </>
  );
}

function ImportPicked({
  auth,
  targets,
  fileCount,
}: {
  auth: AuthStore;
  targets: readonly Target[];
  fileCount: (g: PickGroup) => number | null;
}) {
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const [started, setStarted] = useState(0);
  const { requests, problem: blocked } = pickedImports(targets, fileCount);
  const n = pickCount(targets);

  const send = async () => {
    setProblem(null);
    setBusy(true);
    const ids: string[] = [];
    for (const request of requests) {
      const result = await startImport(auth.config, request, auth, fetchFn);
      if (result.kind === "ended") {
        auth.expire();
        return;
      }
      if (result.kind === "error") {
        const started = ids.length === 0 ? "" : `${ids.length} import(s) started before this one. `;
        setProblem(`${started}${request.source}: ${result.message}`);
        setBusy(false);
        return;
      }
      ids.push(result.run_id);
    }
    setBusy(false);
    setStarted(ids.length);
    const [first] = ids;
    if (first !== undefined) appRouter().navigate(`${runPath(first)}${window.location.search}`);
  };

  return (
    <div className="find-import-start">
      <Button
        variant="primary"
        onClick={() => void send()}
        disabled={busy || targets.length === 0 || blocked !== null}
      >
        {busy
          ? "Starting…"
          : `Import ${n === 1 ? "1 pick" : `${n} picks`} · ≤ ${MAX_RUN_FILES} files per run`}
      </Button>
      {blocked === null ? null : (
        <p className="find-import-cap" data-testid="import-cap">
          {blocked}
        </p>
      )}
      {started > 1 ? (
        <Notice kind="info" testId="import-started">
          {started} imports started, one per dataset; the first is open below and the others are
          under Imports.
        </Notice>
      ) : null}
      {problem === null ? null : (
        <Notice kind="error" testId="import-picked-problem">
          {problem}
        </Notice>
      )}
    </div>
  );
}

function RunPanel({ auth, runId }: { auth: AuthStore; runId: string }) {
  const [run, setRun] = useState<Run | null>(null);
  const [error, setError] = useState<string | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const poll = useCallback(async () => {
    const result = await loadRun(auth.config, runId, auth, fetchFn);
    if (result.kind === "ended") {
      auth.expire();
      return false;
    }
    if (result.kind === "error") {
      setError(result.message);
      return true; // a transient failure keeps polling
    }
    setError(null);
    setRun(result.run);
    return !isTerminal(result.run.state);
  }, [auth, runId]);

  useEffect(() => {
    let live = true;
    const tick = async () => {
      const again = await poll();
      if (live && again) timer.current = setTimeout(() => void tick(), POLL_MS);
    };
    void tick();
    return () => {
      live = false;
      if (timer.current !== null) clearTimeout(timer.current);
    };
  }, [poll]);

  return (
    <section className="find-run" aria-label="Import run" data-testid="import-run">
      <p className="find-label">
        Your import · <span data-testid="import-run-state">{run?.state ?? "loading"}</span>
        <code className="find-run-id">run {runId.slice(0, 8)}</code>
      </p>
      {error === null ? null : (
        <Notice kind="error" testId="import-run-error">
          {error}
        </Notice>
      )}
      {run === null && error === null ? (
        <Notice kind="info" testId="import-run-loading">
          Loading the run…
        </Notice>
      ) : null}
      {run === null ? null : <RunBody auth={auth} run={run} />}
    </section>
  );
}

const ORDER: readonly SquareState[] = ["imported", "already", "failed", "waiting"];

function RunBody({ auth, run }: { auth: AuthStore; run: Run }) {
  const { done, total } = runProgress(run);
  const states = run.files.map(squareState);
  const failures = run.files.map(failureSentence).filter((s): s is string => s !== null);
  return (
    <>
      <p className="find-run-source">
        {run.source}
        {run.dataset === undefined ? "" : ` → ${run.dataset}`}
      </p>
      {total > 0 ? (
        <p className="find-run-progress" data-testid="import-run-progress">
          {done} of {total} files done
          {isTerminal(run.state) ? "" : ` · refreshes every ${POLL_MS / 1000} s`}
        </p>
      ) : null}
      {run.state === "REFUSED" || run.refusal !== undefined ? (
        <Notice kind="error" testId="import-refusal">
          {run.refusal ?? "The import was refused."}
        </Notice>
      ) : null}
      {total > 0 ? (
        <>
          <ol
            className="find-run-grid"
            data-testid="import-grid"
            aria-label={`${total} files, ${RUN_COLUMNS} to a row`}
          >
            {run.files.map((f, i) => {
              const s = states[i] ?? "waiting";
              return (
                <li
                  key={f.relpath}
                  className={`find-square find-square-${s}`}
                  data-state={s}
                  title={`${f.relpath}: ${SQUARE_LABEL[s]}`}
                  aria-label={`${f.relpath}: ${SQUARE_LABEL[s]}`}
                />
              );
            })}
          </ol>
          <ul className="find-run-legend" data-testid="import-legend">
            {ORDER.map((s) => (
              <li key={s}>
                <span className={`find-square find-square-${s}`} aria-hidden="true" />
                {SQUARE_LABEL[s]} {states.filter((x) => x === s).length}
              </li>
            ))}
          </ul>
        </>
      ) : null}
      {failures.length === 0 ? null : (
        <ul className="find-run-failures" data-testid="import-failures">
          {failures.map((s) => (
            <li key={s}>{s}</li>
          ))}
        </ul>
      )}
      {run.state === "DONE" && run.dataset !== undefined ? (
        <Landing auth={auth} dataset={run.dataset} />
      ) : null}
    </>
  );
}

function Landing({ auth, dataset }: { auth: AuthStore; dataset: string }) {
  const read = useCallback(
    () => get(auth.config, "/catalog/studies", auth, fetchFn, TIMEOUT_MS, parseStudyDatasets),
    [auth],
  );
  const { state } = useLoad(auth, read);
  // Until the studies arrive (or when they fail) the link goes to the dataset page.
  const studies = state.kind === "loaded" ? state.value : [];
  const { href, title } = landingFor(dataset, studies);
  return (
    <p className="find-run-landing" data-testid="import-done">
      Import finished.{" "}
      <a href={href} data-testid="open-new-study">
        Open the new study{title === null ? "" : ` “${title}”`} →
      </a>
    </p>
  );
}

function RecentRuns({ auth }: { auth: AuthStore }) {
  const read = useCallback(() => loadRuns(auth.config, auth, fetchFn), [auth]);
  const { state, reload } = useLoad(auth, read);
  return (
    <section className="find-run" aria-label="Recent imports" data-testid="imports-recent">
      <p className="find-label">Your recent imports</p>
      {state.kind === "loading" ? (
        <Notice kind="info" testId="imports-loading">
          Loading your imports…
        </Notice>
      ) : null}
      {state.kind === "error" ? (
        <Notice
          kind="error"
          testId="imports-error"
          action={{ label: "Try again", onClick: reload }}
        >
          {state.message}
        </Notice>
      ) : null}
      {state.kind === "loaded" ? (
        state.runs.length === 0 ? (
          <p className="find-picks-empty">No imports yet.</p>
        ) : (
          <ol className="find-recent">
            {state.runs.slice(0, RECENT).map((r) => (
              <li key={r.run_id}>
                <a href={runPath(r.run_id)}>{r.source}</a> <small>{r.state}</small>
                {r.refusal === undefined ? null : <small> · {r.refusal}</small>}
              </li>
            ))}
            {state.runs.length > RECENT ? (
              <li>
                <small>{state.runs.length - RECENT} older not shown</small>
              </li>
            ) : null}
          </ol>
        )
      ) : null}
    </section>
  );
}
