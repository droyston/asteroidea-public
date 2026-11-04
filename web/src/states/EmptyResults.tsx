// The empty-results state: what a result looks like and the one SDK call
// that makes one. Results come only from the Python analysis module; the page shows the call, it never
// makes a result itself.
import { Button } from "../kit/Button";
import { copyWithToast } from "../kit/Toast";
import { StateCard } from "./StateCard";

/** The one SDK call that saves a result (`sdk/src/asteroidea/__init__.py :: save_artifact`). */
export const SAVE_CALL = `asteroidea.save_artifact(
    "rates.csv", sources=ref.files, snapshot_id=ref.snapshot_id)`;

export interface EmptyResultsProps {
  /** Whose results are empty: the reader's own, or everyone's. */
  scope?: "mine" | "all";
  /** The example's attachment line ("CS62 · ses-1 · you"). */
  example?: string;
  testId?: string;
}

export function EmptyResults({
  scope = "mine",
  example = "your data · you",
  testId = "results-empty",
}: EmptyResultsProps) {
  return (
    <StateCard
      kind="empty"
      eyebrow={scope === "mine" ? "Empty · my results" : "Empty · everyone's results"}
      title="No results yet"
      sentence={`${scope === "mine" ? "" : "No researcher has finished a result yet. "}A result is a file your notebook saves with what it used. It will look like this, attached to its data:`}
      actions={
        <>
          <Button variant="primary" onClick={() => void copyWithToast(SAVE_CALL, "the save call")}>
            Copy the save call
          </Button>
          <a className="state-link" href="/start">
            Set up the notebook →
          </a>
        </>
      }
      testId={testId}
    >
      <div className="state-example" data-ab="example">
        <span className="state-example-name">your-result.csv</span>
        <span className="state-example-meta">{example}</span>
      </div>
      <pre className="state-call" data-ab="call">
        <code>{SAVE_CALL}</code>
      </pre>
    </StateCard>
  );
}
