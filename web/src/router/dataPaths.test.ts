import { describe, expect, it } from "vitest";
import {
  dataCrumbs,
  dataDatasetPath,
  dataPath,
  dataSessionPath,
  dataSubjectPath,
  datasetViewPath,
  parseSessionKey,
  sessionKey,
} from "./dataPaths";

describe("data paths", () => {
  it("builds the data path shapes", () => {
    expect(dataPath()).toBe("/");
    expect(dataDatasetPath("dandi-000623")).toBe("/data/dandi-000623");
    expect(dataSubjectPath("d", "sub-1")).toBe("/data/d/sub-1");
    expect(datasetViewPath("dandi-000623")).toBe("/d/dandi-000623");
    expect(dataSessionPath("d", "sub-1", "ses-2")).toBe("/s/_/d/sub-1/ses-2");
    expect(dataSessionPath("d", "sub-1", "ses-2", "st")).toBe("/s/st/d/sub-1/ses-2");
  });
  it("uses _ for a missing session", () => {
    expect(dataSessionPath("d", "sub-1", null)).toBe("/s/_/d/sub-1/_");
    expect(dataSessionPath("d", "sub-1", "")).toBe("/s/_/d/sub-1/_");
  });
  it("encodes each segment", () => {
    expect(dataSubjectPath("a/b", "s c")).toBe("/data/a%2Fb/s%20c");
  });
});

describe("session key", () => {
  it("round-trips, with _ for no session", () => {
    expect(sessionKey("d", "s", "x")).toBe("d/s/x");
    expect(sessionKey("d", "s", null)).toBe("d/s/_");
    expect(parseSessionKey("d/s/x")).toEqual({ dataset: "d", subject: "s", session: "x" });
    expect(parseSessionKey("d/s/_")).toEqual({ dataset: "d", subject: "s", session: null });
  });
  it("rejects malformed keys", () => {
    expect(parseSessionKey("d/s")).toBeNull();
    expect(parseSessionKey("d/s/x/y")).toBeNull();
    expect(parseSessionKey("d//x")).toBeNull();
  });
});

describe("data crumbs", () => {
  it("leaves the last crumb unlinked", () => {
    expect(dataCrumbs({})).toEqual([{ label: "Studies" }]);
    expect(dataCrumbs({ dataset: "d" })).toEqual([{ label: "Studies", href: "/" }, { label: "d" }]);
  });
  it("walks dataset > subject > session > file", () => {
    expect(dataCrumbs({ dataset: "d", subject: "s", session: "x", file: "f.nwb" })).toEqual([
      { label: "Studies", href: "/" },
      { label: "d", href: "/d/d" },
      { label: "s", href: "/data/d/s" },
      { label: "x", href: "/s/_/d/s/x" },
      { label: "f.nwb" },
    ]);
  });
  it("labels a missing session _ and links it", () => {
    expect(dataCrumbs({ dataset: "d", subject: "s", session: null, file: "f" })[3]).toEqual({
      label: "_",
      href: "/s/_/d/s/_",
    });
  });
  it("cuts at the first omitted level", () => {
    expect(dataCrumbs({ dataset: "d", subject: "s" })).toEqual([
      { label: "Studies", href: "/" },
      { label: "d", href: "/d/d" },
      { label: "s" },
    ]);
  });
});
