// GET /api/signals — the live signal feed, straight from the Turso ledger.
// Replaces the 6 AM static snapshot: whatever the scanner wrote minutes ago
// shows up on the next page load, no rebuild required.
//
// Query params:
//   date=YYYY-MM-DD   one day only (archive view)
//   from=, to=        date range
//   symbol=           substring match
//   status=win|loss|open|expired|cancelled
//   tf=               timeframe exact match
//   type=             signal_type exact match (e.g. ai_longterm)
//   exclude_type=     signal_type to omit
//   version=          engine_version: v2 (default), v1, or "all"
//   limit=            default 300, max 2000
//   offset=           pagination
import { db, num, str, badgeOf, json, fail, columns, optional, currencyOf } from "./_db.js";
import { distinctTargets } from "./_levels.js";
import { simulateWallet, START_DATE as WALLET_START_DATE } from "./_paper_wallet.js";
import { quoteAll } from "./ticker.js";

// The ₹50L paper wallet (?wallet=1) lives in this file rather than its own
// api/paper_wallet.js — Vercel's free Hobby plan caps a deployment at 12
// serverless functions, and this repo was already at exactly 12. A 13th
// route file silently failed the whole deployment (old routes kept serving
// their last good build; the new one 404'd with no build-log access to even
// see why — see 2026-08-17). The simulation itself stays in _paper_wallet.js
// (underscore-prefixed, not a route, doesn't count against the limit, still
// independently unit-tested), so this is a routing consolidation only.

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
const EXPIRED_LIST = "'TIME_STOP','EXPIRED'";
const BADGE_SQL = {
  win: `(upper(coalesce(status,'')) IN (${WIN_LIST}) OR upper(coalesce(lifecycle_status,'')) IN (${WIN_LIST}))`,
  loss: `(upper(coalesce(status,'')) NOT IN (${WIN_LIST})
          AND upper(coalesce(lifecycle_status,'')) NOT IN (${WIN_LIST})
          AND (upper(coalesce(status,'')) IN (${LOSS_LIST}) OR upper(coalesce(lifecycle_status,'')) IN (${LOSS_LIST})))`,
  open: `(upper(coalesce(status,'')) NOT IN (${WIN_LIST},${LOSS_LIST})
          AND upper(coalesce(lifecycle_status,'')) NOT IN (${WIN_LIST},${LOSS_LIST})
          AND (upper(coalesce(status,'')) = 'OPEN' OR upper(coalesce(lifecycle_status,'')) = 'OPEN'))`,
};
// Mirrors the win/loss/open/expired split in badgeOf() (_db.js) — this file
// keeps its own SQL copy so ?status= filtering stays honest across the whole
// ledger, not just the current page (see comment above). Missing this split
// left ?status=cancelled silently including TIME_STOP/EXPIRED rows even
// though each row's own `badge` field (from badgeOf()) already called them
// "expired" — the filter and the row disagreed with each other.
BADGE_SQL.expired = `(NOT ${BADGE_SQL.win} AND NOT ${BADGE_SQL.loss} AND NOT ${BADGE_SQL.open}
          AND (upper(coalesce(status,'')) IN (${EXPIRED_LIST}) OR upper(coalesce(lifecycle_status,'')) IN (${EXPIRED_LIST})))`;
BADGE_SQL.cancelled = `(NOT ${BADGE_SQL.win} AND NOT ${BADGE_SQL.loss} AND NOT ${BADGE_SQL.open} AND NOT ${BADGE_SQL.expired})`;

export default async function handler(req, res) {
  if (req.method !== "GET") return fail(res, 405, "GET only");

  const q = req.query || {};

  if (q.wallet) return handleWallet(res);

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
    if (!clause) return fail(res, 400, "status must be one of: win, loss, open, expired, cancelled");
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
      // What the row RELATES TO, in words. signal_type is an engine name and
      // the log mixes a 1h scalp, a weekly research pick, a monthly SIP
      // allocation and a multi-year idea in one table. Probed like the rest —
      // it arrives via ALTER TABLE and naming a missing column fails the
      // whole query, taking the entire signal log down with it.
      await optional("remarks"),
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
      // Set when this symbol+engine re-fired while this row was still OPEN.
      // duplicate_symbols()/is_duplicate() (tracker.py) drop the new signal
      // rather than log it as a second row — that would double-count one
      // real position in every expectancy figure — and leave this instead.
      await optional("duplicate_note"),
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
      // 600s, not 60. THIS ENDPOINT IS WHY THE ACCOUNT GOT BLOCKED.
      //
      // Turso's free tier caps SYNCS — bytes moved between the app and the
      // database — at 3 GB a month, and hitting ANY limit blocks the whole
      // account. Rows Read was only 13.65M of 500M; syncs was 3.22 GB of 3 GB.
      // The metered resource was never row count, it was bandwidth.
      //
      // This query pulls ~454 KB (22 columns over up to 800 rows). At a
      // 60-second edge cache that is up to 1,440 round trips a day — 638 MB
      // a day at worst, against a 100 MB/day budget. At 600s it is a tenth of
      // that, and the ledger changes a few times a day, not every minute.
    }, 600);
  } catch (e) {
    fail(res, 500, `signals query failed: ${e.message}`);
  }
}

