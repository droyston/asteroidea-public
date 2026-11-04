// Shell: the top bar (title, nav, status slot, auth slot), an optional spine
// row under it, and the main region. `ready` names the routed view once its data has drawn and is
// published as `data-ready` on `<main>`. The theme toggle sits before the auth slot, so it
// shows with sign-in off. The note slot sits before the theme toggle; the app fills it only
// when signed in.
import type { ReactNode } from "react";
import { ThemeToggle } from "../shell/ThemeToggle";

export interface ShellProps {
  title: string;
  status?: ReactNode;
  auth?: ReactNode;
  nav?: ReactNode;
  note?: ReactNode;
  spine?: ReactNode;
  ready?: string | undefined;
  children?: ReactNode;
}

export function Shell({ title, status, auth, nav, note, spine, ready, children }: ShellProps) {
  return (
    <div className="shell">
      <header className="shell-header">
        <h1>{title}</h1>
        {nav}
        {status === undefined ? null : <div className="shell-status">{status}</div>}
        {note}
        <ThemeToggle />
        {auth === undefined ? null : <div className="shell-auth">{auth}</div>}
      </header>
      {spine}
      <main className="shell-main" data-ready={ready}>
        {children}
      </main>
    </div>
  );
}
