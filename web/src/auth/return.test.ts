import { describe, expect, it, vi } from "vitest";
import { json } from "../testApi";
import { runCallback } from "./callback";
import { readAuthConfig } from "./config";
import { type AuthDeps, AuthStore, PENDING_KEY } from "./store";

const CONFIG = readAuthConfig({
  VITE_COGNITO_DOMAIN: "https://auth.example.test",
  VITE_COGNITO_CLIENT_ID: "client-1",
  VITE_API_URL: "https://api.example.test",
});
if (CONFIG === null) throw new Error("test config must parse");
const cfg = CONFIG;

function setup(current: string) {
  const data = new Map<string, string>();
  const storage = {
    getItem: (k: string) => data.get(k) ?? null,
    setItem: (k: string, v: string) => void data.set(k, v),
    removeItem: (k: string) => void data.delete(k),
  };
  const navigate = vi.fn<(url: string) => void>();
  const fetchMock = vi.fn<AuthDeps["fetch"]>();
  fetchMock.mockResolvedValue(json(200, { access_token: "A", refresh_token: "R" }));
  const auth = new AuthStore(cfg, {
    storage,
    fetch: fetchMock,
    crypto: globalThis.crypto,
    origin: "http://localhost:5173",
    navigate,
    currentPath: () => current,
  });
  const stateOf = () => new URL(String(navigate.mock.calls[0]?.[0])).searchParams.get("state");
  return { auth, data, stateOf };
}

describe("return to the requested path after sign-in", () => {
  it("the URL after the callback is the path stored at sign-in", async () => {
    const { auth, stateOf } = setup("/catalog?x=1");
    await auth.signIn();
    const urls: string[] = [];
    await runCallback(auth, `?code=abc&state=${stateOf()}`, (p) => urls.push(p));
    expect(urls).toEqual(["/", "/catalog?x=1"]);
  });

  it("defaults to / and keeps the callback out of the return path", async () => {
    const { auth, stateOf } = setup("/auth/callback?code=x");
    await auth.signIn();
    const urls: string[] = [];
    await runCallback(auth, `?code=abc&state=${stateOf()}`, (p) => urls.push(p));
    expect(urls.at(-1)).toBe("/");
  });

  it("refuses an unsafe stored path and a failed callback does not return anywhere", async () => {
    const { auth, data, stateOf } = setup("//evil.com");
    await auth.signIn();
    expect(JSON.parse(data.get(PENDING_KEY) ?? "null").returnTo).toBe("/");
    const urls: string[] = [];
    await runCallback(auth, "?code=abc&state=wrong", (p) => urls.push(p));
    expect(urls).toEqual(["/"]);
    expect(stateOf()).not.toBeNull();
  });

  it("a tampered stored path is validated on the way back", async () => {
    const { auth, data, stateOf } = setup("/results");
    await auth.signIn();
    const pending = JSON.parse(data.get(PENDING_KEY) ?? "{}");
    data.set(PENDING_KEY, JSON.stringify({ ...pending, returnTo: "https://x" }));
    const urls: string[] = [];
    await runCallback(auth, `?code=abc&state=${stateOf()}`, (p) => urls.push(p));
    expect(urls.at(-1)).toBe("/");
  });
});
