// GET /api/markets — live quotes, replacing the 6 AM snapshot baked into the
// daily shell. That snapshot is why gold read 4093 all day regardless of what
// gold actually did.
//
// Edge-cached for 5 minutes: fresh enough to be worth looking at, cheap enough
// that a page refresh does not cost eight upstream requests every time.
import { num, json, fail, db } from "./_db.js";

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

// NSE sector indices. Same eleven market_intel.py builds the daily heat from,
// so the live map and the 6 AM one cannot disagree about which sectors exist.
const SECTORS = [
  { name: "IT",       symbol: "^CNXIT" },
  { name: "Banking",  symbol: "^NSEBANK" },
  { name: "FMCG",     symbol: "^CNXFMCG" },
  { name: "Pharma",   symbol: "^CNXPHARMA" },
  { name: "Auto",     symbol: "^CNXAUTO" },
  { name: "Metal",    symbol: "^CNXMETAL" },
  { name: "Energy",   symbol: "^CNXENERGY" },
  { name: "Realty",   symbol: "^CNXREALTY" },
  { name: "Media",    symbol: "^CNXMEDIA" },
  { name: "PSU Bank", symbol: "^CNXPSUBANK" },
  { name: "Infra",    symbol: "^CNXINFRA" },
];

// GET /api/markets?heat=1 — the sector map and FII/DII, live.
//
// Folded into this route rather than given its own file: the Vercel Hobby plan
// caps this project at 12 serverless functions and it is AT 12. A 13th file
// silently breaks the whole deployment.
//
// Two different freshness stories, deliberately not blended:
//
//   heat  Yahoo, fetched now, cached 15 minutes at the edge. The daily shell
//         baked a 6 AM snapshot that read the same all day.
//   fii   read from the market_intel cache, because NSE's own API is what
//         publishes it and it is not reliably reachable from a datacenter IP.
//         It is a once-a-day number anyway — NSE publishes provisional FII/DII
//         after the close — so a live fetch would buy nothing. What it carries
//         instead is its own DATE, so a reader can see it is yesterday's.
async function heat(res) {
  const sectors = await Promise.all(
    SECTORS.map(async (s) => {
      try {
        const r = await fetch(
          `https://query1.finance.yahoo.com/v8/finance/chart/${encodeURIComponent(s.symbol)}?range=1d&interval=1d`,
          { headers: { "User-Agent": "Mozilla/5.0" }, signal: AbortSignal.timeout(6000) }
        );
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const meta = (await r.json())?.chart?.result?.[0]?.meta;
        const price = num(meta?.regularMarketPrice);
        const prev = num(meta?.chartPreviousClose) ?? num(meta?.previousClose);
        if (price === null || !prev) throw new Error("no price");
        return { name: s.name, pct: Math.round(((price - prev) / prev) * 10000) / 100 };
      } catch {
        // OMITTED, never shown at 0%. A flat sector and a broken feed must
        // not look the same on a heatmap — the same rule market_intel.py
        // already applies to the daily build.
        return null;
      }
    })
  );
  const live = sectors.filter(Boolean).sort((a, b) => b.pct - a.pct);

  let fii = [];
  let fii_date = null;
  try {
    const rs = await db().execute({
      sql: "SELECT payload FROM newspaper_market_intel ORDER BY date DESC LIMIT 1",
      args: [],
    });
    if (rs.rows.length) {
      const p = JSON.parse(String(rs.rows[0].payload));
      fii = Array.isArray(p.fii_dii) ? p.fii_dii : [];
      fii_date = p.generated_at || null;
    }
  } catch {
    // The map is still worth serving without the flow numbers.
  }

  json(res, 200, {
    ok: true,
    fetched_at: new Date().toISOString(),
    heat: live,
    heat_live: live.length,
    heat_total: SECTORS.length,
    fii,
    fii_as_of: fii_date,
  }, 900);   // 15 minutes
}

export default async function handler(req, res) {
  if (req.method !== "GET") return fail(res, 405, "GET only");
  try {
    if ((req.query || {}).heat) return await heat(res);
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
      // range=1d, not 5d: chartPreviousClose is the close before the requested
      // window, so a 5d range returned a five-day move labelled as the day's.
      `https://query1.finance.yahoo.com/v8/finance/chart/${encodeURIComponent(t.symbol)}?range=1d&interval=1d`,
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
