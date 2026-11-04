import { describe, expect, it } from "vitest";
import type { FetchFn } from "../auth/apiGet";
import { idFirst, parseNoteId, sendable, sendNote, TEXT_MAX, viewOf } from "./notes";

const CONFIG = { apiUrl: "https://api.example" };

function tokens(id: string | null, access: string | null) {
  return { getIdToken: () => id, getAccessToken: () => access, refresh: async () => false };
}

function recorder(status: number, body: unknown) {
  const calls: { url: string; init: RequestInit }[] = [];
  const fetchFn: FetchFn = async (url, init) => {
    calls.push({ url, init });
    return new Response(JSON.stringify(body), {
      status,
      headers: { "content-type": "application/json" },
    });
  };
  return { calls, fetchFn };
}

const BODY = {
  text: "axis unlabelled",
  url: "https://app.example/s?tab=lfp",
  view: { path: "/s", query: { tab: "lfp" }, hash: "" },
};

describe("viewOf", () => {
  it("holds the path, each query parameter and the hash", () => {
    expect(viewOf({ pathname: "/studies/s1", search: "?tab=lfp&zoom=2", hash: "#u3" })).toEqual({
      path: "/studies/s1",
      query: { tab: "lfp", zoom: "2" },
      hash: "#u3",
    });
  });
});

describe("sendable", () => {
  it("refuses blank text and text over the limit", () => {
    expect(sendable("")).toBe(false);
    expect(sendable("  \n ")).toBe(false);
    expect(sendable("x".repeat(TEXT_MAX))).toBe(true);
    expect(sendable("x".repeat(TEXT_MAX + 1))).toBe(false);
  });
});

describe("idFirst", () => {
  it("offers the ID token when there is one, else the access token", () => {
    expect(idFirst(tokens("id-t", "acc-t")).getAccessToken()).toBe("id-t");
    expect(idFirst(tokens(null, "acc-t")).getAccessToken()).toBe("acc-t");
  });
});

describe("sendNote", () => {
  it("posts the body as JSON with the ID token and returns the note id on 201", async () => {
    const { calls, fetchFn } = recorder(201, { note_id: "n1" });
    const r = await sendNote(CONFIG, BODY, idFirst(tokens("id-t", "acc-t")), fetchFn);
    expect(r).toEqual({ kind: "loaded", value: "n1" });
    expect(calls).toHaveLength(1);
    const call = calls[0];
    expect(call?.url).toBe("https://api.example/notes");
    expect(call?.init.method).toBe("POST");
    expect(call?.init.headers).toEqual({
      authorization: "Bearer id-t",
      "content-type": "application/json",
    });
    expect(JSON.parse(String(call?.init.body))).toEqual(BODY);
  });

  it("a 400 carries the server's sentence", async () => {
    const detail = "text is 4001 characters; the limit is 4000.";
    const { fetchFn } = recorder(400, { error: "invalid_request", detail });
    const r = await sendNote(CONFIG, BODY, idFirst(tokens("id-t", null)), fetchFn);
    expect(r).toEqual({ kind: "error", message: detail, status: 400 });
  });

  it("a 201 without a note id cannot be read", () => {
    expect(() => parseNoteId({})).toThrow('"note_id"');
  });
});
