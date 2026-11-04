// The card every state draws: a small-capitals eyebrow, a serif title, a
// plain sentence, the next step as the primary button (others after it), and a footer that keeps the
// error code small for support and folds the technical detail away. Its styles (`states.css`) load
// from `main.tsx`, so the artboard and Playwright can import this module under node.
import type { ReactNode } from "react";

export interface StateCardProps {
  /** Which state: loading, refused, curator, ended, error, empty. */
  kind: string;
  eyebrow: string;
  title?: string;
  sentence?: ReactNode;
  /** The primary button first. */
  actions?: ReactNode | undefined;
  children?: ReactNode;
  /** The error code, shown small for support. */
  code?: string;
  /** The technical detail, folded under "Technical detail". */
  folded?: string;
  /** An error is announced (`role="alert"`); the rest are `role="status"`. */
  alert?: boolean;
  testId?: string | undefined;
}

export function StateCard({
  kind,
  eyebrow,
  title,
  sentence,
  actions,
  children,
  code,
  folded,
  alert = false,
  testId,
}: StateCardProps) {
  return (
    <section
      className="state-card"
      role={alert ? "alert" : "status"}
      data-ab="state"
      data-ab-state={kind}
      data-testid={testId}
    >
      <p className="state-eyebrow">{eyebrow}</p>
      {title === undefined ? null : <h2 className="state-title">{title}</h2>}
      {sentence === undefined ? null : <p className="state-sentence">{sentence}</p>}
      {children}
      {actions === undefined ? null : <div className="state-actions">{actions}</div>}
      {code === undefined && folded === undefined ? null : (
        <div className="state-foot">
          {folded === undefined ? null : (
            <details className="state-fold" data-ab="fold">
              <summary>Technical detail</summary>
              <code className="state-detail">{folded}</code>
            </details>
          )}
          {code === undefined ? null : (
            <code className="state-code" data-ab="code">
              code: {code}
            </code>
          )}
        </div>
      )}
    </section>
  );
}
