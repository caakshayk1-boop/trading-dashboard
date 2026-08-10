// GET /api/stats — performance analytics over the whole Turso ledger.
//
// Every rate here is computed over CLOSED signals only. Counting open signals
// as wins is how a 50% system starts looking like an 80% one, so open trades
// are reported separately and never folded into win rate or expectancy.
//
// Query params:
//   from=, to=   optional date range (YYYY-MM-DD)
//   tf=          restrict to one timeframe
import { db, num, str, badgeOf, json, fail, columns, optional } from "./_db.js";

export default async function handler(req, res) {
  if (req.method !== "GET") return fail(res, 405, "GET only");

  const q = req.query || {};
  const where = [];
  const args = [];
  if (q.from) { where.push("substr(date,1,10) >= ?"); args.push(String(q.from).slice(0, 10)); }
  if (q.to)   { where.push("substr(date,1,10) <= ?"); args.push(String(q.to).slice(0, 10)); }
  if (q.tf)   { where.push("timeframe = ?");          args.push(String(q.tf)); }

  // Long-horizon ideas are NOT trades and must never reach expectancy.
  // ai_longterm rows carry a 200DMA structure stop and a 2-3 year horizon; a
  // single one resolving would land in the R-multiple statistics as though it
  // were a swing trade and move the only honest number on this site. Mirrors
  // ai_longterm.EXCLUDE_FROM_EXPECTANCY on the Python side.
  //
  // multibagger joins it for the same reason, from 2026-08-10: the Saturday
  // weekly scan now writes to the ledger instead of only its own table, and
  // those are 6-12 month holds off weekly bars. They appear in the Signal Log
  // and in /api/signals?type=multibagger; they do not touch win rate,
  // expectancy or the equity curve. Mirrors tracker.EXCLUDE_FROM_EXPECTANCY.
  const NON_TRADING = ["ai_longterm", "multibagger"];
  where.push(`COALESCE(signal_type,'') NOT IN (${NON_TRADING.map(() => "?").join(",")})`);
  args.push(...NON_TRADING);

  try {
    const cols = await columns();
    const versioned = cols.has("engine_version");

    // Defaults to v2, matching /api/signals. This used to default to "all" so
    // the performance page would not start empty, but that meant the headline
    // expectancy blended two different engines — and once the 2026-08-08
    // re-grade showed the v1 record was largely a grading artifact, publishing
    // it as the record was worse than publishing nothing.
    //
    // v1 is not deleted. `?version=v1` and `?version=all` still return it, so
    // the history is auditable; it is simply no longer what the site claims as
    // its record. The cost is a small n, which the page states plainly rather
    // than dressing up.
    const version = String(q.version || "v2").toLowerCase();
    if (versioned && version !== "all") {
      if (version === "v1") where.push("(COALESCE(engine_version,'v1') = 'v1')");
      else { where.push("COALESCE(engine_version,'v1') = ?"); args.push(version); }
    }

    const sql = `SELECT date, symbol, timeframe, signal_type, entry, sl, target1,
                      status, lifecycle_status, pnl_pct, r_multiple, closed_at,
                      ${await optional("engine_version")}, ${await optional("grade")}
               FROM all_signals
               ${where.length ? "WHERE " + where.join(" AND ") : ""}
               ORDER BY date ASC, id ASC`;

    const rs = await db().execute({ sql, args });
    const rows = rs.rows.map((r) => ({
      date: str(r.date).slice(0, 10),
      symbol: str(r.symbol),
      timeframe: str(r.timeframe) || "—",
      signal_type: str(r.signal_type) || "—",
      entry: num(r.entry),
      sl: num(r.sl),
      badge: badgeOf(r.status, r.lifecycle_status),
      pnl_pct: num(r.pnl_pct),
      r_multiple: rMultiple(r),
      closed_at: str(r.closed_at).slice(0, 10) || str(r.date).slice(0, 10),
      engine_version: str(r.engine_version) || "v1",
      grade: str(r.grade) || "—",
    }));

    const closed = rows.filter((r) => r.badge === "win" || r.badge === "loss");
    const open = rows.filter((r) => r.badge === "open").length;
    const cancelled = rows.filter((r) => r.badge === "cancelled").length;

    json(res, 200, {
      ok: true,
      generated_at: new Date().toISOString(),
      version,
      basis: "Win rate, avg R and expectancy are computed over closed signals only. Open signals are excluded.",
      totals: {
        all: rows.length,
        closed: closed.length,
        open,
        cancelled,
        first_date: rows.length ? rows[0].date : null,
        last_date: rows.length ? rows[rows.length - 1].date : null,
      },
      headline: headline(closed),
      equity_curve: equityCurve(closed),
      by_month: group(closed, (r) => r.closed_at.slice(0, 7)),
      by_timeframe: group(closed, (r) => r.timeframe),
      by_signal_type: group(closed, (r) => r.signal_type),
      by_symbol: group(closed, (r) => r.symbol, 5),
      // v1 vs v2 side by side — the only honest way to answer "did the gate
      // help?". v2 will read 0 trades until its first signal closes.
      by_engine_version: group(closed, (r) => r.engine_version),
      by_grade: group(closed, (r) => r.grade),
      // Per-engine break-even R:R, mirroring signals/expectancy.py. This is
      // what sets each scanner's R:R floor, so the site shows the same
      // arithmetic the scanner acts on.
      engine_floors: engineFloors(closed),
    }, 300);
  } catch (e) {
    fail(res, 500, `stats query failed: ${e.message}`);
  }
}

