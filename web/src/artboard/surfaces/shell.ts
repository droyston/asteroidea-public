// The shell: the real `Shell`, `Nav`, `SpineBar` and `userLabel` at the
// session depth of the fixture (Studies / Movie watching / sub-CS62 / ses-2), signed in with an ID token
// whose `email` claim names the user. `App.tsx :: Workspace` itself needs a browser (its stores have
// no server snapshot) and does not export its `NAV`, so the four places are the reference's; the browser
// run (`make web-artboards`) opens the real App and checks the same four links there.
import { createElement } from "react";
import { Nav } from "../../kit/Nav";
import { Shell } from "../../kit/Shell";
import type { SpineCrumb } from "../../shell/marks";
import { SpineBar } from "../../shell/Spine";
import { userLabel } from "../../shell/user";
import { CATALOG_FIXTURE as F } from "../fixture";
import { type El, type Expectation, type Surface, within } from "../harness";

export const PLACES = ["Studies", "Find", "Results", "Setup"] as const;
const PLACE_PATHS = ["/", "/discover", "/results", "/start"] as const;

export const USER_EMAIL = "ada@example.test";

function b64url(s: string): string {
  return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

/** A fixed, unsigned ID token carrying the `email` claim (display only, as in the app). */
export const ID_TOKEN = `${b64url('{"alg":"none"}')}.${b64url(JSON.stringify({ email: USER_EMAIL }))}.x`;

function studyTitle(): string {
  return F.studies.studies.find((s) => s.study === F.session.study)?.title ?? F.session.study;
}

export function spineTrail(): SpineCrumb[] {
  const s = F.session;
  return [
    { label: "Studies", to: "/" },
    { label: studyTitle(), to: `/s/${s.study}` },
    { label: s.subject, to: `/data/${s.dataset}/${s.subject}` },
    { label: s.session },
  ];
}

const inMainNav = (e: El) => e.tag === "nav" && e.attrs["aria-label"] === "Main";
const inSpine = (e: El) => e.tag === "nav" && e.attrs["aria-label"] === "Breadcrumb";

export const shell: Surface = {
  id: "shell",
  board: "01-shell-navigation",
  title: "Shell and navigation",
  pending: null,
  route: `/s/${F.session.study}`,
  ready: "study",
  viewports: ["1440x900", "375x812"],
  noScroll: false,
  render: () =>
    createElement(
      Shell,
      {
        title: "asteroidea",
        nav: createElement(Nav, {
          items: PLACES.map((label, i) => ({
            to: PLACE_PATHS[i] ?? "/",
            label,
            current: label === "Studies",
          })),
        }),
        spine: createElement(SpineBar, { trail: spineTrail() }),
        auth: createElement(
          "span",
          { className: "shell-user", "data-testid": "user-label" },
          userLabel(ID_TOKEN, null),
        ),
        ready: "session",
      },
      createElement(
        "section",
        { "data-ab-standin": "routed view (its own surface)" },
        "Routed view: drawn by its own surface",
      ),
    ),
  expect: (): Expectation => {
    const trail = spineTrail();
    return {
      allow: ["div", "header", "h1", "nav", "a", "span", "main", "section", "button"],
      counts: [
        {
          noun: "top-bar places",
          n: PLACES.length,
          match: (e) => e.tag === "a" && within(e, inMainNav),
        },
        {
          noun: "current top-bar places",
          n: 1,
          match: (e) => e.tag === "a" && e.attrs["aria-current"] === "page" && within(e, inMainNav),
        },
        {
          noun: "spine links",
          n: trail.length - 1,
          match: (e) => e.tag === "a" && within(e, inSpine),
        },
        {
          noun: "current spine crumbs",
          n: 1,
          match: (e) => e.attrs["aria-current"] === "page" && within(e, inSpine),
        },
        { noun: "user labels", n: 1, match: (e) => e.attrs["data-testid"] === "user-label" },
        { noun: "ready marks", n: 1, match: (e) => e.tag === "main" && "data-ready" in e.attrs },
      ],
      texts: [...PLACES, ...trail.map((c) => c.label), USER_EMAIL],
    };
  },
};
