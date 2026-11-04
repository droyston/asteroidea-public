// "Get started" page: the short setup command, and what it does.
import { useCallback, useState } from "react";
import type { AuthStore } from "../auth/store";
import { Button } from "../kit/Button";
import { Notice } from "../kit/Notice";
import { useLoad } from "../useLoad";
import "./start.css";
import { type GetStarted, type GetStartedState, loadGetStarted } from "./start";

export interface GetStartedViewProps {
  state: GetStartedState;
  onReload: () => void;
}

export function GetStartedView({ state, onReload }: GetStartedViewProps) {
  switch (state.kind) {
    case "loading":
      return (
        <Notice kind="info" testId="start-loading">
          Loading your setup command…
        </Notice>
      );
    case "error":
      return (
        <Notice
          kind="error"
          testId="start-error"
          action={{ label: "Try again", onClick: onReload }}
        >
          {state.message}
        </Notice>
      );
    case "ended":
      return null;
    case "loaded":
      return <Loaded body={state.body} />;
  }
}

function CopyButton({ text }: { text: string }) {
  const [note, setNote] = useState<string | null>(null);
  const copy = () => {
    navigator.clipboard.writeText(text).then(
      () => setNote("Copied"),
      () => setNote("Could not copy: select the command and copy it by hand"),
    );
  };
  return (
    <div className="start-copy">
      <Button onClick={copy}>Copy</Button>
      <span role="status" className="start-copy-note">
        {note}
      </span>
    </div>
  );
}

function Loaded({ body }: { body: GetStarted }) {
  return (
    <div className="start-body">
      <section aria-label="Install">
        <h3>Install</h3>
        <p>Run this in a terminal:</p>
        <pre className="codeblock install" data-testid="start-install">
          <code>{body.install}</code>
        </pre>
        <CopyButton text={body.install} />
      </section>
      <section aria-label="What the command does">
        <h3>What the command does</h3>
        <ol className="start-steps" data-testid="start-steps">
          <li>Installs Python 3.14 privately with uv, in its own environment.</li>
          <li>Installs the SDK and the notebook packages there.</li>
          <li>Signs you in and downloads the notebook.</li>
          <li>
            Prints PASS or FAIL for each step; add <code>--pass2</code> to the command for the
            second check.
          </li>
        </ol>
      </section>
    </div>
  );
}

export function GetStartedPage({ auth }: { auth: AuthStore }) {
  const run = useCallback(
    () => loadGetStarted(auth.config, auth, (url, init) => fetch(url, init)),
    [auth],
  );
  const { state, reload: load } = useLoad(auth, run);

  return (
    <section className="start start-setup" data-testid="start-page">
      <h2>Get started</h2>
      <GetStartedView state={state} onReload={load} />
    </section>
  );
}
