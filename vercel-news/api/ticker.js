// GET /api/ticker — everything that scrolls in the header rail.
//
// This replaces /api/markets (nine instruments) and the "What moved" grid that
// duplicated it directly underneath. One rail, many segments, ordered the way
// the trading day actually happens in IST rather than by asset class:
//
//   05:30  Asia-Pacific opens      →  🌏 ASIA
//   09:15  NSE opens               →  🇮🇳 INDIA, then its movers
//   12:30  Europe opens            →  🇪🇺 EUROPE
//   19:00  US opens                →  🇺🇸 US and the ten biggest names in it
//   ——     round the clock         →  🛢 COMMODITIES · 💱 FX · ₿ CRYPTO
//   ——     from the ledger         →  💎 MULTIBAGGERS
//
// Quotes come from Yahoo's spark endpoint, which takes up to 20 symbols per
// request — about 110 symbols here, so six parallel calls instead of 110. The
// v8 chart endpoint used by the old route is one symbol per request and would
// not have fit inside the function's 15s budget.
import { db, num, str, json, fail } from "./_db.js";

const SPARK = "https://query1.finance.yahoo.com/v7/finance/spark";
const CHUNK = 20;             // Yahoo's hard cap; 21 symbols returns a 400
const UA = { "User-Agent": "Mozilla/5.0", Accept: "application/json" };

// dp = decimal places. `spot` routes metals to the spot feed — Yahoo's GC=F is
// a single dated futures contract carrying a cost-of-carry premium and it
// jumps every time the front month rolls, so it disagrees with every price a
// human checks. Kept as the fallback when the spot feed is down.
const ASIA = [
  ["Nikkei 225", "^N225", "¥", 0],
  ["Hang Seng", "^HSI", "", 0],
  ["Shanghai", "000001.SS", "", 0],
  ["KOSPI", "^KS11", "", 0],
  ["ASX 200", "^AXJO", "", 0],
];

const INDIA = [
  ["Nifty 50", "^NSEI", "₹", 0],
  ["Sensex", "^BSESN", "₹", 0],
  ["Bank Nifty", "^NSEBANK", "₹", 0],
  ["Nifty Next 50", "NIFTY_NEXT_50.NS", "₹", 0],
  ["Midcap 100", "NIFTY_MIDCAP_100.NS", "₹", 0],
  ["Smallcap 250", "NIFTYSMLCAP250.NS", "₹", 0],
  ["Nifty IT", "^CNXIT", "₹", 0],
  ["Nifty Auto", "^CNXAUTO", "₹", 0],
  ["Nifty Pharma", "^CNXPHARMA", "₹", 0],
  ["Nifty FMCG", "^CNXFMCG", "₹", 0],
  ["Nifty Metal", "^CNXMETAL", "₹", 0],
  ["Nifty Energy", "^CNXENERGY", "₹", 0],
  ["Nifty Realty", "^CNXREALTY", "₹", 0],
  ["Nifty PSU Bank", "^CNXPSUBANK", "₹", 0],
  ["Nifty Fin Svc", "^CNXFIN", "₹", 0],
  ["India VIX", "^INDIAVIX", "", 2],
];

const EUROPE = [
  ["FTSE 100", "^FTSE", "£", 0],
  ["DAX", "^GDAXI", "€", 0],
  ["CAC 40", "^FCHI", "€", 0],
];

const US_INDICES = [
  ["S&P 500", "^GSPC", "", 0],
  ["Nasdaq", "^IXIC", "", 0],
  ["Dow", "^DJI", "", 0],
];

// The ten largest US listings by market cap. Hand-maintained; the ranking
// moves slowly and every automated source for it needs a crumbed session.
const US_TOP10 = [
  ["NVDA", "NVDA", "$", 2], ["AAPL", "AAPL", "$", 2], ["MSFT", "MSFT", "$", 2],
  ["GOOGL", "GOOGL", "$", 2], ["AMZN", "AMZN", "$", 2], ["META", "META", "$", 2],
  ["AVGO", "AVGO", "$", 2], ["TSLA", "TSLA", "$", 2], ["BRK-B", "BRK-B", "$", 2],
  ["LLY", "LLY", "$", 2],
];

const COMMODITIES = [
  ["Gold", "GC=F", "$", 1, "XAU"],
  ["Silver", "SI=F", "$", 2, "XAG"],
  ["Crude WTI", "CL=F", "$", 2],
  ["Brent", "BZ=F", "$", 2],
  ["Nat Gas", "NG=F", "$", 3],
  ["Copper", "HG=F", "$", 3],
];

// MYR pairs are here because the household spends in ringgit while the income
// and the portfolio are in rupees — USD/MYR and MYR/INR are the two rates that
// actually change a decision.
const FX = [
  ["USD/INR", "USDINR=X", "₹", 2],
  ["MYR/INR", "MYRINR=X", "₹", 2],
  ["USD/MYR", "USDMYR=X", "RM", 3],
  ["AED/INR", "AEDINR=X", "₹", 2],
  ["EUR/USD", "EURUSD=X", "$", 4],
  ["GBP/USD", "GBPUSD=X", "$", 4],
  ["USD/JPY", "USDJPY=X", "¥", 2],
];

