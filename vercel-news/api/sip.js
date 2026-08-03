// GET /api/sip — the monthly SIP buckets, their cost basis and their returns.
//
// The plan: ₹10,000/month stepped up 10% every SIP year, one new bucket per
// month, four names per bucket, each bucket tracked with its own cost basis.
// Buckets are deliberately never blended — a single portfolio average hides
// that one month's picks are up 40% and another's are down 12%, which is the
// only thing that tells you whether the ranking is working.
//
// Holdings are 'proposed' until something marks them held. A proposed name has
// no money in it, so it contributes nothing to invested, value or XIRR.
//
// Query params:
//   bucket=YYYY-MM   one bucket only
//   limit=           max buckets returned (default 36)
import { db, num, str, json, fail, columns } from "./_db.js";

const BASE_MONTHLY = 10000;
const STEP_UP = 0.10;
const SIP_START = { y: 2026, m: 8 };

export default async function handler(req, res) {
  if (req.method !== "GET") return fail(res, 405, "GET only");
  const q = req.query || {};

  try {
    // db() throws synchronously when the env vars are absent, so the .catch()
    // chained onto the query below never gets attached and the route 500s
    // instead of degrading. The projection table is pure arithmetic and worth
    // showing even with no ledger behind it.
    if (!process.env.TURSO_URL || !process.env.TURSO_TOKEN) {
      return json(res, 200, {
        ok: true, ready: false,
        message: "Ledger not configured for this deployment — showing the plan only.",
        plan: plan(), projections: projections(), buckets: [],
      }, 60);
    }

    // The SIP tables are created by sip_engine.init_sip_db() on the Python
    // side. Until that has run at least once they do not exist, and querying a
    // missing table throws — so the page gets an explicit "not set up yet"
    // rather than a 500.
    const t = await db().execute(
      "SELECT name FROM sqlite_master WHERE type='table' AND name='sip_buckets'"
    ).catch(() => ({ rows: [] }));
    if (!t.rows.length) {
      return json(res, 200, {
        ok: true, ready: false,
        message: "No SIP buckets yet — run `python sip_engine.py` to build the first one.",
        plan: plan(), projections: projections(), buckets: [],
      }, 60);
    }

    const where = [];
    const args = [];
    if (q.bucket) { where.push("bucket = ?"); args.push(String(q.bucket).slice(0, 7)); }
    const limit = Math.min(Math.max(parseInt(q.limit, 10) || 36, 1), 240);

    const brs = await db().execute({
      sql: `SELECT bucket, created_at, monthly_amount, sip_year, status
            FROM sip_buckets ${where.length ? "WHERE " + where.join(" AND ") : ""}
            ORDER BY bucket DESC LIMIT ?`,
      args: [...args, limit],
    });
    const names = brs.rows.map((r) => str(r.bucket));

    let holdings = [];
    if (names.length) {
      const ph = names.map(() => "?").join(",");
      // proposed_qty / ref_price arrive via ALTER TABLE in
      // sip_engine.init_sip_db(), so this route can be deployed before the
      // Python side has run. Naming a missing column fails the whole query.
      const cols = await columns("sip_holdings");
      const opt = (c) => (cols.has(c) ? c : `NULL AS ${c}`);
      const hrs = await db().execute({
        sql: `SELECT bucket, symbol, allocated, buy_price, qty, bought_at,
                     score, rank, rationale, status, last_price, last_price_at,
                     ${opt("ref_price")}, ${opt("proposed_qty")}
              FROM sip_holdings WHERE bucket IN (${ph}) ORDER BY bucket DESC, rank ASC`,
        args: names,
      });
      holdings = hrs.rows;
    }

    const byBucket = new Map(names.map((n) => [n, []]));
    for (const h of holdings) {
      const b = str(h.bucket);
      if (byBucket.has(b)) byBucket.get(b).push(shapeHolding(h));
    }

    const buckets = brs.rows.map((r) => {
      const name = str(r.bucket);
      const hs = byBucket.get(name) || [];
      const held = hs.filter((h) => h.status === "held" && h.qty && h.buy_price);
      const invested = held.reduce((a, h) => a + h.qty * h.buy_price, 0);
      const value = held.reduce(
        (a, h) => a + h.qty * (h.last_price || h.buy_price), 0);
      const flows = held
        .map((h) => [h.bought_at, -(h.qty * h.buy_price)])
        .filter(([d]) => d);
      if (flows.length) flows.push([new Date().toISOString(), value]);
      const r_ = flows.length > 1 ? xirr(flows) : null;
      return {
        bucket: name,
        created_at: str(r.created_at),
        monthly_amount: num(r.monthly_amount),
        sip_year: num(r.sip_year),
        status: str(r.status),
        names: hs.length,
        held: held.length,
        proposed_cost: round(
          hs.reduce((a, h) => a + (h.allocated || 0), 0), 2),
        cash_left: round(
          (num(r.monthly_amount) || 0) - hs.reduce((a, h) => a + (h.allocated || 0), 0), 2),
        invested: round(invested, 2),
        value: round(value, 2),
        pnl: round(value - invested, 2),
        pnl_pct: invested ? round((value / invested - 1) * 100, 2) : null,
        xirr_pct: r_ === null ? null : round(r_ * 100, 2),
        holdings: hs,
      };
    });

    const totInv = buckets.reduce((a, b) => a + (b.invested || 0), 0);
    const totVal = buckets.reduce((a, b) => a + (b.value || 0), 0);

    json(res, 200, {
      ok: true,
      ready: true,
      generated_at: new Date().toISOString(),
      plan: plan(),
      projections: projections(),
      totals: {
        buckets: buckets.length,
        invested: round(totInv, 2),
        value: round(totVal, 2),
        pnl: round(totVal - totInv, 2),
        pnl_pct: totInv ? round((totVal / totInv - 1) * 100, 2) : null,
      },
      buckets,
    }, 120);
  } catch (e) {
    fail(res, 500, `sip query failed: ${e.message}`);
  }
}

