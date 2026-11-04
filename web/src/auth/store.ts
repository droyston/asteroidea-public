// Sign-in state: PKCE authorization-code flow against Cognito managed login, using fetch only.
//
// Storage choice: sessionStorage, for the PKCE verifier and state (they must survive the redirect
// round trip, which a full page load wipes from memory) and for the tokens (so a reload in the same
// tab stays signed in). Never localStorage: closing the tab ends the session. Tokens in sessionStorage
// are readable by any script on the origin; the app loads no third-party scripts.
import { errorText } from "../errors";
import { safeReturnPath } from "../router/router";
import type { AuthConfig } from "./config";
import {
  authorizeUrl,
  CALLBACK_PATH,
  type CryptoLike,
  challengeFor,
  randomString,
  redirectUri,
} from "./pkce";

export interface KeyValue {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

export interface AuthDeps {
  readonly storage: KeyValue;
  readonly fetch: (url: string, init: RequestInit) => Promise<Response>;
  readonly crypto: CryptoLike;
  readonly origin: string;
  /** Full-page navigation to the managed-login page. */
  readonly navigate: (url: string) => void;
  /** The page's current path and query, kept across the sign-in round trip; default "/". */
  readonly currentPath?: () => string;
}

/** A path to return to after sign-in: validated, and never the callback itself. */
function returnTarget(value: unknown): string {
  const safe = safeReturnPath(value);
  return safe === null || safe.split(/[?#]/)[0] === CALLBACK_PATH ? "/" : safe;
}

export type AuthState =
  | { kind: "signed-out"; notice: string | null }
  | { kind: "working" }
  | { kind: "signed-in" }
  /** The session ran out while a page was open: the page stays, under the sign-in-again dialog. */
  | { kind: "ended" };

export const TOKENS_KEY = "asteroidea.auth.tokens/1";
export const PENDING_KEY = "asteroidea.auth.pending/1";

interface Tokens {
  access_token: string;
  refresh_token: string | null;
  id_token: string | null;
}

interface Pending {
  state: string;
  verifier: string;
  returnTo: string;
}

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null;
}

export class AuthStore {
  private state: AuthState;
  private readonly listeners = new Set<() => void>();
  /** Where to go after a successful callback: the validated path stored at sign-in, else "/". */
  returnPath = "/";

  constructor(
    readonly config: AuthConfig,
    private readonly deps: AuthDeps,
  ) {
    this.state =
      this.readTokens() === null ? { kind: "signed-out", notice: null } : { kind: "signed-in" };
  }

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };

  getSnapshot = (): AuthState => this.state;

  private set(state: AuthState): void {
    this.state = state;
    for (const l of this.listeners) l();
  }

  getAccessToken(): string | null {
    return this.readTokens()?.access_token ?? null;
  }

  /** The ID token (for the `email` claim shown in the top bar), or null. */
  getIdToken(): string | null {
    return this.readTokens()?.id_token ?? null;
  }

  private readTokens(): Tokens | null {
    try {
      const text = this.deps.storage.getItem(TOKENS_KEY);
      if (text === null) return null;
      const v: unknown = JSON.parse(text);
      if (!isRecord(v) || typeof v.access_token !== "string") return null;
      return {
        access_token: v.access_token,
        refresh_token: typeof v.refresh_token === "string" ? v.refresh_token : null,
        id_token: typeof v.id_token === "string" ? v.id_token : null,
      };
    } catch {
      return null;
    }
  }

  /** Redirect to managed login with a fresh PKCE verifier and state. */
  async signIn(): Promise<void> {
    const verifier = randomString(this.deps.crypto);
    const state = randomString(this.deps.crypto, 16);
    const challenge = await challengeFor(this.deps.crypto, verifier);
    const pending: Pending = {
      state,
      verifier,
      returnTo: returnTarget(this.deps.currentPath?.() ?? "/"),
    };
    this.deps.storage.setItem(PENDING_KEY, JSON.stringify(pending));
    this.deps.navigate(authorizeUrl(this.config, redirectUri(this.deps.origin), state, challenge));
  }

  /**
   * Sign-in gate: when there is no token and no notice to show, go straight to managed login.
   * A notice (a failed callback or an ended session) keeps the page, so a refusal never loops.
   */
  redirectIfSignedOut(): void {
    if (this.state.kind === "signed-out" && this.state.notice === null) void this.signIn();
  }

  /**
   * Finish the redirect: `search` is the callback's query string. The returned `state` must equal
   * the one stored by `signIn`; on a mismatch nothing is sent to the token endpoint.
   */
  async completeCallback(search: string): Promise<void> {
    this.set({ kind: "working" });
    const params = new URLSearchParams(search);
    const pending = this.takePending();
    const fail = (why: string) =>
      this.set({ kind: "signed-out", notice: `Sign-in failed: ${why}` });

    const denied = params.get("error");
    if (denied !== null) {
      fail(
        `${denied}${params.has("error_description") ? `: ${params.get("error_description")}` : ""}`,
      );
      return;
    }
    if (pending === null) {
      fail("this browser has no sign-in in progress. Click Sign in to start again.");
      return;
    }
    if (params.get("state") !== pending.state) {
      fail(
        "the response did not match this browser's request (state mismatch). Click Sign in again.",
      );
      return;
    }
    const code = params.get("code");
    if (code === null || code === "") {
      fail("the response carried no authorization code. Click Sign in again.");
      return;
    }
    try {
      const body = await this.tokenRequest({
        grant_type: "authorization_code",
        client_id: this.config.clientId,
        code,
        redirect_uri: redirectUri(this.deps.origin),
        code_verifier: pending.verifier,
      });
      this.saveTokens(body, null, null);
      this.returnPath = pending.returnTo;
      this.set({ kind: "signed-in" });
    } catch (err) {
      fail(`${errorText(err)}. Click Sign in to try again.`);
    }
  }

  /**
   * Trade the refresh token for a new access token. True on success; false when there is no
   * refresh token or the endpoint refuses it. A network failure throws.
   */
  async refresh(): Promise<boolean> {
    const current = this.readTokens();
    if (current === null || current.refresh_token === null) return false;
    let body: Record<string, unknown>;
    try {
      body = await this.tokenRequest({
        grant_type: "refresh_token",
        client_id: this.config.clientId,
        refresh_token: current.refresh_token,
      });
    } catch (err) {
      if (err instanceof TokenRefusedError) return false;
      throw err;
    }
    this.saveTokens(body, current.refresh_token, current.id_token);
    return true;
  }

  /**
   * The session is over. With no argument it ran out under an open page (a refresh failed): the
   * state is "ended", which keeps the page and asks to sign in again. With a notice (or null, for
   * sign-out) the page is left for the signed-out gate.
   */
  expire(notice?: string | null): void {
    this.deps.storage.removeItem(TOKENS_KEY);
    if (notice === undefined) {
      if (this.state.kind !== "ended") this.set({ kind: "ended" });
      return;
    }
    this.set({ kind: "signed-out", notice });
  }

  signOut(): void {
    this.expire(null);
  }

  private takePending(): Pending | null {
    let text: string | null;
    try {
      text = this.deps.storage.getItem(PENDING_KEY);
      this.deps.storage.removeItem(PENDING_KEY);
    } catch {
      return null;
    }
    if (text === null) return null;
    try {
      const v: unknown = JSON.parse(text);
      if (isRecord(v) && typeof v.state === "string" && typeof v.verifier === "string") {
        return { state: v.state, verifier: v.verifier, returnTo: returnTarget(v.returnTo) };
      }
    } catch {
      // fall through
    }
    return null;
  }

  private saveTokens(
    body: Record<string, unknown>,
    keepRefresh: string | null,
    keepId: string | null,
  ): void {
    const access = body.access_token;
    if (typeof access !== "string" || access === "") {
      throw new Error("the token endpoint returned no access_token");
    }
    const refresh = typeof body.refresh_token === "string" ? body.refresh_token : keepRefresh;
    const id = typeof body.id_token === "string" ? body.id_token : keepId;
    const tokens: Tokens = { access_token: access, refresh_token: refresh, id_token: id };
    this.deps.storage.setItem(TOKENS_KEY, JSON.stringify(tokens));
  }

  private async tokenRequest(form: Record<string, string>): Promise<Record<string, unknown>> {
    const response = await this.deps.fetch(`${this.config.authOrigin}/oauth2/token`, {
      method: "POST",
      headers: { "content-type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams(form).toString(),
    });
    let body: unknown = null;
    try {
      body = await response.json();
    } catch {
      // handled below by status
    }
    if (!response.ok) {
      const reason =
        isRecord(body) && typeof body.error === "string" ? body.error : `HTTP ${response.status}`;
      throw new TokenRefusedError(`the token endpoint refused the request (${reason})`);
    }
    if (!isRecord(body)) throw new Error("the token endpoint returned a body that is not JSON");
    return body;
  }
}

class TokenRefusedError extends Error {
  override name = "TokenRefusedError";
}
