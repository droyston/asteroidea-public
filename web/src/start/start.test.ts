import { describe, expect, it, vi } from "vitest";
import { json, tokens } from "../testApi";
import { loadGetStarted, parseGetStarted } from "./start";

const CFG = { apiUrl: "https://api.example.test" };
const INSTALL =
  "Install uv first if it is missing: https://docs.astral.sh/uv/getting-started/installation/\n" +
  "Then run:\nuvx --from 'https://app.example.test/sdk/a-1-py3-none-any.whl' asteroidea-setup https://app.example.test";
const WIRE = {
  platform: { api_url: "https://api.example.test" },
  wheel: { url: "https://w", filename: "a.whl", sha256: "ab", expires_at: 10 },
  notebook: { url: "https://n", filename: "e.ipynb", expires_at: 10 },
  pins: ["numpy==2.5.4"],
  install: INSTALL,
};
const BODY = { install: INSTALL };

describe("loadGetStarted", () => {
  it("calls GET /get-started with the bearer token and parses the body", async () => {
    const f = vi.fn<(url: string, init: RequestInit) => Promise<Response>>();
    f.mockResolvedValue(json(200, WIRE));
    const r = await loadGetStarted(CFG, tokens(), f);
    expect(f.mock.calls[0]?.[0]).toBe("https://api.example.test/get-started");
    expect(f.mock.calls[0]?.[1].headers).toEqual({ authorization: "Bearer tok" });
    expect(r).toEqual({ kind: "loaded", body: BODY });
  });

  it("keeps the install text exactly, quotes and ampersands included", async () => {
    const f = vi.fn().mockResolvedValue(json(200, WIRE));
    const r = await loadGetStarted(CFG, tokens(), f);
    expect(r.kind === "loaded" && r.body.install).toBe(INSTALL);
  });

  it("reports a body without install text as unreadable", async () => {
    const { install: _drop, ...rest } = WIRE;
    const f = vi.fn().mockResolvedValue(json(200, rest));
    const r = await loadGetStarted(CFG, tokens(), f);
    expect(r.kind === "error" && r.message).toContain("cannot read");
  });

  it("shows the server's error text verbatim on 403 and 503", async () => {
    for (const status of [403, 503]) {
      const f = vi.fn().mockResolvedValue(json(status, { error: `server says ${status}` }));
      expect(await loadGetStarted(CFG, tokens(), f)).toEqual({
        kind: "error",
        message: `server says ${status}`,
      });
    }
  });

  it("names the status when a 403 carries no error text", async () => {
    const f = vi.fn().mockResolvedValue(new Response("nope", { status: 403 }));
    const r = await loadGetStarted(CFG, tokens(), f);
    expect(r.kind === "error" && r.message).toContain("HTTP 403");
  });

  it("reports a network failure specifically", async () => {
    const f = vi.fn().mockRejectedValue(new TypeError("Failed to fetch"));
    const r = await loadGetStarted(CFG, tokens(), f);
    expect(r.kind === "error" && r.message).toContain("Could not reach the platform API");
    expect(r.kind === "error" && r.message).toContain("Failed to fetch");
  });

  it("reports a timeout", async () => {
    const f = vi.fn().mockRejectedValue(new DOMException("t", "TimeoutError"));
    const r = await loadGetStarted(CFG, tokens(), f, 3000);
    expect(r.kind === "error" && r.message).toContain("within 3 seconds");
  });

  it("reports other statuses and unreadable bodies", async () => {
    const f500 = vi.fn().mockResolvedValue(json(500, {}));
    const r1 = await loadGetStarted(CFG, tokens(), f500);
    expect(r1.kind === "error" && r1.message).toContain("HTTP 500");
    const fBad = vi.fn().mockResolvedValue(json(200, { wheel: {} }));
    const r2 = await loadGetStarted(CFG, tokens(), fBad);
    expect(r2.kind === "error" && r2.message).toContain("cannot read");
  });

  it("refreshes once on 401, then ends the session on a second 401", async () => {
    const refresh = vi.fn().mockResolvedValue(true);
    const f = vi.fn().mockResolvedValueOnce(json(401, {})).mockResolvedValueOnce(json(200, WIRE));
    expect((await loadGetStarted(CFG, tokens({ refresh }), f)).kind).toBe("loaded");
    expect(refresh).toHaveBeenCalledTimes(1);

    const g = vi.fn().mockImplementation(() => Promise.resolve(json(401, {})));
    expect(await loadGetStarted(CFG, tokens({ refresh }), g)).toEqual({ kind: "ended" });
  });

  it("ends the session with no token", async () => {
    const f = vi.fn();
    expect(await loadGetStarted(CFG, tokens({ getAccessToken: () => null }), f)).toEqual({
      kind: "ended",
    });
    expect(f).not.toHaveBeenCalled();
  });
});

describe("parseGetStarted", () => {
  it("takes only the install text", () => {
    expect(parseGetStarted(WIRE)).toEqual(BODY);
  });
});
