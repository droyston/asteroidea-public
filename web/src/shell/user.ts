// The label for the signed-in user: the ID token's `email` claim, else the role. Display only; the API
// enforces access. The ID token lives in sessionStorage with the other tokens (auth/store.ts).
import { isCurator } from "../auth/groups";

/** The `email` claim of a JWT, or null when the token is absent, malformed or has none. */
export function tokenEmail(token: string | null): string | null {
  const payload = token?.split(".")[1];
  if (payload === undefined || payload === "") return null;
  try {
    const padded = payload.replace(/-/g, "+").replace(/_/g, "/");
    const bytes = Uint8Array.from(atob(padded), (c) => c.charCodeAt(0));
    const claims: unknown = JSON.parse(new TextDecoder().decode(bytes));
    if (typeof claims !== "object" || claims === null) return null;
    const email = (claims as Record<string, unknown>).email;
    return typeof email === "string" && email !== "" ? email : null;
  } catch {
    return null;
  }
}

export function userLabel(idToken: string | null, accessToken: string | null): string {
  return tokenEmail(idToken) ?? (isCurator(accessToken) ? "curator" : "researcher");
}
