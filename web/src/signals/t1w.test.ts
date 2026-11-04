import { describe, expect, it } from "vitest";
import goldenLinks from "../../e2e/fixtures/ux/catalog-links.golden.json" with { type: "json" };
import goldenStudies from "../../e2e/fixtures/ux/catalog-studies.golden.json" with { type: "json" };
import { parseParticipantLinks } from "../participant/model";
import { parseStudies } from "../study/model";
import { t1wSource } from "./t1w";

const STUDIES = parseStudies(goldenStudies);
const LINKS = parseParticipantLinks(goldenLinks);

describe("t1wSource", () => {
  it("reaches the linked record's MRI session through a pending link", () => {
    const got = t1wSource("dandi-000623", "CS41", STUDIES, LINKS);
    expect(got?.target.dataset).toBe("openneuro-ds004798");
    expect(got?.target.assetIds.slice(0, 2)).toEqual(["m41-scans", "m41-t1"]);
    expect(got?.pending).toBe(true);
  });

  it("is not pending once every link of the person is confirmed", () => {
    const confirmed = LINKS.map((l) => ({ ...l, status: "confirmed" }));
    expect(t1wSource("dandi-000623", "CS41", STUDIES, confirmed)?.pending).toBe(false);
  });

  it("finds nothing for a subject no link names and with no MRI of its own", () => {
    expect(t1wSource("dandi-000623", "sub-CS41", STUDIES, LINKS)).toBeNull();
  });
});