function shapeHolding(h) {
  return {
    symbol: str(h.symbol),
    allocated: num(h.allocated),
    ref_price: num(h.ref_price),
    // Shares the proposal says to buy, as a whole number. Distinct from `qty`,
    // which is shares actually bought.
    proposed_qty: num(h.proposed_qty),
    buy_price: num(h.buy_price),
    qty: num(h.qty),
    bought_at: str(h.bought_at),
    score: num(h.score),
    rank: num(h.rank),
    rationale: str(h.rationale),
    status: str(h.status) || "proposed",
    last_price: num(h.last_price),
    last_price_at: str(h.last_price_at),
  };
}

// Mirrors sip_engine.sip_year() / monthly_amount().
function plan() {
  const now = new Date();
  const months = (now.getUTCFullYear() - SIP_START.y) * 12
               + (now.getUTCMonth() + 1 - SIP_START.m);
  const year = Math.max(1, Math.floor(months / 12) + 1);
  return {
    base_monthly: BASE_MONTHLY,
    step_up_pct: STEP_UP * 100,
    sip_year: year,
    monthly_amount: round(BASE_MONTHLY * Math.pow(1 + STEP_UP, year - 1), 2),
    names_per_bucket: 4,
    start: `${SIP_START.y}-${String(SIP_START.m).padStart(2, "0")}`,
  };
}

// Step-up SIP future value. Contributions at month end, rate compounded
// monthly from the annual figure.
//
// Four horizons, three rates. 5/10/15 are the decision points; 18 is the one
// that matters most here — it is when the daughter reaches college age, and it
// is the only row on this page tied to a real date rather than a round number.
// 12/14/16% brackets what Indian equity has actually delivered; the old 10%
// floor was never the question being asked.
export const PROJECTION_YEARS = [5, 10, 15, 18];
export const PROJECTION_RATES = [["r12", 0.12], ["r14", 0.14], ["r16", 0.16]];

function projections() {
  const out = [];
  for (const years of PROJECTION_YEARS) {
    const row = { years };
    for (const [label, rate] of PROJECTION_RATES) {
      let m = BASE_MONTHLY, corpus = 0, invested = 0;
      const mr = Math.pow(1 + rate, 1 / 12) - 1;
      for (let y = 0; y < years; y++) {
        for (let k = 0; k < 12; k++) { corpus = corpus * (1 + mr) + m; invested += m; }
        m *= 1 + STEP_UP;
      }
      row.invested = Math.round(invested);
      row[label] = Math.round(corpus);
    }
    row.monthly = Math.round(BASE_MONTHLY * Math.pow(1 + STEP_UP, years - 1));
    out.push(row);
  }
  return out;
}

// Money-weighted return. Newton first, bisection when it diverges — a young
// SIP has few, lumpy cashflows and Newton walks off on those.
function xirr(flows, guess = 0.15) {
  const parsed = flows
    .map(([d, a]) => [new Date(d).getTime(), a])
    .filter(([t]) => Number.isFinite(t))
    .sort((a, b) => a[0] - b[0]);
  if (parsed.length < 2) return null;
  const t0 = parsed[0][0];
  const yrs = parsed.map(([t, a]) => [(t - t0) / (365 * 864e5), a]);
  if (!yrs.some(([, a]) => a < 0) || !yrs.some(([, a]) => a > 0)) return null;

  const npv = (r) => (r <= -0.999999 ? Infinity
    : yrs.reduce((s, [t, a]) => s + a / Math.pow(1 + r, t), 0));

  let r = guess;
  for (let i = 0; i < 80; i++) {
    const f = npv(r);
    if (Math.abs(f) < 1e-7) return r;
    const d = (npv(r + 1e-6) - f) / 1e-6;
    if (d === 0) break;
    const step = f / d;
    r -= step;
    if (r <= -0.999999) break;
    if (Math.abs(step) < 1e-9) return r;
  }
  let lo = -0.9999, hi = 10, flo = npv(lo);
  if (flo * npv(hi) > 0) return null;
  for (let i = 0; i < 200; i++) {
    const mid = (lo + hi) / 2, fm = npv(mid);
    if (Math.abs(fm) < 1e-7) return mid;
    if (flo * fm < 0) hi = mid; else { lo = mid; flo = fm; }
  }
  return (lo + hi) / 2;
}

const round = (v, d) =>
  v === null || !Number.isFinite(v) ? null : Math.round(v * 10 ** d) / 10 ** d;
