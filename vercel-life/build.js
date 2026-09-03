// build.js — life.askakshay.com is retired.
//
// The Life pillar used to be the second page of the daily paper: generate.py
// rendered it as docs/desk.html and this script copied that file in as
// index.html. On 2026-09-03 every Life section moved to career.askakshay.com
// and generate.py stopped writing desk.html, so the source is gone.
//
// This deliberately does NOT redirect. The instruction was to remove Life from
// the public estate entirely — no links kept, no forwarding — so the site
// serves a bare "gone" page that names no destination and is not indexed.
//
// The Vercel project itself should be deleted from the dashboard when
// convenient; this build only guarantees that until then it leaks nothing.
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const publicDir = join(dirname(fileURLToPath(import.meta.url)), "public");
mkdirSync(publicDir, { recursive: true });

writeFileSync(join(publicDir, "index.html"), `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Not available</title>
<meta name="robots" content="noindex,nofollow">
<style>
  :root{color-scheme:dark}
  body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0B0F14;
       color:#8D97A5;font:400 15px/1.6 system-ui,-apple-system,sans-serif}
</style>
</head>
<body><p>Not available.</p></body>
</html>
`, "utf8");

writeFileSync(join(publicDir, "robots.txt"), "User-agent: *\nDisallow: /\n", "utf8");
console.log("[life] retired — bare gone page, no redirect, no links");
