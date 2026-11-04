// The artboard harness: each surface renders the real page components against the one fixture,
// CATALOG_FIXTURE, with `react-dom/server :: renderToStaticMarkup` under vitest's node environment (no
// jsdom: it has no canvas and lays out at zero). The default suite asserts completeness over that
// markup; `ARTBOARD_WRITE=1` writes it to files for inspection, and `make web-artboards` screenshots the
// running app beside the reference PNGs.
//
// Ceiling: this harness cannot judge the heatmap canvas (it is a declared stand-in here, carrying
// `data-ab-standin` and `data-ab-rows`), nor the pixel fidelity of fonts and colour, nor whether a page
// matches its reference image: those are judged by eye on a real window. The harness produces the
// comparable artifact and proves that nothing in it went missing; it makes no visual comparison.
import { createElement, type ReactElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

export interface El {
  readonly tag: string;
  readonly attrs: Readonly<Record<string, string>>;
  readonly parent: El | null;
  /** Text directly inside this element (not its children's). */
  text: string;
}

/** One count the surface must meet: `noun` names it in the failure message. */
export interface Count {
  readonly noun: string;
  readonly n: number;
  readonly match: (el: El) => boolean;
}

export interface Expectation {
  /** Every element tag the surface may contain; anything else is leakage (an error fallback, a portal). */
  readonly allow: readonly string[];
  readonly counts: readonly Count[];
  /** Strings that must appear in the surface's text (fixture labels). */
  readonly texts: readonly string[];
  /** Sum of `data-ab-rows` over the surface's canvases, when the surface draws one. */
  readonly canvasRows?: number;
}

export type Viewport = "1440x900" | "375x812";

export interface Surface {
  readonly id: string;
  /** The reference PNG's stem in the design reference set. */
  readonly board: string;
  readonly title: string;
  /** The page that makes this surface real; null once it renders the real page. */
  readonly pending: string | null;
  /** The app path the browser run opens. */
  readonly route: string;
  /** The `data-ready` value the browser run waits for on `<main>`. */
  readonly ready: string;
  readonly viewports: readonly Viewport[];
  /** The page must not scroll vertically at 1440x900. */
  readonly noScroll: boolean;
  readonly render: () => ReactElement;
  readonly expect: () => Expectation;
  /** API bodies by path that replace the fixture's defaults in the browser run (a surface variant). */
  readonly api?: Readonly<Record<string, unknown>>;
  /** The HTTP status the browser run answers an `api` path with (default 200): a refusal state. */
  readonly status?: Readonly<Record<string, number>>;
  /** API paths the browser run never answers: the loading state. */
  readonly hold?: readonly string[];
  /** A selector the browser run waits for instead of the ready mark (a state is not a drawn view). */
  readonly waitFor?: string;
}

/** `data-ab` equals `mark`, and every `where` attribute matches. */
export function ab(
  mark: string,
  where: Readonly<Record<string, string>> = {},
): (el: El) => boolean {
  return (el) =>
    el.attrs["data-ab"] === mark && Object.entries(where).every(([k, v]) => el.attrs[k] === v);
}

/** The declared stand-in a pending surface renders until its row lands. */
export function pendingStandIn(row: string, what: string): ReactElement {
  return createElement(
    "section",
    { "data-ab": "pending", "data-ab-standin": `pending ${row}` },
    `${what}: pending ${row}`,
  );
}

export function renderSurface(surface: Surface): string {
  return renderToStaticMarkup(surface.render());
}

const VOID = new Set([
  "area",
  "base",
  "br",
  "col",
  "embed",
  "hr",
  "img",
  "input",
  "link",
  "meta",
  "source",
  "track",
  "wbr",
]);

const ENTITIES: Readonly<Record<string, string>> = {
  amp: "&",
  lt: "<",
  gt: ">",
  quot: '"',
  "#x27": "'",
  "#39": "'",
  nbsp: " ",
};

function decode(s: string): string {
  return s.replace(/&(#x27|#39|amp|lt|gt|quot|nbsp);/g, (_m, e: string) => ENTITIES[e] ?? "");
}

const TAG = /<(\/?)([a-zA-Z][a-zA-Z0-9-]*)((?:\s+[^\s=>/]+(?:="[^"]*")?)*)\s*(\/?)>/g;
const ATTR = /([^\s=>/]+)(?:="([^"]*)")?/g;

/**
 * Parse renderToStaticMarkup output (well-formed, double-quoted attributes) into a flat element list
 * with parent links, and the document's text. Not a general HTML parser.
 */
export function parse(html: string): { els: El[]; text: string } {
  const els: El[] = [];
  const stack: El[] = [];
  let text = "";
  let last = 0;
  for (const m of html.matchAll(TAG)) {
    const chunk = decode(html.slice(last, m.index));
    text += chunk;
    const top = stack.at(-1);
    if (top !== undefined) top.text += chunk;
    last = m.index + m[0].length;
    const [, close, rawTag, rawAttrs, selfClose] = m;
    const tag = (rawTag ?? "").toLowerCase();
    if (close === "/") {
      const open = stack.pop();
      if (open?.tag !== tag) throw new Error(`artboard parse: </${tag}> closes <${open?.tag}>`);
      continue;
    }
    const attrs: Record<string, string> = {};
    for (const a of (rawAttrs ?? "").matchAll(ATTR)) {
      if (a[1] !== undefined) attrs[a[1]] = decode(a[2] ?? "");
    }
    const el: El = { tag, attrs, parent: stack.at(-1) ?? null, text: "" };
    els.push(el);
    if (!VOID.has(tag) && selfClose !== "/") stack.push(el);
  }
  text += decode(html.slice(last));
  if (stack.length > 0) throw new Error(`artboard parse: <${stack.at(-1)?.tag}> never closed`);
  return { els, text };
}

/** True when `el` or an ancestor satisfies `pred`. */
export function within(el: El, pred: (e: El) => boolean): boolean {
  for (let e: El | null = el; e !== null; e = e.parent) if (pred(e)) return true;
  return false;
}

const GEOMETRY: Readonly<Record<string, readonly string[]>> = {
  rect: ["x", "y", "width", "height"],
  line: ["x1", "y1", "x2", "y2"],
  circle: ["cx", "cy", "r"],
  path: ["d"],
  text: ["x", "y"],
};

/** Every completeness failure of `html` against `exp`; empty when complete. */
export function check(html: string, exp: Expectation): string[] {
  const { els, text } = parse(html);
  const failures: string[] = [];
  const allow = new Set(exp.allow);
  const leaked = [...new Set(els.map((e) => e.tag).filter((t) => !allow.has(t)))];
  if (leaked.length > 0) failures.push(`unexpected elements: ${leaked.join(", ")}`);
  for (const c of exp.counts) {
    const got = els.filter(c.match).length;
    if (got !== c.n) failures.push(`expected ${c.n} ${c.noun}, got ${got}`);
  }
  for (const t of exp.texts) {
    if (!text.includes(t)) failures.push(`missing text: ${JSON.stringify(t)}`);
  }
  for (const el of els) {
    for (const a of GEOMETRY[el.tag] ?? []) {
      const v = el.attrs[a];
      if (v === undefined) failures.push(`<${el.tag}> without ${a}`);
      else if (a === "d" ? v.includes("NaN") || v === "" : Number.isNaN(Number(v))) {
        failures.push(`<${el.tag}> ${a}="${v}" is not a number`);
      }
    }
  }
  const canvases = els.filter((e) => e.tag === "canvas");
  let rows = 0;
  for (const c of canvases) {
    if (c.attrs["data-ab-standin"] === undefined) failures.push("<canvas> without data-ab-standin");
    const r = Number(c.attrs["data-ab-rows"]);
    if (!Number.isInteger(r)) failures.push("<canvas> without integer data-ab-rows");
    else rows += r;
  }
  if (exp.canvasRows !== undefined && rows !== exp.canvasRows) {
    failures.push(`expected ${exp.canvasRows} canvas rows, got ${rows}`);
  }
  return failures;
}
