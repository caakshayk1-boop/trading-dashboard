// /api/tracker — the position book. This is what the static GitHub Pages build
// could never do: real writes, from any device, persisted in Turso.
//
//   GET                          list open positions with live prices, an
//                                auto-fired profit-protection ladder, and a
//                                next-action recommendation
//   GET  ?history=1              closed positions
//   GET  ?calc=size&...          risk-based position sizing (no DB touch)
//   POST {symbol,...}            add a position              (requires x-edit-key)
//   POST {action:"exit",id}      close a position             (requires x-edit-key)
//   POST {action:"manual_exit",id,quantity,execution_price}
//                                 record a partial exit outside the ladder (requires x-edit-key)
//
// Reads are public. Writes are gated on EDIT_KEY and fail closed.
import { db, num, str, json, fail, authorized, readBody, columns } from "./_db.js";
import {
  decoratePosition,
  computeLadderPlan,
  nextAction,
  deriveBattleStatus,
  sizePosition,
  validateStopTarget,
  round,
} from "./_positions.js";

const IST_OFFSET_MIN = 330;

// Bounds BOTH the Yahoo re-fetch and the price-cache write to at most once
// per window, regardless of how many people load the page concurrently.
// This is the fix for the GET-causes-unbounded-writes bug: a public,
// unauthenticated page load used to write to Turso on every single request.
const MIN_REFRESH_AGE_SECONDS = 60;

const AUTH_LOCKOUT_THRESHOLD = 5;
const AUTH_LOCKOUT_MINUTES = 15;

// How long a retried add-position POST is treated as the same request rather
// than a genuine second position in the same stock.
const DEDUPE_WINDOW_SECONDS = 10;

export default async function handler(req, res) {
  try {
    if (req.method !== "GET" && req.method !== "POST") {
      return fail(res, 405, "GET or POST only");
    }

    if (req.method === "GET" && (req.query || {}).calc === "size") {
      return sizeCalc(req, res); // pure calculator, never touches the database
    }

    if (req.method === "POST") {
      // Zero-DB-touch rejection for the common "writes are off on this
      // deployment" case — no point spending a Turso round trip on a lockout
      // check when there is no key to compare against in the first place.
      if (!process.env.EDIT_KEY) {
        return fail(res, 401, "Writes are disabled — EDIT_KEY is not set on this deployment.");
      }
      await ensureMigrated();
      // Checked BEFORE comparing the key: if lockout only gated the failure
      // path, a lucky Nth guess after N-1 failures would still succeed and
      // the lockout would protect nothing. It has to block the comparison
      // itself, including a correct one, for the duration.
      const lock = await checkAuthLockout();
      if (lock.locked) {
        return fail(res, 429, `Too many failed attempts. Try again after ${lock.until}.`);
      }
      if (!authorized(req)) {
        await recordAuthFailure();
        return fail(res, 401, "Wrong edit key.");
      }
      await recordAuthSuccess();
      return await write(req, res);
    }

    await ensureMigrated();
    return await list(req, res);
  } catch (e) {
    return fail(res, 500, `tracker failed: ${e.message}`);
  }
}

// ── schema ──────────────────────────────────────────────────────────────

// Guards against re-probing/re-ALTERing on every request within the same
// warm lambda instance. Resets on cold start, which is fine — the probes are
// idempotent (IF NOT EXISTS / column-presence check before ALTER).
let _migrated = false;

async function ensureMigrated() {
  if (_migrated) return;
  await ensureTable();
  await ensureColumns();
  await ensureEventsTable();
  await ensureAuthTable();
  _migrated = true;
}

async function ensureTable() {
  await db().execute(`CREATE TABLE IF NOT EXISTS stock_tracker (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL, name TEXT, added_date TEXT,
    entry_price REAL, current_price REAL, target_price REAL,
    stop_loss REAL, thesis TEXT, timeframe TEXT,
    status TEXT DEFAULT 'active', updated_at TEXT
  )`);
}

