// The curator-only state: the work is a curator's, so the page says what
// the reader can still do and how a curator takes it from there. No names: the curator directory is
// not built, so the state says "ask a curator" and stops there.
import { Button } from "../kit/Button";
import { StateCard } from "./StateCard";

export interface CuratorOnlyProps {
  /** How many datasets the reader has picked; absent when the state is not about picks. */
  picks?: number;
  /** Export the pick list for a curator; absent when there is nothing to export. */
  onExport?: () => void;
  testId?: string | undefined;
}

export const CURATOR_CODE = "forbidden";

export function curatorSentence(picks: number | undefined): string {
  if (picks === undefined || picks === 0) {
    return "You can keep finding and picking datasets. Ask a curator to import them; a curator can import an exported pick list in one step.";
  }
  const what = `${picks} pick${picks === 1 ? "" : "s"}`;
  return `You can keep finding and picking datasets. Export your ${what} and send them to a curator; they can import the list in one step.`;
}

export function CuratorOnly({ picks, onExport, testId }: CuratorOnlyProps) {
  return (
    <StateCard
      kind="curator"
      eyebrow="Refused · curators only"
      title="Imports are started by curators"
      sentence={curatorSentence(picks)}
      actions={
        onExport === undefined ? (
          <a className="btn btn-primary" href="/discover">
            Back to Find
          </a>
        ) : (
          <Button variant="primary" onClick={onExport}>
            Export pick list
          </Button>
        )
      }
      code={CURATOR_CODE}
      alert
      testId={testId}
    />
  );
}
