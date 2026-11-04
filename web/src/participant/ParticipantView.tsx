// The participant page's pure view: the identity with its bridge, the modality
// timeline, the ephys cards, the T1w card and the regions panel. It takes a `ParticipantModel`, the
// per-session extras the page has read so far and the T1w state, and nothing else, so the artboard
// harness renders the same component over the fixture. `data-ab` marks are what the harness counts;
// `data-ab-line` is also what styles the bridge dashed (pending) or solid (confirmed).
import { filePath } from "../catalog/depth";
import { dataSessionPath, NO_STUDY } from "../router/dataPaths";
import { typeLabel } from "../session/model";
import { CopyCode } from "../snippets/CopyCode";
import type { Glyph, GlyphState, SessionExtras } from "./glyph";
import {
  type Bridge,
  type EphysSession,
  type Lane,
  layoutLane,
  type ParticipantModel,
  type RecordFact,
  type RegionLine,
  regionLines,
  type T1wState,
} from "./model";

export interface ParticipantViewProps {
  readonly model: ParticipantModel;
  /** Per ephys session id; absent while that session's extras load. */
  readonly extras: Readonly<Record<string, SessionExtras | undefined>>;
  readonly t1w: T1wState;
  /** The timeline's lane width in px, for the 6 px merge. */
  readonly laneWidth?: number;
}

export const DEFAULT_LANE_PX = 720;

function demographics(m: ParticipantModel): string {
  const sessions = m.records.reduce((n, r) => n + r.sessions, 0);
  const parts = [
    m.species,
    m.sex,
    m.age === null ? null : `age ${m.age}`,
    `${m.records.length} ${m.records.length === 1 ? "dataset" : "datasets"}`,
    `${sessions} ${sessions === 1 ? "session" : "sessions"}`,
  ];
  return parts.filter((p) => p !== null && p !== "").join(" · ");
}

function RecordBox({ r }: { r: RecordFact }) {
  const head = r.dataset;
  return (
    <article className="pp-record" data-ab="record" data-testid={`record-${r.dataset}`}>
      <p className="pp-record-id">
        <a href={r.datasetHref}>{head}</a> · {r.subject}
      </p>
      <p className="pp-record-mods">
        {r.modalities.length === 0 ? "no files listed" : r.modalities.join(", ")}
      </p>
      <p className="pp-record-meta">
        {r.sessions} {r.sessions === 1 ? "session" : "sessions"}
        {r.version === null ? "" : ` · version ${r.version}`}
      </p>
    </article>
  );
}

function bridgeSentence(b: Bridge): string {
  const who = b.linkedBy === null ? "a curator (no email recorded)" : b.linkedBy;
  const when = b.date === null ? "" : ` on ${b.date}`;
  const tail = b.confirmed
    ? "Confirmed: these records are one person."
    : "Not confirmed yet: an analysis that joins these records should say so.";
  return `Link status ${b.status}. Linked by ${who}${when}. ${tail}`;
}

function Identity({ m }: { m: ParticipantModel }) {
  const b = m.bridge;
  return (
    <section className="pp-identity" data-ab="identity" aria-label="Identity across archives">
      <div className="pp-bridge-row">
        {m.records.map((r, i) => (
          <div key={`${r.dataset}/${r.subject}`} className="pp-bridge-part">
            {i > 0 && b !== null ? (
              <div
                className="pp-bridge"
                data-ab="bridge"
                data-ab-line={b.confirmed ? "solid" : "dashed"}
                data-testid="participant-bridge"
              >
                <span className="pp-bridge-line" aria-hidden="true" />
                <span className="pp-bridge-status" data-testid="bridge-status">
                  {b.confirmed ? "confirmed" : `${b.status} review`}
                </span>
              </div>
            ) : null}
            <RecordBox r={r} />
          </div>
        ))}
      </div>
      {b === null ? (
        <p className="pp-bridge-text" data-testid="bridge-text">
          Not linked to a record in another archive.
        </p>
      ) : (
        <div className="pp-bridge-text" data-testid="bridge-text">
          <p>{bridgeSentence(b)}</p>
          <p data-testid="bridge-basis">
            Basis: {b.basis ?? "not recorded"}
            {b.note === null ? "" : ` · Curator note: ${b.note}`}
          </p>
        </div>
      )}
    </section>
  );
}

