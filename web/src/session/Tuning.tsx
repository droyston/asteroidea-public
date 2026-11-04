// The session page's exploratory tuning screen: a ● on each unit row that passed the
// server's screen, and a rail section that sums the marks per region group. Every number is the
// server's (`screened`, the levels and their counts, alpha); the page only counts marks per group.
import type { CSSProperties } from "react";
import type { Group, HeatRow, TuningMarks, TuningState } from "./model";

const MARK_W = 10;

/** One column of ● beside a group's rows, one per unit that passed the screen. */
export function MarkColumn({
  rows,
  px,
  marks,
}: {
  rows: readonly HeatRow[];
  px: number;
  marks: TuningMarks | null;
}) {
  const h = rows.length * px;
  const r = Math.max(1.5, Math.min(3, px / 2 - 0.5));
  return (
    <svg className="sh-marks" width={MARK_W} height={h} aria-hidden="true">
      {marks === null
        ? null
        : rows.map((row, i) =>
            marks.screened.has(row.unit) ? (
              <circle
                key={row.unit}
                data-ab="tuning-mark"
                data-unit={row.unit}
                cx={MARK_W / 2}
                cy={i * px + px / 2}
                r={r}
              />
            ) : null,
          )}
    </svg>
  );
}

function levelText(m: TuningMarks): string {
  return m.levels.map((l) => `${l.replaceAll("_", " ")} (${m.counts[l] ?? 0})`).join(" and ");
}

/** The rail's tuning section: the label, the question, and per group its passed units of all. */
export function TuningSection({
  groups,
  tuning,
}: {
  groups: readonly Group[];
  tuning: TuningState;
}) {
  return (
    <section className="sh-tuning" data-ab="tuning-summary" aria-label="Tuning screen">
      <header>
        <small className="sh-kicker">
          Tuning to{" "}
          {tuning.kind === "marks" ? tuning.marks.attribute.replaceAll("_", " ") : "an attribute"}
        </small>
        <span className="sh-exploratory">Exploratory</span>
      </header>
      {tuning.kind === "loading" ? (
        <p className="sh-default" data-testid="tuning-loading">
          Running the exploratory screen…
        </p>
      ) : tuning.kind === "none" ? (
        <p className="sh-default" data-testid="tuning-none">
          {tuning.reason}
        </p>
      ) : (
        <>
          <p className="sh-default">
            Does the response differ between {levelText(tuning.marks)}? ● = passed the screen
            {tuning.marks.alpha === null ? "" : `, q < ${tuning.marks.alpha}`}; not a confirmed
            result.
          </p>
          <ul className="sh-tuning-groups">
            {groups.map((g) => {
              const passed = g.rows.filter((r) => tuning.marks.screened.has(r.unit)).length;
              return (
                <li
                  key={g.key}
                  data-ab="tuning-region"
                  data-region={g.key}
                  style={{ "--sh-region": g.cssVar } as CSSProperties}
                >
                  <span className="sh-tuning-name">{g.label}</span>
                  <span className="sh-dots" aria-hidden="true">
                    {g.rows.map((r) => (
                      <i
                        key={r.unit}
                        className={
                          tuning.marks.screened.has(r.unit) ? "sh-dot sh-dot-on" : "sh-dot"
                        }
                      />
                    ))}
                  </span>
                  <span className="sh-tuning-count">
                    {passed}/{g.rows.length}
                  </span>
                </li>
              );
            })}
          </ul>
          {tuning.marks.storedAt === null ? null : (
            <p className="sh-default" data-testid="tuning-from-summary">
              From the stored summary built {tuning.marks.storedAt}.
            </p>
          )}
        </>
      )}
    </section>
  );
}
