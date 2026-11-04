// The spike raster: one short vertical tick per spike, one row per trial, drawn as a single SVG
// path so thousands of spikes stay one DOM node. The server aligned the spikes; the web only places them.

/** The path of the ticks: spike `i` at x = `xs(t[i])` in row `trial[i]` of `rows` over `height` pixels. */
export function rasterPath(
  trial: readonly number[],
  t: readonly number[],
  rows: number,
  xs: (v: number) => number,
  height: number,
): string {
  if (rows < 1) return "";
  const rowH = height / rows;
  const tick = Math.max(1, Math.min(rowH * 0.8, 12));
  let d = "";
  for (let i = 0; i < t.length; i += 1) {
    const row = trial[i] ?? 0;
    const y = row * rowH + (rowH - tick) / 2;
    d += `M${xs(t[i] ?? 0).toFixed(2)} ${y.toFixed(2)}v${tick.toFixed(2)}`;
  }
  return d;
}
