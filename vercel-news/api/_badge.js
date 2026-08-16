// badgeOf() — pure, zero imports, deliberately. _db.js re-exports it for
// every route that already does `import { badgeOf } from "./_db.js"`, but
// this is the file to import it from anywhere that must stay dependency-free
// — notably vercel-news/test/*.test.js, which newspaper.yml's CI step runs
// via plain `node --test` with NO npm install first (see the comment on
// "Position tracker tests" in that workflow). Importing badgeOf from _db.js
// there pulls in @libsql/client through _db.js's own top-level import and
// fails with ERR_MODULE_NOT_FOUND in that step specifically — it works fine
// locally only because npm install already happened for other reasons.

function str(v) {
  return v === null || v === undefined ? "" : String(v);
}

const WIN = new Set(["TARGET_HIT", "T1_HIT", "T2_HIT", "TP1_HIT", "TP2_HIT", "PROFIT"]);
const LOSS = new Set(["SL_HIT", "STOPPED", "STOP_HIT", "LOSS"]);
// A trade that ran its course and exited on a time-based rule, or a signal
// that simply never triggered before its window closed — a real, resolved
// outcome, not a withdrawal. Was previously falling through to "cancelled"
// alongside VOID/CANCELLED (genuinely-withdrawn signals), which hid 136 real
// trade outcomes behind the same label as 53 signals that never happened.
const EXPIRED = new Set(["TIME_STOP", "EXPIRED"]);

// Mirrors fetch_alert_log() in newspaper.py so the live layer and the daily
// static build never disagree about what counts as a win.
export function badgeOf(status, lifecycle) {
  const s = str(status).toUpperCase();
  const lc = str(lifecycle).toUpperCase();
  if (WIN.has(s) || WIN.has(lc)) return "win";
  if (LOSS.has(s) || LOSS.has(lc)) return "loss";
  if (s === "OPEN" || lc === "OPEN") return "open";
  if (EXPIRED.has(s) || EXPIRED.has(lc)) return "expired";
  return "cancelled"; // VOID, CANCELLED, and any unrecognized status
}
