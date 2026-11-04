// The registered routes (`/`, `/s/:study`, `/d/:dataset`, the session path, `/uploads`; Find, Results, Setup; `/results/:artifact_id` is the result page). Retired paths (`/catalog*`, `/data/:dataset`, the old session path, `/imports*`) are in `redirectFor` (router.ts). `/data/:dataset/:subject` redirects to the participant page (`/s/:study/p/:participant`; `:participant` is a `subject_key` or `<dataset>:<subject>`; the `participant` chunk). The `file` route loads the `research` chunk; `session` (`_` for no study and for no session label) loads the `session` chunk.
// `/index.json`, `/platform.json` and `/assets/*` are static files and are never routes.

export type RouteName =
  | "data"
  | "study"
  | "uploads"
  | "discover"
  | "results"
  | "result"
  | "start"
  | "callback"
  | "dataset"
  | "participant"
  | "session"
  | "file"
  | "imports"
  | "import";

export interface RouteDef {
  readonly name: RouteName;
  /** Segments starting with ":" capture one path segment into `params`. */
  readonly pattern: string;
}

export const ROUTES: readonly RouteDef[] = [
  { name: "data", pattern: "/" },
  { name: "study", pattern: "/s/:study" },
  { name: "dataset", pattern: "/d/:dataset" },
  { name: "participant", pattern: "/s/:study/p/:participant" },
  { name: "session", pattern: "/s/:study/:dataset/:subject/:session" },
  { name: "uploads", pattern: "/uploads" },
  { name: "file", pattern: "/files/:asset_id" },
  { name: "discover", pattern: "/discover" },
  { name: "imports", pattern: "/discover/imports" },
  { name: "import", pattern: "/discover/imports/:run_id" },
  { name: "results", pattern: "/results" },
  { name: "result", pattern: "/results/:artifact_id" },
  { name: "start", pattern: "/start" },
  { name: "callback", pattern: "/auth/callback" },
];
