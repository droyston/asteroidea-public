// The session-ended state: a Dialog over the dimmed page, not a blank
// one. Sign in goes through managed login and comes back to the same URL, so the view's state (study,
// session, alignment, window: all in the URL) is kept; the tray is in `localStorage` under the user's
// `sub`, so it is kept too. Esc does not close it: the page behind cannot load anything until the
// reader signs in again.
import { useSyncExternalStore } from "react";
import { Button } from "../kit/Button";
import { Dialog } from "../kit/Dialog";
import { spineSlot } from "../shell/marks";

/** Where the reader comes back to: the spine trail ("CS62 · ses-1"), else the page's path. */
export function returnLabel(trail: readonly { label: string }[] | null, path: string): string {
  const labels = (trail ?? []).map((c) => c.label).filter((l) => l !== "" && l !== "Studies");
  if (labels.length > 0) return labels.join(" · ");
  return path === "/" ? "the Studies list" : path;
}

export function endedSentence(where: string): string {
  return `You'll come back to ${where} with the same view. Your tray is kept.`;
}

export interface SessionEndedProps {
  open: boolean;
  onSignIn: () => void;
  /** The page's path, when the view publishes no spine. */
  path: string;
}

export function SessionEnded({ open, onSignIn, path }: SessionEndedProps) {
  const trail = useSyncExternalStore(spineSlot.subscribe, spineSlot.get, spineSlot.get);
  return (
    <Dialog
      open={open}
      eyebrow="Session ended"
      title="Sign in again to carry on"
      onClose={() => undefined}
      testId="session-ended"
    >
      <p className="dialog-sentence">{endedSentence(returnLabel(trail, path))}</p>
      <div className="dialog-actions dialog-actions-start">
        <Button variant="primary" onClick={onSignIn} data-autofocus>
          Sign in
        </Button>
      </div>
    </Dialog>
  );
}