function LaneRow({ lane, width, axis }: { lane: Lane; width: number; axis: string }) {
  const blocks = layoutLane(lane.ticks, width);
  return (
    <div className="pp-lane" data-ab="lane" data-ab-lane={lane.key}>
      <span className="pp-lane-name">{lane.label}</span>
      <div className="pp-lane-track">
        <svg
          className="pp-lane-svg"
          width={width}
          height={22}
          viewBox={`0 0 ${width} 22`}
          role="img"
          aria-label={`${lane.label}: ${lane.ticks.length} recordings, ${axis}`}
        >
          <line x1={0} y1={21} x2={width} y2={21} className="pp-lane-rule" />
          {blocks.map((b) => (
            <g key={b.x} data-ab="tick" data-ab-count={String(b.count)}>
              <rect x={b.x - 1} y={3} width={3} height={14} className="pp-tick" />
              <text
                x={b.x > width - 90 ? b.x - 6 : b.x + 6}
                y={14}
                textAnchor={b.x > width - 90 ? "end" : "start"}
                className="pp-tick-label"
              >
                {b.label}
              </text>
              <title>{b.label}</title>
            </g>
          ))}
        </svg>
      </div>
    </div>
  );
}

function Timeline({ m, width }: { m: ParticipantModel; width: number }) {
  const axis =
    m.axis.kind === "time"
      ? `${m.axis.from} to ${m.axis.to}`
      : "in session order (no recording dates)";
  return (
    <section className="pp-timeline" data-ab="timeline" data-testid="participant-timeline">
      <h3>Timeline · every recording of this person</h3>
      {m.lanes.length === 0 ? (
        <p className="pp-none">No recordings listed.</p>
      ) : (
        <>
          {m.lanes.map((l) => (
            <LaneRow key={l.key} lane={l} width={width} axis={axis} />
          ))}
          <p className="pp-axis" data-testid="timeline-axis">
            {m.axis.kind === "time" ? (
              <>
                <span>{m.axis.from}</span>
                <span>{m.axis.to}</span>
              </>
            ) : (
              <span>{axis}</span>
            )}
          </p>
        </>
      )}
    </section>
  );
}

const DIV = [
  "--color-div-1",
  "--color-div-2",
  "--color-div-3",
  "--color-div-4",
  "--color-div-5",
  "--color-div-6",
  "--color-div-7",
] as const;

/** The diverging token for a z: +3 and above is the first (red), -3 and below the last (blue). */
export function zToken(z: number): string {
  const i = Math.min(6, Math.max(0, Math.round(3 - z)));
  return `var(${DIV[i] ?? DIV[3]})`;
}

function GlyphSvg({ glyph, label }: { glyph: Glyph; label: string }) {
  const h = glyph.rows.length;
  const px = Math.max(2, Math.min(6, Math.floor(72 / Math.max(1, h))));
  return (
    <div className="pp-glyph" data-ab="glyph" data-ab-rows={String(h)} data-testid="ephys-glyph">
      <svg
        className="pp-glyph-edge"
        width={4}
        height={h * px}
        viewBox={`0 0 1 ${h}`}
        aria-hidden="true"
        preserveAspectRatio="none"
      >
        {glyph.rows.map((r) => (
          <rect
            key={r.id}
            x={0}
            y={r.id}
            width={1}
            height={1}
            fill={r.cssVar}
            data-ab="glyph-row"
          />
        ))}
      </svg>
      <svg
        className="pp-glyph-cells"
        height={h * px}
        viewBox={`0 0 ${glyph.cols} ${h}`}
        preserveAspectRatio="none"
        role="img"
        aria-label={label}
      >
        {glyph.rows.flatMap((r) =>
          r.cells.map((c) => (
            <rect key={`${r.id}/${c.x}`} x={c.x} y={r.id} width={1} height={1} fill={zToken(c.z)} />
          )),
        )}
      </svg>
    </div>
  );
}

function GlyphBlock({ state, e }: { state: GlyphState | undefined; e: EphysSession }) {
  if (state === undefined || state.kind === "loading") {
    return (
      <p className="pp-glyph-wait" data-testid="glyph-loading">
        Reading the response…
      </p>
    );
  }
  if (state.kind === "none") {
    return (
      <p className="pp-none" data-ab="glyph-none" data-testid="glyph-none">
        Response preview unavailable: {state.reason}
      </p>
    );
  }
  const g = state.glyph;
  const [lo, hi] = g.window;
  return (
    <>
      <GlyphSvg
        glyph={g}
        label={`${e.label}: z-scored response of ${g.nUnits} units around ${typeLabel(g.eventType)}`}
      />
      <p className="pp-caption" data-testid="glyph-caption">
        Preview: each unit's z-scored response around {typeLabel(g.eventType)}, {lo} to {hi} s (
        {g.storedAt === null ? "computed live" : "stored summary"}).
      </p>
    </>
  );
}