// breakeven_rr = (1 - p) / p. Mirrors signals/expectancy.py — keep the
// constants in step with that module.
function engineFloors(closed) {
  const MIN_SAMPLE = 25, DEFAULT_FLOOR = 2.0, SAFETY = 1.15, CAP = 6.0;
  const buckets = new Map();
  for (const r of closed) {
    const k = r.signal_type || "—";
    if (!buckets.has(k)) buckets.set(k, []);
    buckets.get(k).push(r);
  }
  const out = [];
  for (const [key, items] of buckets) {
    const wins = items.filter((r) => r.badge === "win").length;
    const n = items.length;
    const p = wins / n;
    let breakeven = null, floor = DEFAULT_FLOOR, status = "insufficient-sample";
    if (n >= MIN_SAMPLE) {
      if (p <= 0) { floor = CAP; status = "disabled"; }
      else {
        breakeven = (1 - p) / p;
        floor = round(Math.min(Math.max(breakeven * SAFETY, DEFAULT_FLOOR), CAP), 2);
        status = floor >= CAP ? "disabled" : "active";
      }
    }
    out.push({ key, trades: n, win_rate: round(p * 100, 1),
               breakeven_rr: breakeven === null ? null : round(breakeven, 2),
               floor, status });
  }
  return out.sort((a, b) => b.trades - a.trades);
}

// Prefer the stored r_multiple. Fall back to deriving it from the realised
// percentage move against the original risk (entry → stop) so older rows that
// predate the r_multiple column still contribute.
function rMultiple(r) {
  const stored = num(r.r_multiple);
  if (stored !== null) return stored;
  const pnl = num(r.pnl_pct);
  const entry = num(r.entry);
  const sl = num(r.sl);
  if (pnl === null || entry === null || sl === null || entry === 0) return null;
  const riskPct = (Math.abs(entry - sl) / entry) * 100;
  if (riskPct === 0) return null;
  return pnl / riskPct;
}

function headline(closed) {
  const n = closed.length;
  if (!n) {
    return { trades: 0, win_rate: null, avg_r: null, expectancy_r: null,
             avg_win_r: null, avg_loss_r: null, profit_factor: null,
             best_r: null, worst_r: null, max_drawdown_r: null };
  }
  const wins = closed.filter((r) => r.badge === "win");
  const losses = closed.filter((r) => r.badge === "loss");
  const rs = closed.map((r) => r.r_multiple).filter((v) => v !== null);
  const winR = wins.map((r) => r.r_multiple).filter((v) => v !== null);
  const lossR = losses.map((r) => r.r_multiple).filter((v) => v !== null);

  const grossWin = winR.reduce((a, b) => a + Math.max(b, 0), 0);
  const grossLoss = Math.abs(lossR.reduce((a, b) => a + Math.min(b, 0), 0));

  return {
    trades: n,
    wins: wins.length,
    losses: losses.length,
    win_rate: round((wins.length / n) * 100, 1),
    avg_r: rs.length ? round(mean(rs), 3) : null,
    // Expectancy per trade in R — the number that actually decides whether the
    // system is worth running.
    expectancy_r: rs.length ? round(mean(rs), 3) : null,
    avg_win_r: winR.length ? round(mean(winR), 2) : null,
    avg_loss_r: lossR.length ? round(mean(lossR), 2) : null,
    profit_factor: grossLoss > 0 ? round(grossWin / grossLoss, 2) : null,
    best_r: rs.length ? round(Math.max(...rs), 2) : null,
    worst_r: rs.length ? round(Math.min(...rs), 2) : null,
    max_drawdown_r: maxDrawdown(closed),
  };
}

function equityCurve(closed) {
  const ordered = [...closed]
    .filter((r) => r.r_multiple !== null)
    .sort((a, b) => (a.closed_at < b.closed_at ? -1 : a.closed_at > b.closed_at ? 1 : 0));
  let cum = 0;
  return ordered.map((r, i) => {
    cum += r.r_multiple;
    return { i: i + 1, date: r.closed_at, symbol: r.symbol,
             r: round(r.r_multiple, 3), cum_r: round(cum, 3) };
  });
}

function maxDrawdown(closed) {
  const curve = equityCurve(closed);
  if (!curve.length) return null;
  let peak = 0, dd = 0;
  for (const p of curve) {
    if (p.cum_r > peak) peak = p.cum_r;
    dd = Math.min(dd, p.cum_r - peak);
  }
  return round(dd, 2);
}

// Group closed trades by an arbitrary key. minTrades drops buckets too small
// to mean anything — a 1-trade symbol at 100% win rate is noise, not an edge.
function group(closed, keyFn, minTrades = 1) {
  const buckets = new Map();
  for (const r of closed) {
    const k = keyFn(r) || "—";
    if (!buckets.has(k)) buckets.set(k, []);
    buckets.get(k).push(r);
  }
  const out = [];
  for (const [key, items] of buckets) {
    if (items.length < minTrades) continue;
    const wins = items.filter((r) => r.badge === "win").length;
    const rs = items.map((r) => r.r_multiple).filter((v) => v !== null);
    out.push({
      key,
      trades: items.length,
      wins,
      losses: items.length - wins,
      win_rate: round((wins / items.length) * 100, 1),
      avg_r: rs.length ? round(mean(rs), 3) : null,
      total_r: rs.length ? round(rs.reduce((a, b) => a + b, 0), 2) : null,
    });
  }
  return out.sort((a, b) => (b.total_r ?? -999) - (a.total_r ?? -999));
}

const mean = (a) => a.reduce((x, y) => x + y, 0) / a.length;
const round = (v, d) => (v === null || !Number.isFinite(v) ? null : Math.round(v * 10 ** d) / 10 ** d);
