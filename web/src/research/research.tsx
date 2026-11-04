// The research pages (the `research` chunk): the file page. The dataset page is the study page
// (`study/`) and a subject is the participant page (`participant/`); `/data/:dataset/:subject`
// redirects there.
import type { AuthStore } from "../auth/store";
import { FilePage } from "./FilePage";

export interface ResearchRoute {
  name: "file";
  params: Readonly<Record<string, string>>;
}

export function ResearchPage({ auth, route }: { auth: AuthStore; route: ResearchRoute }) {
  const p = route.params;
  return <FilePage key={p.asset_id} auth={auth} assetId={p.asset_id ?? ""} />;
}
