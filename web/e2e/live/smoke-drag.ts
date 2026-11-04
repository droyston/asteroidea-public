// The pointer path of a drag across the LFP overview plot, kept apart from smoke.spec.ts so a
// vitest test can pin it (src/lfpDrag.test.ts): the live run cannot be run offline.
export interface Box {
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface Point {
  x: number;
  y: number;
}

// LfpTab.tsx plot margins.
const LEFT = 52;
const RIGHT = 12;
const TOP = 6;
const PLOT_H = 150;

/** The pointer positions that select `[start, start + length]` seconds of a `total`-second recording. */
export function dragPath(box: Box, total: number, start: number, length: number): Point[] {
  const at = (s: number) => box.x + LEFT + (s / total) * (box.width - LEFT - RIGHT);
  const y = box.y + TOP + PLOT_H / 2;
  return [
    { x: at(start), y },
    { x: at(start + length / 2), y },
    { x: at(start + length), y },
  ];
}

/** True when every point lies inside the viewport: `page.mouse` does not scroll, so a point below the fold hits nothing. */
export function allInView(
  points: readonly Point[],
  viewport: { width: number; height: number },
): boolean {
  return points.every(
    (p) => p.x >= 0 && p.y >= 0 && p.x <= viewport.width && p.y <= viewport.height,
  );
}
