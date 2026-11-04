// PKCE (RFC 7636) with the S256 method, and the managed-login authorize URL. Web Crypto only.
import type { AuthConfig } from "./config";

export type CryptoLike = Pick<Crypto, "getRandomValues" | "subtle">;

export const CALLBACK_PATH = "/auth/callback";
export const SCOPES = "openid email profile";

export function base64Url(bytes: Uint8Array): string {
  let binary = "";
  for (const b of bytes) binary += String.fromCharCode(b);
  return btoa(binary).replaceAll("+", "-").replaceAll("/", "_").replaceAll("=", "");
}

/** `byteLength` random bytes as base64url: 32 gives a 43-character verifier, the RFC minimum. */
export function randomString(crypto: CryptoLike, byteLength = 32): string {
  return base64Url(crypto.getRandomValues(new Uint8Array(byteLength)));
}

export async function challengeFor(crypto: CryptoLike, verifier: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier));
  return base64Url(new Uint8Array(digest));
}

export function redirectUri(origin: string): string {
  return `${origin}${CALLBACK_PATH}`;
}

export function authorizeUrl(
  config: AuthConfig,
  redirect: string,
  state: string,
  challenge: string,
): string {
  const q = new URLSearchParams({
    response_type: "code",
    client_id: config.clientId,
    redirect_uri: redirect,
    scope: SCOPES,
    state,
    code_challenge: challenge,
    code_challenge_method: "S256",
  });
  return `${config.authOrigin}/oauth2/authorize?${q.toString()}`;
}
