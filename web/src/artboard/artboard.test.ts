// Completeness over every registered surface (default suite). A real surface must meet its
// expectation exactly; a pending one must render only its declared stand-in, and its expectation's
// numbers are pinned here so the row that makes it real inherits them.
import { describe, expect, it } from "vitest";
import { check, type Expectation, parse, renderSurface } from "./harness";
import { SURFACES } from "./surfaces";

function counts(exp: Expectation): Record<string, number> {
  return Object.fromEntries(exp.counts.map((c) => [c.noun, c.n]));
}

const PINNED: Record<string, Record<string, number>> = {
  home: {
    "Copy code buttons": 1,
    "study rows": 5,
    "matrix columns": 8,
    "rail entries": 3,
    "dashed (pending) rail links": 2,
    "solid (confirmed) rail links": 1,
    "modality facet chips": 8,
    "region facet chips": 18,
    "species facet chips": 2,
    "archive facet chips": 2,
    "sandbox fold lines": 1,
    "Mine rows": 0,
    "Mine empty states": 1,
    "other researchers' lines": 1,
  },
  study: {
    "Copy code buttons": 1,
    releases: 2,
    "strips (releases above 20 participants)": 0,
    "relation rows, one per release": 2,
    "Source files buttons": 2,
    "participant rows": 17,
    "session cells": 5,
    "session cells linking the session page": 4,
    "session cells linking a file page": 1,
    "dashed (pending) link cells": 4,
    "solid (confirmed) link cells": 0,
  },
  "study-24": {
    "Copy code buttons": 1,
    releases: 2,
    "strips (releases above 20 participants)": 1,
    "relation rows, one per release": 2,
    "Source files buttons": 2,
    "participant rows": 25,
    "session cells": 0,
    "session cells linking the session page": 0,
    "session cells linking a file page": 0,
    "dashed (pending) link cells": 0,
    "solid (confirmed) link cells": 2,
  },
  participant: {
    "Copy code buttons": 1,
    records: 2,
    "dashed (pending) bridges": 1,
    "solid (confirmed) bridges": 0,
    "timeline lanes": 4,
    "timeline ticks": 5,
    "ephys cards": 2,
    glyphs: 2,
    "glyph rows": 88,
    "T1w cards": 1,
    "T1w cards not built": 1,
    "region panels": 1,
    "region lines": 4,
  },
  session: {
    "Copy code buttons": 1,
    "unit rows": 44,
    "region groups": 4,
    "Δ bars": 43,
    "hatched null-z rows": 1,
    "tuning marks": 5,
    "totals lines": 1,
    "baseline boxes": 1,
    "tuning summaries": 1,
    "tuning region lines": 4,
  },
  "session-multiples": {
    "Copy code buttons": 1,
    "unit PSTHs": 44,
    "totals lines": 1,
    "baseline boxes": 1,
    "tuning summaries": 1,
    "tuning region lines": 4,
  },
  "session-152-multiples": {
    "Copy code buttons": 1,
    "unit PSTHs": 152,
    "totals lines": 1,
    "baseline boxes": 1,
    "tuning summaries": 1,
    "tuning region lines": 6,
  },
  "session-152": {
    "Copy code buttons": 1,
    "unit rows": 152,
    "region groups": 6,
    "Δ bars": 152,
    "hatched null-z rows": 0,
    "tuning marks": 17,
    "totals lines": 1,
    "baseline boxes": 1,
    "tuning summaries": 1,
    "tuning region lines": 6,
  },
};
PINNED.find = {
  "matrix columns": 6,
  "matrix cells with datasets": 19,
  "best fits": 4,
  "fits marked in catalog": 1,
  histograms: 2,
};
PINNED["home-375"] = PINNED.home ?? {};
PINNED.inspect = {
  "control rows": 1,
  "local-only notes": 1,
  "scatter dots": 43,
  "region strips": 4,
  "strip dots": 43,
  "ranked rows": 12,
};
PINNED.signals = {
  "region groups": 6,
  "region not known groups": 0,
  "series options": 2,
  "channel options": 96,
  "window brushes": 1,
  "event lanes": 3,
  "label ticks under the overview": 14,
  "labels drawn in the window": 5,
  "labels listed": 3,
  "anatomy planes": 3,
};
PINNED["signals-unmapped"] = {
  "region groups": 1,
  "region not known groups": 1,
  "series options": 2,
  "channel options": 80,
  "window brushes": 1,
  "event lanes": 3,
  "label ticks under the overview": 14,
  "labels drawn in the window": 5,
  "labels listed": 3,
  "anatomy planes": 0,
};
PINNED["home-mine"] = { ...PINNED.home, "Mine rows": 2, "Mine empty states": 0 };
PINNED["states-loading"] = { "loading states": 1, "progress bars": 1 };
PINNED["states-slow"] = {
  "refused states": 1,
  "primary buttons": 1,
  "small codes": 1,
  "folded details": 1,
};
PINNED["states-curator"] = { "curator states": 1, "primary buttons": 1, "small codes": 1 };
PINNED["states-ended"] = { dialogs: 1, "primary buttons": 1 };
PINNED["states-catalog-error"] = {
  "error states": 1,
  "primary buttons": 1,
  "small codes": 1,
  "folded details": 1,
};
PINNED["states-empty"] = {
  "empty states": 1,
  "primary buttons": 1,
  "example results": 1,
  "save calls": 1,
};

describe("artboard surfaces", () => {
  it("registers every surface group with unique ids", () => {
    const ids = SURFACES.map((s) => s.id);
    expect(new Set(ids).size).toBe(ids.length);
    expect([...new Set(SURFACES.map((s) => s.board))].sort()).toEqual([
      "01-shell-navigation",
      "02-data-home",
      "03-study",
      "04-participant",
      "05-session-density",
      "06-flexible-inspection",
      "07-signal-inspection",
      "08-find-import",
      "09-results-provenance",
      "10-states",
      "10b-data-home-375",
    ]);
    expect(SURFACES.filter((s) => s.noScroll).map((s) => s.id)).toEqual([
      "home",
      "home-mine",
      "study-24",
      "session",
      "session-152",
      "session-multiples",
      "session-152-multiples",
      "inspect",
    ]);
  });

  it("the states group has one surface per state", () => {
    expect(SURFACES.filter((s) => s.board === "10-states").map((s) => s.id)).toEqual([
      "states-loading",
      "states-slow",
      "states-curator",
      "states-ended",
      "states-catalog-error",
      "states-empty",
    ]);
  });

  for (const surface of SURFACES) {
    if (surface.pending === null) {
      it(`${surface.id} (reference ${surface.board}) is complete`, () => {
        expect(check(renderSurface(surface), surface.expect())).toEqual([]);
      });
    } else {
      it(`${surface.id} (reference ${surface.board}) renders a stand-in until ${surface.pending} lands`, () => {
        const { els } = parse(renderSurface(surface));
        expect(els.map((e) => e.attrs["data-ab-standin"])).toEqual([`pending ${surface.pending}`]);
      });
    }
    if (surface.id in PINNED) {
      it(`${surface.id}'s expectation is pinned to the fixture`, () => {
        expect(counts(surface.expect())).toEqual(PINNED[surface.id]);
      });
    }
  }

  it("the session surfaces keep left and right apart (152: six side-kept groups)", () => {
    const s152 = SURFACES.find((s) => s.id === "session-152");
    expect(s152?.expect().texts).toEqual(
      expect.arrayContaining(["amygdala left", "amygdala right", "hippocampus left"]),
    );
  });
});
