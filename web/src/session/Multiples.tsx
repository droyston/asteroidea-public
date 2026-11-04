// The session page's small multiples (the heatmap's alternative view): one PSTH per unit, the server's z
// per bin as a line, in the heatmap's order (grouped by region, sorted by response), each cell edged in
// its region's colour. Cells shrink with the unit count so 152 units fit 1440x900 with no scroll.
import type { CSSProperties } from "react";
import { type Heatmap, NULL_Z_TEXT, signed, Z_RANGE } from "./model";

const VW = 100;
const VH = 40;

/** Cell height: 64 px up to 48 units, 46 up to 96, else 34 (152 units in 13 rows of 12). */
export function cellPx(units: number): number {
  if (units <= 48) return 64;
  if (units <= 96) return 46;
  return 34;
}

function path(z: readonly (number | null)[]): string {
  const n = z.length;
  let d = "";
  let pen = false;
  for (let i = 0; i < n; i += 1) {
    const v = z[i];
    if (v === null || v === undefined || !Number.isFinite(v)) {
      pen = false;
      continue;
    }
    const x = ((i + 0.5) / n) * VW;
    const c = Math.max(-Z_RANGE, Math.min(Z_RANGE, v));
    const y = VH / 2 - (c / Z_RANGE) * (VH / 2 - 1);
    d += `${pen ? "L" : "M"}${x.toFixed(2)} ${y.toFixed(2)}`;
    pen = true;
  }
  return d;
}

export function Multiples({ heat, screened }: { heat: Heatmap; screened: ReadonlySet<number> }) {
  const first = heat.edges[0] ?? 0;
  const last = heat.edges[heat.edges.length - 1] ?? 1;
  const zeroX = last === first ? 0 : (VW * (0 - first)) / (last - first);
  const h = cellPx(heat.units);
  return (
    <div
      className="sh-multiples"
      data-testid="session-multiples"
      style={{ "--sh-cell": `${h}px` } as CSSProperties}
    >
      {heat.groups.flatMap((g) =>
        g.rows.map((r) => (
          <figure
            key={r.unit}
            className="sh-multiple"
            data-ab="multiple"
            data-unit={r.unit}
            style={{ "--sh-region": g.cssVar } as CSSProperties}
          >
            <figcaption>
              <span>
                {r.unit}
                {screened.has(r.unit) ? " ●" : ""}
              </span>
              <span>{r.delta === null ? "—" : signed(r.delta)}</span>
            </figcaption>
            {r.z === null ? (
              <div className="sh-multiple-null">{NULL_Z_TEXT}</div>
            ) : (
              <svg
                viewBox={`0 0 ${VW} ${VH}`}
                preserveAspectRatio="none"
                role="img"
                aria-label={`unit ${r.unit}, ${r.location}: z per bin`}
              >
                <line x1={0} y1={VH / 2} x2={VW} y2={VH / 2} className="sh-zero" />
                <line x1={zeroX} y1={0} x2={zeroX} y2={VH} className="sh-multiple-onset" />
                <path d={path(r.z)} className={`sh-psth sh-psth-${r.direction}`} />
              </svg>
            )}
          </figure>
        )),
      )}
    </div>
  );
}
