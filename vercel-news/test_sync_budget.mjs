// Turso's free tier caps SYNCS — bytes moved between the app and the database —
// at 3 GB a month, and hitting ANY limit blocks the whole account. On
// 2026-08-27 it did: syncs 3.22 GB of 3 GB, while Rows Read sat at 13.65M of
// 500M. The metered resource was never row count. It was bandwidth, and the
// only lever on bandwidth is how often a cache miss reaches the database.
//
// This fails the build if a DB-backed endpoint's edge TTL drops low enough to
// put the monthly budget at risk again. A shorter TTL is not a small change
// here; going from 600s back to 60s on /api/signals alone is 575 MB a day.
import { readFileSync } from "node:fs";

const BYTES_PER_ROW = 581;          // measured from the published alerts.json
const BUDGET_MB_DAY = 3072 / 30;    // 3 GB a month

const ENDPOINTS = [
  { file: "api/signals.js", rows: 800, minTtl: 600 },
  { file: "api/stats.js",   rows: 774, minTtl: 900 },
  { file: "api/archive.js", rows: 774, minTtl: 900 },
];

let worst = 0, fails = [];
for (const e of ENDPOINTS) {
  const src = readFileSync(new URL(e.file, import.meta.url), "utf8");
  const ttls = [...src.matchAll(/\}, (\d+)\);/g)].map((m) => +m[1]);
  const ttl = ttls.length ? Math.min(...ttls) : 0;
  if (!ttl) { fails.push(`${e.file}: no edge cache TTL at all`); continue; }
  if (ttl < e.minTtl) fails.push(`${e.file}: TTL ${ttl}s is below the ${e.minTtl}s floor`);
  const mbDay = (BYTES_PER_ROW * e.rows / 1024 / 1024) * (86400 / ttl);
  worst += mbDay;
  console.log(`  ${e.file.padEnd(18)} ttl ${String(ttl).padStart(4)}s  worst ${mbDay.toFixed(1).padStart(6)} MB/day`);
}
console.log(`  ${"TOTAL".padEnd(18)}            worst ${worst.toFixed(1).padStart(6)} MB/day  (budget ${BUDGET_MB_DAY.toFixed(0)})`);
if (worst > BUDGET_MB_DAY * 1.2) {
  fails.push(`combined worst case ${worst.toFixed(0)} MB/day exceeds the ${BUDGET_MB_DAY.toFixed(0)} MB/day sync budget`);
}
if (fails.length) { console.error("\nSYNC BUDGET FAIL:\n  " + fails.join("\n  ")); process.exit(1); }
console.log("\nsync budget OK");
