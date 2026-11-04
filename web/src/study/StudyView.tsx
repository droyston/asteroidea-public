// The study page's pure view: the generated paragraph, each release with its
// licence and full version, the participant by modality matrix split by release with the link column,
// and readiness. It takes a `StudyModel` and nothing else, so the artboard harness renders the same
// component over the fixture. `data-ab` marks are what the harness counts; `data-ab-line` is also what
// styles a link dashed (pending) or solid (confirmed).
//
// Above 20 participants a release's matrix is a strip (Strip.tsx); "How the recordings relate" counts
// release to participants to sessions to recording groups and opens the Source files drawer.
import { regionColour } from "../kit/regions";
import { CopyCode } from "../snippets/CopyCode";
import { formatBytes } from "../table/facets";
import type { LinkCell, Readiness, Release, Row, SessionCell, StudyModel } from "./model";
import { Strip } from "./Strip";

function Paragraph({ model }: { model: StudyModel }) {
  return (
    <p className="study-paragraph" data-testid="study-paragraph">
      {model.paragraph.map((seg) =>
        seg.strong === true ? (
          <strong key={seg.text}>{seg.text}</strong>
        ) : (
          <span key={seg.text}>{seg.text}</span>
        ),
      )}
    </p>
  );
}

function Meta({ model }: { model: StudyModel }) {
  const licences = [
    ...new Set(model.releases.flatMap((r) => (r.license === null ? [] : [r.license]))),
  ];
  const items: { label: string; value: string }[] = [
    { label: "Licence", value: licences.join(" · ") },
    { label: "DOI", value: model.doi ?? "" },
    { label: "Authors", value: model.authors ?? "" },
    { label: "Citation", value: model.citation ?? "" },
  ].filter((i) => i.value !== "");
  return (
    <ul className="study-meta" data-testid="study-meta">
      {items.map((i) => (
        <li key={i.label}>
          <span className="study-meta-label">{i.label}</span> {i.value}
        </li>
      ))}
    </ul>
  );
}

function SessionLink({ cell }: { cell: SessionCell }) {
  if (cell.href === null) {
    return (
      <span className="session-cell" data-ab="session-cell" data-ab-to="none">
        {cell.label}
      </span>
    );
  }
  return (
    <a
      className="session-cell"
      href={cell.href}
      data-ab="session-cell"
      data-ab-to={cell.to}
      title={cell.to === "session" ? "Open the session" : "Open the file"}
    >
      {cell.label}
    </a>
  );
}

function LinkColumn({ link }: { link: LinkCell }) {
  return (
    <td
      className="study-link"
      data-ab="link-cell"
      data-ab-line={link.confirmed ? "solid" : "dashed"}
      data-testid="link-cell"
    >
      <span className="link-status">{link.status}</span>{" "}
      {link.counterparts.length === 0 ? null : <span>{link.counterparts.join(", ")} · </span>}
      <span>linked by {link.linkedBy ?? "curator"}</span>
      {link.date === null ? null : <span>, {link.date}</span>}
    </td>
  );
}

function ParticipantRow({ row }: { row: Row }) {
  return (
    <tr data-ab="participant-row">
      <th scope="row" className="what">
        <a href={row.href}>{row.subject}</a>
        {row.species === null ? null : <small className="study-species">{row.species}</small>}
      </th>
      {row.cells.map((c) => (
        <td key={c.column} className="num" data-ab="cell" data-present={c.present ? "yes" : "no"}>
          <span className={c.present ? "cell-on" : "cell-off"}>{c.text}</span>
        </td>
      ))}
      <td className="study-sessions">
        {row.sessions.length === 0 ? (
          <span className="study-none">no sessions</span>
        ) : (
          row.sessions.map((s) => <SessionLink key={`${s.label}/${s.href}`} cell={s} />)
        )}
      </td>
      {row.link === null ? <td className="study-none">–</td> : <LinkColumn link={row.link} />}
    </tr>
  );
}

