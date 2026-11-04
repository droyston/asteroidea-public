// The sideways strip: above `STRIP_ABOVE` participants the matrix
// turns on its side. One lane per modality, one column per participant, a few pixels wide (one pixel
// from 150 participants up), so 608 participants fit in one row of the page. A column is a link to the
// participant and carries a hover title naming it. The link lane keeps the pending/confirmed
// distinction: a confirmed link is a filled mark, a pending one an outlined, dashed mark.
import type { Release, Row } from "./model";

const LABEL_W = 84;
const LANE_H = 14;
const LANE_GAP = 3;

/** Pixels per participant column: wide enough to hit while few, one pixel when hundreds. */
export function stripPx(participants: number): number {
  if (participants <= 60) return 6;
  if (participants <= 150) return 3;
  return 1;
}

function titleOf(row: Row): string {
  const have = row.cells.filter((c) => c.present).map((c) => `${c.column} ${c.text}`);
  const link = row.link === null ? "" : `; link ${row.link.status}`;
  return `${row.subject}: ${have.length === 0 ? "no modality" : have.join(", ")}${link}`;
}

export function Strip({ release }: { release: Release }) {
  const n = release.rows.length;
  const px = stripPx(n);
  const hasLinks = release.rows.some((r) => r.link !== null);
  const lanes = [
    ...release.columns.map((c) => ({ key: c.key, label: c.label })),
    ...(hasLinks ? [{ key: "link", label: "Same person" }] : []),
  ];
  const height = lanes.length * (LANE_H + LANE_GAP);
  const width = LABEL_W + n * px;
  const laneY = (i: number): number => i * (LANE_H + LANE_GAP);
  const confirmed = release.rows.filter((r) => r.link?.confirmed === true).length;
  const pending = release.rows.filter((r) => r.link !== null && !r.link.confirmed).length;
  return (
    <div className="study-strip-wrap">
      <svg
        className="study-strip"
        data-ab="strip"
        data-testid="study-strip"
        width={width}
        height={height}
        viewBox={`0 0 ${width} ${height}`}
      >
        <title>{`Participants of ${release.dataset}, one column each`}</title>
        {lanes.map((l, i) => (
          <g key={l.key}>
            <text className="strip-label" x={0} y={laneY(i) + LANE_H - 3}>
              {l.label}
            </text>
            <line
              className="strip-track"
              x1={LABEL_W}
              x2={width}
              y1={laneY(i) + LANE_H / 2}
              y2={laneY(i) + LANE_H / 2}
            />
          </g>
        ))}
        {release.rows.map((r, j) => {
          const x = LABEL_W + j * px;
          return (
            <a key={r.subject} href={r.href} data-ab="participant-row">
              <title>{titleOf(r)}</title>
              <rect className="strip-hit" x={x} y={0} width={px} height={height} />
              {r.cells.map((c) => {
                if (!c.present) return null;
                const i = lanes.findIndex((l) => l.key === c.column);
                return (
                  <rect
                    key={c.column}
                    className="strip-on"
                    data-ab="cell"
                    data-present="yes"
                    x={x}
                    y={laneY(i)}
                    width={px}
                    height={LANE_H}
                  />
                );
              })}
              {r.link === null ? null : (
                <rect
                  className={
                    r.link.confirmed
                      ? "strip-link strip-link-solid"
                      : "strip-link strip-link-dashed"
                  }
                  data-ab="link-cell"
                  data-ab-line={r.link.confirmed ? "solid" : "dashed"}
                  x={x + 0.5}
                  y={laneY(lanes.length - 1) + 0.5}
                  width={Math.max(px - 1, 0.5)}
                  height={LANE_H - 1}
                />
              )}
            </a>
          );
        })}
      </svg>
      <p className="study-strip-note" data-testid="strip-note">
        {`${n} participants, one column each; hover a column to name it, press it to open the participant.${
          hasLinks
            ? ` Same person: ${confirmed} confirmed (filled), ${pending} pending (outlined, dashed).`
            : ""
        }`}
      </p>
    </div>
  );
}
