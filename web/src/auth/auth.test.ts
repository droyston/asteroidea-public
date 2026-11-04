import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import { json } from "../testApi";
import { readAuthConfig } from "./config";
import { authorizeUrl, challengeFor, randomString, redirectUri } from "./pkce";
import { type AuthDeps, AuthStore, PENDING_KEY, TOKENS_KEY } from "./store";
import { formatCreated, UploadsView } from "./UploadsPanel";
import { loadUploads, parseUploads, type TokenSource, type UploadsState } from "./uploads";

const CONFIG = readAuthConfig({
  VITE_COGNITO_DOMAIN: "auth.example.test",
  VITE_COGNITO_CLIENT_ID: "client-1",
  VITE_API_URL: "https://api.example.test/",
});
if (CONFIG === null) throw new Error("test config must parse");
const cfg = CONFIG;

const ORIGIN = "http://localhost:5173";

interface MemoryStorage {
  data: Map<string, string>;
  getItem(k: string): string | null;
  setItem(k: string, v: string): void;
  removeItem(k: string): void;
}

function memory(): MemoryStorage {
  const data = new Map<string, string>();
  return {
    data,
    getItem: (k) => data.get(k) ?? null,
    setItem: (k, v) => void data.set(k, v),
    removeItem: (k) => void data.delete(k),
  };
}

function setup() {
  const storage = memory();
  const navigate = vi.fn<(url: string) => void>();
  const fetchMock = vi.fn<AuthDeps["fetch"]>();
  const d: AuthDeps = {
    storage,
    fetch: fetchMock,
    crypto: globalThis.crypto,
    origin: ORIGIN,
    navigate,
  };
  return { d, storage, navigate, fetchMock };
}

describe("config", () => {
  it("is null unless all three values are present and non-blank", () => {
    expect(readAuthConfig({})).toBeNull();
    expect(readAuthConfig({ VITE_COGNITO_DOMAIN: "d", VITE_COGNITO_CLIENT_ID: "c" })).toBeNull();
    expect(
      readAuthConfig({ VITE_COGNITO_DOMAIN: "d", VITE_COGNITO_CLIENT_ID: " ", VITE_API_URL: "u" }),
    ).toBeNull();
  });
  it("normalises the host and the API URL", () => {
    expect(cfg).toEqual({
      authOrigin: "https://auth.example.test",
      clientId: "client-1",
      apiUrl: "https://api.example.test",
    });
  });
});

describe("pkce", () => {
  it("S256 challenge matches the RFC 7636 appendix B vector", async () => {
    const verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk";
    expect(await challengeFor(globalThis.crypto, verifier)).toBe(
      "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
    );
  });
  it("random strings are base64url and differ", () => {
    const a = randomString(globalThis.crypto);
    const b = randomString(globalThis.crypto);
    expect(a).toMatch(/^[A-Za-z0-9_-]{43}$/);
    expect(a).not.toBe(b);
  });
  it("the authorize URL carries S256, state and the exact redirect", () => {
    const url = new URL(authorizeUrl(cfg, redirectUri(ORIGIN), "st", "ch"));
    expect(url.origin + url.pathname).toBe("https://auth.example.test/oauth2/authorize");
    expect(Object.fromEntries(url.searchParams)).toEqual({
      response_type: "code",
      client_id: "client-1",
      redirect_uri: "http://localhost:5173/auth/callback",
      scope: "openid email profile",
      state: "st",
      code_challenge: "ch",
      code_challenge_method: "S256",
    });
  });
});

