import { describe, expect, it, vi } from "vitest";
import { dataSessionPath } from "./dataPaths";
import {
  type ClickInfo,
  createRouter,
  linkTarget,
  matchRoute,
  redirectFor,
  safeReturnPath,
  viewFor,
  type Win,
} from "./router";
import type { RouteDef } from "./routes";

function fakeWin(start: string) {
  let path = start;
  const pops = new Set<() => void>();
  const win: Win = {
    location: {
      get pathname() {
        return path.split("?")[0] ?? "/";
      },
    },
    history: {
      pushState: (_s, _u, url) => {
        path = url;
      },
      replaceState: (_s, _u, url) => {
        path = url;
      },
    },
    addEventListener: (_t, l) => void pops.add(l),
    removeEventListener: (_t, l) => void pops.delete(l),
  };
  return {
    win,
    pops,
    back: (to: string) => {
      path = to;
      for (const l of pops) l();
    },
  };
}

describe("matchRoute", () => {
  it("matches the registered routes", () => {
    expect(matchRoute("/")?.name).toBe("data");
    expect(matchRoute("/d/dandi-000623")).toEqual({
      name: "dataset",
      params: { dataset: "dandi-000623" },
    });
    expect(matchRoute("/s/st/p/bmovie-patient-41")).toEqual({
      name: "participant",
      params: { study: "st", participant: "bmovie-patient-41" },
    });
    expect(matchRoute("/s/_/p/d%3Asub-a")?.params.participant).toBe("d:sub-a");
    expect(matchRoute("/s/st")).toEqual({ name: "study", params: { study: "st" } });
    expect(matchRoute("/uploads")?.name).toBe("uploads");
    expect(matchRoute("/files/a1")?.name).toBe("file");
    expect(matchRoute("/discover")?.name).toBe("discover");
    expect(matchRoute("/discover/imports")?.name).toBe("imports");
    expect(matchRoute("/discover/imports/r1")).toEqual({
      name: "import",
      params: { run_id: "r1" },
    });
    expect(matchRoute("/results")?.name).toBe("results");
    expect(matchRoute("/results/a%2F1")).toEqual({
      name: "result",
      params: { artifact_id: "a/1" },
    });
    expect(matchRoute("/start")?.name).toBe("start");
    expect(matchRoute("/auth/callback")?.name).toBe("callback");
  });

  it("/ is Data and /discover is the public browser", () => {
    expect(viewFor("/")).toBe("data");
    expect(viewFor("/discover")).toBe("discover");
  });

  it("/s/:study/:d/:s/:session is the session page (`_` for no study or session); /results/:id is the result page", () => {
    expect(matchRoute("/s/_/d/s/ses-1")).toEqual({
      name: "session",
      params: { study: "_", dataset: "d", subject: "s", session: "ses-1" },
    });
    expect(matchRoute(dataSessionPath("d", "s", null))?.params.session).toBe("_");
    expect(viewFor("/results/a1")).toBe("result");
  });

  it("retired paths are not routes; redirectFor handles them", () => {
    for (const p of [
      "/data/d",
      "/data/d/s",
      "/data/d/s/ses-1",
      "/catalog",
      "/catalog/d",
      "/catalog/d/subjects/s",
      "/imports",
      "/imports/r1",
    ]) {
      expect(viewFor(p)).toBe("not-found");
    }
  });

  it("unknown paths and static files are not routes", () => {
    expect(viewFor("/nope")).toBe("not-found");
    expect(viewFor("/s/x/y/z/w/v")).toBe("not-found");
    for (const p of ["/index.json", "/platform.json", "/assets/index-abc.js"]) {
      expect(matchRoute(p)).toBeNull();
    }
  });

  it("captures and decodes params", () => {
    const routes: RouteDef[] = [
      { name: "dataset", pattern: "/catalog/:dataset" },
      { name: "start", pattern: "/files/:asset_id/:v" },
    ];
    expect(matchRoute("/catalog/dandi-000623", routes)).toEqual({
      name: "dataset",
      params: { dataset: "dandi-000623" },
    });
    expect(matchRoute("/catalog/a%20b", routes)?.params).toEqual({ dataset: "a b" });
    expect(matchRoute("/files/u1/2", routes)?.params).toEqual({ asset_id: "u1", v: "2" });
    expect(matchRoute("/catalog/%E0%A4%A", routes)).toBeNull();
    expect(matchRoute("/catalog", routes)).toBeNull();
  });
});