function ReleaseSection({ release }: { release: Release }) {
  const facts = [
    release.archive,
    release.version === null ? null : `version ${release.version}`,
    release.license === null ? null : `Licence ${release.license}`,
    release.doi === null ? null : `DOI ${release.doi}`,
    release.files === null ? null : `${release.files} files`,
    release.bytes === null ? null : formatBytes(release.bytes),
  ].filter((f) => f !== null);
  return (
    <section
      className="study-release"
      data-ab="release"
      data-testid={`release-${release.dataset}`}
      aria-label={`Release ${release.dataset}`}
    >
      <header>
        <h3>
          <a href={release.href}>{release.dataset}</a>
        </h3>
        {release.role === null ? null : <span className="study-role">{release.role}</span>}
        {release.title === null ? null : <p className="study-release-title">{release.title}</p>}
        <p className="study-release-meta" data-testid="release-meta">
          {facts.join(" · ")}
        </p>
      </header>
      {release.rows.length === 0 ? (
        <p className="table-empty">This release lists no participants.</p>
      ) : release.strip ? (
        <Strip release={release} />
      ) : (
        <div className="table-wrap">
          <table className="table study-matrix" aria-label={`Participants of ${release.dataset}`}>
            <thead>
              <tr>
                <th scope="col">Participant</th>
                {release.columns.map((c) => (
                  <th key={c.key} scope="col" className="num" data-ab="matrix-col">
                    {c.label}
                  </th>
                ))}
                <th scope="col">Sessions</th>
                <th scope="col">Same person across releases</th>
              </tr>
            </thead>
            <tbody>
              {release.rows.map((r) => (
                <ParticipantRow key={r.subject} row={r} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function plural(n: number, one: string, many: string = `${one}s`): string {
  return `${n} ${n === 1 ? one : many}`;
}

/**
 * How the recordings relate: per release, release to participants to sessions to recording groups (a
 * participant's files of one modality), with the release's files and size. Thousands of files show as
 * these few numbers; the files themselves are one button away, in the Source files drawer.
 */
function RelateSection({
  model,
  onOpenSources,
}: {
  model: StudyModel;
  onOpenSources: (dataset: string) => void;
}) {
  return (
    <section className="study-relate" data-ab="relate" aria-label="How the recordings relate">
      <h3>How the recordings relate</h3>
      <ul className="study-relate-list">
        {model.releases.map((r) => {
          const x = r.relate;
          const size = [
            x.files === null ? null : plural(x.files, "file"),
            x.bytes === null ? null : formatBytes(x.bytes),
          ].filter((p) => p !== null);
          return (
            <li key={r.dataset} data-ab="relate-row" data-testid={`relate-${r.dataset}`}>
              <div className="study-chain">
                <strong>{r.dataset}</strong>
                <span className="study-arrow" aria-hidden="true">
                  →
                </span>
                <span>{plural(x.participants, "participant")}</span>
                <span className="study-arrow" aria-hidden="true">
                  →
                </span>
                <span>{plural(x.sessions, "session")}</span>
                <span className="study-arrow" aria-hidden="true">
                  →
                </span>
                <span>{plural(x.recordings, "recording group")}</span>
              </div>
              <p className="study-relate-meta">
                {[
                  ...x.byModality.filter((m) => m.n > 0).map((m) => `${m.label} ${m.n}`),
                  ...(size.length === 0 ? [] : [size.join(", ")]),
                ].join(" · ")}
              </p>
              <button
                type="button"
                className="study-sources-open"
                data-ab="sources-button"
                onClick={() => onOpenSources(r.dataset)}
              >
                Source files
              </button>
            </li>
          );
        })}
      </ul>
    </section>
  );
}

function ReadinessSection({ r }: { r: Readiness }) {
  return (
    <section className="study-readiness" data-ab="readiness" aria-label="Readiness for analysis">
      <h3>Readiness for analysis</h3>
      <table className="table" aria-label="Readiness">
        <tbody>
          <tr>
            <th scope="row">Trials</th>
            <td>
              {r.trials === null
                ? "not reported"
                : `${r.trials.total} across ${r.trials.participants} participants`}
            </td>
          </tr>
          <tr>
            <th scope="row">Units</th>
            <td>
              {r.units === null
                ? "none sorted"
                : `${r.units.total} sorted in ${r.units.regions} regions`}
            </td>
          </tr>
          <tr>
            <th scope="row">LFP</th>
            <td>
              {r.lfp === null
                ? "no LFP series listed"
                : `${r.lfp.channels} channels in ${r.lfp.series} series${
                    r.lfp.rates.length === 0 ? "" : ` at ${r.lfp.rates.join(", ")} Hz`
                  }`}
            </td>
          </tr>
          <tr>
            <th scope="row">Regions</th>
            <td>
              {r.regions.length === 0 ? (
                "no region labels"
              ) : (
                <ul className="study-regions">
                  {r.regions.map((g) => (
                    <li key={g.region}>
                      <span
                        className="region-dot"
                        aria-hidden="true"
                        style={{ backgroundColor: regionColour(g.region).cssVar }}
                      />
                      {g.region}{" "}
                      <small>
                        {g.units} units, {g.channels} channels
                      </small>
                    </li>
                  ))}
                </ul>
              )}
            </td>
          </tr>
        </tbody>
      </table>
    </section>
  );
}

export function StudyView({
  model,
  onOpenSources = () => undefined,
}: {
  model: StudyModel;
  /** Opens the Source files drawer of a release; the page wires it, the artboard leaves it. */
  onOpenSources?: (dataset: string) => void;
}) {
  return (
    <section className="start study" data-testid="study-page" data-mode={model.mode}>
      <header className="study-head">
        <h2>{model.title}</h2>
        {model.mode === "study" ? <CopyCode page="study" values={{ study: model.slug }} /> : null}
        {model.mode === "dataset" ? (
          <p className="study-part" data-testid="study-part">
            {model.partOf === null ? (
              "This dataset is in no study."
            ) : (
              <>
                Part of the study{" "}
                <a href={`/s/${encodeURIComponent(model.partOf.study)}`}>{model.partOf.title}</a>.
              </>
            )}
          </p>
        ) : null}
        <Paragraph model={model} />
        <Meta model={model} />
      </header>
      {model.releases.map((r) => (
        <ReleaseSection key={r.dataset} release={r} />
      ))}
      <div className="study-foot">
        <RelateSection model={model} onOpenSources={onOpenSources} />
        <ReadinessSection r={model.readiness} />
      </div>
    </section>
  );
}