describe("sign-in flow", () => {
  async function begin() {
    const ctx = setup();
    const auth = new AuthStore(cfg, ctx.d);
    await auth.signIn();
    const url = new URL(String(ctx.navigate.mock.calls[0]?.[0]));
    return { ...ctx, auth, url, state: url.searchParams.get("state") ?? "" };
  }
  function notice(auth: AuthStore): string | null {
    const s = auth.getSnapshot();
    return s.kind === "signed-out" ? s.notice : null;
  }

  it("signIn navigates to managed login and keeps verifier and state in the storage it was given", async () => {
    const { storage, url, state } = await begin();
    expect(url.searchParams.get("code_challenge_method")).toBe("S256");
    expect(state).not.toBe("");
    const pending = JSON.parse(storage.data.get(PENDING_KEY) ?? "null");
    expect(pending.state).toBe(state);
    expect(await challengeFor(globalThis.crypto, pending.verifier)).toBe(
      url.searchParams.get("code_challenge"),
    );
  });

  it("exchanges the code with the stored verifier and keeps the tokens", async () => {
    const { auth, fetchMock, storage, state } = await begin();
    fetchMock.mockResolvedValue(
      json(200, { access_token: "A", refresh_token: "R", id_token: "I" }),
    );
    await auth.completeCallback(`?code=abc&state=${state}`);
    expect(auth.getSnapshot()).toEqual({ kind: "signed-in" });
    expect(auth.getAccessToken()).toBe("A");
    expect(storage.data.has(PENDING_KEY)).toBe(false);
    const [url, init] = fetchMock.mock.calls[0] ?? [];
    expect(url).toBe("https://auth.example.test/oauth2/token");
    const form = new URLSearchParams(String(init?.body));
    expect(form.get("grant_type")).toBe("authorization_code");
    expect(form.get("code")).toBe("abc");
    expect(form.get("client_id")).toBe("client-1");
    expect(form.get("redirect_uri")).toBe("http://localhost:5173/auth/callback");
    expect(form.get("code_verifier")).toMatch(/^[A-Za-z0-9_-]{43}$/);
  });

  it("rejects a state mismatch without calling the token endpoint or storing tokens", async () => {
    const { auth, fetchMock, storage } = await begin();
    fetchMock.mockResolvedValue(json(200, { access_token: "A" }));
    await auth.completeCallback("?code=abc&state=not-the-state");
    expect(fetchMock).not.toHaveBeenCalled();
    expect(storage.data.has(TOKENS_KEY)).toBe(false);
    expect(auth.getSnapshot().kind).toBe("signed-out");
    expect(notice(auth)).toContain("state mismatch");
  });

  it("rejects a callback when no sign-in is pending", async () => {
    const ctx = setup();
    const auth = new AuthStore(cfg, ctx.d);
    await auth.completeCallback("?code=abc&state=x");
    expect(ctx.fetchMock).not.toHaveBeenCalled();
    expect(auth.getSnapshot().kind).toBe("signed-out");
  });

  it("a state is single-use", async () => {
    const { auth, fetchMock, state } = await begin();
    fetchMock.mockResolvedValue(json(200, { access_token: "A" }));
    await auth.completeCallback(`?code=abc&state=${state}`);
    await auth.completeCallback(`?code=abc&state=${state}`);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(auth.getSnapshot().kind).toBe("signed-out");
  });

  it("surfaces the provider error and the token endpoint refusal", async () => {
    const a = await begin();
    await a.auth.completeCallback("?error=access_denied&error_description=nope");
    expect(notice(a.auth)).toContain("access_denied: nope");

    const b = await begin();
    b.fetchMock.mockResolvedValue(json(400, { error: "invalid_grant" }));
    await b.auth.completeCallback(`?code=abc&state=${b.state}`);
    expect(notice(b.auth)).toContain("invalid_grant");
  });

  it("refresh keeps the old refresh token when the endpoint sends no new one", async () => {
    const { auth, fetchMock, state } = await begin();
    fetchMock.mockResolvedValueOnce(json(200, { access_token: "A", refresh_token: "R" }));
    await auth.completeCallback(`?code=abc&state=${state}`);
    fetchMock.mockResolvedValueOnce(json(200, { access_token: "A2" }));
    expect(await auth.refresh()).toBe(true);
    expect(auth.getAccessToken()).toBe("A2");
    const form = new URLSearchParams(String(fetchMock.mock.calls[1]?.[1]?.body));
    expect(form.get("grant_type")).toBe("refresh_token");
    expect(form.get("refresh_token")).toBe("R");
    fetchMock.mockResolvedValueOnce(json(200, { access_token: "A3" }));
    await auth.refresh();
    const again = new URLSearchParams(String(fetchMock.mock.calls[2]?.[1]?.body));
    expect(again.get("refresh_token")).toBe("R");
  });

  it("refresh is false without a refresh token, and false when the endpoint refuses it", async () => {
    const { auth, fetchMock, state } = await begin();
    fetchMock.mockResolvedValueOnce(json(200, { access_token: "A" }));
    await auth.completeCallback(`?code=abc&state=${state}`);
    expect(await auth.refresh()).toBe(false);
    expect(fetchMock).toHaveBeenCalledTimes(1);

    const c = await begin();
    c.fetchMock.mockResolvedValueOnce(json(200, { access_token: "A", refresh_token: "R" }));
    await c.auth.completeCallback(`?code=abc&state=${c.state}`);
    c.fetchMock.mockResolvedValueOnce(json(400, { error: "invalid_grant" }));
    expect(await c.auth.refresh()).toBe(false);
  });

  it("expire clears the tokens and ends the session under the page; sign-out says nothing", () => {
    const ctx = setup();
    ctx.storage.setItem(TOKENS_KEY, JSON.stringify({ access_token: "A", refresh_token: null }));
    const auth = new AuthStore(cfg, ctx.d);
    expect(auth.getSnapshot()).toEqual({ kind: "signed-in" });
    auth.expire();
    expect(auth.getSnapshot()).toEqual({ kind: "ended" });
    expect(ctx.storage.data.has(TOKENS_KEY)).toBe(false);
    const before = auth.getSnapshot();
    auth.expire();
    expect(auth.getSnapshot()).toBe(before);
    auth.signOut();
    expect(auth.getSnapshot()).toEqual({ kind: "signed-out", notice: null });
  });
});

