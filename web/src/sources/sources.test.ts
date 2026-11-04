// Adapters against recorded listing responses (web/e2e/fixtures/live/).
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { dandiAdapter } from "./dandi";
import { openneuroAdapter } from "./openneuro";
import { ArchiveError, type DatasetRef, type Fetch } from "./types";

const dir = new URL("../../e2e/fixtures/live/", import.meta.url);
const read = (name: string): string => readFileSync(new URL(name, dir), "utf8");
const json = (body: string, status = 200): Response =>
  new Response(body, { status, headers: { "content-type": "application/json" } });

const dandi: DatasetRef = {
  source: "dandi",
  id: "000576",
  version: "0.231010.1811",
  apiUrl: "https://api.dandiarchive.org/api/dandisets/000576/versions/0.231010.1811/",
};
const neuro: DatasetRef = {
  source: "openneuro",
  id: "ds004993",
  version: "1.1.2",
  apiUrl: "https://openneuro.org/crn/graphql",
};

describe("dandi listing", () => {
  it("lists asset page 1 with paths and sizes", async () => {
    const urls: string[] = [];
    const fetchFn: Fetch = async (u) => {
      urls.push(u);
      return json(read("dandi-000576-assets-p1.json"));
    };
    const page = await dandiAdapter(fetchFn).listFiles(dandi, null);
    expect(urls).toEqual([`${dandi.apiUrl}assets/?page_size=100`]);
    expect(page.entries).toHaveLength(27);
    expect(page.next).toBeNull();
    expect(page.entries).toContainEqual({
      name: "sub-03_ses-20150304T164400_ecephys.nwb",
      path: "sub-03/sub-03_ses-20150304T164400_ecephys.nwb",
      isDirectory: false,
      size: 14105034,
      open: null,
    });
  });

  it("follows the next cursor to the second page", async () => {
    const pages = new Map([
      [`${dandi.apiUrl}assets/?page_size=100`, read("dandi-000576-assets-ps10-p1.json")],
      [
        "https://api.dandiarchive.org/api/dandisets/000576/versions/0.231010.1811/assets/?page=2&page_size=10",
        read("dandi-000576-assets-ps10-p2.json"),
      ],
    ]);
    const fetchFn: Fetch = async (u) => json(pages.get(u) ?? "{}", pages.has(u) ? 200 : 404);
    const a = dandiAdapter(fetchFn);
    const p1 = await a.listFiles(dandi, null);
    expect(p1.entries).toHaveLength(10);
    expect(p1.next).toEqual({
      kind: "page",
      url: "https://api.dandiarchive.org/api/dandisets/000576/versions/0.231010.1811/assets/?page=2&page_size=10",
    });
    const p2 = await a.listFiles(dandi, p1.next);
    expect(p2.entries).toHaveLength(10);
    expect(p2.next).toMatchObject({ kind: "page" });
    expect(new Set([...p1.entries, ...p2.entries].map((e) => e.path)).size).toBe(20);
  });

  it("reports the archive status and message on an error", async () => {
    const fetchFn: Fetch = async () => json('{"detail":"Not found."}', 404);
    await expect(dandiAdapter(fetchFn).listFiles(dandi, null)).rejects.toMatchObject({
      archive: "dandi",
      status: 404,
      message: "dandi: HTTP 404: Not found.",
    });
  });

  it("reports a network failure with no status", async () => {
    const fetchFn: Fetch = async () => {
      throw new TypeError("Failed to fetch");
    };
    await expect(dandiAdapter(fetchFn).listFiles(dandi, null)).rejects.toThrow(
      "dandi: no response: Failed to fetch",
    );
  });
});

describe("openneuro listing", () => {
  const trees: Record<string, string> = {
    "": "openneuro-ds004993-root.json",
    "183cc0bb6af6ef26827c42bd522689cdeb9fdc17": "openneuro-ds004993-sub-W1.json",
    "19885833aedacff6f6ca9f9844126560549be804": "openneuro-ds004993-ses-mri.json",
    "5160b23dc59de43d0324f864a1071d37145ce387": "openneuro-ds004993-anat.json",
  };
  const requests: { tree: string | undefined }[] = [];
  const fetchFn: Fetch = async (_u, init) => {
    const { variables } = JSON.parse(String(init?.body)) as { variables: { tree?: string } };
    requests.push({ tree: variables.tree });
    const name = trees[variables.tree ?? ""];
    return name === undefined ? json('{"data":{"snapshot":null}}') : json(read(name));
  };

  it("walks root, sub-W1, ses-mri and anat, building paths by joining filenames", async () => {
    const a = openneuroAdapter(fetchFn);
    const root = await a.listFiles(neuro, null);
    expect(root.next).toBeNull();
    const sub = root.entries.find((e) => e.name === "sub-W1");
    expect(sub).toMatchObject({ isDirectory: true, path: "sub-W1", size: null });
    expect(root.entries.find((e) => e.name === "README")).toMatchObject({
      isDirectory: false,
      path: "README",
      size: 4005,
      open: null,
    });
    const s = await a.listFiles(neuro, sub?.open ?? null);
    const ses = s.entries.find((e) => e.name === "ses-mri");
    expect(ses?.path).toBe("sub-W1/ses-mri");
    const m = await a.listFiles(neuro, ses?.open ?? null);
    const anat = m.entries.find((e) => e.name === "anat");
    expect(anat?.path).toBe("sub-W1/ses-mri/anat");
    const leaf = await a.listFiles(neuro, anat?.open ?? null);
    expect(leaf.entries).toEqual([
      {
        name: "sub-W1_ses-mri_acq-B3_T1w.nii.gz",
        path: "sub-W1/ses-mri/anat/sub-W1_ses-mri_acq-B3_T1w.nii.gz",
        isDirectory: false,
        size: 1301119,
        open: null,
      },
    ]);
    expect(requests[0]?.tree).toBeUndefined();
  });

  it("reports a GraphQL error message with the status", async () => {
    const f: Fetch = async () =>
      json('{"errors":[{"message":"You do not have access to read this dataset."}]}');
    await expect(openneuroAdapter(f).listFiles(neuro, null)).rejects.toMatchObject({
      archive: "openneuro",
      status: 200,
      message: "openneuro: HTTP 200: You do not have access to read this dataset.",
    });
  });

  it("reports an HTTP failure and a missing snapshot", async () => {
    const down: Fetch = async () =>
      new Response("bad gateway", { status: 502, statusText: "Bad Gateway" });
    await expect(openneuroAdapter(down).listFiles(neuro, null)).rejects.toThrow(
      "openneuro: HTTP 502: Bad Gateway",
    );
    const none: Fetch = async () => json('{"data":{"snapshot":null}}');
    await expect(openneuroAdapter(none).listFiles(neuro, null)).rejects.toBeInstanceOf(
      ArchiveError,
    );
  });
});
