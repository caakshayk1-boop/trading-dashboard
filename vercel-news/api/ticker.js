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

    // Both Turso reads in parallel. Sequential awaits put two round trips on
    // the critical path of a cold invocation for no reason — neither query
    // depends on the other.
    const [mb, ledgerSyms] = await Promise.all([multibaggers(), openLedgerSymbols()]);
    for (const m of mb) defs.push([m.symbol, `${m.symbol}.NS`, "₹", 2]);
    const movers = NIFTY50.map((s) => [s, `${s}.NS`, "₹", 2]);

    // Open ledger symbols ride the SAME batch. The signal log showed an entry
    // and then a dash for price and P&L on every open row — 77 of them — so a
    // reader could see what was signalled and never what it had done since.
    // A dedicated endpoint was not an option (Hobby caps a deployment at 12
    // functions and this project is at 12), and quoting from the browser is
    // blocked by connect-src 'self'. This route already batches Yahoo quotes
    // and already reads Turso, so the marginal cost is ~53 symbols on an
    // existing fetch and no new request from the page at all.
    const ledgerDefs = ledgerSyms.map((s) => [s, `${s}.NS`, "₹", 2]);

    // Non-equity ledger rows (commodities, FX) resolved to their REAL ticker
    // instead of "SYMBOL.NS". Excluding them stopped the bogus fetches, but it
    // also meant an open silver trade showed a dash forever. Worse, while they
    // were mistagged as NSE equities the row was quoted as SILVER.NS — a real,
    // unrelated NSE company at ₹233 — and the page published that as the last
    // price of a silver trade at $66/oz.
    //
    // These tickers are ALREADY in the batch above for the commodity and FX
    // rail, so mapping the ledger onto them costs no extra fetch: quoteAll is
    // keyed by Yahoo symbol and de-duplicates.
    const ledgerNonEq = await openLedgerNonEquities();

    const quotes = await quoteAll(
      [...defs, ...movers, ...ledgerDefs],
      ledgerDefs.map((d) => d[1])        // ledger quotes retry first
    );
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
      // Every NIFTY 50 constituent with its live move, not just the ten that
      // reach the rail. The heat map above the fold names eleven SECTORS and
      // could not name a single stock inside one, because the only per-stock
      // data on the page was the five gainers and five losers already shown.
      //
      // These quotes are ALREADY fetched for the movers segments — the rail
      // needs the whole 50 to know which five are top and which five bottom —
      // so shipping all of them costs one array in a response that was going
      // out anyway. No extra request, no extra function (Hobby caps this
      // project at 12 and it is at 12).
      //
      // Sector is deliberately NOT attached here: the mapping lives in the
      // stock screen, which is a build-time artefact, and duplicating it into a
      // serverless function is how the two drift apart.
      constituents: nifty.map((s) => ({
        symbol: s.name, price: s.price_raw, change_pct: s.change_pct, up: s.up,
      })),
      // Keyed by bare ledger symbol, not the Yahoo one, because that is what
      // all_signals stores and what the table joins on. Symbols Yahoo could
      // not price — commodities and FX rows like BRNUSD, which are not
      // "SYMBOL.NS" — are simply absent, and the table falls back to a dash
      // exactly as it does today. A missing quote must never render as 0.
      ledger: Object.fromEntries(
        [...ledgerDefs, ...ledgerNonEq]
          // The currency marker comes from the DEF, not the quote — quoteAll
          // returns {price, prev, basis} and carries no currency. The page
          // renders ₹ by default because the ledger is overwhelmingly NSE, so
          // a dollar-quoted commodity has to say so or $66 silver reads as ₹66.
          .map(([name, ysym, prefix]) => [name, quotes.get(ysym), prefix])
          .filter(([, q]) => q && q.price !== null && q.price !== undefined)
          .map(([name, q, prefix]) => [name, {
            price: q.price,
            change_pct: q.prev ? Math.round(((q.price - q.prev) / q.prev) * 10000) / 100 : null,
            ccy: prefix || "₹",
          }])
      ),
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
// `priority` holds the Yahoo symbols whose absence is VISIBLE to a reader —
// the open ledger rows. They get the retry budget before decorative movers.
async function quoteAll(defs, priority = []) {
  const symbols = [...new Set(defs.map((d) => d[1]))];
  const chunks = [];
  for (let i = 0; i < symbols.length; i += CHUNK) {
    chunks.push(symbols.slice(i, i + CHUNK));
  }

  const out = new Map();
  const results = await Promise.all(chunks.map(spark));
  for (const r of results) for (const [k, v] of r) out.set(k, v);
  await retryMissing(symbols, out, priority);

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
// Retry budget and wave size. Spark silently drops symbols from a 20-symbol
// batch — it answers 200 with fewer results than asked for — so the retry path
// is not an edge case, it is load-bearing.
//
// It used to be `missing.slice(0, 24)`, one flat wave, ordered by whatever the
// caller happened to pass. That was survivable while the ledger held ~55 open
// rows. Reopening 29 wrongly-expired multibagger positions (see
// fix_horizons.py) pushed the batch past ~115 symbols, spark dropped more than
// 24, and everything past the 24th silently rendered "—" in the Last column.
// The reported symptom was "live price not coming for all".
//
// Two changes: a bigger budget spread over bounded waves so concurrency against
// Yahoo stays sane, and PRIORITY, because a missing ledger quote is a visible
// hole in the signal log while a missing mover is one absent decoration.
const RETRY_BUDGET = 48;
const RETRY_WAVE = 16;

async function retryMissing(symbols, out, priority = []) {
  const missing = symbols.filter((s) => !out.has(s));
  if (!missing.length) return;

  // Ledger symbols first, then everything else, then truncate to the budget.
  const pri = new Set(priority);
  const queue = [
    ...missing.filter((s) => pri.has(s)),
    ...missing.filter((s) => !pri.has(s)),
  ].slice(0, RETRY_BUDGET);

  const one = async (s) => {
    try {
      const r = await fetch(
        `https://query1.finance.yahoo.com/v8/finance/chart/${encodeURIComponent(s)}?${RANGE}`,
        { headers: UA, signal: AbortSignal.timeout(4500) }
      );
      if (!r.ok) return;
      const meta = (await r.json())?.chart?.result?.[0]?.meta;
      const q = meta && readMeta(meta);
      if (q) out.set(s, q);
    } catch { /* genuinely unavailable — segment renders without it */ }
  };

  // Waves rather than one Promise.all over 48: the per-symbol timeout is 4.5s
  // and the whole route has 15s, so three waves is the most that can run before
  // the budget has to matter. Later waves are simply skipped if time is short,
  // which degrades the least important symbols first because of the ordering
  // above.
  const started = Date.now();
  for (let i = 0; i < queue.length; i += RETRY_WAVE) {
    if (Date.now() - started > 9000) break;      // leave room for the response
    await Promise.all(queue.slice(i, i + RETRY_WAVE).map(one));
  }
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

// Canonical symbol → real Yahoo ticker for everything that is NOT an NSE
// equity. Mirrors symbols.py's NON_EQUITY map — the Python side is the source
// of truth and writes all_signals.market/.asset_type from it; this is the read
// side. Keep the two in step: a symbol missing here is simply not priced,
// which is the safe failure (a dash), never a wrong number.
const LEDGER_NON_EQUITY = {
  GOLD: ["GC=F", "$"], XAUUSD: ["GC=F", "$"], GC: ["GC=F", "$"],
  SILVER: ["SI=F", "$"], XAGUSD: ["SI=F", "$"], SI: ["SI=F", "$"],
  CRUDE: ["CL=F", "$"], WTIUSD: ["CL=F", "$"], WTI: ["CL=F", "$"],
  BRENT: ["BZ=F", "$"], BRNUSD: ["BZ=F", "$"],
  NATGAS: ["NG=F", "$"], NGAS: ["NG=F", "$"],
  COPPER: ["HG=F", "$"],
  USDINR: ["INR=X", "₹"], EURUSD: ["EURUSD=X", "$"], GBPUSD: ["GBPUSD=X", "$"],
  NIFTY: ["^NSEI", ""], NIFTY50: ["^NSEI", ""], BANKNIFTY: ["^NSEBANK", ""],
  SENSEX: ["^BSESN", ""],
};

/**
 * Open ledger rows that are NOT NSE equities, mapped to their real ticker.
 *
 * Deliberately selects the complement of openLedgerSymbols() rather than
 * pattern-matching the symbol string: the ledger records what each row IS.
 * Rows still carrying the old default tagging are caught by the symbol-map
 * lookup, so this is correct both before and after the backfill.
 */
async function openLedgerNonEquities() {
  try {
    const rs = await db().execute(
      `SELECT symbol FROM all_signals
        WHERE UPPER(COALESCE(status,'OPEN')) = 'OPEN'
          AND symbol IS NOT NULL AND symbol != ''
        GROUP BY symbol
        ORDER BY MAX(date) DESC, symbol LIMIT 120`
    );
    const out = [];
    const seen = new Set();
    for (const r of rs.rows) {
      const s = str(r.symbol).toUpperCase();
      const hit = LEDGER_NON_EQUITY[s];
      if (!hit || seen.has(s)) continue;
      seen.add(s);
      out.push([s, hit[0], hit[1], 2]);
    }
    return out;
  } catch (e) {
    // Never let this take the rail down — it is an enhancement over the
    // equity path, which has its own try/catch and its own fallback.
    console.warn("openLedgerNonEquities:", e && e.message);
    return [];
  }
}

/** Distinct symbols on OPEN ledger rows. Bounded — this rides a live fetch. */
async function openLedgerSymbols() {
  try {
    // market/asset_type, NOT a shape test on the ticker. A regex accepted
    // BRNUSD, XAUUSD and every other commodity and FX row, which then went to
    // Yahoo as "BRNUSD.NS" — a symbol that cannot resolve. Each one fell
    // through to retryMissing(), which spends up to 6s per symbol and is
    // capped at 24, so the bogus ones both added seconds to a cold start and
    // crowded out retries for instruments that are genuinely real. That is
    // what took this route past the function timeout and dropped the whole
    // rail to its /api/markets fallback. The ledger already records what each
    // row IS; ask it instead of guessing from the string.
    const rs = await db().execute(
      // GROUP BY, not SELECT DISTINCT: ordering a DISTINCT by a column that is
      // not in the select list is invalid, and this query throwing would drop
      // the whole rail to its /api/markets fallback. MAX(date) gives the most
      // recent signal per symbol, which is what "degrade by recency" needs.
      `SELECT symbol FROM all_signals
        WHERE UPPER(COALESCE(status,'OPEN')) = 'OPEN'
          AND UPPER(COALESCE(market,'NSE')) = 'NSE'
          AND UPPER(COALESCE(asset_type,'EQUITY')) = 'EQUITY'
          AND symbol IS NOT NULL AND symbol != ''
        -- date DESC, not symbol: the cap has to degrade by RECENCY. Ordered
        -- alphabetically it would silently drop the tail of the alphabet, so
        -- every open position in S-Z loses its price the moment the ledger
        -- outgrows the cap. 120 because the open set reached 84 after 29
        -- wrongly-expired positions were reopened, and spark chunks are fetched
        -- in parallel so the marginal cost of a higher cap is one round trip.
        GROUP BY symbol
        ORDER BY MAX(date) DESC, symbol LIMIT 120`
    );
    return rs.rows
      .map((r) => str(r.symbol).toUpperCase())
      .filter((s) => /^[A-Z0-9&-]{2,20}$/.test(s));
  } catch {
    return [];
  }
}

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
