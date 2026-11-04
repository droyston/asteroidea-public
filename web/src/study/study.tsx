// The study page (the `study` chunk): `/s/:study`, and `/d/:dataset` in one-dataset
// mode for a dataset in no study. The loader reads the studies and links routes; `StudyView` draws.
// The page sets `data-ready="study"` once the model has drawn.
import "./study.css";
import { useCallback, useState } from "react";
import type { AuthStore } from "../auth/store";
import { doFetch, QueryTime } from "../catalog/loadable";
import { Notice } from "../kit/Notice";
import { Ready, Spine, type SpineCrumb } from "../shell/marks";
import { LoadState } from "../states/LoadState";
import { useLoad } from "../useLoad";
import { loadStudy } from "./load";
import type { StudyModel, Target } from "./model";
import { SourcesDrawer } from "./SourcesDrawer";
import { StudyView } from "./StudyView";

function trailOf(model: StudyModel): SpineCrumb[] {
  const studies = { label: "Studies", to: "/" };
  if (model.mode === "study") return [studies, { label: model.title }];
  if (model.partOf === null) return [studies, { label: model.slug }];
  return [
    studies,
    { label: model.partOf.title, to: `/s/${encodeURIComponent(model.partOf.study)}` },
    { label: model.slug },
  ];
}

export function StudyPage({ auth, target }: { auth: AuthStore; target: Target }) {
  const id = target.kind === "study" ? target.study : target.dataset;
  const kind = target.kind;
  const run = useCallback(
    () =>
      loadStudy(
        auth.config,
        kind === "study" ? { kind, study: id } : { kind, dataset: id },
        auth,
        doFetch,
      ),
    [auth, kind, id],
  );
  const { state, reload } = useLoad(auth, run);
  const [sources, setSources] = useState<string | null>(null);
  return (
    <LoadState state={state} noun="the study" path="/catalog/studies" onRetry={reload}>
      {({ model, queryMs }) =>
        model === null ? (
          <section className="start study">
            <Ready />
            <Notice kind="info" testId="study-missing">
              There is no {kind === "study" ? "study" : "dataset"} {id}.
            </Notice>
            <p>
              <a href="/">Back to Studies</a>
            </p>
          </section>
        ) : (
          <>
            <Ready />
            <Spine trail={trailOf(model)} />
            <StudyView model={model} onOpenSources={setSources} />
            <SourcesDrawer
              auth={auth}
              dataset={sources}
              files={model.releases.find((r) => r.dataset === sources)?.relate.files ?? null}
              onClose={() => setSources(null)}
            />
            <p className="meta study-meta-foot">
              <QueryTime ms={queryMs} />
            </p>
          </>
        )
      }
    </LoadState>
  );
}
