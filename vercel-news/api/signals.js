// GET /api/signals — the live signal feed, straight from the Turso ledger.
// Replaces the 6 AM static snapshot: whatever the scanner wrote minutes ago
// shows up on the next page load, no rebuild required.
//
// Query params:
//   date=YYYY-MM-DD   one day only (archive view)
//   from=, to=        date range
//   symbol=           substring match
//   status=win|loss|open|cancelled
//   tf=               timeframe exact match
//   limit=            default 300, max 2000
//   offset=           pagination
import { db, num, str, badgeOf, json, fail } from "./_db.js";

const COLS = `date, symbol, action, timeframe, signal_type, entry, sl,
              target1, target2, rr, score, status, lifecycle_status,
              exit_price, pnl_pct, r_multiple, closed_at, sent_at, market, asset_type`;

// The badge is derived from two columns, so filtering it in JavaScript after
// the query would filter only the current page — ask for 300 wins and you'd get
// however many happen to sit in the newest 300 rows. These mirror badgeOf() in
// SQL so the filter applies to the whole ledger and pagination stays honest.
const WIN_LIST = "'TARGET_HIT','T1_HIT','T2_HIT','TP1_HIT','TP2_HIT','PROFIT'";
const LOSS_LIST = "'SL_HIT','STOPPED','STOP_HIT','LOSS'";
const BADGE_SQL = {
  win: `(upper(coalesce(status,'')) IN (${WIN_LIST}) OR upper(coalesce(lifecycle_status,'')) IN (${WIN_LIST}))`,
  loss: `(upper(coalesce(status,'')) NOT IN (${WIN_LIST})
          AND upper(coalesce(lifecycle_status,'')) NOT IN (${WIN_LIST})
          AND (upper(coalesce(status,'')) IN (${LOSS_LIST}) OR upper(coalesce(lifecycle_status,'')) IN (${LOSS_LIST})))`,
  open: `(upper(coalesce(status,'')) NOT IN (${WIN_LIST},${LOSS_LIST})
          AND upper(coalesce(lifecycle_status,'')) NOT IN (${WIN_LIST},${LOSS_LIST})
          AND (upper(coalesce(status,'')) = 'OPEN' OR upper(coalesce(lifecycle_status,'')) = 'OPEN'))`,
};
BADGE_SQL.cancelled = `(NOT ${BADGE_SQL.win} AND NOT ${BADGE_SQL.loss} AND NOT ${BADGE_SQL.open})`;

export default async function handler(req, res) {
  if (req.method !== "GET") return fail(res, 405, "GET only");

  const q = req.query || {};
  const where = [];
  const args = [];

  if (q.date) {
    where.push("substr(date,1,10) = ?");
    args.push(String(q.date).slice(0, 10));
  } else {
    if (q.from) { where.push("substr(date,1,10) >= ?"); args.push(String(q.from).slice(0, 10)); }
    if (q.to)   { where.push("substr(date,1,10) <= ?"); args.push(String(q.to).slice(0, 10)); }
  }
  if (q.symbol) {
    where.push("upper(symbol) LIKE ?");
    args.push(`%${String(q.symbol).toUpperCase()}%`);
  }
  if (q.tf) { where.push("timeframe = ?"); args.push(String(q.tf)); }
  if (q.status) {
    const clause = BADGE_SQL[String(q.status).toLowerCase()];
    if (!clause) return fail(res, 400, "status must be one of: win, loss, open, cancelled");
    where.push(clause);
  }

  const limit = Math.min(Math.max(parseInt(q.limit, 10) || 300, 1), 2000);
  const offset = Math.max(parseInt(q.offset, 10) || 0, 0);

  const sql = `SELECT ${COLS} FROM all_signals
               ${where.length ? "WHERE " + where.join(" AND ") : ""}
               ORDER BY date DESC, id DESC
               LIMIT ? OFFSET ?`;

  try {
    const rs = await db().execute({ sql, args: [...args, limit, offset] });
    const rows = rs.rows.map(shape);

    json(res, 200, {
      ok: true,
      count: rows.length,
      offset,
      limit,
      generated_at: new Date().toISOString(),
      signals: rows,
    }, 60);
  } catch (e) {
    fail(res, 500, `signals query failed: ${e.message}`);
  }
}

function shape(r) {
  const pnl = num(r.pnl_pct);
  return {
    date: str(r.date).slice(0, 10),
    symbol: str(r.symbol),
    action: str(r.action) || "BUY",
    timeframe: str(r.timeframe),
    signal_type: str(r.signal_type),
    entry: num(r.entry),
    sl: num(r.sl),
    target1: num(r.target1),
    target2: num(r.target2),
    rr: num(r.rr),
    score: num(r.score),
    status: str(r.status),
    badge: badgeOf(r.status, r.lifecycle_status),
    exit_price: num(r.exit_price),
    pnl_pct: pnl,
    pnl_str: pnl === null ? "—" : `${pnl > 0 ? "+" : ""}${pnl.toFixed(1)}%`,
    r_multiple: num(r.r_multiple),
    closed_at: str(r.closed_at).slice(0, 10) || "—",
    sent_at: str(r.sent_at),
    market: str(r.market),
    asset_type: str(r.asset_type),
  };
}
