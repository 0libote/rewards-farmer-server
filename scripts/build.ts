#!/usr/bin/env bun
/**
 * Build dashboard frontend assets with Bun.
 *
 *  1. Compiles `web/src/input.css` -> `web/static/app.css` with the pinned
 *     Tailwind CLI (replaces the 407 KB in-browser Play CDN JIT).
 *  2. Copies the pinned Lucide UMD bundle from node_modules into
 *     `web/static/vendor/` (replaces the checked-in 365 KB blob).
 *
 * Run:  bun install && bun run build
 * The Dockerfile runs this automatically; output files are gitignored.
 */
import { $ } from "bun";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
const LUCIDE_VERSION = "0.544.0";
const LUCIDE_SRC = join(ROOT, "node_modules", "lucide", "dist", "umd", "lucide.min.js");
const LUCIDE_DEST = join(ROOT, "web", "static", "vendor", `lucide-${LUCIDE_VERSION}.min.js`);
const CSS_OUT = join(ROOT, "web", "static", "app.css");

// 1. Tailwind: scan web/index.html and emit minified CSS.
await $`./node_modules/.bin/tailwindcss -i web/src/input.css -o web/static/app.css --minify`.cwd(ROOT);

// 2. Lucide: copy the pinned UMD bundle (Bun.file -> Bun.write, no subprocess).
const lucide = Bun.file(LUCIDE_SRC);
if (!(await lucide.exists())) {
  console.error(`Missing ${LUCIDE_SRC}. Did you run 'bun install'?`);
  process.exit(1);
}
await Bun.write(LUCIDE_DEST, lucide);

// 3. Sanity-check the artifacts so a broken build fails loudly in CI.
const css = Bun.file(CSS_OUT);
const cssText = await css.text();
const checks: Array<[string, boolean]> = [
  ["app.css non-empty", cssText.length > 0],
  ["app.css sane size (<150 KB)", cssText.length < 150 * 1024],
  ["contains .bg-zinc-950", cssText.includes(".bg-zinc-950")],
  ["contains .animate-pulse", cssText.includes(".animate-pulse")],
  ["contains .terminal-font", cssText.includes(".terminal-font")],
  ["contains scrollbar style", cssText.includes("::-webkit-scrollbar")],
];
const lucideText = await Bun.file(LUCIDE_DEST).text();
checks.push(["lucide exposes createIcons", lucideText.includes("createIcons")]);

let failed = false;
for (const [name, ok] of checks) {
  console.log(`${ok ? "ok" : "FAIL"} - ${name}`);
  if (!ok) failed = true;
}
console.log(`app.css: ${(css.size / 1024).toFixed(1)} KB`);
console.log(`lucide: ${(lucide.size / 1024).toFixed(1)} KB`);
if (failed) {
  console.error("Asset verification failed.");
  process.exit(1);
}
console.log("Frontend assets built successfully.");
