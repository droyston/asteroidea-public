// gzip (level 9) the built JS, split by the Vite manifest (build.manifest).
//   main = the entry chunk plus its static imports; compared with bundle-budget.json cap_kb.
//   lazy = each dynamic-import chunk (plus static imports not already in main); each needs its own
//   entry in bundle-budget.json `chunks` (by chunk name), or the gate exits 1.
//   fonts: the `.woff2` files the built CSS references (each Fontsource CSS also lists a `.woff`
//   fallback that browsers do not fetch, so those are not counted), summed as raw bytes, against
//   `fonts_kb`. A `.woff2` in dist/assets that no CSS references fails the gate.
// 1 KB = 1024 bytes. One output line per chunk.
import { readdirSync, readFileSync, statSync } from "node:fs";
import { basename, join } from "node:path";
import { gzipSync } from "node:zlib";

const root = process.argv[2] ?? new URL("..", import.meta.url).pathname;
const dist = join(root, "dist");
const budget = JSON.parse(readFileSync(join(root, "bundle-budget.json"), "utf8"));
const chunkBudgets = budget.chunks ?? {};

function die(message) {
  console.error(`bundle: ${message}`);
  process.exit(1);
}

let manifest;
try {
  manifest = JSON.parse(readFileSync(join(dist, ".vite", "manifest.json"), "utf8"));
} catch (err) {
  die(`cannot read ${join(dist, ".vite", "manifest.json")}: ${err.message} (run vite build first)`);
}

const gzKb = (file) => gzipSync(readFileSync(join(dist, file)), { level: 9 }).length / 1024;
const entries = Object.entries(manifest);
const entry = entries.find(([, c]) => c.isEntry);
if (entry === undefined) die("the manifest has no entry chunk");

// Files reachable from `key` through static imports.
function staticFiles(key, seen = new Set()) {
  const chunk = manifest[key];
  if (chunk === undefined || seen.has(key)) return seen;
  seen.add(key);
  for (const dep of chunk.imports ?? []) staticFiles(dep, seen);
  return seen;
}

const jsOf = (keys) => [...keys].map((k) => manifest[k].file).filter((f) => f.endsWith(".js"));
const mainKeys = staticFiles(entry[0]);
const mainFiles = jsOf(mainKeys);

const claimed = new Set(mainFiles);
const lazy = [];
for (const [key, chunk] of entries) {
  if (!chunk.isDynamicEntry || mainKeys.has(key)) continue;
  const files = jsOf(staticFiles(key)).filter((f) => !claimed.has(f));
  for (const f of files) claimed.add(f);
  lazy.push({ name: chunk.name ?? key, files });
}

let failed = false;
const mainKb = mainFiles.reduce((sum, f) => sum + gzKb(f), 0);
console.log(
  `bundle: main ${mainFiles.join(" ")} ${mainKb.toFixed(1)} KB gz, cap ${budget.cap_kb} KB`,
);
if (mainKb > budget.cap_kb) {
  console.error(`bundle: main ${mainKb.toFixed(1)} KB gz exceeds cap ${budget.cap_kb} KB`);
  failed = true;
}

for (const { name, files } of lazy.sort((a, b) => a.name.localeCompare(b.name))) {
  const kb = files.reduce((sum, f) => sum + gzKb(f), 0);
  const cap = chunkBudgets[name];
  console.log(
    `bundle: lazy ${name} ${files.join(" ")} ${kb.toFixed(1)} KB gz, cap ${cap ?? "none"} KB`,
  );
  if (cap === undefined) {
    console.error(`bundle: lazy chunk ${name} has no budget`);
    failed = true;
  } else if (kb > cap) {
    console.error(`bundle: lazy chunk ${name} ${kb.toFixed(1)} KB gz exceeds cap ${cap} KB`);
    failed = true;
  }
}

// A .js file the manifest does not reach would escape the gate.
for (const f of readdirSync(join(dist, "assets")).filter((n) => n.endsWith(".js"))) {
  if (!claimed.has(`assets/${f}`)) {
    console.error(`bundle: ${f} is not reachable from the manifest`);
    failed = true;
  }
}

// Fonts: only the .woff2 files the built CSS references count; an unreferenced .woff2 fails.
const assetNames = readdirSync(join(dist, "assets"));
const referenced = new Set();
for (const css of assetNames.filter((n) => n.endsWith(".css"))) {
  const text = readFileSync(join(dist, "assets", css), "utf8");
  for (const m of text.matchAll(/url\(\s*["']?([^"')]+?\.woff2)["']?\s*\)/g)) {
    referenced.add(basename(m[1]));
  }
}
let fontBytes = 0;
for (const name of [...referenced].sort()) {
  try {
    fontBytes += statSync(join(dist, "assets", name)).size;
  } catch {
    console.error(`bundle: the CSS references ${name} but dist/assets has no such file`);
    failed = true;
  }
}
const fontsKb = fontBytes / 1024;
const fontsCap = budget.fonts_kb;
console.log(
  `bundle: fonts ${referenced.size} woff2 ${fontsKb.toFixed(2)} KB, cap ${fontsCap ?? "none"} KB`,
);
if (fontsCap === undefined) {
  console.error("bundle: bundle-budget.json has no fonts_kb");
  failed = true;
} else if (fontsKb > fontsCap) {
  console.error(`bundle: fonts ${fontsKb.toFixed(2)} KB exceed cap ${fontsCap} KB`);
  failed = true;
}
for (const name of assetNames.filter((n) => n.endsWith(".woff2") && !referenced.has(n))) {
  console.error(`bundle: ${name} is a .woff2 that no built CSS references`);
  failed = true;
}

process.exit(failed ? 1 : 0);