describe("redirectFor", () => {
  it("sends each retired path to its new address", () => {
    expect(redirectFor("/catalog")).toBe("/");
    expect(redirectFor("/catalog/dandi-000623")).toBe("/d/dandi-000623");
    expect(redirectFor("/data/dandi-000623")).toBe("/d/dandi-000623");
    expect(redirectFor("/data/a%20b/s/ses-1")).toBe("/s/_/a%20b/s/ses-1");
    expect(redirectFor("/data/d/s/_")).toBe("/s/_/d/s/_");
    expect(redirectFor("/data/dandi-000623/sub-a")).toBe("/s/_/p/dandi-000623%3Asub-a");
    expect(redirectFor("/catalog/dandi-000623/subjects/sub-a")).toBe("/s/_/p/dandi-000623%3Asub-a");
    expect(redirectFor("/catalog/a%20b/subjects/c%2Fd")).toBe("/s/_/p/a%20b%3Ac%2Fd");
    expect(redirectFor("/imports")).toBe("/discover/imports");
    expect(redirectFor("/imports/r1")).toBe("/discover/imports/r1");
  });
  it("leaves live routes and unknown paths alone", () => {
    for (const p of [
      "/",
      "/d/d",
      "/s/_/p/d%3As",
      "/data/d/s/x/y",
      "/discover/imports",
      "/files/a",
      "/catalog/d/x",
      "/catalog/d/subjects",
      "/catalog/%E0%A4%A",
      "/catalog/d/subjects/s/x",
      "/nope",
    ]) {
      expect(redirectFor(p)).toBeNull();
    }
  });
});

describe("safeReturnPath", () => {
  it("accepts a same-origin path", () => {
    expect(safeReturnPath("/ok?q=1")).toBe("/ok?q=1");
    expect(safeReturnPath("/catalog")).toBe("/catalog");
    expect(safeReturnPath("/")).toBe("/");
  });
  it("refuses anything else", () => {
    for (const bad of ["//evil.com", "https://x", "/\\evil.com", "evil", "", "/a\nb", null, 3]) {
      expect(safeReturnPath(bad)).toBeNull();
    }
  });
});

describe("createRouter", () => {
  it("navigate pushes, notifies, and popstate notifies", () => {
    const { win, pops, back } = fakeWin("/");
    const router = createRouter(win);
    const seen = vi.fn();
    const off = router.subscribe(seen);
    router.navigate("/catalog");
    expect(router.getPath()).toBe("/catalog");
    expect(seen).toHaveBeenCalledTimes(1);
    back("/start");
    expect(router.getPath()).toBe("/start");
    expect(seen).toHaveBeenCalledTimes(2);
    off();
    expect(pops.size).toBe(0);
  });

  it("navigate to an unsafe path goes to /", () => {
    const { win } = fakeWin("/catalog");
    const router = createRouter(win);
    router.navigate("//evil.com");
    expect(router.getPath()).toBe("/");
  });
});

describe("linkTarget", () => {
  const O = "http://localhost:5173";
  const click = (href: string, over: Partial<ClickInfo> = {}): ClickInfo => ({
    button: 0,
    defaultPrevented: false,
    metaKey: false,
    ctrlKey: false,
    shiftKey: false,
    altKey: false,
    anchor: { href, target: "", hasDownload: false },
    ...over,
  });
  it("intercepts an in-app route link", () => {
    expect(linkTarget(click(`${O}/catalog?x=1`), O)).toBe("/catalog?x=1");
  });
  it("intercepts a retired path, which the app then redirects", () => {
    expect(linkTarget(click(`${O}/imports/r1`), O)).toBe("/imports/r1");
  });
  it("leaves static files, other origins, modified clicks and new tabs alone", () => {
    expect(linkTarget(click(`${O}/index.json`), O)).toBeNull();
    expect(linkTarget(click(`${O}/assets/a.js`), O)).toBeNull();
    expect(linkTarget(click("https://x.test/catalog"), O)).toBeNull();
    expect(linkTarget(click(`${O}/catalog`, { ctrlKey: true }), O)).toBeNull();
    expect(linkTarget(click(`${O}/catalog`, { button: 1 }), O)).toBeNull();
    const blank = { href: `${O}/catalog`, target: "_blank", hasDownload: false };
    expect(linkTarget(click(`${O}/catalog`, { anchor: blank }), O)).toBeNull();
    expect(linkTarget(click(`${O}/catalog`, { anchor: null }), O)).toBeNull();
  });
});
