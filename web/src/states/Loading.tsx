// The loading state: what is being fetched, a bar that fills against the
// route's usual time (`expected.ts`), the elapsed time against that usual time, and the limit after
// which the page stops and says why. The testid ends in `-loading`, so `Settled` waits for it.
import { useEffect, useState } from "react";
import { TIMEOUT_MS } from "../auth/apiGet";
import { expectedFor, progress, seconds } from "./expected";
import { StateCard } from "./StateCard";

export interface LoadingProps {
  /** The request path; it picks the usual time. */
  path: string;
  /** "Loading studies", "Computing responses for 44 units". */
  what: string;
  /** What is already on the page and what fills in when this arrives. */
  meanwhile?: string;
  /** Start the clock here (the artboard draws a mid-flight bar). */
  initialElapsedMs?: number;
  testId?: string;
}

/** The sentence under the bar. */
export function loadingSentence(elapsedMs: number, path: string, meanwhile?: string): string {
  const usual = expectedFor(path).ms;
  const head = `${seconds(elapsedMs)} so far · usually ${seconds(usual)}.`;
  const tail = `If it passes ${seconds(TIMEOUT_MS)} we stop and say why.`;
  return [head, meanwhile, tail].filter((s) => s !== undefined && s !== "").join(" ");
}

export function Loading({
  path,
  what,
  meanwhile,
  initialElapsedMs = 0,
  testId = "catalog-loading",
}: LoadingProps) {
  const [elapsed, setElapsed] = useState(initialElapsedMs);
  useEffect(() => {
    const start = performance.now() - initialElapsedMs;
    const timer = window.setInterval(() => setElapsed(performance.now() - start), 200);
    return () => window.clearInterval(timer);
  }, [initialElapsedMs]);
  const usual = expectedFor(path);
  const fill = progress(elapsed, usual.ms);
  return (
    <StateCard kind="loading" eyebrow="Loading" testId={testId}>
      <div className="state-skeleton" aria-hidden="true">
        <span />
        <span />
        <span />
      </div>
      <p className="state-what">{what}…</p>
      <div
        className="state-bar"
        role="progressbar"
        aria-label={what}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={Math.round(fill * 100)}
        data-ab="progress"
      >
        <span style={{ width: `${(fill * 100).toFixed(1)}%` }} />
      </div>
      <p className="state-sentence state-small">{loadingSentence(elapsed, path, meanwhile)}</p>
    </StateCard>
  );
}
