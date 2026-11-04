// The heatmap: the web only colours the values the server computed. A `Grid` is nx
// columns by ny rows (row 0 is the lowest frequency); `renderHeatmap` maps each value to a colour on a
// ramp whose stops come from the CSS tokens `--color-ramp-1` to `--color-ramp-5`, and `drawHeatmap`
// paints that image into a rectangle of a canvas without smoothing.

export type Rgb = readonly [number, number, number];

export interface Grid {
  nx: number;
  ny: number;
  at(ix: number, iy: number): number;
}

export const RAMP_TOKENS = [
  "--color-ramp-1",
  "--color-ramp-2",
  "--color-ramp-3",
  "--color-ramp-4",
  "--color-ramp-5",
] as const;

/** The ramp used when a token cannot be read (the token values, dark to light). */
export const FALLBACK_STOPS: readonly Rgb[] = [
  [68, 1, 84],
  [59, 82, 139],
  [33, 145, 140],
  [94, 201, 98],
  [253, 231, 37],
];

export function parseHex(text: string): Rgb | null {
  const m = /^#([0-9a-f]{6})$/i.exec(text.trim());
  if (m === null || m[1] === undefined) return null;
  const n = Number.parseInt(m[1], 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

/** The ramp stops from `read(token)`; a token that is not `#rrggbb` falls back to the default stop. */
export function stopsFrom(read: (token: string) => string): Rgb[] {
  return RAMP_TOKENS.map((t, i) => parseHex(read(t)) ?? FALLBACK_STOPS[i] ?? [0, 0, 0]);
}

/** The colour at `t` (0 to 1, clamped) on `stops`, interpolated between neighbours. */
export function rampColour(stops: readonly Rgb[], t: number): Rgb {
  const last = stops.length - 1;
  const x = Math.min(1, Math.max(0, Number.isFinite(t) ? t : 0)) * last;
  const i = Math.min(last - 1, Math.floor(x));
  const f = x - i;
  const a = stops[i] ?? [0, 0, 0];
  const b = stops[i + 1] ?? a;
  return [
    Math.round(a[0] + (b[0] - a[0]) * f),
    Math.round(a[1] + (b[1] - a[1]) * f),
    Math.round(a[2] + (b[2] - a[2]) * f),
  ];
}

/**
 * The colour range of `grid`: its `lo` and `hi` quantiles over at most `limit` evenly spread finite
 * values, so one outlier cannot flatten the picture. An empty grid gives [0, 1].
 */
export function valueRange(grid: Grid, lo = 0.02, hi = 0.98, limit = 20000): [number, number] {
  const total = grid.nx * grid.ny;
  const stride = Math.max(1, Math.floor(total / limit));
  const sample: number[] = [];
  for (let k = 0; k < total; k += stride) {
    const v = grid.at(k % grid.nx, Math.floor(k / grid.nx));
    if (Number.isFinite(v)) sample.push(v);
  }
  if (sample.length === 0) return [0, 1];
  sample.sort((a, b) => a - b);
  const at = (q: number) => sample[Math.min(sample.length - 1, Math.floor(q * sample.length))] ?? 0;
  const a = at(lo);
  const b = at(hi);
  return b > a ? [a, b] : [a, a + 1];
}

export interface Image {
  width: number;
  height: number;
  /** RGBA, row 0 at the top (the highest row of the grid). */
  data: Uint8ClampedArray<ArrayBuffer>;
}

/** One pixel per grid cell; a non-finite value is transparent. */
export function renderHeatmap(
  grid: Grid,
  range: readonly [number, number],
  stops: readonly Rgb[],
): Image {
  const data = new Uint8ClampedArray(grid.nx * grid.ny * 4);
  const span = range[1] - range[0];
  for (let iy = 0; iy < grid.ny; iy += 1) {
    const row = grid.ny - 1 - iy;
    for (let ix = 0; ix < grid.nx; ix += 1) {
      const v = grid.at(ix, iy);
      if (!Number.isFinite(v)) continue;
      const [r, g, b] = rampColour(stops, span === 0 ? 0 : (v - range[0]) / span);
      const o = (row * grid.nx + ix) * 4;
      data[o] = r;
      data[o + 1] = g;
      data[o + 2] = b;
      data[o + 3] = 255;
    }
  }
  return { width: grid.nx, height: grid.ny, data };
}

export interface Box {
  x: number;
  y: number;
  w: number;
  h: number;
}

/** Paint `image` into `box` of `ctx`, each cell a flat block. */
export function drawHeatmap(ctx: CanvasRenderingContext2D, image: Image, box: Box): void {
  if (image.width === 0 || image.height === 0) return;
  const off = document.createElement("canvas");
  off.width = image.width;
  off.height = image.height;
  const offCtx = off.getContext("2d");
  if (offCtx === null) return;
  offCtx.putImageData(new ImageData(image.data, image.width, image.height), 0, 0);
  ctx.imageSmoothingEnabled = false;
  ctx.drawImage(off, box.x, box.y, box.w, box.h);
}
