import { describe, expect, it } from "vitest";
import {
  authorText,
  createLabel,
  deleteLabel,
  draftOf,
  labelAlignOptions,
  labelsPath,
  parseLabel,
  parseLabels,
  updateLabel,
  validateDraft,
} from "./labels";

const wire = {
  label_id: "abc",
  name: "burst",
  note: "",
  start_s: 1.5,
  stop_s: 2.5,
  mine: true,
  version: 1,
  dataset: "dandi-000623",
  s3_version_id: "v1",
  snapshots: null,
  created_at: 1,
  updated_at: 1,
};

interface Call {
  url: string;
  method: string | undefined;
  body: string | undefined;
}

function recorder(status: number, body: unknown) {
  const calls: Call[] = [];
  const fetchFn = (url: string, init: RequestInit) => {
    calls.push({ url, method: init.method, body: init.body as string | undefined });
    const text = status === 204 ? null : JSON.stringify(body);
    return Promise.resolve(new Response(text, { status }));
  };
  return { calls, fetchFn };
}

const tokens = { getAccessToken: () => "t", refresh: () => Promise.resolve(false) };
const config = { apiUrl: "https://api.test" };

describe("parsing", () => {
  it("keeps the fields the page uses and defaults a missing note", () => {
    expect(parseLabel({ ...wire, note: undefined }).note).toBe("");
    expect(parseLabels({ labels: [wire] })).toHaveLength(1);
  });
  it("rejects a malformed label", () => {
    expect(() => parseLabel({ ...wire, mine: "yes" })).toThrow('"mine"');
    expect(() => parseLabels({})).toThrow("labels");
  });
});

describe("validateDraft", () => {
  const ok = { name: "n", note: "", start: "1", stop: "2" };
  it("accepts a valid draft", () => {
    expect(validateDraft(ok)).toEqual({
      ok: true,
      fields: { name: "n", note: "", start_s: 1, stop_s: 2 },
    });
  });
  it("refuses an empty or 65-character name", () => {
    expect(validateDraft({ ...ok, name: "  " }).ok).toBe(false);
    expect(validateDraft({ ...ok, name: "x".repeat(64) }).ok).toBe(true);
    expect(validateDraft({ ...ok, name: "x".repeat(65) }).ok).toBe(false);
  });
  it("refuses a note over 1024 bytes, a stop before start and a blank time", () => {
    expect(validateDraft({ ...ok, note: "é".repeat(513) }).ok).toBe(false);
    expect(validateDraft({ ...ok, start: "3" }).ok).toBe(false);
    expect(validateDraft({ ...ok, stop: "" }).ok).toBe(false);
  });
  it("rounds a dragged span for the editor", () => {
    expect(draftOf({ start_s: 1.23456, stop_s: 2 })).toMatchObject({ start: "1.235", stop: "2" });
  });
});

describe("the calls", () => {
  it("POST sends the four fields", async () => {
    const { calls, fetchFn } = recorder(201, wire);
    const fields = { name: "burst", note: "", start_s: 1.5, stop_s: 2.5 };
    const r = await createLabel(config, "a/1", fields, tokens, fetchFn);
    expect(r.kind).toBe("loaded");
    expect(calls[0]).toEqual({
      url: `https://api.test${labelsPath("a/1")}`,
      method: "POST",
      body: JSON.stringify(fields),
    });
  });
  it("PATCH sends the fields and the version", async () => {
    const { calls, fetchFn } = recorder(200, { ...wire, version: 2 });
    const fields = { name: "burst", note: "x", start_s: 1, stop_s: 2 };
    await updateLabel(config, "a", { label_id: "abc", version: 1 }, fields, tokens, fetchFn);
    expect(calls[0]?.method).toBe("PATCH");
    expect(calls[0]?.url).toBe("https://api.test/files/a/labels/abc");
    expect(JSON.parse(calls[0]?.body ?? "")).toEqual({ ...fields, version: 1 });
  });
  it("DELETE sends no body and accepts a 204", async () => {
    const { calls, fetchFn } = recorder(204, null);
    const r = await deleteLabel(config, "a", "abc", tokens, fetchFn);
    expect(r).toEqual({ kind: "loaded", value: null });
    expect(calls[0]).toMatchObject({ method: "DELETE", body: undefined });
  });
  it("a 409, a 403 and a 500 carry the status and the server's detail", async () => {
    for (const status of [403, 409, 500]) {
      const body = { error: `code_${status}`, detail: `server says ${status}` };
      const { fetchFn } = recorder(status, body);
      const r = await deleteLabel(config, "a", "abc", tokens, fetchFn);
      expect(r).toEqual({ kind: "error", message: `server says ${status}`, status });
    }
  });
  it("a 409 and a 403 from an older API carry its error text", async () => {
    for (const status of [403, 409]) {
      const { fetchFn } = recorder(status, { error: `server says ${status}` });
      const r = await deleteLabel(config, "a", "abc", tokens, fetchFn);
      expect(r).toEqual({ kind: "error", message: `server says ${status}`, status });
    }
  });
  it("a 401 refreshes once and then ends the session", async () => {
    let refreshes = 0;
    const t = {
      getAccessToken: () => "t",
      refresh: () => {
        refreshes += 1;
        return Promise.resolve(true);
      },
    };
    const { fetchFn } = recorder(401, {});
    expect(await deleteLabel(config, "a", "abc", t, fetchFn)).toEqual({ kind: "ended" });
    expect(refreshes).toBe(1);
  });
});

describe("align options", () => {
  it("offers each label and each distinct name", () => {
    const a = parseLabel(wire);
    const b = parseLabel({ ...wire, label_id: "def", mine: false });
    expect(labelAlignOptions([a, b])).toEqual([
      { value: "label:abc", label: "Label: burst" },
      { value: "label:def", label: "Label: burst" },
      { value: "label_name:burst", label: "All labels named burst" },
    ]);
  });
  it("words the author without the sub", () => {
    expect(authorText({ mine: true })).toBe("you");
    expect(authorText({ mine: false })).toBe("another researcher");
  });
});
