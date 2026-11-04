// Build-time auth configuration. The three Vite env variables are all required: with any of
// them absent or blank the sign-in control is hidden, so fixture mode and the dataset browser are
// unchanged.
//
//   VITE_COGNITO_DOMAIN     Cognito managed-login host, e.g. asteroidea-<account>.auth.us-east-1.amazoncognito.com
//   VITE_COGNITO_CLIENT_ID  the `web` app client id
//   VITE_API_URL            the platform HTTP API base URL, e.g. https://<id>.execute-api.us-east-1.amazonaws.com

export interface AuthConfig {
  /** Origin of the managed-login host, no trailing slash. */
  readonly authOrigin: string;
  readonly clientId: string;
  /** API base URL, no trailing slash. */
  readonly apiUrl: string;
}

type Env = Readonly<Record<string, string | boolean | undefined>>;

function text(env: Env, key: string): string {
  const v = env[key];
  return typeof v === "string" ? v.trim() : "";
}

export function readAuthConfig(env: Env): AuthConfig | null {
  const domain = text(env, "VITE_COGNITO_DOMAIN");
  const clientId = text(env, "VITE_COGNITO_CLIENT_ID");
  const api = text(env, "VITE_API_URL");
  if (domain === "" || clientId === "" || api === "") return null;
  const host = domain.replace(/^https?:\/\//, "").replace(/\/+$/, "");
  return { authOrigin: `https://${host}`, clientId, apiUrl: api.replace(/\/+$/, "") };
}
