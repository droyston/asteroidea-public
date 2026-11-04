import { useEffect, useState, useSyncExternalStore } from "react";
import { AuthControl } from "./auth/AuthControl";
import { isCurator } from "./auth/groups";
import type { AuthStore } from "./auth/store";
import { FindLoader } from "./find/FindLoader";
import { type LoadState, loadIndex } from "./index/load";
import { Button } from "./kit/Button";
import { Nav } from "./kit/Nav";
import { Notice } from "./kit/Notice";
import { Shell } from "./kit/Shell";
import { ToastHost } from "./kit/Toast";
import { NoteButton } from "./notes/NoteButton";
import { appRouter, matchRoute, redirectFor, viewFor } from "./router/router";
import { Ready, readySlot, type SpineCrumb } from "./shell/marks";
import { SpineBar, usePublishedSpine } from "./shell/Spine";
import { userLabel } from "./shell/user";
import { readyName, routedView } from "./shell/views";
import { SessionEnded } from "./states/SessionEnded";
import { StateCard } from "./states/StateCard";

export { Status } from "./find/Status";

export function App({ auth = null }: { auth?: AuthStore | null }) {
  return (
    <>
      {auth === null ? <Workspace auth={null} /> : <Gate auth={auth} />}
      <ToastHost />
    </>
  );
}

// Sign-in first: nothing but the header renders, and the harvested index is not requested,
// until there is a token. A session that ends under an open page keeps the page, dimmed under the
// session-ended dialog, which signs in again and comes back to the same URL.
function Gate({ auth }: { auth: AuthStore }) {
  const state = useSyncExternalStore(auth.subscribe, auth.getSnapshot);
  const router = appRouter();
  const path = useSyncExternalStore(router.subscribe, router.getPath);
  if (state.kind === "signed-in" || state.kind === "ended") {
    return (
      <>
        <Workspace auth={auth} />
        <SessionEnded
          open={state.kind === "ended"}
          onSignIn={() => void auth.signIn()}
          path={path}
        />
      </>
    );
  }
  if (state.kind === "signed-out" && state.notice !== null) {
    // A failed sign-in: the notice and the way back in, in the page body.
    return (
      <Shell title={TITLE}>
        <div className="signed-out">
          <StateCard
            kind="signed-out"
            eyebrow="Signed out"
            title="Sign in to carry on"
            sentence={<span data-testid="auth-notice">{state.notice}</span>}
            actions={
              <Button variant="primary" onClick={() => void auth.signIn()}>
                Sign in
              </Button>
            }
            alert
          />
        </div>
      </Shell>
    );
  }
  return (
    <Shell
      title={TITLE}
      status={
        state.kind === "signed-out" ? (
          <Notice kind="info">Redirecting to sign in…</Notice>
        ) : undefined
      }
      auth={<AuthControl auth={auth} />}
    />
  );
}

const TITLE = "asteroidea";

type NavView = "studies" | "find" | "results" | "setup";

// The top bar: four places. Imports sit under Find, for curators.
const NAV: readonly { to: string; label: string; view: NavView }[] = [
  { to: "/", label: "Studies", view: "studies" },
  { to: "/discover", label: "Find", view: "find" },
  { to: "/results", label: "Results", view: "results" },
  { to: "/start", label: "Setup", view: "setup" },
];

// Which top-bar place a view sits under.
function placeOf(view: string): NavView | null {
  switch (view) {
    case "data":
    case "study":
    case "dataset":
    case "participant":
    case "session":
    case "file":
    case "uploads":
      return "studies";
    case "discover":
    case "imports":
    case "import":
      return "find";
    case "results":
    case "result":
      return "results";
    case "start":
      return "setup";
    default:
      return null;
  }
}

// The spine for a view that publishes none of its own (the pages that draw it publish theirs).
function routeSpine(view: string, params: Readonly<Record<string, string>>): SpineCrumb[] | null {
  if (view === "study") return [{ label: "Studies", to: "/" }, { label: params.study ?? "" }];
  if (view === "uploads") return [{ label: "Studies", to: "/" }, { label: "My uploads" }];
  return null;
}

function Workspace({ auth }: { auth: AuthStore | null }) {
  const [state, setState] = useState<LoadState>({ kind: "loading" });
  const router = appRouter();
  const path = useSyncExternalStore(router.subscribe, router.getPath);
  const ready = useSyncExternalStore(readySlot.subscribe, readySlot.get);
  // Imports are for curators: the Imports routes show for the curators group in the access
  // token (display only; the API enforces). Anyone else gets "Page not found" there.
  const curator = auth !== null && isCurator(auth.getAccessToken());
  // A retired path (/catalog*, /imports*, /data/:dataset) is replaced by its new address, query kept.
  const redirect = redirectFor(path);
  useEffect(() => {
    if (redirect !== null) {
      router.navigate(`${redirect}${window.location.search}${window.location.hash}`, {
        replace: true,
      });
    }
  }, [redirect, router]);
  const routed = viewFor(path);
  const importsRoute = routed === "imports" || routed === "import";
  const view = importsRoute && !curator ? "not-found" : routed;
  const place = placeOf(view);
  const match = matchRoute(path);
  const spine = usePublishedSpine(routeSpine(view, match?.params ?? {}));
  // One title pattern for every view: "<place> - asteroidea".
  const placeLabel = NAV.find((n) => n.view === place)?.label;
  useEffect(() => {
    document.title = placeLabel === undefined ? TITLE : `${placeLabel} - ${TITLE}`;
  }, [placeLabel]);
  useEffect(() => {
    let live = true;
    void loadIndex().then((s) => {
      if (live) setState(s);
    });
    return () => {
      live = false;
    };
  }, []);
  const settled = redirect === null && match !== null && view !== "not-found";

  return (
    <Shell
      title={TITLE}
      auth={
        auth === null ? undefined : (
          <>
            <span className="shell-user" data-testid="user-label">
              {userLabel(auth.getIdToken(), auth.getAccessToken())}
            </span>
            <AuthControl auth={auth} />
          </>
        )
      }
      note={auth === null ? undefined : <NoteButton auth={auth} />}
      nav={
        auth === null ? undefined : (
          <Nav items={NAV.map((n) => ({ to: n.to, label: n.label, current: n.view === place }))} />
        )
      }
      spine={spine === null || spine.length === 0 ? undefined : <SpineBar trail={spine} />}
      ready={ready ? readyName(view) : undefined}
    >
      {auth !== null && settled ? routedView(match, auth) : null}
      {view === "not-found" && redirect === null ? (
        <section className="not-found" aria-label="Page not found">
          <Ready />
          <h2>Page not found</h2>
          <p>
            There is no page at <code>{path}</code>. <a href="/">Back to Studies</a>
          </p>
        </section>
      ) : null}
      {view === "discover" || view === "imports" || view === "import" ? (
        <div className="app-view">
          <FindLoader
            state={state}
            auth={auth}
            curator={curator}
            tab={view === "discover" ? "index" : "imports"}
            runId={view === "import" ? (match?.params.run_id ?? null) : null}
          />
        </div>
      ) : null}
    </Shell>
  );
}