// Additive only. Production has zero rows in this table (verified before
// writing this), so no backfill/migration report is needed — ADD COLUMN
// with a default is the whole story.
const NEW_COLUMNS = [
  ["side", "TEXT NOT NULL DEFAULT 'LONG'"],
  ["trade_type", "TEXT NOT NULL DEFAULT 'SWING'"],
  ["original_quantity", "REAL"],
  ["remaining_quantity", "REAL"],
  ["realized_pnl", "REAL NOT NULL DEFAULT 0"],
  ["fees", "REAL NOT NULL DEFAULT 0"],
  ["battle_status", "TEXT NOT NULL DEFAULT 'ACCUMULATION'"],
  ["stop_moved_to_breakeven", "INTEGER NOT NULL DEFAULT 0"],
  ["highest_price", "REAL"],
  ["lowest_price", "REAL"],
];

async function ensureColumns() {
  const existing = await columns("stock_tracker");
  for (const [name, ddl] of NEW_COLUMNS) {
    if (!existing.has(name)) {
      await db().execute(`ALTER TABLE stock_tracker ADD COLUMN ${name} ${ddl}`);
    }
  }
}

async function ensureEventsTable() {
  await db().execute(`CREATE TABLE IF NOT EXISTS stock_tracker_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id INTEGER NOT NULL REFERENCES stock_tracker(id),
    event_type TEXT NOT NULL CHECK (event_type IN
      ('ENTRY','PARTIAL_EXIT','STOP_MOVE','FINAL_EXIT','MANUAL_ADJUSTMENT')),
    event_time TEXT NOT NULL,
    trigger_price REAL, execution_price REAL, quantity REAL,
    quantity_pct_remaining_after REAL, realized_pnl REAL,
    stop_before REAL, stop_after REAL,
    reason TEXT, created_at TEXT NOT NULL
  )`);
  // A ladder milestone can fire at most once per position — this is the
  // idempotency guarantee (INSERT OR IGNORE + rowsAffected below relies on
  // it). Manual exits (reason='manual') are deliberately NOT covered, since
  // those are repeatable by design.
  await db().execute(`CREATE UNIQUE INDEX IF NOT EXISTS ux_stock_tracker_events_milestone
    ON stock_tracker_events(position_id, event_type, reason)
    WHERE event_type = 'PARTIAL_EXIT' AND reason IN ('milestone_30','milestone_50')`);
}

async function ensureAuthTable() {
  await db().execute(`CREATE TABLE IF NOT EXISTS stock_tracker_auth (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    fail_count INTEGER NOT NULL DEFAULT 0,
    locked_until TEXT,
    updated_at TEXT
  )`);
}

// ── auth lockout ────────────────────────────────────────────────────────
// Global, not per-IP — there's no session/trusted-IP infrastructure
// anywhere in this codebase to do better, and x-forwarded-for is spoofable
// without a trusted edge config. This is a light mitigation: it raises the
// cost of key-guessing, it does not stop a distributed attacker, and it has
// a minor self-DoS risk (an attacker who wants to annoy the admin can force
// a lockout on purpose). Acceptable tradeoff for a solo-admin, unindexed
// personal tool.

async function checkAuthLockout() {
  const rs = await db().execute("SELECT locked_until FROM stock_tracker_auth WHERE id = 1");
  const row = rs.rows[0];
  const until = row ? str(row.locked_until) : "";
  if (until && until > istNow()) return { locked: true, until };
  return { locked: false };
}

async function recordAuthFailure() {
  const now = istNow();
  const lockUntil = istPlusMinutes(AUTH_LOCKOUT_MINUTES);
  await db().execute({
    sql: `INSERT INTO stock_tracker_auth (id, fail_count, locked_until, updated_at)
          VALUES (1, 1, NULL, ?)
          ON CONFLICT(id) DO UPDATE SET
            fail_count = stock_tracker_auth.fail_count + 1,
            locked_until = CASE WHEN stock_tracker_auth.fail_count + 1 >= ?
                                 THEN ? ELSE stock_tracker_auth.locked_until END,
            updated_at = ?`,
    args: [now, AUTH_LOCKOUT_THRESHOLD, lockUntil, now],
  });
}

async function recordAuthSuccess() {
  const now = istNow();
  await db().execute({
    sql: `INSERT INTO stock_tracker_auth (id, fail_count, locked_until, updated_at)
          VALUES (1, 0, NULL, ?)
          ON CONFLICT(id) DO UPDATE SET fail_count = 0, locked_until = NULL, updated_at = ?`,
    args: [now, now],
  });
}

