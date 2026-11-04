// Region labels to colour tokens. An author's label is normalised for case, punctuation and
// the side word, and the side is kept: "amygdala left" and "amygdala right" are two regions with two
// colours. A composite label ("Amyg, Right Amygdala mAmyg, ...") resolves to its coarse region and
// side. The seven designed regions map to their named tokens; any other name maps by
// a stable hash to --color-region-x1..x8. The colour never stands alone: callers print `label`
// beside it.

export type Side = "left" | "right" | null;

export type NormalRegion = {
  /** Lower-case name without punctuation, spaces or the side word: "amygdala", "presma". */
  base: string;
  side: Side;
  /** `base` plus the side word, the identity two labels share: "amygdala left". */
  key: string;
};

export type RegionColour = {
  /** The author's label, trimmed; always printed beside the colour. */
  label: string;
  key: string;
  /** The custom property name, e.g. "--color-region-amygdala-l". */
  token: string;
  /** `var(...)` of the token, for a style attribute or canvas lookup. */
  cssVar: string;
};

const NAMED: Readonly<Record<string, string>> = {
  "amygdala left": "amygdala-l",
  "amygdala right": "amygdala-r",
  "hippocampus left": "hippocampus-l",
  "hippocampus right": "hippocampus-r",
  presma: "presma",
  vmpfc: "vmpfc",
  acc: "acc",
};

/** Abbreviations and words that name a coarse region, for composite labels ("Amyg, Right Amygdala ..."). */
const COARSE: Readonly<Record<string, string>> = {
  amygdala: "amygdala",
  amyg: "amygdala",
  hippocampus: "hippocampus",
  hipp: "hippocampus",
  hpc: "hippocampus",
  dhpc: "hippocampus",
  vhpc: "hippocampus",
};

const EXTRA_COUNT = 8;

function sideOf(word: string): Side {
  if (word === "left" || word === "l") return "left";
  if (word === "right" || word === "r") return "right";
  return null;
}

/** Lower-case, strip punctuation, and turn "Left", "L" or "_left" at either end into one side word. */
export function normalizeRegion(label: string): NormalRegion {
  const words = label
    .toLowerCase()
    .split(/[^a-z0-9]+/)
    .filter((w) => w !== "");
  let side: Side = null;
  const first = words[0];
  const last = words[words.length - 1];
  if (words.length > 1 && last !== undefined && sideOf(last) !== null) {
    side = sideOf(last);
    words.pop();
  } else if (words.length > 1 && first !== undefined && sideOf(first) !== null) {
    side = sideOf(first);
    words.shift();
  }
  let base = words.join("");
  const coarse = COARSE[base];
  if (coarse !== undefined) {
    base = coarse;
  } else {
    // A composite label ("Amyg, Right Amygdala mAmyg, medial amygdala") resolves to the first coarse
    // region word and, if no side word sat at an end, the first full side word.
    const hit = words.find((w) => COARSE[w] !== undefined);
    if (words.length > 1 && hit !== undefined) {
      base = COARSE[hit] ?? base;
      side ??=
        words
          .map((w) => (w === "left" || w === "right" ? sideOf(w) : null))
          .find((x) => x !== null) ?? null;
    }
  }
  return { base, side, key: side === null ? base : `${base} ${side}` };
}

/** 32-bit FNV-1a: stable across runs and engines. */
function fnv1a(text: string): number {
  let h = 0x811c9dc5;
  for (let i = 0; i < text.length; i++) {
    h ^= text.charCodeAt(i);
    h = Math.imul(h, 0x01000193) >>> 0;
  }
  return h;
}

/** The colour token for a region label. The same label gives the same token on every call. */
export function regionColour(label: string): RegionColour {
  const { key } = normalizeRegion(label);
  const named = NAMED[key];
  const suffix = named ?? `x${(fnv1a(key) % EXTRA_COUNT) + 1}`;
  const token = `--color-region-${suffix}`;
  return { label: label.trim(), key, token, cssVar: `var(${token})` };
}
