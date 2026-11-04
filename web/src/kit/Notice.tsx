// Notice: info, error or success, with an optional action button. An error is role="alert";
// the others are role="status". Replaces the ad hoc `status error` classes.
import type { ReactNode } from "react";
import { Button } from "./Button";

export type NoticeKind = "info" | "error" | "success";

export interface NoticeProps {
  kind: NoticeKind;
  children?: ReactNode;
  action?: { label: string; onClick: () => void };
  testId?: string;
}

export function Notice({ kind, children, action, testId }: NoticeProps) {
  return (
    <div
      className={`notice notice-${kind}`}
      role={kind === "error" ? "alert" : "status"}
      data-testid={testId}
    >
      <span className="notice-body">{children}</span>
      {action === undefined ? null : <Button onClick={action.onClick}>{action.label}</Button>}
    </div>
  );
}