// ── read ────────────────────────────────────────────────────────────────

async function list(req, res) {
  const history = String((req.query || {}).history || "") === "1";
  const rs = await db().execute({
    sql: `SELECT id, symbol, name, added_date, entry_price, current_price,
                 target_price, stop_loss, thesis, timeframe, status, updated_at,
                 side, trade_type, original_quantity, remaining_quantity,
                 realized_pnl, fees, battle_status, stop_moved_to_breakeven,
                 highest_price, lowest_price
          FROM stock_tracker WHERE status ${history ? "!=" : "="} 'active'
          ORDER BY added_date DESC, id DESC`,
    args: [],
  });

  const rows = rs.rows.map(mapRow);

  let fired = new Map();
  if (!history && rows.length) {
    await refreshPrices(rows);
    fired = await loadFiredMilestones(rows.map((r) => r.id));
    await applyLadders(rows, fired);
  }

  const out = rows.map((r) => {
    r.currency = /\.(NS|BO)$/i.test(r.symbol) ? "₹" : "$";
    const decorated = decoratePosition(r);
    decorated.next_action = history ? null : nextAction(decorated, { firedMilestones: fired.get(r.id) || new Set() });
    return decorated;
  });

  json(res, 200, {
    ok: true,
    generated_at: new Date().toISOString(),
    can_edit: authorized(req),
    count: out.length,
    positions: out,
  });
}

function mapRow(r) {
  return {
    id: Number(r.id),
    symbol: str(r.symbol),
    name: str(r.name) || str(r.symbol),
    added_date: str(r.added_date),
    entry_price: num(r.entry_price),
    current_price: num(r.current_price),
    target_price: num(r.target_price),
    stop_loss: num(r.stop_loss),
    thesis: str(r.thesis),
    timeframe: str(r.timeframe),
    status: str(r.status),
    updated_at: str(r.updated_at),
    side: str(r.side) || "LONG",
    trade_type: str(r.trade_type) || "SWING",
    original_quantity: num(r.original_quantity),
    remaining_quantity: num(r.remaining_quantity),
    realized_pnl: num(r.realized_pnl) ?? 0,
    fees: num(r.fees) ?? 0,
    battle_status: str(r.battle_status) || "ACCUMULATION",
    stop_moved_to_breakeven: Number(r.stop_moved_to_breakeven) === 1,
    highest_price: num(r.highest_price),
    lowest_price: num(r.lowest_price),
  };
}

function ageMs(updatedAt) {
  if (!updatedAt) return Infinity;
  const t = Date.parse(updatedAt);
  return Number.isFinite(t) ? Date.now() - t : Infinity;
}

async function refreshPrices(rows) {
  const stale = rows.filter((r) => ageMs(r.updated_at) > MIN_REFRESH_AGE_SECONDS * 1000);
  if (!stale.length) return;

  const quotes = await quoteAll(stale.map((r) => r.symbol));
  const now = istNow();
  const writes = [];
  for (const r of stale) {
    const q = quotes[r.symbol];
    if (q === null || q === undefined) continue;
    r.current_price = q;
    r.highest_price = r.highest_price === null ? q : Math.max(r.highest_price, q);
    r.lowest_price = r.lowest_price === null ? q : Math.min(r.lowest_price, q);
    writes.push({
      sql: "UPDATE stock_tracker SET current_price=?, highest_price=?, lowest_price=?, updated_at=? WHERE id=?",
      args: [r.current_price, r.highest_price, r.lowest_price, now, r.id],
    });
  }
  if (writes.length) {
    // A failed price write must not blank the response — the caller still
    // gets live prices, they just aren't cached for the next static build.
    try {
      await db().batch(writes, "write");
    } catch (e) {
      console.warn(`tracker: price cache write failed (${e.message})`);
    }
  }
}

async function loadFiredMilestones(ids) {
  const map = new Map();
  if (!ids.length) return map;
  const rs = await db().execute({
    sql: `SELECT position_id, reason FROM stock_tracker_events
          WHERE event_type='PARTIAL_EXIT' AND reason IN ('milestone_30','milestone_50')
          AND position_id IN (${ids.map(() => "?").join(",")})`,
    args: ids,
  });
  for (const r of rs.rows) {
    const pid = Number(r.position_id);
    if (!map.has(pid)) map.set(pid, new Set());
    map.get(pid).add(str(r.reason));
  }
  return map;
}

