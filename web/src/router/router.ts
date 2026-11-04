// A small router over the History API: path matching with params, navigate(), link click
// interception and popstate. No dependency.
import {
  dataPath,
  dataSessionPath,
  datasetViewPath,
  NO_SESSION,
  participantPath,
  unlinkedKey,
} from "./dataPaths";
import { ROUTES, type RouteDef } from "./routes";

export interface Match {
  readonly name: RouteDef["name"];
  readonly params: Readonly<Record<string, string>>;
}

function segments(path: string): string[] {
  return path.split("/").filter((s) => s !== "");
}

/** Match a pathname (no query or hash) against `routes`; null when none matches. */
export function matchRoute(path: string, routes: readonly RouteDef[] = ROUTES): Match | null {
  const want = segments(path);
  for (const route of routes) {
    const have = segments(route.pattern);
    if (have.length !== want.length) continue;
    const params: Record<string, string> = {};
    let ok = true;
    for (const [i, seg] of have.entries()) {
      const got = want[i] ?? "";
      if (seg.startsWith(":")) {
        try {
          params[seg.slice(1)] = decodeURIComponent(got);
        } catch {
          ok = false;
        }
      } else if (seg !== got) {
        ok = false;
      }
      if (!ok) break;
    }
    if (ok) return { name: route.name, params };
  }
  return null;
}

function decoded(seg: string | undefined): string | null {
  try {
    return decodeURIComponent(seg ?? "");
  } catch {
    return null;
  }
}

/**
 * Where a retired path goes: `/catalog` to `/`, `/catalog/:d` and
 * `/data/:d` to `/d/:d`, `/data/:d/:s` and `/catalog/:d/subjects/:s` to the participant page
 * `/s/_/p/<d>:<s>` (a linked subject resolves to its person there),
 * `/data/:d/:s/:session` to `/s/_/:d/:s/:session`, `/imports*` to `/discover/imports*`. Null for any
 * other path. Takes a pathname; the caller keeps the query and hash.
 */
export function redirectFor(path: string): string | null {
  const s = segments(path);
  const [head, d, mid, sub] = s;
  if (head === "catalog") {
    if (s.length === 1) return dataPath();
    const dataset = decoded(d);
    if (dataset === null) return null;
    if (s.length === 2) return datasetViewPath(dataset);
    const subject = decoded(sub);
    if (s.length === 4 && mid === "subjects" && subject !== null) {
      return participantPath(unlinkedKey(dataset, subject));
    }
    return null;
  }
  if (head === "data" && s.length >= 2 && s.length <= 4) {
    const dataset = decoded(d);
    if (dataset === null) return null;
    if (s.length === 2) return datasetViewPath(dataset);
    if (s.length === 3) {
      const only = decoded(mid);
      return only === null ? null : participantPath(unlinkedKey(dataset, only));
    }
    const subject = decoded(mid);
    const session = decoded(sub);
    if (subject === null || session === null) return null;
    return dataSessionPath(dataset, subject, session === NO_SESSION ? null : session);
  }
  if (head === "imports") return `/discover/${s.join("/")}`;
  return null;
}

/**
 * A same-origin path that is safe to return to after sign-in: starts with a single "/", has no
 * scheme, no "//" or "/\" at the start, no control characters. Anything else gives null.
 */
export function safeReturnPath(value: unknown): string | null {
  if (typeof value !== "string" || !value.startsWith("/")) return null;
  if (value.startsWith("//") || value.startsWith("/\\")) return null;
  for (const ch of value) {
    const code = ch.charCodeAt(0);
    if (code < 0x20 || code === 0x7f) return null;
  }
  return value;
}

export interface Win {
  readonly location: { readonly pathname: string };
  readonly history: {
    pushState(state: null, unused: string, url: string): void;
    replaceState(state: null, unused: string, url: string): void;
  };
  addEventListener(type: "popstate", listener: () => void): void;
  removeEventListener(type: "popstate", listener: () => void): void;
}

export interface Router {
  getPath(): string;
  subscribe(listener: () => void): () => void;
  navigate(path: string, opts?: { replace?: boolean }): void;
}

export function createRouter(win: Win): Router {
  const listeners = new Set<() => void>();
  const emit = () => {
    for (const l of listeners) l();
  };
  return {
    getPath: () => win.location.pathname,
    subscribe(listener) {
      if (listeners.size === 0) win.addEventListener("popstate", emit);
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
        if (listeners.size === 0) win.removeEventListener("popstate", emit);
      };
    },
    navigate(path, opts = {}) {
      const safe = safeReturnPath(path) ?? "/";
      if (opts.replace === true) win.history.replaceState(null, "", safe);
      else win.history.pushState(null, "", safe);
      emit();
    },
  };
}

export interface ClickInfo {
  readonly button: number;
  readonly defaultPrevented: boolean;
  readonly metaKey: boolean;
  readonly ctrlKey: boolean;
  readonly shiftKey: boolean;
  readonly altKey: boolean;
  /** The nearest enclosing anchor, if any. */
  readonly anchor: {
    readonly href: string;
    readonly target: string;
    readonly hasDownload: boolean;
  } | null;
}

/**
 * The path to navigate to for a click on an in-app link, or null to let the browser handle it:
 * modified clicks, other targets, other origins and any path that is not a registered route
 * (a retired path counts: the app redirects it; so `/index.json`, `/platform.json` and `/assets/*` load natively).
 */
export function linkTarget(click: ClickInfo, origin: string): string | null {
  const a = click.anchor;
  if (a === null || click.defaultPrevented || click.button !== 0) return null;
  if (click.metaKey || click.ctrlKey || click.shiftKey || click.altKey) return null;
  if ((a.target !== "" && a.target !== "_self") || a.hasDownload) return null;
  let url: URL;
  try {
    url = new URL(a.href);
  } catch {
    return null;
  }
  if (url.origin !== origin) return null;
  if (matchRoute(url.pathname) === null && redirectFor(url.pathname) === null) return null;
  return `${url.pathname}${url.search}${url.hash}`;
}

/** Install the document-level click handler; returns the remover. */
export function interceptLinks(router: Router, doc: Document = document): () => void {
  const onClick = (e: MouseEvent) => {
    const el = e.target instanceof Element ? e.target.closest("a[href]") : null;
    const anchor = el instanceof HTMLAnchorElement ? el : null;
    const path = linkTarget(
      {
        button: e.button,
        defaultPrevented: e.defaultPrevented,
        metaKey: e.metaKey,
        ctrlKey: e.ctrlKey,
        shiftKey: e.shiftKey,
        altKey: e.altKey,
        anchor:
          anchor === null
            ? null
            : {
                href: anchor.href,
                target: anchor.target,
                hasDownload: anchor.hasAttribute("download"),
              },
      },
      doc.location.origin,
    );
    if (path === null) return;
    e.preventDefault();
    router.navigate(path);
  };
  doc.addEventListener("click", onClick);
  return () => doc.removeEventListener("click", onClick);
}

export type View = RouteDef["name"] | "not-found";

/** The view a pathname shows: a registered route, else "not-found". */
export function viewFor(path: string): View {
  return matchRoute(path)?.name ?? "not-found";
}

let shared: Router | null = null;
/** The app's router over `window`, created on first use. */
export function appRouter(): Router {
  shared ??= createRouter(window);
  return shared;
}
