import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import { runCallback } from "./auth/callback";
import { readAuthConfig } from "./auth/config";
import { CALLBACK_PATH } from "./auth/pkce";
import { AuthStore } from "./auth/store";
import { appRouter, interceptLinks } from "./router/router";
import "@fontsource/ibm-plex-mono/latin-400.css";
import "@fontsource/ibm-plex-sans/latin-400.css";
import "@fontsource/ibm-plex-sans/latin-600.css";
import "@fontsource/source-serif-4/latin-400.css";
import "@fontsource/source-serif-4/latin-600.css";
import "./kit/tokens.css";
import "./kit/kit.css";
import "./app.css";
import "./states/states.css";

const root = document.getElementById("root");
if (root === null) throw new Error("index.html has no #root element");

// Sign-in is opt-in: without all three VITE_* values the control is absent.
const config = readAuthConfig(import.meta.env);
const auth =
  config === null
    ? null
    : new AuthStore(config, {
        storage: window.sessionStorage,
        fetch: (url, init) => fetch(url, init),
        crypto: window.crypto,
        origin: window.location.origin,
        navigate: (url) => window.location.assign(url),
        currentPath: () => `${window.location.pathname}${window.location.search}`,
      });

// The callback runs once, here, outside React: the code is single-use, and StrictMode runs effects twice.
if (auth !== null && window.location.pathname === CALLBACK_PATH) {
  void runCallback(auth, window.location.search, (path) =>
    appRouter().navigate(path, { replace: true }),
  );
} else if (auth !== null) {
  // Sign-in first: without a token, go to managed login before anything renders.
  auth.redirectIfSignedOut();
}

interceptLinks(appRouter());
createRoot(root).render(
  <StrictMode>
    <App auth={auth} />
  </StrictMode>,
);