async function handleWallet(res) {
  try {
    const gradeCol = await optional("grade");
    // Direction and the target ladder. The wallet simulated allocation and
    // P&L without ever reading which WAY a trade was taken, so a short and a
    // long were indistinguishable on the page — and this book carries real
    // shorts on Gold, Crude, Natural Gas and Silver. optional() so the API
    // still answers on a schema that predates any of these columns.
    const [actionCol, slCol, t1Col, t2Col] = await Promise.all([
      optional("action"), optional("sl"),
      optional("target1"), optional("target2"),
    ]);
    const sql = `SELECT id, date, symbol, signal_type, entry, status, lifecycle_status,
                        exit_price, pnl_pct, closed_at, ${gradeCol},
                        ${actionCol}, ${slCol}, ${t1Col}, ${t2Col}
                 FROM all_signals
                 WHERE substr(date,1,10) >= ?
                 ORDER BY date ASC, id ASC`;
    const rs = await db().execute({ sql, args: [WALLET_START_DATE] });
    const rows = rs.rows.map((r) => ({
      ...r,
      date: str(r.date),
      symbol: str(r.symbol),
      closed_at: str(r.closed_at) || null,
      grade: str(r.grade) || null,
      action: str(r.action) || null,
    }));

    const result = simulateWallet(rows, badgeOf, currencyOf);
    await markToMarket(result);
    // 600s, same as the main signals response and for the same reason: this
    // reads the whole ledger to simulate the wallet, so a 60-second cache put
    // ~450 KB on the wire up to 1,440 times a day. The sync-budget test caught
    // this one — the first pass raised the response above and missed that this
    // handler has its own.
    json(res, 200, { ok: true, generated_at: new Date().toISOString(), ...result }, 600);
  } catch (e) {
    fail(res, 500, `paper_wallet query failed: ${e.message}`);
  }
}

/** Mark the book's OPEN positions to live prices.
 *
 * The crore book reported `realized_pnl` and nothing else, from six closed
 * trades — while 55.1% of the capital (Rs 55.10 lakh across 20 open
 * positions) sat in the market unpriced. A book that is 55% invested was
 * showing a P&L that ignored 55% of itself. That is what "Rs 1,00,00,000
 * doesn't appear to be live P&L" was: not a rendering fault, a missing
 * calculation.
 *
 * Prices come from the same Yahoo spark feed the ticker rail already uses, so
 * the number on the book and the number on the rail cannot disagree. Twenty
 * positions is exactly one batch — spark's hard cap is 20 symbols — so this
 * costs a single request per cache miss, and it is Yahoo rather than Turso, so
 * it does not touch the sync budget that blocked the account.
 *
 * FAILS VISIBLE, NOT SILENT. A position whose quote is missing is counted in
 * `unmarked`, not silently valued at cost. A total that quietly treats an
 * unpriced holding as flat is the same class of lie as the missing number it
 * replaces — the reader must be able to see the mark is partial.
 */
async function markToMarket(result) {
  const open = (result.trades || []).filter(
    (t) => t.badge === "open" && (t.allocated_qty || 0) > 0 && t.entry > 0);
  result.wallet.unrealized_pnl = null;
  result.wallet.marked_at = null;
  result.wallet.marked = 0;
  result.wallet.unmarked = open.length;
  result.wallet.total_pnl = result.wallet.realized_pnl;
  if (!open.length) {
    result.wallet.unrealized_pnl = 0;
    result.wallet.total_pnl = result.wallet.realized_pnl;
    return;
  }
  try {
    // .NS only. The book is Indian listed equity by mandate; anything else
    // that reaches here has no reliable Yahoo mapping and is better left
    // UNMARKED and counted than mapped by guesswork onto a different company —
    // that is exactly how SILVER was once quoted as SILVER.NS at Rs 233.
    const defs = open.map((t) => [t.symbol, `${t.symbol}.NS`, "\u20b9", 2]);
    const quotes = await quoteAll(defs, defs.map((d) => d[1]));
    let unreal = 0, marked = 0;
    for (const t of open) {
      const q = quotes.get(`${t.symbol}.NS`);
      if (!q || !(q.price > 0)) { t.mark_price = null; continue; }
      t.mark_price = q.price;
      // Direction matters: a SHORT gains when the price falls.
      const dir = String(t.side || "").toUpperCase() === "SHORT" ? -1 : 1;
      t.unrealized_pnl = Math.round(dir * (q.price - t.entry) * t.allocated_qty);
      t.unrealized_pnl_pct = Number((dir * ((q.price / t.entry - 1) * 100)).toFixed(2));
      unreal += t.unrealized_pnl;
      marked += 1;
    }
    result.wallet.unrealized_pnl = unreal;
    result.wallet.unrealized_pnl_pct = Number(((unreal / result.capital) * 100).toFixed(2));
    result.wallet.total_pnl = result.wallet.realized_pnl + unreal;
    result.wallet.total_pnl_pct =
      Number(((result.wallet.total_pnl / result.capital) * 100).toFixed(2));
    result.wallet.marked = marked;
    result.wallet.unmarked = open.length - marked;
    result.wallet.marked_at = new Date().toISOString();
  } catch {
    // Quotes down. realized_pnl still stands and unrealized stays null, which
    // the page renders as "not marked" rather than as zero.
  }
}

function shape(r) {
  const pnl = num(r.pnl_pct);
  const _t = distinctTargets(r.entry, r.sl, r.target1, r.target2, r.target3);
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
    // Collapsed pairs are blanked, never rewritten — see distinctTargets.
    target1: _t[0],
    target2: _t[1],
    target3: _t[2],
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
    remarks: str(r.remarks) || null,
    grade: str(r.grade) || null,
    breakeven_wr: num(r.breakeven_wr),
    turnover_cr: num(r.turnover_cr),
    // Engine-specific payload — thesis, sector, rationale, factor scores.
    // Stored as a JSON string; a malformed blob must not take down the feed.
    metadata: parseMeta(r.metadata),
    duplicate_note: str(r.duplicate_note) || null,
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
