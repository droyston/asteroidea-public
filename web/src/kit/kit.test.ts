import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { Status } from "../App";
import { AuthControl } from "../auth/AuthControl";
import { AuthStore } from "../auth/store";
import { UploadsView } from "../auth/UploadsPanel";
import { PicksRail } from "../find/PicksRail";
import { TargetsStore } from "../targets/store";
import { Button } from "./Button";
import { KeyValue } from "./KeyValue";
import { Nav } from "./Nav";
import { Notice } from "./Notice";
import { Shell } from "./Shell";
import { type Column, Table } from "./Table";
import { nextTab, searchWithTab, Tabs, tabFromSearch } from "./Tabs";

const SRC = new URL("..", import.meta.url).pathname;

/** Raw colour values: hex, rgb()/rgba(), hsl()/hsla(). Only kit/tokens.css may hold one. */
function rawColours(css: string): string[] {
  const noComments = css.replace(/\/\*[\s\S]*?\*\//g, "");
  return noComments.match(/#[0-9a-fA-F]{3,8}\b|\b(?:rgba?|hsla?)\(/g) ?? [];
}

function cssFiles(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((e) =>
    e.isDirectory()
      ? cssFiles(join(dir, e.name))
      : e.name.endsWith(".css")
        ? [join(dir, e.name)]
        : [],
  );
}

describe("tokens", () => {
  it("no CSS file under web/src except kit/tokens.css holds a raw colour", () => {
    const files = cssFiles(SRC);
    expect(files.length).toBeGreaterThanOrEqual(3);
    for (const file of files.filter((f) => !f.endsWith(join("kit", "tokens.css")))) {
      expect({ file, hits: rawColours(readFileSync(file, "utf8")) }).toEqual({ file, hits: [] });
    }
  });

  it("the check can fail: a planted hex, rgb() and hsl() are found", () => {
    expect(rawColours("a { color: #abc; }")).toEqual(["#abc"]);
    expect(rawColours("a { color: rgb(1 2 3); background: hsla(0 0% 0%); }")).toHaveLength(2);
    expect(rawColours("/* #fff */ a { color: var(--color-text); }")).toEqual([]);
  });

  it("tokens.css defines light and dark, a focus ring, spacing, radius and type", () => {
    const css = readFileSync(join(SRC, "kit", "tokens.css"), "utf8");
    expect(css).toContain("prefers-color-scheme: dark");
    for (const name of ["--color-surface", "--color-text", "--color-muted", "--color-border"]) {
      expect(css).toContain(name);
    }
    for (const name of ["--color-accent", "--color-danger", "--color-success", "--focus-ring"]) {
      expect(css).toContain(name);
    }
    for (const name of ["--space-1", "--radius", "--text-md"]) expect(css).toContain(name);
  });

  it("the two dark blocks hold identical declarations, and set color-scheme", () => {
    const css = readFileSync(join(SRC, "kit", "tokens.css"), "utf8");
    const { media, attr } = darkBlocks(css);
    expect([...declarations(attr)]).toEqual([...declarations(media)]);
    expect(declarations(media).size).toBeGreaterThan(30);
    expect(css).toMatch(/:root\[data-theme="light"\]\s*{\s*color-scheme: light;/);
    expect(attr).toMatch(/color-scheme: dark;/);
    expect(media).toMatch(/color-scheme: dark;/);
  });

  it("every interactive kit element has a focus ring rule", () => {
    const css = readFileSync(join(SRC, "kit", "kit.css"), "utf8");
    expect(css).toMatch(/:focus-visible\s*{[^}]*var\(--focus-ring\)/);
  });
});

/** Custom properties of a CSS block as name -> value. */
function declarations(block: string): Map<string, string> {
  const out = new Map<string, string>();
  for (const m of block.replace(/\/\*[\s\S]*?\*\//g, "").matchAll(/(--[\w-]+)\s*:\s*([^;]+);/g)) {
    out.set(m[1] as string, (m[2] as string).trim());
  }
  return out;
}

/** The dark blocks of tokens.css: the media-query one and the explicit-choice one. */
function darkBlocks(css: string): { media: string; attr: string } {
  const media =
    /@media \(prefers-color-scheme: dark\)\s*{\s*:root:not\(\[data-theme="light"\]\)\s*{([^}]*)}/.exec(
      css,
    );
  const attr = /:root\[data-theme="dark"\]\s*{([^}]*)}/.exec(css);
  if (media === null || attr === null) throw new Error("tokens.css lacks a dark block");
  return { media: media[1] as string, attr: attr[1] as string };
}

/** The light theme (the first :root block) and the dark theme (light overlaid by the dark block). */
function themes(css: string): { light: Map<string, string>; dark: Map<string, string> } {
  const light = declarations(css.slice(0, css.indexOf("@media (prefers-color-scheme: dark)")));
  const dark = new Map([...light, ...declarations(darkBlocks(css).media)]);
  return { light, dark };
}

function luminance(hex: string): number {
  const h = hex.replace("#", "");
  const v = [0, 2, 4].map((i) => {
    const c = Number.parseInt(h.slice(i, i + 2), 16) / 255;
    return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * (v[0] as number) + 0.7152 * (v[1] as number) + 0.0722 * (v[2] as number);
}

/** WCAG 2.x contrast ratio of two #rrggbb colours. */
function contrast(a: string, b: string): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return ((hi as number) + 0.05) / ((lo as number) + 0.05);
}

const MIN_REGION_RATIO = 3;
const REGION_NAMES = [
  "amygdala-l",
  "amygdala-r",
  "hippocampus-l",
  "hippocampus-r",
  "presma",
  "vmpfc",
  "acc",
];
const REGION_EXTRAS = [1, 2, 3, 4, 5, 6, 7, 8].map((n) => `x${n}`);

/** Every region token of a theme under 3:1 against either surface, as "name vs surface ratio". */
function regionFailures(theme: Map<string, string>): string[] {
  const out: string[] = [];
  for (const surface of ["--color-surface", "--color-surface-raised"]) {
    const bg = theme.get(surface) as string;
    for (const [name, value] of theme) {
      if (!name.startsWith("--color-region-")) continue;
      const ratio = contrast(value, bg);
      if (ratio < MIN_REGION_RATIO) out.push(`${name} ${value} on ${surface} ${ratio.toFixed(2)}`);
    }
  }
  return out;
}

describe("region tokens", () => {
  const css = readFileSync(join(SRC, "kit", "tokens.css"), "utf8");
  const { light, dark } = themes(css);

  it("the seven named regions and x1..x8 exist in both themes", () => {
    for (const theme of [light, dark]) {
      for (const n of [...REGION_NAMES, ...REGION_EXTRAS]) {
        expect(theme.get(`--color-region-${n}`), n).toMatch(/^#[0-9a-f]{6}$/);
      }
    }
  });

  it("every region token is at least 3:1 against both surfaces of its theme", () => {
    expect(regionFailures(light)).toEqual([]);
    expect(regionFailures(dark)).toEqual([]);
  });

  it("the check can fail: the README hues as given are found failing", () => {
    expect(contrast("#ffffff", "#000000")).toBeCloseTo(21, 5);
    const planted = new Map([
      ["--color-surface", "#ffffff"],
      ["--color-surface-raised", "#f6f8fa"],
      ["--color-region-amygdala-l", "#E69F00".toLowerCase()],
    ]);
    expect(regionFailures(planted)).toEqual([
      "--color-region-amygdala-l #e69f00 on --color-surface 2.25",
      "--color-region-amygdala-l #e69f00 on --color-surface-raised 2.12",
    ]);
    const darkPlanted = new Map([
      ["--color-surface", "#0d1117"],
      ["--color-surface-raised", "#161b22"],
      ["--color-region-acc", "#882255"],
    ]);
    expect(regionFailures(darkPlanted)[0]).toMatch(/#882255 on --color-surface 2\.17$/);
  });

  it("the type, hairline and diverging tokens exist", () => {
    for (const name of [
      "--font-serif",
      "--font-sans",
      "--font-mono",
      "--color-ink",
      "--color-hairline",
    ]) {
      expect(light.has(name), name).toBe(true);
    }
    for (let i = 1; i <= 7; i++) {
      expect(light.has(`--color-div-${i}`), `div-${i}`).toBe(true);
      expect(dark.has(`--color-div-${i}`), `div-${i}`).toBe(true);
    }
  });
});

describe("fonts", () => {
  const WEB = join(SRC, "..");
  const FONTS = ["source-serif-4", "ibm-plex-sans", "ibm-plex-mono"];

  it("each OFL text in public/licenses is byte-equal to its package's LICENSE", () => {
    for (const name of FONTS) {
      const shipped = readFileSync(join(WEB, "public", "licenses", `${name}-OFL.txt`));
      const pkg = readFileSync(join(WEB, "node_modules", "@fontsource", name, "LICENSE"));
      expect(shipped.equals(pkg), name).toBe(true);
    }
  });

  it("the font packages are pinned exactly", () => {
    const deps = JSON.parse(readFileSync(join(WEB, "package.json"), "utf8")).dependencies;
    expect(deps["@fontsource/source-serif-4"]).toBe("5.3.1");
    expect(deps["@fontsource/ibm-plex-sans"]).toBe("5.3.0");
    expect(deps["@fontsource/ibm-plex-mono"]).toBe("5.3.0");
  });
});

describe("kit pieces", () => {
  it("Button renders each variant as type=button", () => {
    for (const variant of ["primary", "secondary", "quiet"] as const) {
      const html = renderToStaticMarkup(createElement(Button, { variant }, "Go"));
      expect(html).toContain('type="button"');
      expect(html).toContain(`btn-${variant}`);
    }
  });

  it("Nav marks only the current route with aria-current=page and uses hrefs", () => {
    const html = renderToStaticMarkup(
      createElement(Nav, {
        items: [
          { to: "/", label: "Datasets", current: true },
          { to: "/catalog", label: "Catalog", current: false },
        ],
      }),
    );
    expect(html.match(/aria-current="page"/g)).toHaveLength(1);
    expect(html).toMatch(/<a href="\/" aria-current="page">Datasets<\/a>/);
    expect(html).toContain('<a href="/catalog">Catalog</a>');
  });

  it("Notice: error is an alert, info and success are status, and the action is a button", () => {
    const error = renderToStaticMarkup(
      createElement(
        Notice,
        { kind: "error", action: { label: "Try again", onClick: () => undefined } },
        "bad",
      ),
    );
    expect(error).toContain('role="alert"');
    expect(error).toContain("notice-error");
    expect(error).toContain(">Try again</button>");
    for (const kind of ["info", "success"] as const) {
      const html = renderToStaticMarkup(createElement(Notice, { kind }, "ok"));
      expect(html).toContain('role="status"');
      expect(html).not.toContain("<button");
    }
  });

  it("Shell renders the title, status, auth slot, nav and a main region", () => {
    const html = renderToStaticMarkup(
      createElement(
        Shell,
        { title: "T", status: "S", auth: "A", nav: createElement("nav") },
        createElement("p", null, "body"),
      ),
    );
    expect(html).toContain("<h1>T</h1>");
    expect(html).toContain("shell-status");
    expect(html).toContain("shell-auth");
    expect(html).toMatch(/<main class="shell-main"><p>body<\/p><\/main>/);
  });
});

describe("error states render through Notice", () => {
  const noticed = (html: string) => {
    expect(html).toContain('class="notice notice-error"');
    expect(html).toContain('role="alert"');
    expect(html).not.toContain("status error");
  };

  it("datasets survey: index missing and index error", () => {
    noticed(
      renderToStaticMarkup(createElement(Status, { state: { kind: "missing", detail: "d" } })),
    );
    noticed(
      renderToStaticMarkup(createElement(Status, { state: { kind: "error", message: "m" } })),
    );
  });

  it("picks: the store error", () => {
    const store = new TargetsStore(null);
    const html = renderToStaticMarkup(
      createElement(PicksRail, { store, state: { targets: [], error: "disk full" }, notice: null }),
    );
    noticed(html);
    expect(html).toContain("disk full");
  });

  it("uploads: the load error carries a Try again action", () => {
    const html = renderToStaticMarkup(
      createElement(UploadsView, {
        state: { kind: "error", message: "nope" },
        onRetry: () => undefined,
      }),
    );
    noticed(html);
    expect(html).toContain('data-testid="uploads-error"');
    expect(html).toContain(">Try again</button>");
  });

  it("auth control: a sign-in notice", () => {
    const data = new Map<string, string>();
    const auth = new AuthStore(
      {
        authOrigin: "https://auth.example.test",
        clientId: "c",
        apiUrl: "https://api.example.test",
      },
      {
        storage: {
          getItem: (k) => data.get(k) ?? null,
          setItem: (k, v) => void data.set(k, v),
          removeItem: (k) => void data.delete(k),
        },
        fetch: () => Promise.reject(new Error("offline")),
        crypto: globalThis.crypto,
        origin: "http://localhost:5173",
        navigate: () => undefined,
      },
    );
    auth.expire("Session ended");
    const html = renderToStaticMarkup(createElement(AuthControl, { auth }));
    noticed(html);
    expect(html).toContain('data-testid="auth-notice"');
    expect(html).toContain("Sign in</button>");
  });
});

describe("research kit pieces", () => {
  type Row = { n: string; c: number };
  const columns: Column<Row>[] = [
    { key: "a", header: "Name", render: (r) => r.n },
    { key: "b", header: "Count", numeric: true, render: (r) => r.c },
  ];

  it("Table right-aligns numeric columns and binds paging to page and more", () => {
    const html = renderToStaticMarkup(
      createElement(Table<Row>, {
        columns,
        rows: [{ n: "x", c: 3 }],
        rowKey: (r) => r.n,
        label: "T",
        paging: { page: 1, more: false, onPage: () => undefined },
      }),
    );
    expect(html).toContain('<th class="num">Count</th>');
    expect(html).toContain('<td class="num">3</td>');
    expect(html).toContain("Page 2");
    expect(html).toMatch(/<button[^>]*>Previous<\/button>/);
    expect(html).toMatch(/<button[^>]*disabled=""[^>]*>Next<\/button>/);
    expect(html).not.toMatch(/<button[^>]*disabled=""[^>]*>Previous<\/button>/);
  });

  it("Table: page 0 disables Previous, an empty table shows the message and no table", () => {
    const first = renderToStaticMarkup(
      createElement(Table<Row>, {
        columns,
        rows: [{ n: "x", c: 1 }],
        rowKey: (r) => r.n,
        label: "T",
        paging: { page: 0, more: true, onPage: () => undefined },
      }),
    );
    expect(first).toMatch(/<button[^>]*disabled=""[^>]*>Previous<\/button>/);
    expect(first).not.toMatch(/<button[^>]*disabled=""[^>]*>Next<\/button>/);
    const empty = renderToStaticMarkup(
      createElement(Table<Row>, {
        columns,
        rows: [],
        rowKey: (r) => r.n,
        label: "T",
        empty: "No rows.",
      }),
    );
    expect(empty).toContain("No rows.");
    expect(empty).not.toContain("<table");
  });

  it("Tabs: a tablist, one selected tab with tabindex 0, the others -1", () => {
    const html = renderToStaticMarkup(
      createElement(
        Tabs,
        {
          tabs: [
            { id: "a", label: "A" },
            { id: "b", label: "B" },
          ],
          selected: "b",
          onSelect: () => undefined,
          label: "Sections",
        },
        "body",
      ),
    );
    expect(html).toContain('role="tablist"');
    expect(html.match(/aria-selected="true"/g)).toHaveLength(1);
    expect(html).toMatch(/id="tab-b"[^>]*aria-selected="true"[^>]*tabindex="0"/);
    expect(html).toMatch(/id="tab-a"[^>]*aria-selected="false"[^>]*tabindex="-1"/);
    expect(html).toContain('role="tabpanel"');
    expect(html).toContain('aria-labelledby="tab-b"');
  });

  it("nextTab: arrows wrap, Home and End jump, other keys do not move", () => {
    const ids = ["a", "b", "c"];
    expect(nextTab(ids, "a", "ArrowRight")).toBe("b");
    expect(nextTab(ids, "c", "ArrowRight")).toBe("a");
    expect(nextTab(ids, "a", "ArrowLeft")).toBe("c");
    expect(nextTab(ids, "b", "Home")).toBe("a");
    expect(nextTab(ids, "b", "End")).toBe("c");
    expect(nextTab(ids, "b", "Enter")).toBeNull();
    expect(nextTab(ids, "zz", "ArrowRight")).toBeNull();
  });

  it("the selected tab round-trips through the URL query", () => {
    const ids = ["subjects", "sessions"];
    expect(tabFromSearch("", ids)).toBe("subjects");
    expect(tabFromSearch("?tab=sessions", ids)).toBe("sessions");
    expect(tabFromSearch("?tab=nope", ids)).toBe("subjects");
    expect(searchWithTab("", "sessions", "subjects")).toBe("?tab=sessions");
    expect(searchWithTab("?tab=sessions", "subjects", "subjects")).toBe("");
    expect(searchWithTab("?x=1", "sessions", "subjects")).toBe("?x=1&tab=sessions");
  });

  it("KeyValue is a definition list and shows a dash for an empty value", () => {
    const html = renderToStaticMarkup(
      createElement(KeyValue, {
        items: [
          { label: "Licence", value: "CC0" },
          { label: "DOI", value: null },
        ],
      }),
    );
    expect(html).toContain("<dl");
    expect(html).toContain("<dt>Licence</dt><dd>CC0</dd>");
    expect(html).toContain("<dt>DOI</dt><dd>—</dd>");
  });
});
