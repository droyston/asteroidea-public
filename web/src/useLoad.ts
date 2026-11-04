// The load state shared by the Uploads panel, Get started and the Catalog view: run `run` on mount
// and whenever it changes (callers memoise it), show "loading" until it settles, drop a result that
// arrives after unmount or after a newer run, and expire the sign-in when the session has ended.
import { useCallback, useEffect, useRef, useState } from "react";
import type { AuthStore } from "./auth/store";

export function useLoad<R extends { kind: string }>(
  auth: AuthStore,
  run: () => Promise<R>,
): { state: { kind: "loading" } | R; reload: () => void } {
  const [state, setState] = useState<{ kind: "loading" } | R>({ kind: "loading" });
  const ticket = useRef(0);
  const reload = useCallback(() => {
    ticket.current += 1;
    const mine = ticket.current;
    setState({ kind: "loading" });
    void run().then((result) => {
      if (mine !== ticket.current) return;
      if (result.kind === "ended") auth.expire();
      else setState(result);
    });
  }, [auth, run]);
  useEffect(() => {
    reload();
    return () => {
      ticket.current += 1;
    };
  }, [reload]);
  return { state, reload };
}
