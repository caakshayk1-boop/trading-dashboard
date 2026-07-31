// GET /api/markets — live quotes, replacing the 6 AM snapshot baked into the
// daily shell. That snapshot is why gold read 4093 all day regardless of what
// gold actually did.
//
// Edge-cached for 5 minutes: fresh enough to be worth looking at, cheap enough
// that a page refresh does not cost eight upstream requests every time.
import { num, json, fail } from "./_db.js";

// Mirrors MARKET_TICKERS in content_cache.py — same symbols, same formatting,
// so the live values and the static fallback are directly comparable.
const TICKERS = [
  { name: "Nifty 50", symbol: "^NSEI",    prefix: "₹", dp: 0 },
  { name: "S&P 500",  symbol: "^GSPC",    prefix: "",  dp: 0 },
  { name: "Nasdaq",   symbol: "^IXIC",    prefix: "",  dp: 0 },
  { name: "Gold",     symbol: "GC=F",     prefix: "$", dp: 1 },
  { name: "Crude",    symbol: "CL=F",     prefix: "$", dp: 2 },
  { name: "USD/INR",  symbol: "USDINR=X", prefix: "₹", dp: 2 },
  { name: "BTC",      symbol: "BTC-USD",  prefix: "$", dp: 0 },
  { name: "Sensex",   symbol: "^BSESN",   prefix: "₹", dp: 0 },
];

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
    const price = num(meta?.regularMarketPrice);
    // chartPreviousClose is the prior session's close, which is what makes the
    // percentage match what every other quote screen shows.
    const prev = num(meta?.chartPreviousClose) ?? num(meta?.previousClose) ?? price;
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
    };
  } catch (e) {
    // A dead symbol must not blank the whole rail — the client keeps whatever
    // the daily shell rendered for this one.
    return { ...base, price_raw: null, price: "—", change_pct: 0, up: true, error: e.message };
  }
}
