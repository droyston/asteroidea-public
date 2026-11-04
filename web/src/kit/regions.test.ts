import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { normalizeRegion, regionColour } from "./regions";

const css = readFileSync(
  join(new URL("..", import.meta.url).pathname, "kit", "tokens.css"),
  "utf8",
);

describe("normalizeRegion", () => {
  it("folds case, punctuation and the side word into one key", () => {
    for (const label of [
      "Amygdala Left",
      "amygdala left",
      "amygdala_left",
      "Left Amygdala",
      "amygdala-L",
      "L_amygdala",
      "  AMYGDALA, left ",
    ]) {
      expect(normalizeRegion(label).key, label).toBe("amygdala left");
    }
    expect(normalizeRegion("Amygdala R").key).toBe("amygdala right");
    expect(normalizeRegion("pre-SMA").key).toBe("presma");
  });

  it("keeps the side: left and right are different keys", () => {
    expect(normalizeRegion("amygdala left").key).not.toBe(normalizeRegion("amygdala right").key);
    expect(normalizeRegion("amygdala left").side).toBe("left");
    expect(normalizeRegion("amygdala right").side).toBe("right");
    expect(normalizeRegion("vmPFC").side).toBeNull();
  });

  it("a lone letter or word is a name, not a side", () => {
    expect(normalizeRegion("L").key).toBe("l");
    expect(normalizeRegion("Left").key).toBe("left");
  });
});

describe("live labels (frozen datasets listing)", () => {
  const cases: [string, string, string][] = [
    ["Right amygdala", "amygdala right", "amygdala-r"],
    ["Left amygdala", "amygdala left", "amygdala-l"],
    ["Left hippocampus", "hippocampus left", "hippocampus-l"],
    ["Right hippocampus", "hippocampus right", "hippocampus-r"],
    ["Amyg, Right Amygdala mAmyg, medial amygdala", "amygdala right", "amygdala-r"],
    ["Amyg, Left Amygdala lAmyg, lateral amygdala", "amygdala left", "amygdala-l"],
    ["Hipp, Right Hippocampus rHipp, rostral hippocampus", "hippocampus right", "hippocampus-r"],
    ["right dHPC", "hippocampus right", "hippocampus-r"],
  ];

  it("resolves each label to its coarse region and side, and the named token", () => {
    for (const [label, key, suffix] of cases) {
      expect(normalizeRegion(label).key, label).toBe(key);
      expect(regionColour(label).token, label).toBe(`--color-region-${suffix}`);
    }
  });

  it("a sided vmPFC, preSMA or ACC keeps its side-kept key and the hash path (two colours)", () => {
    expect(normalizeRegion("Left vmPFC").key).toBe("vmpfc left");
    expect(normalizeRegion("Right preSMA").key).toBe("presma right");
    expect(normalizeRegion("Right ACC").key).toBe("acc right");
    for (const label of ["Left vmPFC", "Right preSMA", "Right ACC"]) {
      expect(regionColour(label).token, label).toMatch(/^--color-region-x[1-8]$/);
    }
    expect(regionColour("Left vmPFC").token).not.toBe(regionColour("Right vmPFC").token);
  });

  it("keeps the full label for printing and the side apart", () => {
    const c = regionColour("Amyg, Right Amygdala mAmyg, medial amygdala");
    expect(c.label).toBe("Amyg, Right Amygdala mAmyg, medial amygdala");
    expect(normalizeRegion("Amyg, Left Amygdala mAmyg, medial amygdala").key).not.toBe(c.key);
  });

  it("subregions of one coarse region share a key", () => {
    expect(normalizeRegion("Amyg, Left Amygdala mAmyg, medial amygdala").key).toBe(
      normalizeRegion("Amyg, Left Amygdala lAmyg, lateral amygdala").key,
    );
  });

  it("unknown and labels with no recognisable region keep the stable hash", () => {
    for (const label of ["unknown", "RSPE", "LSPE"]) {
      expect(regionColour(label).token, label).toMatch(/^--color-region-x[1-8]$/);
      expect(regionColour(label).key, label).toBe(label.toLowerCase());
    }
  });
});

describe("regionColour", () => {
  it("maps the seven designed regions to their named tokens, keeping the side", () => {
    const cases: [string, string][] = [
      ["amygdala L", "amygdala-l"],
      ["Amygdala right", "amygdala-r"],
      ["Hippocampus_Left", "hippocampus-l"],
      ["hippocampus R", "hippocampus-r"],
      ["preSMA", "presma"],
      ["vmPFC", "vmpfc"],
      ["ACC", "acc"],
    ];
    for (const [label, suffix] of cases) {
      expect(regionColour(label).token, label).toBe(`--color-region-${suffix}`);
    }
    expect(regionColour("amygdala left").token).not.toBe(regionColour("amygdala right").token);
  });

  it("maps any other name to x1..x8, the same on every call", () => {
    for (const label of ["insula", "Insula left", "insula right", "dACC", "thalamus", "OFC"]) {
      const a = regionColour(label);
      expect(a.token, label).toMatch(/^--color-region-x[1-8]$/);
      expect(regionColour(label)).toEqual(a);
    }
    // Pinned: a change to the hash would recolour every saved view.
    expect(regionColour("insula").token).toBe("--color-region-x6");
    expect(regionColour("thalamus").token).toBe("--color-region-x7");
  });

  it("a named region with a side its name lacks falls to the hash, not to another side", () => {
    expect(regionColour("amygdala").token).toMatch(/^--color-region-x[1-8]$/);
  });

  it("always carries the label to print beside the colour", () => {
    const c = regionColour("  Amygdala Left ");
    expect(c.label).toBe("Amygdala Left");
    expect(c.cssVar).toBe("var(--color-region-amygdala-l)");
  });

  it("every token it can return is defined in tokens.css", () => {
    const labels = [
      "amygdala left",
      "amygdala right",
      "hippocampus left",
      "hippocampus right",
      "preSMA",
      "vmPFC",
      "ACC",
      ...Array.from({ length: 200 }, (_, i) => `region ${i}`),
    ];
    const seen = new Set(labels.map((l) => regionColour(l).token));
    for (let n = 1; n <= 8; n++) expect(seen.has(`--color-region-x${n}`), `x${n}`).toBe(true);
    for (const token of seen) expect(css, token).toContain(`${token}:`);
  });
});
