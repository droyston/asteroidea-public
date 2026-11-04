import { describe, expect, it } from "vitest";
import { json } from "../testApi";
import { serverError, serverRefusal } from "./apiGet";

describe("serverRefusal", () => {
  it("reads the uniform body: the code from error, the text from detail", async () => {
    const r = json(403, { error: "dataset_not_yours", detail: "dataset ds1 belongs to another" });
    expect(await serverRefusal(r)).toEqual({
      code: "dataset_not_yours",
      text: "dataset ds1 belongs to another",
    });
  });

  it("reads an older API's body: its error is the text and there is no code", async () => {
    expect(await serverRefusal(json(409, { error: "raw key already exists" }))).toEqual({
      code: null,
      text: "raw key already exists",
    });
  });

  it("finds nothing in an empty, non-JSON or non-text body", async () => {
    const none = { code: null, text: null };
    expect(await serverRefusal(json(500, {}))).toEqual(none);
    expect(await serverRefusal(new Response("oops", { status: 502 }))).toEqual(none);
    expect(await serverRefusal(json(400, { error: 7, detail: "" }))).toEqual(none);
  });
});

describe("serverError", () => {
  it("is the detail, else an older API's error text, else null", async () => {
    expect(await serverError(json(404, { error: "not_found", detail: "no such asset" }))).toBe(
      "no such asset",
    );
    expect(await serverError(json(404, { error: "no such asset" }))).toBe("no such asset");
    expect(await serverError(json(404, {}))).toBeNull();
  });
});
