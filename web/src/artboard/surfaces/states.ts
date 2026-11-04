// States: one surface per state, each the real `states/*` component. The
// markup render draws the reference's own example (44 units mid-load, a 44-unit rates request refused as
// slow, 3 picks for a curator, the CS62 session the dialog returns to). The browser run opens the real
// app and makes the API answer the state: the studies list held (loading), refused 503 `slow` (no
// request to cut there, so its primary is Try again), 403 for the curators group, 401 twice (session
// ended), 502 (catalog error), and an empty Mine listing on Results. The "Who are the
// curators?" button is not built. Expectations come from `states/codes.ts` and the fixture.
import { createElement, type ReactElement } from "react";
import { CuratorOnly, curatorSentence } from "../../states/CuratorOnly";
import { CODE_COPY } from "../../states/codes";
import { EmptyResults, SAVE_CALL } from "../../states/EmptyResults";
import { Loading } from "../../states/Loading";
import { Refusal } from "../../states/Refusal";
import { endedSentence, returnLabel, SessionEnded } from "../../states/SessionEnded";
import { CATALOG_FIXTURE as F } from "../fixture";
import { ab, type Count, type Expectation, type Surface } from "../harness";

const REFERENCE = "10-states";
const NOOP = () => undefined;
const STUDIES = "/catalog/studies";
const UNITS = 44;
const PICKS = 3;
const CATALOG_DETAIL = "the catalog query failed: HIVE_METASTORE_ERROR (502)";
const SLOW_DETAIL = "the computation ran out of time; use fewer units or a shorter window";
const CURATORS_DETAIL = "caller is not in the curators group";

const ALLOW = [
  "section",
  "p",
  "h2",
  "div",
  "span",
  "button",
  "a",
  "details",
  "summary",
  "code",
  "pre",
  "dialog",
];

const state = (kind: string): Count => ({
  noun: `${kind} states`,
  n: 1,
  match: ab("state", { "data-ab-state": kind }),
});
const primary = (n: number): Count => ({
  noun: "primary buttons",
  n,
  match: (e) => (e.attrs.class ?? "").split(" ").includes("btn-primary"),
});
const codes = (n: number): Count => ({ noun: "small codes", n, match: ab("code") });
const folds = (n: number): Count => ({ noun: "folded details", n, match: ab("fold") });

function where(): string {
  return `${F.session.subject} · ${F.session.session}`;
}

function surface(
  id: string,
  title: string,
  route: string,
  waitFor: string,
  render: () => ReactElement,
  expect: () => Expectation,
  browser: Pick<Surface, "api" | "status" | "hold">,
): Surface {
  return {
    id,
    board: REFERENCE,
    title,
    pending: null,
    route,
    ready: "home",
    waitFor,
    viewports: ["1440x900"],
    noScroll: false,
    render,
    expect,
    ...browser,
  };
}

export const statesSurfaces: readonly Surface[] = [
  surface(
    "states-loading",
    "Loading with elapsed time",
    "/",
    '[data-testid="catalog-loading"]',
    () =>
      createElement(Loading, {
        path: "/compute/rates",
        what: `Computing responses for ${UNITS} units`,
        meanwhile:
          "The task strip and unit list are already here; the heatmap fills in when ready.",
        initialElapsedMs: 2400,
      }),
    () => ({
      allow: ALLOW,
      counts: [state("loading"), { noun: "progress bars", n: 1, match: ab("progress") }],
      texts: [`Computing responses for ${UNITS} units`, "2.4 s so far · usually 8.4 s.", "30 s"],
    }),
    { hold: [STUDIES] },
  ),
  surface(
    "states-slow",
    "Slow refusal with a smaller request",
    "/",
    '[data-testid="catalog-error"]',
    () =>
      createElement(Refusal, {
        status: 503,
        code: "slow",
        detail: SLOW_DETAIL,
        request: { units: UNITS },
        onSmaller: NOOP,
        onRetry: NOOP,
        notebookCode: `nwb = asteroidea.open_nwb("${F.session.asset_id}")`,
      }),
    () => ({
      allow: ALLOW,
      counts: [state("refused"), primary(1), codes(1), folds(1)],
      texts: [
        CODE_COPY.slow?.title ?? "",
        `Use ${UNITS / 2} units`,
        "Copy notebook code",
        "code: slow",
      ],
    }),
    {
      api: { [STUDIES]: { error: "slow", detail: SLOW_DETAIL } },
      status: { [STUDIES]: 503 },
    },
  ),
  surface(
    "states-curator",
    "Curators only",
    "/",
    '[data-ab-state="curator"]',
    () => createElement(CuratorOnly, { picks: PICKS, onExport: NOOP }),
    () => ({
      allow: ALLOW,
      counts: [state("curator"), primary(1), codes(1)],
      texts: ["Imports are started by curators", curatorSentence(PICKS), "Export pick list"],
    }),
    {
      api: { [STUDIES]: { error: "forbidden", detail: CURATORS_DETAIL } },
      status: { [STUDIES]: 403 },
    },
  ),
  surface(
    "states-ended",
    "Session ended",
    "/",
    '[data-testid="session-ended"]',
    () => createElement(SessionEnded, { open: true, onSignIn: NOOP, path: "/" }),
    () => ({
      allow: ALLOW,
      counts: [{ noun: "dialogs", n: 1, match: (e) => e.tag === "dialog" }, primary(1)],
      texts: [
        "Session ended",
        "Sign in again to carry on",
        endedSentence(returnLabel(null, "/")),
        "Sign in",
      ],
    }),
    { api: { [STUDIES]: { message: "Unauthorized" } }, status: { [STUDIES]: 401 } },
  ),
  surface(
    "states-catalog-error",
    "Catalog error",
    "/",
    '[data-testid="catalog-error"]',
    () =>
      createElement(Refusal, {
        status: 502,
        code: "upstream_failed",
        detail: CATALOG_DETAIL,
        noun: "the studies list",
        onRetry: NOOP,
      }),
    () => ({
      allow: ALLOW,
      counts: [state("error"), primary(1), codes(1), folds(1)],
      texts: [
        CODE_COPY.upstream_failed?.title ?? "",
        "We couldn't load the studies list just now.",
        "Try again",
        "Copy details for support",
        "Technical detail",
        CATALOG_DETAIL,
        "code: upstream_failed",
      ],
    }),
    {
      api: { [STUDIES]: { error: "upstream_failed", detail: CATALOG_DETAIL } },
      status: { [STUDIES]: 502 },
    },
  ),
  surface(
    "states-empty",
    "Empty results",
    "/results",
    '[data-ab-state="empty"]',
    () => createElement(EmptyResults, { example: `${where()} · you` }),
    () => ({
      allow: ALLOW,
      counts: [
        state("empty"),
        primary(1),
        { noun: "example results", n: 1, match: ab("example") },
        { noun: "save calls", n: 1, match: ab("call") },
      ],
      texts: ["No results yet", "your-result.csv", SAVE_CALL, "Set up the notebook"],
    }),
    { api: { "/artifacts": { artifacts: [] } } },
  ),
];
