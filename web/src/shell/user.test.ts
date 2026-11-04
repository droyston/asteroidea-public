import { describe, expect, it } from "vitest";
import { tokenEmail, userLabel } from "./user";

function jwt(claims: Record<string, unknown>): string {
  const b64 = (v: unknown) =>
    btoa(JSON.stringify(v)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  return `${b64({ alg: "none" })}.${b64(claims)}.sig`;
}

describe("userLabel", () => {
  it("is the email claim of the ID token", () => {
    expect(tokenEmail(jwt({ email: "ada@lab.test" }))).toBe("ada@lab.test");
    expect(userLabel(jwt({ email: "ada@lab.test" }), jwt({}))).toBe("ada@lab.test");
  });
  it("falls back to the role when the ID token is absent, opaque or has no email", () => {
    const researcher = jwt({ "cognito:groups": ["researchers"] });
    const curator = jwt({ "cognito:groups": ["researchers", "curators"] });
    expect(userLabel(null, researcher)).toBe("researcher");
    expect(userLabel("id-1", researcher)).toBe("researcher");
    expect(userLabel(jwt({ sub: "x" }), curator)).toBe("curator");
    expect(userLabel(null, null)).toBe("researcher");
  });
});
