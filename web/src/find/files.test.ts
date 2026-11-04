import { describe, expect, it } from "vitest";
import type { FileEntry } from "../sources/types";
import { fileType, narrow, shortBytes, typeCounts, windowOf } from "./files";

const f = (path: string, isDirectory = false): FileEntry => ({
  name: path.split("/").pop() ?? path,
  path,
  isDirectory,
  size: isDirectory ? null : 10,
  open: null,
});

const entries = [
  f("sub-1/sub-1_ecephys.nwb"),
  f("sub-2/sub-2_ecephys.nwb"),
  f("sub-1/anat/sub-1_T1w.nii.gz"),
  f("dataset_description.json"),
  f("README"),
  f("sub-3", true),
];

describe("the file listing under a picked dataset", () => {
  it("types each entry, double extensions and folders included", () => {
    expect(entries.map(fileType)).toEqual([
      ".nwb",
      ".nwb",
      ".nii.gz",
      ".json",
      "no extension",
      "folder",
    ]);
    expect(typeCounts(entries)[0]).toEqual([".nwb", 2]);
  });

  it("narrows by path text and by type", () => {
    expect(narrow(entries, "SUB-1", "").map((e) => e.path)).toEqual([
      "sub-1/sub-1_ecephys.nwb",
      "sub-1/anat/sub-1_T1w.nii.gz",
    ]);
    expect(narrow(entries, "sub", ".nwb")).toHaveLength(2);
    expect(narrow(entries, "", "folder").map((e) => e.path)).toEqual(["sub-3"]);
  });

  it("shows ten rows at a time with the counts, clamping the page", () => {
    const rows = Array.from({ length: 23 }, (_, i) => i);
    expect(windowOf(rows, 0)).toMatchObject({ first: 1, last: 10, total: 23, page: 0, pages: 3 });
    expect(windowOf(rows, 2).rows).toEqual([20, 21, 22]);
    expect(windowOf(rows, 9)).toMatchObject({ page: 2, first: 21, last: 23 });
    expect(windowOf([], 0)).toMatchObject({ first: 0, last: 0, total: 0, pages: 1 });
  });

  it("writes sizes short", () => {
    expect([shortBytes(null), shortBytes(512), shortBytes(2_200_000_000)]).toEqual([
      "",
      "512 B",
      "2.2 GB",
    ]);
  });
});
