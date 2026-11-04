// The top-bar Note control: a button that opens a small dialog with a textarea,
// Send and Cancel. Send posts the text with the page address and route state; the dialog then says
// "Sent" or shows the server's sentence. The typed text stays on an error and on Cancel; only a sent
// note clears it.
import { useState } from "react";
import { doFetch } from "../catalog/loadable";
import { Button } from "../kit/Button";
import { Dialog } from "../kit/Dialog";
import { Notice } from "../kit/Notice";
import { idFirst, sendable, sendNote, TEXT_MAX, viewOf } from "./notes";

interface NoteAuth {
  config: { apiUrl: string };
  getIdToken(): string | null;
  getAccessToken(): string | null;
  refresh(): Promise<boolean>;
}

type Outcome =
  | { kind: "idle" }
  | { kind: "sending" }
  | { kind: "sent" }
  | { kind: "error"; message: string };

export function NoteButton({ auth }: { auth: NoteAuth }) {
  const [open, setOpen] = useState(false);
  const [text, setText] = useState("");
  const [outcome, setOutcome] = useState<Outcome>({ kind: "idle" });

  const close = () => {
    setOpen(false);
    if (outcome.kind !== "sending") setOutcome({ kind: "idle" });
  };

  const send = async () => {
    setOutcome({ kind: "sending" });
    const body = { text, url: window.location.href, view: viewOf(window.location) };
    const r = await sendNote(auth.config, body, idFirst(auth), doFetch);
    if (r.kind === "loaded") {
      setText("");
      setOutcome({ kind: "sent" });
    } else if (r.kind === "error") {
      setOutcome({ kind: "error", message: r.message });
    } else {
      setOutcome({
        kind: "error",
        message: "Your session ended. Sign in again, then send the note.",
      });
    }
  };

  return (
    <>
      <Button
        variant="quiet"
        className="note-open"
        data-testid="note-open"
        onClick={() => {
          setOutcome({ kind: "idle" });
          setOpen(true);
        }}
      >
        Note
      </Button>
      <Dialog open={open} title="Leave a note" onClose={close} testId="note-dialog">
        <form
          className="note-form"
          onSubmit={(event) => {
            event.preventDefault();
            if (sendable(text) && outcome.kind !== "sending") void send();
          }}
        >
          <label className="note-label" htmlFor="note-text">
            What should change on this page? The page address and view go with it.
          </label>
          <textarea
            id="note-text"
            className="note-text"
            data-testid="note-text"
            data-autofocus
            rows={6}
            maxLength={TEXT_MAX}
            value={text}
            onChange={(event) => setText(event.target.value)}
          />
          {outcome.kind === "sent" ? (
            <Notice kind="success" testId="note-outcome">
              Sent
            </Notice>
          ) : null}
          {outcome.kind === "error" ? (
            <Notice kind="error" testId="note-outcome">
              {outcome.message}
            </Notice>
          ) : null}
          <div className="dialog-actions">
            <Button
              variant="primary"
              type="submit"
              data-testid="note-send"
              disabled={!sendable(text) || outcome.kind === "sending"}
            >
              {outcome.kind === "sending" ? "Sending…" : "Send"}
            </Button>
            <Button onClick={close} data-testid="note-cancel">
              {outcome.kind === "sent" ? "Close" : "Cancel"}
            </Button>
          </div>
        </form>
      </Dialog>
    </>
  );
}
