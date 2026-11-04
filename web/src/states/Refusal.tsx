// A refused or failed request: the browser sentence for its code
// (`codes.ts`), the next step as the primary button, the code kept small for support. A `slow` or
// `too_large` refusal offers the same request cut down to fit (`smaller.ts`) and, when the caller has
// one, the notebook code that runs the full request. A 403 for the curators group is the curator-only
// state. A platform failure folds its technical detail away.
import type { ReactElement } from "react";
import { Button } from "../kit/Button";
import { copyWithToast } from "../kit/Toast";
import { CuratorOnly } from "./CuratorOnly";
import { type CodeCopy, copyFor } from "./codes";
import { StateCard } from "./StateCard";
import { type Shrinkable, type Smaller, smaller } from "./smaller";

export interface RefusalProps {
  status: number | null;
  code: string | null;
  /** The server's sentence (or the client's own failure text). */
  detail: string | null;
  /** What was being loaded, for the sentence ("the studies list"). */
  noun?: string;
  /** The request path, for the support details. */
  path?: string;
  onRetry?: () => void;
  /** The request's shrinkable parts and how to ask again with the smaller one. */
  request?: Shrinkable;
  onSmaller?: (next: Smaller) => void;
  /** The SDK code that runs the full request in a notebook. */
  notebookCode?: string;
  onSignIn?: () => void;
  testId?: string;
}

/** The text "Copy details for support" puts on the clipboard. */
export function supportText(p: Pick<RefusalProps, "status" | "code" | "detail" | "path">): string {
  const c = copyFor(p);
  return [
    `code: ${c.code}`,
    `status: ${p.status ?? "no answer"}`,
    p.path === undefined ? null : `request: ${p.path}`,
    p.detail === null ? null : `detail: ${p.detail}`,
    `page: ${typeof window === "undefined" ? "" : window.location.href}`,
    `time: ${new Date().toISOString()}`,
  ]
    .filter((s) => s !== null)
    .join("\n");
}

/** True for a 403 that names the curators group: the curator-only state. */
export function isCuratorRefusal(status: number | null, detail: string | null): boolean {
  return status === 403 && detail !== null && /curators/i.test(detail);
}

/** The sentence with "{noun}" filled in. */
function sentenceOf(copy: CodeCopy, noun: string | undefined): string {
  return copy.sentence.replace("this just now", `${noun ?? "this"} just now`);
}

export function Refusal(p: RefusalProps) {
  if (isCuratorRefusal(p.status, p.detail)) return <CuratorOnly testId={p.testId} />;
  const copy = copyFor(p);
  const support = (
    <Button
      key="support"
      variant="secondary"
      onClick={() => void copyWithToast(supportText(p), "details for support")}
    >
      Copy details for support
    </Button>
  );
  const notebook =
    p.notebookCode === undefined ? null : (
      <Button
        key="notebook"
        onClick={() => void copyWithToast(p.notebookCode ?? "", "notebook code")}
      >
        Copy notebook code
      </Button>
    );
  const retry =
    p.onRetry === undefined ? null : (
      <Button key="retry" variant="primary" onClick={p.onRetry}>
        Try again
      </Button>
    );
  const cut = p.request === undefined ? null : smaller(p.request, copy.code, p.detail);
  let actions: (ReactElement | null)[];
  switch (copy.next) {
    case "smaller":
      if (cut !== null && p.onSmaller !== undefined) {
        const go = p.onSmaller;
        actions = [
          <Button key="smaller" variant="primary" onClick={() => go(cut)}>
            {cut.label}
          </Button>,
          notebook,
        ];
      } else if (copy.code === "slow") {
        // Nothing to cut: the work may fit on a second try.
        actions = [retry, notebook, support];
      } else {
        actions = [notebook, support];
      }
      break;
    case "retry":
      actions = [retry, support];
      break;
    case "sign-in":
      actions = [
        p.onSignIn === undefined ? null : (
          <Button key="sign-in" variant="primary" onClick={p.onSignIn}>
            Sign in
          </Button>
        ),
      ];
      break;
    case "studies":
      actions = [
        <a key="studies" className="btn btn-primary" href="/">
          Back to Studies
        </a>,
        support,
      ];
      break;
    default:
      actions = [support];
  }
  const shown = actions.filter((a) => a !== null);
  const sentence = sentenceOf(copy, p.noun);
  return (
    <StateCard
      kind={copy.next === "smaller" ? "refused" : "error"}
      eyebrow={copy.eyebrow}
      title={copy.title}
      sentence={sentence}
      actions={shown.length === 0 ? undefined : shown}
      code={copy.code}
      {...(copy.detail === "fold" && p.detail !== null ? { folded: p.detail } : {})}
      alert
      testId={p.testId}
    >
      {copy.detail === "inline" && p.detail !== null ? (
        <p className="state-server">{p.detail}</p>
      ) : null}
    </StateCard>
  );
}