// Runs on every GET, not just refreshed rows — this is deliberately NOT
// gated by MIN_REFRESH_AGE_SECONDS. computeLadderPlan is pure and idempotent
// (a milestone already in `fired` is skipped), so re-running it against
// whatever price is currently on hand costs nothing when nothing new has
// crossed, and reacts immediately once a fresh price does cross a milestone
// rather than waiting for the next refresh window. The refresh window above
// only bounds the Yahoo fetch + price-cache write, not this.
async function applyLadders(rows, fired) {
  for (const r of rows) {
    const set = fired.get(r.id) || new Set();
    const plan = computeLadderPlan(r, r.current_price, set);
    if (plan.length) {
      await applyLadderPlan(r, plan, set);
      fired.set(r.id, set);
    }
  }
}

async function applyLadderPlan(row, plan, firedSet) {
  const now = istNow();
  let applied = false;
  for (const step of plan) {
    const ins = await db().execute({
      sql: `INSERT OR IGNORE INTO stock_tracker_events
            (position_id, event_type, event_time, trigger_price, execution_price, quantity,
             quantity_pct_remaining_after, realized_pnl, stop_before, stop_after, reason, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)`,
      args: [
        row.id, "PARTIAL_EXIT", now, step.trigger_price, step.execution_price, step.quantity,
        step.quantity_pct_remaining_after, step.realized_pnl_delta, step.stop_before, step.stop_after,
        step.reason, now,
      ],
    });
    // rowsAffected === 0 means a concurrent request already recorded this
    // exact milestone (the unique index rejected the duplicate) — someone
    // else won the race, so skip applying the quantity/P&L delta again.
    if (Number(ins.rowsAffected || 0) === 0) continue;

    row.remaining_quantity = round(row.remaining_quantity - step.quantity, 6);
    row.realized_pnl = round((row.realized_pnl || 0) + step.realized_pnl_delta, 2);
    if (step.stop_after !== step.stop_before) {
      row.stop_loss = step.stop_after;
      row.stop_moved_to_breakeven = true;
    }
    firedSet.add(step.reason);
    applied = true;
  }
  if (!applied) return;

  row.battle_status = deriveBattleStatus(row, { firedMilestones: firedSet });
  await db().execute({
    sql: `UPDATE stock_tracker
          SET remaining_quantity=?, realized_pnl=?, stop_loss=?, stop_moved_to_breakeven=?, battle_status=?, updated_at=?
          WHERE id=?`,
    args: [
      row.remaining_quantity, row.realized_pnl, row.stop_loss,
      row.stop_moved_to_breakeven ? 1 : 0, row.battle_status, now, row.id,
    ],
  });
}

// Decision-support calculator — GET-only, reads nothing, writes nothing.
function sizeCalc(req, res) {
  const q = req.query || {};
  const result = sizePosition({
    capital: num(q.capital),
    riskPct: num(q.risk_pct),
    entry: num(q.entry),
    stop: num(q.stop),
    side: str(q.side).toUpperCase() === "SHORT" ? "SHORT" : "LONG",
  });
  if (result.error) return fail(res, 400, result.error);
  return json(res, 200, { ok: true, ...result });
}

// ── writes ──────────────────────────────────────────────────────────────

async function write(req, res) {
  await ensureMigrated();
  const body = await readBody(req);
  const action = str(body.action);

  if (action === "exit") return exitPosition(body, res);
  if (action === "manual_exit") return manualExit(body, res);
  return addPosition(body, res);
}

