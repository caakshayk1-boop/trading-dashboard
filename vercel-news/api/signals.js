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
//   type=             signal_type exact match (e.g. ai_longterm)
//   exclude_type=     signal_type to omit
//   version=          engine_version: v2 (default), v1, or "all"
//   limit=            default 300, max 2000
//   offset=           pagination
import { db, num, str, badgeOf, json, fail, columns, optional, currencyOf } from "./_db.js";

// `id` is here so a single trade can be linked to and audited. Without it the
// ledger was a list you could read but not point at.
const BASE_COLS = `id, date, symbol, action, timeframe, signal_type, entry, sl,
              target1, target2, target3, rr, score, status, lifecycle_status,
              exit_price, pnl_pct, r_multiple, closed_at, sent_at, market, asset_type`;

// Signals generated before the quality gate landed (2026-08-02) are tagged v1
// and are NOT deleted — they are the control group the new gate is measured
// against. The site just stops showing them by default.
const CURRENT_VERSION = "v2";

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
  // signal_type filter — the long-horizon engine writes ai_longterm rows
  // that belong in their own section, not mixed into the trade log.
  if (q.type) { where.push("signal_type = ?"); args.push(String(q.type)); }
  else if (q.exclude_type) { where.push("COALESCE(signal_type,'') != ?"); args.push(String(q.exclude_type)); }
  if (q.status) {
    const clause = BADGE_SQL[String(q.status).toLowerCase()];
    if (!clause) return fail(res, 400, "status must be one of: win, loss, open, cancelled");
    where.push(clause);
  }

  const limit = Math.min(Math.max(parseInt(q.limit, 10) || 300, 1), 2000);
  const offset = Math.max(parseInt(q.offset, 10) || 0, 0);

  try {
    const cols = await columns();
    const versioned = cols.has("engine_version");

    // Default to the current engine. Older rows stay queryable with
    // ?version=v1 or ?version=all — nothing is hidden, just not the default.
    // An EXPLICIT type bypasses the version default. The version filter exists
    // to keep v1 and v2 TRADE engines comparable; asking for `magic` by name is
    // asking for magic whatever version it carries. Without this, /api/signals
    // ?type=magic returned 0 rows while 12 existed — the engine was queryable
    // in theory and invisible in practice, which is how it came to look like
    // the magic screener was never logged at all.
    const explicitType = Boolean(q.type);
    const version = String(q.version || (explicitType ? "all" : CURRENT_VERSION)).toLowerCase();
    if (versioned && version !== "all") {
      // Rows written before the column existed carry the 'v1' backfill
      // default, but a NULL can still appear if a writer predates the
      // migration, so treat NULL as v1 rather than dropping it silently.
      if (version === "v1") {
        where.push("(COALESCE(engine_version,'v1') = 'v1')");
      } else {
        where.push("COALESCE(engine_version,'v1') = ?");
        args.push(version);
      }
    }

    const extra = [
      await optional("engine_version"),
      await optional("grade"),
      await optional("breakeven_wr"),
      await optional("turnover_cr"),
      // Engine payload — the long-term cards render their thesis, sector and
      // factor scores from here. Probed like the rest because it arrives via
      // ALTER TABLE and naming a missing column fails the whole query.
      await optional("metadata"),
      // Lifecycle. These columns existed and were never returned, so the site
      // could show WHAT happened to a trade but never WHEN, or whether the
      // outcome was assumed. exit_ambiguous in particular marks a bar that
      // straddled both stop and target — daily data cannot say which came
      // first, and hiding that is how an assumption becomes a fact.
      await optional("entry_triggered_at"),
      await optional("fill_type"),
      await optional("exit_ambiguous"),
      await optional("regraded_at"),
      await optional("max_profit_pct"),
      await optional("max_drawdown_pct"),
    ].join(", ");

    const sql = `SELECT ${BASE_COLS}, ${extra} FROM all_signals
               ${where.length ? "WHERE " + where.join(" AND ") : ""}
               ORDER BY date DESC, id DESC
               LIMIT ? OFFSET ?`;

    const rs = await db().execute({ sql, args: [...args, limit, offset] });
    const rows = rs.rows.map(shape);

    json(res, 200, {
      ok: true,
      count: rows.length,
      offset,
      limit,
      version: versioned ? version : "unversioned",
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
    id: num(r.id),
    date: str(r.date).slice(0, 10),
    symbol: str(r.symbol),
    // Derived, because the ledger has no currency column and the table was
    // printing dollar-quoted commodities with a rupee sign.
    currency: currencyOf(r.symbol),
    action: str(r.action) || "BUY",
    timeframe: str(r.timeframe),
    signal_type: str(r.signal_type),
    entry: num(r.entry),
    sl: num(r.sl),
    target1: num(r.target1),
    target2: num(r.target2),
    target3: num(r.target3),
    rr: num(r.rr),
    score: num(r.score),
    status: str(r.status),
    badge: badgeOf(r.status, r.lifecycle_status),
    exit_price: num(r.exit_price),
    pnl_pct: pnl,
    pnl_str: pnl === null ? "—" : `${pnl > 0 ? "+" : ""}${pnl.toFixed(1)}%`,
    r_multiple: num(r.r_multiple),
    // The audit trail: generated -> sent -> triggered -> closed, plus whether
    // anything about the outcome was inferred rather than observed.
    lifecycle_status: str(r.lifecycle_status),
    entry_triggered_at: str(r.entry_triggered_at),
    fill_type: str(r.fill_type),
    exit_ambiguous: num(r.exit_ambiguous) === 1,
    regraded_at: str(r.regraded_at),
    max_profit_pct: num(r.max_profit_pct),
    max_drawdown_pct: num(r.max_drawdown_pct),
    closed_at: str(r.closed_at).slice(0, 10) || "—",
    sent_at: str(r.sent_at),
    market: str(r.market),
    asset_type: str(r.asset_type),
    engine_version: str(r.engine_version) || "v1",
    grade: str(r.grade) || null,
    breakeven_wr: num(r.breakeven_wr),
    turnover_cr: num(r.turnover_cr),
    // Engine-specific payload — thesis, sector, rationale, factor scores.
    // Stored as a JSON string; a malformed blob must not take down the feed.
    metadata: parseMeta(r.metadata),
  };
}

function parseMeta(v) {
  const raw = str(v).trim();
  if (!raw || raw === "{}") return null;
  try {
    const o = JSON.parse(raw);
    return o && typeof o === "object" ? o : null;
  } catch {
    return null;
  }
}
