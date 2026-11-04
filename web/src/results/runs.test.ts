// The Runs box: the All listing grouped by `run_id`, newest run first, at most five,
// each one link to the run's first image by name (else its first file), labelled with the analysis
// name, subject, file count, date and author ("you" for the signed-in researcher).
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { MAX_RUNS, runDetail, runsModel } from "./model";
import { RunsBox } from "./ResultsView";
import type { Artifact, AssetName } from "./results";

const ME = "ada@example.test";

function row(over: Partial<Artifact> & { artifact_id: string }): Artifact {
  return {
    name: `${over.artifact_id}.csv`,
    size: 1,
    state: "COMMITTED",
    created_at: 1_792_000_000,
    author_email: ME,
    source_asset_ids: [],
    source_asset_count: 0,
    content_type: "text/csv",
    run_id: null,
    run_function: null,
    ...over,
  };
}

function name(asset_id: string, subject: string | null): AssetName {
  return {
    asset_id,
    dataset: "ds",
    subject,
    session: null,
    datatype: null,
    suffix: null,
    format: null,
    path: `${asset_id}.nwb`,
    version: "v1",
    current: true,
  };
}

const NAMES = new Map([
  ["a-1", name("a-1", "CS62")],
  ["a-2", name("a-2", "CS62")],
  ["a-3", name("a-3", "CS63")],
]);

describe("runsModel", () => {
  it("groups by run, newest run first, and leaves rows without a run to the tree", () => {
    const rows = [
      row({ artifact_id: "n1", run_id: null, created_at: 1_792_000_900 }),
      row({
        artifact_id: "b1",
        run_id: "rb",
        run_function: "showcase-cs62",
        created_at: 1_792_000_500,
      }),
      row({ artifact_id: "a1", run_id: "ra", run_function: "rates", created_at: 1_792_000_100 }),
      row({
        artifact_id: "b2",
        run_id: "rb",
        run_function: "showcase-cs62",
        created_at: 1_792_000_400,
      }),
      row({ artifact_id: "a2", run_id: "ra", run_function: "rates", created_at: 1_792_000_600 }),
    ];
    const runs = runsModel(rows, NAMES, ME);
    expect(runs.map((r) => [r.run_id, r.analysis, r.files])).toEqual([
      ["ra", "rates", 2],
      ["rb", "showcase-cs62", 2],
    ]);
  });

  it("shows at most five runs", () => {
    const rows = Array.from({ length: 7 }, (_, i) =>
      row({ artifact_id: `x${i}`, run_id: `r${i}`, created_at: 1_792_000_000 + i }),
    );
    const runs = runsModel(rows, NAMES, ME);
    expect(MAX_RUNS).toBe(5);
    expect(runs.map((r) => r.run_id)).toEqual(["r6", "r5", "r4", "r3", "r2"]);
  });

  it("links the first image by name, else the first file by name", () => {
    const withImages = [
      row({ artifact_id: "t", name: "a-table.csv", run_id: "r" }),
      row({ artifact_id: "z", name: "z-fig.png", run_id: "r", content_type: "image/png" }),
      row({ artifact_id: "b", name: "b-fig.png", run_id: "r", content_type: "image/png" }),
    ];
    expect(runsModel(withImages, NAMES, ME)[0]?.href).toBe("/results/b");
    const noImage = [
      row({ artifact_id: "y", name: "y.json", run_id: "r" }),
      row({ artifact_id: "c", name: "c.csv", run_id: "r" }),
    ];
    expect(runsModel(noImage, NAMES, ME)[0]?.href).toBe("/results/c");
  });

  it("names the subject from the loaded asset names, or the count when several", () => {
    const one = [
      row({ artifact_id: "1", run_id: "r", source_asset_ids: ["a-1"] }),
      row({ artifact_id: "2", run_id: "r", source_asset_ids: ["a-2", "unknown"] }),
    ];
    expect(runsModel(one, NAMES, ME)[0]?.subject).toBe("CS62");
    const two = [...one, row({ artifact_id: "3", run_id: "r", source_asset_ids: ["a-3"] })];
    expect(runsModel(two, NAMES, ME)[0]?.subject).toBe("2 subjects");
    expect(runsModel(one, new Map(), ME)[0]?.subject).toBeNull();
  });

  it("marks the signed-in researcher's run as you, and shows others by email", () => {
    const rows = [
      row({ artifact_id: "m", run_id: "mine", created_at: 1_792_000_200 }),
      row({ artifact_id: "o", run_id: "theirs", author_email: "grace@example.test" }),
    ];
    const [mine, theirs] = runsModel(rows, NAMES, ME);
    expect([mine?.author, mine?.mine]).toEqual(["you", true]);
    expect([theirs?.author, theirs?.mine]).toEqual(["grace@example.test", false]);
    expect(runsModel(rows, NAMES, null)[0]?.author).toBe(ME);
  });

  it("labels a run: subject, file count, date, author", () => {
    const rows = [
      row({
        artifact_id: "1",
        run_id: "r",
        run_function: "showcase-cs62",
        source_asset_ids: ["a-1"],
      }),
      row({ artifact_id: "2", run_id: "r", run_function: "showcase-cs62" }),
    ];
    const [run] = runsModel(rows, NAMES, ME);
    expect(run === undefined ? "" : runDetail(run)).toBe("CS62 · 2 files · 14 Oct · you");
  });
});

describe("RunsBox", () => {
  it("draws one link per run with its analysis name and label, and nothing without runs", () => {
    const rows = [
      row({ artifact_id: "f", name: "fig.png", run_id: "r", run_function: "showcase-cs62" }),
    ];
    const html = renderToStaticMarkup(createElement(RunsBox, { runs: runsModel(rows, NAMES, ME) }));
    expect(html).toContain('aria-label="Runs"');
    expect(html).toContain('href="/results/f"');
    expect(html).toContain("showcase-cs62");
    expect(html).toContain("1 file · 14 Oct · you");
    expect(renderToStaticMarkup(createElement(RunsBox, { runs: [] }))).toBe("");
  });
});