async function exitPosition(body, res) {
  const id = parseInt(body.id, 10);
  if (!id) return fail(res, 400, "id required");

  const rs = await db().execute({ sql: "SELECT * FROM stock_tracker WHERE id=?", args: [id] });
  const row = rs.rows[0];
  if (!row) return fail(res, 404, "position not found");
  if (str(row.status) !== "active") return json(res, 200, { ok: true, exited: id }); // already closed — idempotent no-op

  const entry = num(row.entry_price);
  const side = str(row.side) || "LONG";
  const remaining = num(row.remaining_quantity) || 0;
  const exitPrice = num(body.exit_price) ?? num(row.current_price);
  const realizedDelta =
    remaining > 0 && exitPrice !== null && entry !== null
      ? round(side === "SHORT" ? (entry - exitPrice) * remaining : (exitPrice - entry) * remaining, 2)
      : 0;

  const now = istNow();
  await db().execute({
    sql: `UPDATE stock_tracker
          SET status='exited', current_price=COALESCE(?,current_price),
              remaining_quantity=0, realized_pnl=COALESCE(realized_pnl,0)+?,
              battle_status='CLOSED', updated_at=?
          WHERE id=?`,
    args: [exitPrice, realizedDelta, now, id],
  });
  await db().execute({
    sql: `INSERT INTO stock_tracker_events
          (position_id, event_type, event_time, execution_price, quantity,
           quantity_pct_remaining_after, realized_pnl, reason, created_at)
          VALUES (?,?,?,?,?,?,?,?,?)`,
    args: [id, "FINAL_EXIT", now, exitPrice, remaining, 0, realizedDelta, "exit", now],
  });
  return json(res, 200, { ok: true, exited: id });
}

// Records an exit the admin made outside the ladder's own logic (e.g. they
// sold a custom amount for a reason the milestones don't capture). Keeps
// remaining_quantity authoritative regardless of what the ladder would have
// done — this does not touch stop_loss (only a fired milestone moves that).
async function manualExit(body, res) {
  const id = parseInt(body.id, 10);
  if (!id) return fail(res, 400, "id required");
  const qty = num(body.quantity);
  const execPrice = num(body.execution_price);
  if (qty === null || qty <= 0) return fail(res, 400, "quantity required");
  if (execPrice === null) return fail(res, 400, "execution_price required");

  const rs = await db().execute({ sql: "SELECT * FROM stock_tracker WHERE id=?", args: [id] });
  const row = rs.rows[0];
  if (!row) return fail(res, 404, "position not found");
  if (str(row.status) !== "active") return fail(res, 400, "position already closed");

  const remaining = num(row.remaining_quantity) || 0;
  if (qty > remaining) return fail(res, 400, `cannot exit more than remaining (${remaining})`);

  const entry = num(row.entry_price);
  const side = str(row.side) || "LONG";
  const realizedDelta = round(side === "SHORT" ? (entry - execPrice) * qty : (execPrice - entry) * qty, 2);
  const newRemaining = round(remaining - qty, 6);
  const original = num(row.original_quantity) || remaining || 1;
  const now = istNow();

  await db().execute({
    sql: `UPDATE stock_tracker
          SET remaining_quantity=?, realized_pnl=COALESCE(realized_pnl,0)+?, updated_at=?
          WHERE id=?`,
    args: [newRemaining, realizedDelta, now, id],
  });
  await db().execute({
    sql: `INSERT INTO stock_tracker_events
          (position_id, event_type, event_time, execution_price, quantity,
           quantity_pct_remaining_after, realized_pnl, reason, created_at)
          VALUES (?,?,?,?,?,?,?,?,?)`,
    args: [id, "PARTIAL_EXIT", now, execPrice, qty, round((newRemaining / original) * 100, 2), realizedDelta, "manual", now],
  });
  return json(res, 200, { ok: true, id, remaining_quantity: newRemaining });
}

