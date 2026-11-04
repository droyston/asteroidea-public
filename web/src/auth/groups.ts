// The Cognito groups in an access token, read client-side for display only: the Imports nav
// item shows for curators. The server checks the group on every call; a forged token here only
// reveals a page whose calls the API refuses.

export const CURATORS = "curators";

/** The `cognito:groups` claim of a JWT, or [] when the token is absent, malformed or has none. */
export function tokenGroups(token: string | null): readonly string[] {
  const payload = token?.split(".")[1];
  if (payload === undefined || payload === "") return [];
  try {
    const padded = payload.replace(/-/g, "+").replace(/_/g, "/");
    const bytes = Uint8Array.from(atob(padded), (c) => c.charCodeAt(0));
    const claims: unknown = JSON.parse(new TextDecoder().decode(bytes));
    if (typeof claims !== "object" || claims === null) return [];
    const groups = (claims as Record<string, unknown>)["cognito:groups"];
    if (Array.isArray(groups)) return groups.filter((g): g is string => typeof g === "string");
    return typeof groups === "string" && groups !== "" ? [groups] : [];
  } catch {
    return [];
  }
}

export function isCurator(token: string | null): boolean {
  return tokenGroups(token).includes(CURATORS);
}