describe("loadUploads", () => {
  const row = {
    session_id: "s1",
    dataset: "sample-1",
    relpath: "sub-01/anat/sub-01_T1w.nii",
    state: "COMMITTED",
    created_at: 1_790_000_000,
    asset_id: "a1",
  };

  type Call = [string, RequestInit & { headers: Record<string, string> }];
  const fetchOf = (...responses: Response[]) => {
    let n = 0;
    // The last response repeats, so a test that expects two calls queues one.
    return vi.fn(async (_url: string, _init: RequestInit): Promise<Response> => {
      const next = responses[Math.min(n, responses.length - 1)];
      n += 1;
      if (next === undefined) throw new Error("no response queued");
      return next.clone();
    });
  };

  function tokens(initial: string | null, refreshOk: boolean) {
    let current = initial;
    const refresh = vi.fn(async () => {
      if (refreshOk) current = "NEW";
      return refreshOk;
    });
    const src: TokenSource = { getAccessToken: () => current, refresh };
    return { src, refresh };
  }
  const authHeader = (f: ReturnType<typeof fetchOf>, n: number) =>
    (f.mock.calls[n] as unknown as Call)[1].headers.authorization;

  it("sends the bearer token and returns the rows", async () => {
    const f = fetchOf(json(200, { uploads: [row] }));
    const r = await loadUploads(cfg, tokens("T", true).src, f);
    expect(r).toEqual({ kind: "loaded", uploads: [row] });
    expect(f.mock.calls[0]?.[0]).toBe("https://api.example.test/uploads");
    expect(authHeader(f, 0)).toBe("Bearer T");
  });

  it("an empty list is loaded with no rows", async () => {
    const f = fetchOf(json(200, { uploads: [] }));
    expect(await loadUploads(cfg, tokens("T", true).src, f)).toEqual({
      kind: "loaded",
      uploads: [],
    });
  });

  it("on 401 refreshes once and retries with the new token", async () => {
    const f = fetchOf(json(401, { message: "Unauthorized" }), json(200, { uploads: [row] }));
    const t = tokens("OLD", true);
    const r = await loadUploads(cfg, t.src, f);
    expect(r.kind).toBe("loaded");
    expect(t.refresh).toHaveBeenCalledTimes(1);
    expect(authHeader(f, 0)).toBe("Bearer OLD");
    expect(authHeader(f, 1)).toBe("Bearer NEW");
  });

  it("ends the session when the refresh fails", async () => {
    const f = fetchOf(json(401, {}));
    const t = tokens("OLD", false);
    expect(await loadUploads(cfg, t.src, f)).toEqual({ kind: "ended" });
    expect(t.refresh).toHaveBeenCalledTimes(1);
    expect(f).toHaveBeenCalledTimes(1);
  });

  it("ends the session on a second 401, after exactly one refresh", async () => {
    const f = fetchOf(json(401, {}));
    const t = tokens("OLD", true);
    expect(await loadUploads(cfg, t.src, f)).toEqual({ kind: "ended" });
    expect(t.refresh).toHaveBeenCalledTimes(1);
    expect(f).toHaveBeenCalledTimes(2);
  });

  it("ends the session with no token", async () => {
    const f = fetchOf();
    expect(await loadUploads(cfg, tokens(null, true).src, f)).toEqual({ kind: "ended" });
    expect(f).not.toHaveBeenCalled();
  });

  it("403 says the account is not a researcher", async () => {
    const r = await loadUploads(cfg, tokens("T", true).src, fetchOf(json(403, {})));
    expect(r.kind === "error" && r.message).toContain("researchers group");
  });

  it("a server error, a network failure, a timeout and a bad body each become an error", async () => {
    const t = tokens("T", true).src;
    const server = await loadUploads(cfg, t, fetchOf(json(500, {})));
    expect(server.kind === "error" && server.message).toContain("HTTP 500");
    const net = await loadUploads(cfg, t, async () => {
      throw new TypeError("Failed to fetch");
    });
    expect(net.kind === "error" && net.message).toContain("Failed to fetch");
    const slow = await loadUploads(cfg, t, async () => {
      throw new DOMException("timed out", "TimeoutError");
    });
    expect(slow.kind === "error" && slow.message).toContain("30 seconds");
    const bad = await loadUploads(cfg, t, fetchOf(json(200, { rows: [] })));
    expect(bad.kind === "error" && bad.message).toContain("cannot read");
  });

  it("parseUploads rejects a row that breaks the contract and reads a null asset_id", () => {
    expect(() => parseUploads({ uploads: [{ ...row, created_at: "x" }] })).toThrow("uploads[0]");
    expect(parseUploads({ uploads: [{ ...row, asset_id: null }] })[0]?.asset_id).toBeNull();
  });
});