function EphysCard({
  e,
  m,
  extras,
}: {
  e: EphysSession;
  m: ParticipantModel;
  extras: SessionExtras | undefined;
}) {
  const lines = extras === undefined ? [] : regionLines(extras.locations, []);
  const facts = [
    `${e.units} ${e.units === 1 ? "unit" : "units"}`,
    extras === undefined || lines.length === 0
      ? null
      : `${lines.length} ${lines.length === 1 ? "region" : "regions"}`,
    e.lfpChannels > 0
      ? `${e.lfpChannels} LFP channels${e.lfpRates.length === 0 ? "" : ` at ${e.lfpRates.join(", ")} Hz`}`
      : null,
    e.trials === null ? null : `${e.trials} trials`,
  ].filter((f) => f !== null);
  return (
    <article className="pp-card" data-ab="ephys-card" data-testid="ephys-card">
      <header>
        <h4>Ephys · {e.label}</h4>
        <span className="pp-card-where">{e.dataset}</span>
      </header>
      <p className="pp-card-facts">{facts.join(" · ")}</p>
      <GlyphBlock state={extras?.glyph} e={e} />
      <p className="pp-card-links">
        <a href={dataSessionPath(e.dataset, e.subject, e.session, m.studyKey ?? NO_STUDY)}>
          Open session →
        </a>{" "}
        <a href={filePath(e.assetId)} data-testid="ephys-file-link">
          Open file →
        </a>
      </p>
    </article>
  );
}

const PLANES = ["axial", "coronal", "sagittal"] as const;
function viewerHref(assetId: string): string {
  return `${filePath(assetId)}?tab=image`;
}

function T1wCard({ state }: { state: T1wState }) {
  if (state.kind === "none") return null;
  return (
    <article className="pp-card" data-ab="t1w-card" data-ab-state="missing" data-testid="t1w-card">
      <header>
        <h4>Anatomical MRI · T1w</h4>
        <span className="pp-card-where">NIfTI</span>
      </header>
      <div className="pp-planes">
        {PLANES.map((p) => (
          <figure key={p} className="pp-plane pp-plane-empty" data-ab="t1w-placeholder">
            <figcaption>{p} mid</figcaption>
          </figure>
        ))}
      </div>
      <p className="pp-none" data-testid="t1w-missing">
        Preview not built yet.
      </p>
      <p className="pp-card-links">
        <a href={viewerHref(state.assetId)}>Open in viewer →</a>
      </p>
    </article>
  );
}

function Regions({
  m,
  lines,
  waiting,
}: {
  m: ParticipantModel;
  lines: readonly RegionLine[];
  waiting: boolean;
}) {
  const hasChannels = lines.some((l) => l.channels !== null);
  return (
    <aside className="pp-regions" data-ab="region-panel" data-testid="participant-regions">
      <h3>Regions recorded</h3>
      {lines.length === 0 ? (
        <p className="pp-none">
          {waiting ? "Reading the units…" : "No region labels on this person's units."}
        </p>
      ) : (
        <ul>
          {lines.map((l) => (
            <li key={l.key} data-ab="region-line">
              <span
                className="region-dot"
                aria-hidden="true"
                style={{ backgroundColor: l.cssVar }}
              />
              <span className="pp-region-name">{l.label}</span>
              <span className="pp-region-n">
                {l.units} {l.units === 1 ? "unit" : "units"}
                {l.channels === null ? "" : ` · ${l.channels} ch`}
              </span>
            </li>
          ))}
        </ul>
      )}
      <p className="pp-note">
        Region names are the authors' labels.
        {hasChannels
          ? " Units are this person's; channels are the release's count at that region."
          : ""}
        {m.ephysHidden > 0 ? " Units are counted over the sessions shown." : ""}
      </p>
    </aside>
  );
}

export function ParticipantView({ model, extras, t1w, laneWidth }: ParticipantViewProps) {
  const waiting = model.ephys.some((e) => extras[e.id] === undefined);
  const lines = regionLines(
    model.ephys.flatMap((e) => extras[e.id]?.locations ?? []),
    model.releaseRegions,
  );
  const total = model.ephys.length + model.ephysHidden;
  return (
    <section className="start participant" data-testid="participant-page">
      <div className="pp-grid">
        <div className="pp-main">
          <header className="pp-head">
            <h2>{model.heading}</h2>
            <CopyCode page="participant" values={{ subject_key: model.key }} />
            <p className="pp-demo" data-testid="participant-facts">
              {demographics(model)}
            </p>
          </header>
          <Identity m={model} />
          <Timeline m={model} width={laneWidth ?? DEFAULT_LANE_PX} />
          <div className="pp-cards">
            {model.ephys.map((e) => (
              <EphysCard key={e.id} e={e} m={model} extras={extras[e.id]} />
            ))}
            <T1wCard state={t1w} />
          </div>
          {model.ephysHidden > 0 ? (
            <p className="pp-more" data-testid="ephys-more">
              {model.ephys.length} most recent of {total} ephys sessions.{" "}
              <a href={model.records[0]?.datasetHref ?? "/"}>All {total} →</a>
            </p>
          ) : null}
        </div>
        <Regions m={model} lines={lines} waiting={waiting} />
      </div>
    </section>
  );
}