const CRYPTO = [
  ["BTC", "BTC-USD", "$", 0], ["ETH", "ETH-USD", "$", 0],
  ["SOL", "SOL-USD", "$", 2], ["BNB", "BNB-USD", "$", 2],
  ["XRP", "XRP-USD", "$", 4], ["DOGE", "DOGE-USD", "$", 4],
];

// Nifty 50 constituents, for the movers segment. Reconstituted twice a year;
// a wrong name costs one row, not the segment.
const NIFTY50 = [
  "RELIANCE", "HDFCBANK", "ICICIBANK", "INFY", "TCS", "BHARTIARTL", "SBIN",
  "LT", "ITC", "HINDUNILVR", "KOTAKBANK", "AXISBANK", "BAJFINANCE", "M&M",
  "MARUTI", "SUNPHARMA", "NTPC", "TATAMOTORS", "TITAN", "ULTRACEMCO",
  "ASIANPAINT", "POWERGRID", "ADANIENT", "ADANIPORTS", "COALINDIA", "BAJAJFINSV",
  "NESTLEIND", "WIPRO", "ONGC", "JSWSTEEL", "TATASTEEL", "HCLTECH", "GRASIM",
  "TECHM", "HINDALCO", "INDUSINDBK", "DRREDDY", "CIPLA", "BRITANNIA", "EICHERMOT",
  "APOLLOHOSP", "DIVISLAB", "HEROMOTOCO", "BAJAJ-AUTO", "TATACONSUM", "BPCL",
  "SBILIFE", "HDFCLIFE", "SHRIRAMFIN", "TRENT",
];

export default async function handler(req, res) {
  if (req.method !== "GET") return fail(res, 405, "GET only");

  try {
    // One symbol list, one batched fetch, then sliced back into segments —
    // a symbol appearing in two segments is quoted once.
    const defs = [
      ...ASIA, ...INDIA, ...EUROPE, ...US_INDICES, ...US_TOP10,
      ...COMMODITIES, ...FX, ...CRYPTO,
    ];

    const mb = await multibaggers();
    for (const m of mb) defs.push([m.symbol, `${m.symbol}.NS`, "₹", 2]);
    const movers = NIFTY50.map((s) => [s, `${s}.NS`, "₹", 2]);

    const quotes = await quoteAll([...defs, ...movers]);
    const pick = (list) => list.map((d) => shape(d, quotes)).filter(Boolean);

    // Up-only and down-only, not "top and bottom five". On a strong day the
    // bottom five are all green, and a segment headed 🔻 LOSERS full of green
    // numbers is worse than a short segment.
    const nifty = pick(movers).sort((a, b) => b.change_pct - a.change_pct);
    const gainers = nifty.filter((s) => s.change_pct > 0).slice(0, 5);
    const losers = nifty.filter((s) => s.change_pct < 0).slice(-5).reverse();

    const mbItems = pick(mb.map((m) => [m.symbol, `${m.symbol}.NS`, "₹", 2]))
      .map((it) => {
        const m = mb.find((x) => x.symbol === it.name);
        return { ...it, note: m && m.target ? `T ₹${fmtNum(m.target, 0)}` : null };
      });

    const segments = [
      seg("asia", "ASIA", "🌏", pick(ASIA)),
      seg("india", "INDIA", "🇮🇳", pick(INDIA)),
      seg("gainers", "NIFTY 50 GAINERS", "🚀", gainers),
      seg("losers", "NIFTY 50 LOSERS", "🔻", losers),
      seg("europe", "EUROPE", "🇪🇺", pick(EUROPE)),
      seg("us", "US", "🇺🇸", pick(US_INDICES)),
      seg("ustop", "US TOP 10", "🏛", pick(US_TOP10)),
      seg("commodities", "COMMODITIES", "🛢", pick(COMMODITIES)),
      seg("fx", "FX", "💱", pick(FX)),
      seg("crypto", "CRYPTO", "₿", pick(CRYPTO)),
      seg("multibagger", "MULTIBAGGER IDEAS", "💎", mbItems),
    ].filter((s) => s.items.length);

    // "Markets advancing" in the hero counts headline instruments only —
    // indices and majors. Counting 50 Nifty constituents would make it a
    // breadth reading of one exchange wearing a global label.
    const headline = [...ASIA, ...INDIA, ...EUROPE, ...US_INDICES,
                      ...COMMODITIES, ...FX, ...CRYPTO].map((d) => shape(d, quotes))
                      .filter(Boolean);

    json(res, 200, {
      ok: true,
      fetched_at: new Date().toISOString(),
      live: headline.length,
      total: headline.length,
      advancing: headline.filter((m) => m.up).length,
      segments,
    }, 300);
  } catch (e) {
    fail(res, 500, `ticker fetch failed: ${e.message}`);
  }
}

function seg(key, label, icon, items) {
  return { key, label, icon, items };
}

// ── quotes ───────────────────────────────────────────────────────────────────