describe("UploadsView states", () => {
  const html = (state: UploadsState) =>
    renderToStaticMarkup(createElement(UploadsView, { state, onRetry: () => undefined }));

  it("loading", () => {
    expect(html({ kind: "loading" })).toContain("Loading your uploads");
  });
  it("error names the problem and offers Try again", () => {
    const h = html({ kind: "error", message: "Check your network, then try again." });
    expect(h).toContain("Check your network");
    expect(h).toContain("Try again");
  });
  it("empty", () => {
    expect(html({ kind: "loaded", uploads: [] })).toContain("No uploads of yours yet");
  });
  it("rows show dataset, path, state and a UTC time", () => {
    const h = html({
      kind: "loaded",
      uploads: [
        {
          session_id: "s1",
          dataset: "sample-1",
          relpath: "sub-01/x.nii",
          state: "INCOMPLETE",
          created_at: 0,
          asset_id: null,
        },
      ],
    });
    expect(h).toContain("sample-1");
    expect(h).toContain("sub-01/x.nii");
    expect(h).toContain("INCOMPLETE");
    expect(h).toContain("1970-01-01 00:00:00 UTC");
    expect(formatCreated(0)).toBe("1970-01-01 00:00:00 UTC");
  });
});

describe("sign-in gate", () => {
  it("redirects only when signed out with no notice", async () => {
    const a = setup();
    const store = new AuthStore(cfg, a.d);
    store.redirectIfSignedOut();
    await vi.waitFor(() => expect(a.navigate).toHaveBeenCalledTimes(1));
    expect(a.storage.data.has(PENDING_KEY)).toBe(true);

    const b = setup();
    b.storage.setItem(TOKENS_KEY, JSON.stringify({ access_token: "t" }));
    new AuthStore(cfg, b.d).redirectIfSignedOut();
    expect(b.storage.data.has(PENDING_KEY)).toBe(false);

    const c = setup();
    const ended = new AuthStore(cfg, c.d);
    ended.expire();
    ended.redirectIfSignedOut();
    await new Promise((r) => setTimeout(r, 20));
    expect(c.navigate).not.toHaveBeenCalled();
    expect(c.storage.data.has(PENDING_KEY)).toBe(false);
  });
});
