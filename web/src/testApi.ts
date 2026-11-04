// Test helpers shared by the web unit tests: a JSON `Response` and a `TokenSource` stub.
// Imported by `*.test.ts` only, never by the app, so it stays out of the bundle.
import type { TokenSource } from "./auth/apiGet";

export function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

export function tokens(over: Partial<TokenSource> = {}): TokenSource {
  return { getAccessToken: () => "tok", refresh: () => Promise.resolve(true), ...over };
}
