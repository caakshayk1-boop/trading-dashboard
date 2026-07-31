// /api/tracker — the position book. This is what the static GitHub Pages build
// could never do: real writes, from any device, persisted in Turso.
//
//   GET                      list open positions with live prices + proximity alerts
//   GET  ?history=1          closed positions
//   POST {symbol,...}        add a position          (requires x-edit-key)
//   POST {action:"exit",id}  close a position        (requires x-edit-key)
//
// Reads are public. Writes are gated on EDIT_KEY and fail closed.
import { db, num, str, json, fail, authorized, readBody } from "./_db.js";

const IST_OFFSET_MIN = 330;

export default async function handler(req, res) {
  try {
    await ensureTable();
    if (req.method === "GET") return await list(req, res);
    if (req.method === "POST") return await write(req, res);
    return fail(res, 405, "GET or POST only");
  } catch (e) {
    return fail(res, 500, `tracker failed: ${e.message}`);
  }
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

async function list(req, res) {
  const history = String((req.query || {}).history || "") === "1";
  const rs = await db().execute({
    sql: `SELECT id, symbol, name, added_date, entry_price, current_price,
                 target_price, stop_loss, thesis, timeframe, status, updated_at
          FROM stock_tracker WHERE status ${history ? "!=" : "="} 'active'
          ORDER BY added_date DESC, id DESC`,
    args: [],
  });

  const rows = rs.rows.map((r) => ({
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
  }));

  // Live prices only for the open book — history is settled, no point re-quoting.
  if (!history && rows.length) {
    const quotes = await quoteAll(rows.map((r) => r.symbol));
    const now = istNow();
    for (const r of rows) {
      const q = quotes[r.symbol];
      if (q !== null && q !== undefined) r.current_price = q;
    }
    // Persist the refreshed prices so the daily static build renders something
    // recent even when it runs without a quote provider. One batch, not one
    // round trip per position — the book is 28 deep and the database is in
    // Tokyo while the function runs in Singapore.
    const writes = rows
      .filter((r) => r.current_price !== null)
      .map((r) => ({
        sql: "UPDATE stock_tracker SET current_price=?, updated_at=? WHERE id=?",
        args: [r.current_price, now, r.id],
      }));
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

  for (const r of rows) decorate(r);

  json(res, 200, {
    ok: true,
    generated_at: new Date().toISOString(),
    can_edit: authorized(req),
    count: rows.length,
    positions: rows,
  });
}

// P&L, currency and the proximity flags — how close price is to the stop or the
// target, expressed as a share of the distance from entry.
function decorate(r) {
  const entry = r.entry_price;
  const cur = r.current_price ?? entry;
  r.current_price = cur;
  r.currency = /\.(NS|BO)$/i.test(r.symbol) ? "₹" : "$";
  r.pnl_pct = entry && cur !== null ? round(((cur - entry) / entry) * 100, 2) : null;
  r.winning = (r.pnl_pct ?? 0) >= 0;

  r.alert = null;
  if (cur === null || entry === null) return;
  if (r.stop_loss !== null && r.stop_loss > 0) {
    if (cur <= r.stop_loss) r.alert = "stop-hit";
    else if (entry > r.stop_loss) {
      const travelled = (entry - cur) / (entry - r.stop_loss);
      if (travelled >= 0.7) r.alert = "near-stop";
    }
  }
  if (!r.alert && r.target_price !== null && r.target_price > 0) {
    if (cur >= r.target_price) r.alert = "target-hit";
    else if (r.target_price > entry) {
      const travelled = (cur - entry) / (r.target_price - entry);
      if (travelled >= 0.7) r.alert = "near-target";
    }
  }
  r.r_multiple =
    r.stop_loss !== null && entry !== null && Math.abs(entry - r.stop_loss) > 0
      ? round((cur - entry) / (entry - r.stop_loss), 2)
      : null;
}

async function write(req, res) {
  if (!authorized(req)) {
    return fail(
      res,
      401,
      process.env.EDIT_KEY
        ? "Wrong edit key."
        : "Writes are disabled — EDIT_KEY is not set on this deployment."
    );
  }
  const body = await readBody(req);

  if (str(body.action) === "exit") {
    const id = parseInt(body.id, 10);
    if (!id) return fail(res, 400, "id required");
    await db().execute({
      sql: "UPDATE stock_tracker SET status='exited', current_price=COALESCE(?,current_price), updated_at=? WHERE id=?",
      args: [num(body.exit_price), istNow(), id],
    });
    return json(res, 200, { ok: true, exited: id });
  }

  const symbol = str(body.symbol).trim().toUpperCase();
  if (!symbol) return fail(res, 400, "symbol required");
  const entry = num(body.entry_price);
  if (entry === null || entry <= 0) return fail(res, 400, "entry_price required");

  await db().execute({
    sql: `INSERT INTO stock_tracker
          (symbol, name, added_date, entry_price, current_price, target_price,
           stop_loss, thesis, timeframe, status, updated_at)
          VALUES (?,?,?,?,?,?,?,?,?,'active',?)`,
    args: [
      symbol,
      str(body.name) || symbol,
      istNow().slice(0, 10),
      entry,
      entry,
      num(body.target_price),
      num(body.stop_loss),
      str(body.thesis),
      str(body.timeframe) || "2-3 months",
      istNow(),
    ],
  });
  return json(res, 200, { ok: true, added: symbol });
}

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

const round = (v, d) => (v === null || !Number.isFinite(v) ? null : Math.round(v * 10 ** d) / 10 ** d);
