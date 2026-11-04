// The route-to-loader map: each registered route and the page that draws it. Until its page exists, a new path points at today's page
// (the session path at the session heatmap, `/uploads` at UploadsPanel). `/` draws the home; `/s/:study` and `/d/:dataset` draw the study page; `/s/:study/p/:participant` draws the participant page. Discover is not here:
// the app keeps it mounted and hidden so its filters survive a visit elsewhere.
import type { ReactNode } from "react";
import type { AuthStore } from "../auth/store";
import { UploadsPanel } from "../auth/UploadsPanel";
import { HomeLoader } from "../home/HomeLoader";
import { ParticipantLoader } from "../participant/ParticipantLoader";
import { ResearchLoader } from "../research/ResearchLoader";
import { ResultsLoader } from "../results/ResultsLoader";
import type { Match } from "../router/router";
import type { RouteName } from "../router/routes";
import { SessionLoader } from "../session/SessionLoader";
import { GetStartedPage } from "../start/GetStarted";
import { StudyLoader } from "../study/StudyLoader";
import { Settled } from "./marks";

type View = (match: Match, auth: AuthStore) => ReactNode;

function research(name: "file"): View {
  return (match, auth) => <ResearchLoader auth={auth} route={{ name, params: match.params }} />;
}

const VIEWS: Partial<Record<RouteName, View>> = {
  data: (_match, auth) => <HomeLoader auth={auth} />,
  study: (match, auth) => (
    <StudyLoader auth={auth} target={{ kind: "study", study: match.params.study ?? "" }} />
  ),
  dataset: (match, auth) => (
    <StudyLoader auth={auth} target={{ kind: "dataset", dataset: match.params.dataset ?? "" }} />
  ),
  participant: (match, auth) => (
    <ParticipantLoader
      auth={auth}
      participant={match.params.participant ?? ""}
      study={match.params.study ?? ""}
    />
  ),
  file: research("file"),
  session: (match, auth) => (
    <SessionLoader
      auth={auth}
      route={{
        study: match.params.study ?? "",
        dataset: match.params.dataset ?? "",
        subject: match.params.subject ?? "",
        session: match.params.session ?? "",
      }}
    />
  ),
  uploads: (_match, auth) => (
    <Settled>
      <UploadsPanel auth={auth} />
    </Settled>
  ),
  results: (_match, auth) => (
    <Settled>
      <ResultsLoader auth={auth} route={{ name: "results" }} />
    </Settled>
  ),
  result: (match, auth) => (
    <Settled>
      <ResultsLoader
        auth={auth}
        route={{ name: "result", artifact_id: match.params.artifact_id ?? "" }}
      />
    </Settled>
  ),
  start: (_match, auth) => (
    <Settled>
      <GetStartedPage auth={auth} />
    </Settled>
  ),
};

/** The page for a matched route, or null when the route draws elsewhere (Find and its Imports tab, the callback). */
export function routedView(match: Match, auth: AuthStore): ReactNode {
  return VIEWS[match.name]?.(match, auth) ?? null;
}

/** The value of `data-ready` for a view: the route name, with the renamed places by their label (both study routes are `study`). */
export function readyName(view: RouteName | "not-found"): string {
  switch (view) {
    case "data":
    case "callback":
      return "home";
    case "discover":
      return "find";
    case "start":
      return "setup";
    case "dataset":
      return "study";
    default:
      return view;
  }
}
