// Header control: "Sign in" when signed out, "Sign out" when signed in. Rendered only when the build
// carries auth configuration (see config.ts).
import { useSyncExternalStore } from "react";
import { Button } from "../kit/Button";
import { Notice } from "../kit/Notice";
import type { AuthStore } from "./store";

export function AuthControl({ auth }: { auth: AuthStore }) {
  const state = useSyncExternalStore(auth.subscribe, auth.getSnapshot, auth.getSnapshot);
  switch (state.kind) {
    case "working":
      return <Notice kind="info">Signing in…</Notice>;
    case "signed-in":
      return (
        <>
          <span>Signed in</span>
          <Button onClick={() => auth.signOut()}>Sign out</Button>
        </>
      );
    case "ended":
      // The session-ended dialog carries Sign in.
      return null;
    case "signed-out":
      return (
        <>
          {state.notice === null ? null : (
            <Notice kind="error" testId="auth-notice">
              {state.notice}
            </Notice>
          )}
          <Button variant="primary" onClick={() => void auth.signIn()}>
            Sign in
          </Button>
        </>
      );
  }
}