async function addPosition(body, res) {
  const symbol = str(body.symbol).trim().toUpperCase();
  if (!symbol) return fail(res, 400, "symbol required");
  const entry = num(body.entry_price);
  if (entry === null || entry <= 0) return fail(res, 400, "entry_price required");
  const quantity = num(body.quantity);
  if (quantity === null || quantity <= 0) return fail(res, 400, "quantity required");

  const side = str(body.side).toUpperCase() === "SHORT" ? "SHORT" : "LONG";
  const tradeTypeIn = str(body.trade_type).toUpperCase();
  const tradeType = ["INTRADAY", "SWING", "LONG_TERM", "INVESTMENT"].includes(tradeTypeIn) ? tradeTypeIn : "SWING";
  const target = num(body.target_price);
  const stop = num(body.stop_loss);

  const validation = validateStopTarget({ side, entry, stop, target });
  if (!validation.valid) return fail(res, 400, validation.error);

  // A double-click or a retried POST (browser retry, flaky connection) must
  // not create a second row for the same trade. Heuristic, not a true
  // idempotency key: a matching active position for this exact
  // symbol/side/entry/quantity added in the last DEDUPE_WINDOW_SECONDS is
  // treated as the same request and its id is returned instead of inserting
  // again. Outside that window, an identical add is assumed to be a genuine
  // second position (e.g. re-entering the same stock later) and is allowed.
  const cutoff = istSecondsAgo(DEDUPE_WINDOW_SECONDS);
  const dupe = await db().execute({
    sql: `SELECT id FROM stock_tracker
          WHERE symbol=? AND side=? AND entry_price=? AND original_quantity=?
          AND status='active' AND updated_at >= ?
          ORDER BY id DESC LIMIT 1`,
    args: [symbol, side, entry, quantity, cutoff],
  });
  if (dupe.rows.length) {
    return json(res, 200, { ok: true, added: symbol, id: Number(dupe.rows[0].id), deduped: true });
  }

  const now = istNow();
  const ins = await db().execute({
    sql: `INSERT INTO stock_tracker
          (symbol, name, added_date, entry_price, current_price, target_price,
           stop_loss, thesis, timeframe, status, updated_at, side, trade_type,
           original_quantity, remaining_quantity, realized_pnl, fees, battle_status)
          VALUES (?,?,?,?,?,?,?,?,?,'active',?,?,?,?,?,0,0,'ACCUMULATION')`,
    args: [
      symbol, str(body.name) || symbol, now.slice(0, 10), entry, entry, target, stop,
      str(body.thesis), str(body.timeframe) || "2-3 months", now, side, tradeType,
      quantity, quantity,
    ],
  });
  const positionId = Number(ins.lastInsertRowid);
  await db().execute({
    sql: `INSERT INTO stock_tracker_events
          (position_id, event_type, event_time, execution_price, quantity,
           quantity_pct_remaining_after, reason, created_at)
          VALUES (?,?,?,?,?,?,?,?)`,
    args: [positionId, "ENTRY", now, entry, quantity, 100, "initial_entry", now],
  });
  return json(res, 200, { ok: true, added: symbol, id: positionId });
}

// ── quotes ──────────────────────────────────────────────────────────────

// Yahoo's chart endpoint stays reachable from serverless IPs where the older
// /v7/quote endpoint returns 401. Failures are silent — a missing quote falls
// back to the last stored price rather than blanking the row.
//
// Concurrency is capped: firing 28+ parallel requests at Yahoo invites rate
// limiting, and the whole handler has to finish inside the function timeout.
// The deadline below is what guarantees that — once it passes, the remaining
// symbols keep their stored prices instead of hanging the request.
const QUOTE_CONCURRENCY = 8;
const QUOTE_DEADLINE_MS = 7000;

async function quoteAll(symbols) {
  const out = {};
  const queue = [...new Set(symbols)];
  const deadline = Date.now() + QUOTE_DEADLINE_MS;

  const worker = async () => {
    while (queue.length) {
      if (Date.now() >= deadline) return;
      const sym = queue.shift();
      try {
        const budget = Math.max(deadline - Date.now(), 1);
        const r = await fetch(
          `https://query1.finance.yahoo.com/v8/finance/chart/${encodeURIComponent(sym)}?range=1d&interval=1d`,
          { headers: { "User-Agent": "Mozilla/5.0" }, signal: AbortSignal.timeout(budget) }
        );
        if (!r.ok) continue;
        const j = await r.json();
        out[sym] = num(j?.chart?.result?.[0]?.meta?.regularMarketPrice);
      } catch {
        /* leave undefined — caller keeps the stored price */
      }
    }
  };

  await Promise.all(
    Array.from({ length: Math.min(QUOTE_CONCURRENCY, queue.length) }, worker)
  );
  return out;
}

function istNow() {
  return new Date(Date.now() + IST_OFFSET_MIN * 60_000).toISOString().replace("Z", "+05:30");
}

function istPlusMinutes(mins) {
  return new Date(Date.now() + (IST_OFFSET_MIN + mins) * 60_000).toISOString().replace("Z", "+05:30");
}

function istSecondsAgo(seconds) {
  return new Date(Date.now() + IST_OFFSET_MIN * 60_000 - seconds * 1000).toISOString().replace("Z", "+05:30");
}
