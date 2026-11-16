// PWA build (#84): one esbuild bundle + static assets copied to ../tonearm/web-dist.
// esbuild is the ONLY npm dependency — no framework runtime, no lockfile drama.
// CI runs this as a compile check; the Docker build stage runs it for the image
// (node exists only in that stage — the runtime image has no node).
import { build } from "esbuild";
import { cpSync, mkdirSync, writeFileSync } from "fs";
import { dirname, join } from "path";
import { fileURLToPath } from "url";

// resolve EVERYTHING off the script's own directory — `node web/build.mjs`
// must work from the repo root (CI) and from web/ (local) alike
const HERE = dirname(fileURLToPath(import.meta.url));   // .../web
const ROOT = join(HERE, "..");                          // repo root
const OUT = join(ROOT, "tonearm", "web-dist");
mkdirSync(OUT, { recursive: true });

// build stamp — written to build.json AND baked into the bundle. The running
// app polls build.json and reloads when the stamp differs, so a long-lived
// tab (or a stale service-worker shell) can never pin an old bundle forever.
// Found live: an hours-old PWA window kept serving the pre-redesign shell.
const STAMP = new Date().toISOString();

await build({
  entryPoints: [join(HERE, "src", "main.js")],
  bundle: true,
  minify: true,
  outfile: `${OUT}/app.js`,
  format: "iife",
  target: ["es2020"],
  define: { __BUILD__: JSON.stringify(STAMP) },
  logLevel: "info",
});

for (const f of ["index.html", "manifest.webmanifest", "sw.js", "styles.css",
                 "icon.png"]) {
  try { cpSync(join(HERE, f), join(OUT, f)); } catch { /* optional asset */ }
}

writeFileSync(`${OUT}/build.json`, JSON.stringify({ built: STAMP }, null, 1));
console.log("PWA built ->", OUT, `(build ${STAMP})`);
