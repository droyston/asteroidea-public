// The four page snippets stay the notebook's code: every `asteroidea.<call>` a
// template makes is a call `notebooks/exemplar.ipynb` makes, and every template line without an id in it
// is a line of a notebook code cell. A change to either side that the other lacks fails here.
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { pyString, renderSnippet, type SnippetPage, snippetWhat } from "./render";
import templates from "./templates.json" with { type: "json" };

const NOTEBOOK = new URL("../../../notebooks/exemplar.ipynb", import.meta.url);

interface Cell {
  cell_type: string;
  source: string[];
}

const cells: Cell[] = (JSON.parse(readFileSync(NOTEBOOK, "utf8")) as { cells: Cell[] }).cells;
const notebookLines = cells
  .filter((c) => c.cell_type === "code")
  .flatMap((c) => c.source.join("").split("\n"));
const notebookCalls = new Set(
  notebookLines.flatMap((l) => [...l.matchAll(/asteroidea\.(\w+)\(/g)].map((m) => m[1])),
);

/** Template lines that only wrap the notebook's own (its message is built from the session ids; a bare `links` shows the table). */
const OWN_LINES = new Set(['    msg = "No stored summary for this session yet."', "links"]);

const PAGES = Object.keys(templates) as SnippetPage[];

describe("snippet templates against the notebook", () => {
  it("has the four pages", () => {
    expect(PAGES).toEqual(["home", "study", "participant", "session"]);
  });

  for (const page of PAGES) {
    const code = templates[page].code;
    it(`${page}: each asteroidea call is one the notebook makes`, () => {
      const calls = [...code.matchAll(/asteroidea\.(\w+)\(/g)].map((m) => m[1]);
      expect(calls.length).toBeGreaterThan(1);
      expect(calls).toContain("sign_in");
      for (const call of calls) expect(notebookCalls, `${page}: ${call}`).toContain(call);
    });

    it(`${page}: starts with the import and the sign-in, so it runs alone`, () => {
      expect(code.startsWith("import asteroidea\n\nasteroidea.sign_in()\n")).toBe(true);
    });

    it(`${page}: each line without an id is a notebook line`, () => {
      const own = code
        .split("\n")
        .filter((l) => l !== "" && !l.includes("{") && l !== "import asteroidea")
        .filter((l) => !OWN_LINES.has(l));
      for (const line of own) expect(notebookLines, `${page}: ${line}`).toContain(line);
    });
  }
});

describe("renderSnippet", () => {
  it("fills the session ids as Python string literals", () => {
    const text = renderSnippet("session", {
      dataset: "dandi-000623",
      subject: "sub-CS62",
      session: "ses-P62CSR2",
    });
    expect(text).toContain('asteroidea.session_summary("dandi-000623", "sub-CS62", "ses-P62CSR2")');
    expect(text).not.toMatch(/\{\w+\}/);
  });

  it("escapes quotes, backslashes and newlines in a value", () => {
    expect(pyString('a"b\\c\nd')).toBe('"a\\"b\\\\c\\nd"');
    expect(renderSnippet("study", { study: 'x"y' })).toContain('study="x\\"y"');
  });

  it("renders the home page with no values and names what each copies", () => {
    expect(renderSnippet("home", {})).toContain("asteroidea.studies()");
    for (const page of PAGES) expect(snippetWhat(page)).toMatch(/^the code that /);
  });
});
