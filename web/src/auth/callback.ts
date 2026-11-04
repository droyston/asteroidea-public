// The OAuth callback, run once from main.tsx. The code is single-use, so the URL is cleaned first;
// after a successful sign-in the page returns to the path stored when sign-in started.
import type { AuthStore } from "./store";

export async function runCallback(
  auth: AuthStore,
  search: string,
  replace: (path: string) => void,
): Promise<void> {
  replace("/");
  await auth.completeCallback(search);
  if (auth.getSnapshot().kind === "signed-in") replace(auth.returnPath);
}
