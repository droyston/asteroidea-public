// The participant page (the `participant` chunk): `/s/:study/p/:participant`.
// The loader reads the person (studies, links, session start times) and `ParticipantView` draws. The page
// shows at once; each ephys card reads its own stored summary (or computes live) and fills its glyph, and
// the T1w card stands "not built" (the preview route is deferred). The page is ready
// (`data-ready="participant"`) once every card has its answer, a refusal included.
import "./participant.css";
import { type ReactNode, useCallback, useEffect, useRef, useState } from "react";
import type { AuthStore } from "../auth/store";
import { doFetch } from "../catalog/loadable";
import { Notice } from "../kit/Notice";
import { Ready, Spine, type SpineCrumb } from "../shell/marks";
import { LoadState } from "../states/LoadState";
import { useLoad } from "../useLoad";
import { loadSessionExtras, type SessionExtras } from "./glyph";
import { loadParticipant } from "./load";
import { type ParticipantModel, t1wState } from "./model";
import { DEFAULT_LANE_PX, ParticipantView } from "./ParticipantView";

function trailOf(m: ParticipantModel): SpineCrumb[] {
  const studies = { label: "Studies", to: "/" };
  const first = m.records[0]?.dataset ?? "";
  if (m.studyKey !== null) {
    return [
      studies,
      { label: m.studyTitle ?? m.studyKey, to: `/s/${encodeURIComponent(m.studyKey)}` },
      { label: m.heading },
    ];
  }
  return [studies, { label: first, to: `/d/${encodeURIComponent(first)}` }, { label: m.heading }];
}

/** The width of the first timeline lane's track, kept current as the window resizes. */
function useLaneWidth(root: React.RefObject<HTMLDivElement | null>): number {
  const [width, setWidth] = useState(DEFAULT_LANE_PX);
  useEffect(() => {
    const el = root.current;
    if (el === null) return;
    const measure = () => {
      const track = el.querySelector(".pp-lane-track");
      const w = track instanceof HTMLElement ? Math.round(track.clientWidth) : 0;
      if (w > 0) setWidth(w);
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    return () => observer.disconnect();
  }, [root]);
  return width;
}

function Loaded({ auth, model }: { auth: AuthStore; model: ParticipantModel }) {
  const [extras, setExtras] = useState<Record<string, SessionExtras | undefined>>({});
  const root = useRef<HTMLDivElement>(null);
  const laneWidth = useLaneWidth(root);
  useEffect(() => {
    let live = true;
    for (const e of model.ephys) {
      void loadSessionExtras(auth.config, { ...e, session: e.session }, auth, doFetch).then((x) => {
        if (live) setExtras((prev) => ({ ...prev, [e.id]: x }));
      });
    }
    return () => {
      live = false;
    };
  }, [auth, model]);
  // Ready once every card has its glyph (or the reason it has none) and the T1w card has its answer.
  const settled = model.ephys.every((e) => extras[e.id] !== undefined);
  return (
    <div ref={root}>
      {settled ? <Ready /> : null}
      <Spine trail={trailOf(model)} />
      <ParticipantView
        model={model}
        extras={extras}
        t1w={t1wState(model.t1w)}
        laneWidth={laneWidth}
      />
    </div>
  );
}

export function ParticipantPage({
  auth,
  participant,
}: {
  auth: AuthStore;
  participant: string;
}): ReactNode {
  const run = useCallback(
    () => loadParticipant(auth.config, participant, auth, doFetch),
    [auth, participant],
  );
  const { state, reload } = useLoad(auth, run);
  return (
    <LoadState state={state} noun="the participant" path="/catalog/studies" onRetry={reload}>
      {({ model }) =>
        model === null ? (
          <section className="start participant">
            <Ready />
            <Notice kind="info" testId="participant-missing">
              There is no participant {participant}.
            </Notice>
            <p>
              <a href="/">Back to Studies</a>
            </p>
          </section>
        ) : (
          <Loaded auth={auth} model={model} />
        )
      }
    </LoadState>
  );
}