/** symbol → {price, prev} for every symbol in `defs`, batched 20 at a time. */
async function quoteAll(defs) {
  const symbols = [...new Set(defs.map((d) => d[1]))];
  const chunks = [];
  for (let i = 0; i < symbols.length; i += CHUNK) {
    chunks.push(symbols.slice(i, i + CHUNK));
  }

  const out = new Map();
  const results = await Promise.all(chunks.map(spark));
  for (const r of results) for (const [k, v] of r) out.set(k, v);
  await retryMissing(symbols, out);

  // Metals last: overwrite the futures price with spot, keeping the futures
  // previous close because the spot feed carries no history.
  await Promise.all(
    COMMODITIES.filter((c) => c[4]).map(async (c) => {
      const cur = out.get(c[1]);
      if (!cur) return;
      try {
        const p = await spot(c[4]);
        out.set(c[1], { ...cur, price: p, basis: "spot" });
      } catch { /* spot down — the futures quote already in the map stands */ }
    })
  );

  return out;
}

// range=1d, NOT 5d. chartPreviousClose is the close before the requested
// window, so on a 5-day range it yields a five-day move wearing a daily
// label — that is what had the site printing Nifty +3.29% on a day it moved
// +1.60%, and MSFT +25%. One day in, one day's change out.
const RANGE = "range=1d&interval=1d";

async function spark(symbols) {
  const out = new Map();
  try {
    const url = `${SPARK}?symbols=${symbols.map(encodeURIComponent).join(",")}&${RANGE}`;
    const r = await fetch(url, { headers: UA, signal: AbortSignal.timeout(8000) });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const j = await r.json();
    for (const row of j?.spark?.result || []) {
      const meta = row?.response?.[0]?.meta;
      if (!meta) continue;
      const q = readMeta(meta);
      if (q) out.set(str(meta.symbol), q);
    }
  } catch {
    // A dead chunk drops its symbols; the individual retry below picks them up,
    // and anything still missing is skipped by shape(). Never blank the rail.
  }
  return out;
}

function readMeta(meta) {
  const price = num(meta.regularMarketPrice);
  if (price === null) return null;
  const prev = num(meta.chartPreviousClose) ?? num(meta.previousClose) ?? price;
  return { price, prev, basis: "market" };
}

// Spark silently omits a symbol now and then — USD/MYR dropped out of an
// otherwise healthy batch during testing. One retry each on the single-symbol
// chart endpoint, which does not have that habit.
async function retryMissing(symbols, out) {
  const missing = symbols.filter((s) => !out.has(s));
  if (!missing.length) return;
  await Promise.all(missing.slice(0, 24).map(async (s) => {
    try {
      const r = await fetch(
        `https://query1.finance.yahoo.com/v8/finance/chart/${encodeURIComponent(s)}?${RANGE}`,
        { headers: UA, signal: AbortSignal.timeout(6000) }
      );
      if (!r.ok) return;
      const meta = (await r.json())?.chart?.result?.[0]?.meta;
      const q = meta && readMeta(meta);
      if (q) out.set(s, q);
    } catch { /* genuinely unavailable — segment renders without it */ }
  }));
}

async function spot(code) {
  const r = await fetch(`https://api.gold-api.com/price/${code}`, {
    headers: UA, signal: AbortSignal.timeout(5000),
  });
  if (!r.ok) throw new Error(`spot HTTP ${r.status}`);
  const p = Number((await r.json())?.price);
  if (!Number.isFinite(p) || p <= 0) throw new Error("spot: no usable price");
  return p;
}

function shape([name, symbol, prefix, dp], quotes) {
  const q = quotes.get(symbol);
  if (!q || q.price === null) return null;
  const pct = q.prev ? ((q.price - q.prev) / q.prev) * 100 : 0;
  return {
    name,
    symbol,
    price: prefix + fmtNum(q.price, dp),
    price_raw: q.price,
    change_pct: Math.round(pct * 100) / 100,
    up: pct >= 0,
    basis: q.basis || "market",
  };
}

const fmtNum = (v, dp) =>
  v.toLocaleString("en-US", { minimumFractionDigits: dp, maximumFractionDigits: dp });

// ── multibaggers ─────────────────────────────────────────────────────────────

/** The most recent weekly multibagger scan, best five by score. */
async function multibaggers() {
  try {
    const t = await db().execute(
      "SELECT name FROM sqlite_master WHERE type='table' AND name='multibaggers'"
    );
    if (!t.rows.length) return [];
    // The multibagger scan is weekly (Saturday). If the newest row is older
    // than a month the scan has stopped running, and quietly presenting
    // stale ideas as current is worse than dropping the segment.
    const rs = await db().execute(
      `SELECT symbol, score, target2 FROM multibaggers
       WHERE date = (SELECT MAX(date) FROM multibaggers)
         AND date >= date('now', '-31 days')
       ORDER BY score DESC LIMIT 5`
    );
    return rs.rows
      .map((r) => ({
        symbol: str(r.symbol).replace(/\.NS$/i, "").toUpperCase(),
        score: num(r.score),
        target: num(r.target2),
      }))
      .filter((r) => r.symbol);
  } catch {
    return [];
  }
}
