// Code snippets: each page's "Copy code" button copies the notebook's code for that view,
// with the page's ids filled in. `templates.json` holds one template per page; `{name}` marks an id.
// `infra/tests/test_snippets.py` binds every `asteroidea.*` call in them against the real SDK, and
// `snippets.test.ts` checks them against `notebooks/exemplar.ipynb`.
import templates from "./templates.json" with { type: "json" };

export type SnippetPage = keyof typeof templates;

/** The ids each page fills in. */
export interface SnippetValues {
  readonly home: Record<string, never>;
  readonly study: { readonly study: string };
  readonly participant: { readonly subject_key: string };
  readonly session: {
    readonly dataset: string;
    readonly subject: string;
    readonly session: string;
  };
}

/** `value` as a Python string literal (a JSON string is one: same escapes, double quotes). */
export function pyString(value: string): string {
  return JSON.stringify(value);
}

/** What the page's button copies, named for its tooltip. */
export function snippetWhat(page: SnippetPage): string {
  return templates[page].what;
}

/** The page's template with each `{name}` replaced by that value as a Python string literal. */
export function renderSnippet<P extends SnippetPage>(page: P, values: SnippetValues[P]): string {
  const given: Readonly<Record<string, string>> = values;
  return templates[page].code.replace(/\{(\w+)\}/g, (_all, name: string) => {
    const value = given[name];
    if (value === undefined) throw new Error(`the ${page} snippet needs a value for ${name}`);
    return pyString(value);
  });
}
