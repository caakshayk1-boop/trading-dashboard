// build.js — assembles public/ for the Vercel deploy.
//
// The daily paper (weather, news, lessons, chess) is still rendered once a day
// by generate.py into docs/index.html. This copies that shell into public/.
// Everything live — signals, positions, stats, archive — is fetched from /api
// at page load, so a stale shell never means stale market data.
//
// Two situations, both handled:
//   1. Repo checkout available  → copy ../docs/index.html
//   2. CI copied it in already  → public/index.html already present, keep it
import { existsSync, mkdirSync, copyFileSync, writeFileSync, statSync, readdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const publicDir = join(here, "public");
const target = join(publicDir, "index.html");
const source = join(here, "..", "docs", "index.html");
const alertsSrc = join(here, "..", "docs", "alerts.json");
// Build stamp for the shell. Open tabs poll it to notice they are showing a
// superseded edition; without it they render yesterday's paper indefinitely.
const editionSrc = join(here, "..", "docs", "edition.json");

mkdirSync(publicDir, { recursive: true });

if (existsSync(source)) {
  copyFileSync(source, target);
  if (existsSync(alertsSrc)) copyFileSync(alertsSrc, join(publicDir, "alerts.json"));
  if (existsSync(editionSrc)) copyFileSync(editionSrc, join(publicDir, "edition.json"));
  // Crawl files, PWA manifest and the social card. All 404'd before:
  // /robots.txt, /sitemap.xml, /favicon.ico and /manifest.json.
  for (const f of ["desk.html", "robots.txt", "sitemap.xml", "manifest.webmanifest", "icon.svg", "og.png"]) {
    const src = join(here, "..", "docs", f);
    if (existsSync(src)) copyFileSync(src, join(publicDir, f));
  }
  // Self-hosted faces. These are the reason the page contacts no third party.
  const fontSrc = join(here, "..", "docs", "fonts");
  if (existsSync(fontSrc)) {
    const fontOut = join(publicDir, "fonts");
    mkdirSync(fontOut, { recursive: true });
    let n = 0;
    for (const f of readdirSync(fontSrc)) {
      if (f.endsWith(".woff2")) { copyFileSync(join(fontSrc, f), join(fontOut, f)); n++; }
    }
    console.log(`[build] copied ${n} font files`);
  }
  console.log(`[build] copied docs/index.html → public/ (${Math.round(statSync(target).size / 1024)}KB)`);
} else if (existsSync(target)) {
  console.log(`[build] using pre-staged public/index.html (${Math.round(statSync(target).size / 1024)}KB)`);
} else {
  // Never ship a blank domain. A deploy that lost its shell should say so
  // rather than 404, because the API layer below it is still fine.
  writeFileSync(
    target,
    `<!doctype html><meta charset="utf-8"><title>THE DAILY SIGNAL</title>
<body style="background:#0a0d0a;color:#c8d6c8;font:15px/1.6 ui-monospace,monospace;padding:48px;max-width:640px">
<h1 style="color:#c3f53c;font-size:20px">THE DAILY SIGNAL</h1>
<p>The daily shell was not included in this deploy. The live API is unaffected:</p>
<ul>
  <li><a style="color:#c3f53c" href="/api/health">/api/health</a></li>
  <li><a style="color:#c3f53c" href="/api/signals">/api/signals</a></li>
  <li><a style="color:#c3f53c" href="/api/stats">/api/stats</a></li>
</ul>
<p style="color:#6d7a6d">Fix: run <code>python generate.py</code>, then redeploy.</p>
</body>`,
    "utf8"
  );
  console.log("[build] ⚠️  docs/index.html missing — wrote fallback shell");
}
