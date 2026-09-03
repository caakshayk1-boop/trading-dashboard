// build.js — life.askakshay.com is now a redirect to career.askakshay.com.
//
// The Life pillar used to be the second page of the daily paper: generate.py
// rendered it as docs/desk.html and this script copied that file in as
// index.html. On 2026-09-03 every Life section — career, learning, practice,
// mind and drills — moved to career.askakshay.com, which renders them from
// today.json["desk"] instead. generate.py no longer writes desk.html, so the
// source this script used to copy does not exist any more.
//
// Rather than delete the project and break every existing link, the site
// becomes a permanent redirect. Three mechanisms, because each covers a case
// the others miss: an HTTP 308 from vercel.json (fastest, and what crawlers
// honour), a <meta http-equiv=refresh> for anything that ignores it, and a
// canonical link so search engines transfer the ranking rather than treating
// the two as duplicates.
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const publicDir = join(here, "public");
const TARGET = "https://career.askakshay.com/";

mkdirSync(publicDir, { recursive: true });

writeFileSync(join(publicDir, "index.html"), `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Moved to career.askakshay.com</title>
<meta http-equiv="refresh" content="0; url=${TARGET}">
<link rel="canonical" href="${TARGET}">
<meta name="robots" content="noindex,follow">
<style>
  :root{color-scheme:light dark}
  body{margin:0;min-height:100vh;display:grid;place-items:center;
       background:#0B0F14;color:#F2F5F9;
       font:400 16px/1.6 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
       text-align:center;padding:24px}
  a{color:#5B9CFF}
  p{max-width:44ch}
</style>
</head>
<body>
  <div>
    <p><b>The Life page moved.</b></p>
    <p>Career, learning, practice, mind and the drills now live at
       <a href="${TARGET}">career.askakshay.com</a>.</p>
    <p>Redirecting&hellip;</p>
  </div>
</body>
</html>
`, "utf8");

writeFileSync(join(publicDir, "robots.txt"),
  "User-agent: *\nDisallow: /\n", "utf8");

console.log("[life] built redirect ->", TARGET);
