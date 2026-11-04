// The min/max trace: the server decimated the window to alternating minimum and maximum
// samples; the web draws them as one line, in order, and does nothing else to them.
import type { Domain } from "./axis";
import type { Box } from "./heatmap";

/** The value domain of `values`, padded by 5 %; [-1, 1] when there are none. */
export function traceDomain(values: readonly number[]): Domain {
  let lo = Number.POSITIVE_INFINITY;
  let hi = Number.NEGATIVE_INFINITY;
  for (const v of values) {
    if (!Number.isFinite(v)) continue;
    lo = Math.min(lo, v);
    hi = Math.max(hi, v);
  }
  if (!Number.isFinite(lo)) return [-1, 1];
  if (hi <= lo) return [lo - 0.5, hi + 0.5];
  const pad = (hi - lo) * 0.05;
  return [lo - pad, hi + pad];
}

/** Stroke `values` against `times` into `box`; `xs` and `ys` map data to pixels of the canvas. */
export function drawTrace(
  ctx: CanvasRenderingContext2D,
  times: readonly number[],
  values: readonly number[],
  xs: (v: number) => number,
  ys: (v: number) => number,
  box: Box,
  colour: string,
): void {
  ctx.save();
  ctx.beginPath();
  ctx.rect(box.x, box.y, box.w, box.h);
  ctx.clip();
  ctx.strokeStyle = colour;
  ctx.lineWidth = 1;
  ctx.beginPath();
  let pen = false;
  for (let i = 0; i < values.length; i += 1) {
    const v = values[i];
    const t = times[i];
    if (v === undefined || t === undefined || !Number.isFinite(v)) {
      pen = false;
      continue;
    }
    if (pen) ctx.lineTo(xs(t), ys(v));
    else ctx.moveTo(xs(t), ys(v));
    pen = true;
  }
  ctx.stroke();
  ctx.restore();
}
