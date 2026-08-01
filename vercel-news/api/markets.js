// GET /api/markets — live quotes, replacing the 6 AM snapshot baked into the
// daily shell. That snapshot is why gold read 4093 all day regardless of what
// gold actually did.
//
// Edge-cached for 5 minutes: fresh enough to be worth looking at, cheap enough
// that a page refresh does not cost eight upstream requests every time.
import { num, json, fail } from "./_db.js";

// Mostly mirrors MARKET_TICKERS in content_cache.py, with one deliberate
// departure: gold and silver quote SPOT, not futures.
//
// Yahoo's GC=F is a single dated contract — right now "Gold Aug 26" — which
// carries a cost-of-carry premium over spot and jumps discontinuously every
// time the front month rolls. It read 4105 while spot was 4048. Every price
// people actually check (Google, a jeweller, XAU/USD) is spot, so spot is what
// this shows. `metal` routes the symbol to the spot feed with the futures
// contract kept as a fallback.
const TICKERS = [
  { name: "Nifty 50", symbol: "^NSEI",    prefix: "₹", dp: 0 },
  { name: "S&P 500",  symbol: "^GSPC",    prefix: "",  dp: 0 },
  { name: "Nasdaq",   symbol: "^IXIC",    prefix: "",  dp: 0 },
  { name: "Gold",     symbol: "GC=F",     prefix: "$", dp: 1, metal: "XAU" },
  { name: "Silver",   symbol: "SI=F",     prefix: "$", dp: 2, metal: "XAG" },
  { name: "Crude",    symbol: "CL=F",     prefix: "$", dp: 2 },
  { name: "USD/INR",  symbol: "USDINR=X", prefix: "₹", dp: 2 },
  { name: "BTC",      symbol: "BTC-USD",  prefix: "$", dp: 0 },
  { name: "Sensex",   symbol: "^BSESN",   prefix: "₹", dp: 0 },
];

// Spot metals. The previous close still comes from the futures chart, because
// the spot feed has no history — the percentage is therefore spot-vs-futures-
// close and can be a few basis points off. Better a slightly soft percentage
// on the right price than an exact percentage on a price nobody quotes.
async function spotMetal(code) {
  const r = await fetch(`https://api.gold-api.com/price/${code}`, {
    headers: { "User-Agent": "Mozilla/5.0" },
    signal: AbortSignal.timeout(5000),
  });
  if (!r.ok) throw new Error(`spot HTTP ${r.status}`);
  const j = await r.json();
  const p = Number(j?.price);
  if (!Number.isFinite(p) || p <= 0) throw new Error("spot: no usable price");
  return p;
}

export default async function handler(req, res) {
  if (req.method !== "GET") return fail(res, 405, "GET only");
  try {
    const markets = await Promise.all(TICKERS.map(quote));
    const ok = markets.filter((m) => m.price_raw !== null).length;
    json(res, 200, {
      ok: true,
      fetched_at: new Date().toISOString(),
      live: ok,
      total: TICKERS.length,
      advancing: markets.filter((m) => m.price_raw !== null && m.up).length,
      markets,
    }, 300);
  } catch (e) {
    fail(res, 500, `markets fetch failed: ${e.message}`);
  }
}

async function quote(t) {
  const base = { name: t.name, symbol: t.symbol, prefix: t.prefix };
  try {
    const r = await fetch(
      `https://query1.finance.yahoo.com/v8/finance/chart/${encodeURIComponent(t.symbol)}?range=5d&interval=1d`,
      { headers: { "User-Agent": "Mozilla/5.0" }, signal: AbortSignal.timeout(6000) }
    );
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const meta = (await r.json())?.chart?.result?.[0]?.meta;
    let price = num(meta?.regularMarketPrice);
    // chartPreviousClose is the prior session's close, which is what makes the
    // percentage match what every other quote screen shows.
    const prev = num(meta?.chartPreviousClose) ?? num(meta?.previousClose) ?? price;

    let spot = false;
    if (t.metal) {
      try { price = await spotMetal(t.metal); spot = true; }
      catch { /* spot feed down — fall through to the futures price */ }
    }
    if (price === null) throw new Error("no price in response");

    const pct = prev ? ((price - prev) / prev) * 100 : 0;
    return {
      ...base,
      price_raw: price,
      price: t.prefix + price.toLocaleString("en-US", {
        minimumFractionDigits: t.dp, maximumFractionDigits: t.dp,
      }),
      change_pct: Math.round(pct * 100) / 100,
      up: pct >= 0,
      market_state: meta?.marketState || null,
      basis: spot ? "spot" : t.metal ? "futures (spot feed unavailable)" : "market",
    };
  } catch (e) {
    // A dead symbol must not blank the whole rail — the client keeps whatever
    // the daily shell rendered for this one.
    return { ...base, price_raw: null, price: "—", change_pct: 0, up: true, error: e.message };
  }
}
