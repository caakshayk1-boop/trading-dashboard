// build.js — assembles public/ for life.askakshay.com.
//
// The Life pillar was always the second page of the daily paper: generate.py
// renders it as docs/desk.html from the same SECTION_MAP, the same template and
// the same build. It reached readers as news.askakshay.com/desk, one link deep
// behind a page about trading, which is the wrong shelf for career, learning,
// practice and mind.
//
// This gives it its own domain. Nothing is duplicated and nothing is forked:
// desk.html is copied here as index.html, so the two sites cannot drift — a
// change to the template appears on both, and there is one generator to keep
// honest.
//
// Deliberately NO api/ directory. The Life page renders entirely from the
// daily build; it holds no ledger, no positions and no market data, so it needs
// no functions, no Turso credentials and no database reachable from it. That is
// a smaller attack surface than news.askakshay.com by construction rather than
// by configuration.
import { existsSync, mkdirSync, copyFileSync, writeFileSync, statSync, readdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const publicDir = join(here, "public");
const target = join(publicDir, "index.html");
const source = join(here, "..", "docs", "desk.html");

mkdirSync(publicDir, { recursive: true });

if (existsSync(source)) {
  // desk.html becomes THIS site's index. The file keeps its own name too, so a
  // /desk link from news.askakshay.com still resolves after the split.
  // The new client-rendered Life page is the index. The old server-rendered
  // desk.html is kept under its own name so existing /desk links resolve —
  // one source of truth for the data, two shells while the switch settles.
  const lifeShell = join(here, "..", "docs", "life.html");
  if (existsSync(lifeShell)) copyFileSync(lifeShell, target);
  else copyFileSync(source, target);
  copyFileSync(source, join(publicDir, "desk.html"));

  // Same by-name allow-list discipline as vercel-news/build.js. A docs/ file
  // needs naming in THREE places to reach the web — written by generate.py,
  // named in .vercelignore, and named here. today.json had the first two and
  // still 404'd for days. If a file is missing in production and the build log
  // says it was written, this list is why.
  for (const f of ["app.js", "icon.svg", "og.png", "manifest.webmanifest",
                   "robots.txt", "today.json", "edition.json", "jobs.json", "mandate.json", "v2-core.js", "life.html", "life.js"]) {
    const src = join(here, "..", "docs", f);
    if (existsSync(src)) copyFileSync(src, join(publicDir, f));
  }

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
  console.log(`[build] copied docs/desk.html → public/index.html (${Math.round(statSync(target).size / 1024)}KB)`);
} else if (existsSync(target)) {
  console.log(`[build] using pre-staged public/index.html (${Math.round(statSync(target).size / 1024)}KB)`);
} else {
  // Never ship a blank domain. Say what is wrong instead of 404ing.
  writeFileSync(
    target,
    `<!doctype html><meta charset="utf-8"><title>LIFE — askakshay</title>
<body style="background:#FBFAF7;color:#17191C;font:15px/1.6 ui-monospace,monospace;padding:48px;max-width:640px">
<h1 style="color:#5C7A0B;font-size:20px">LIFE</h1>
<p>The daily shell was not included in this deploy.</p>
<p style="color:#656C74">Fix: run <code>python generate.py</code>, then redeploy.</p>
</body>`,
    "utf8"
  );
  console.log("[build] ⚠️  docs/desk.html missing — wrote fallback shell");
}
