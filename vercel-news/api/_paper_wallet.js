// Pure simulation logic for the paper wallet — no DB, no HTTP, so it can be
// unit-tested directly (paper_wallet.test.js) the same way _positions.js's
// pure functions are tested by positions.test.js. paper_wallet.js is the
// thin HTTP handler that fetches rows and hands them to simulateWallet().

export const CAPITAL = 5_000_000;
// Forward-only — confirmed with Akshay 2026-08-16. No historical replay: a
// signal from before this date never enters the simulation at all.
export const START_DATE = "2026-08-17";
export const GLOBAL_CAP_PCT = 0.65;

export const TIERS = {
  long: {
    label: "Long-horizon",
    engines: new Set(["multibagger", "magic", "magicmagic", "ai_longterm"]),
    maxPct: 0.05,
    capPct: 0.25,
  },
  swing: {
    label: "Swing/medium",
    // ohl (added 2026-08-17): same-day pattern signal on the F&O-eligible
    // universe, same classification as breakout — liquid names, pattern-
    // based, not a multi-month thesis or a high-frequency intraday churn.
    engines: new Set(["breakout", "4h", "ai_4h", "ai_daily", "equity_measured", "ohl"]),
    maxPct: 0.03,
    capPct: 0.30,
  },
  hf: {
    label: "High-frequency",
    engines: new Set(["cf_1h", "intraday", "commodity"]),
    maxPct: 0.015,
    capPct: 0.20,
  },
};

export function tierFor(signalType) {
  const t = String(signalType || "").toLowerCase();
  for (const [key, tier] of Object.entries(TIERS)) {
    if (tier.engines.has(t)) return key;
  }
  return null;
}

// C, ungraded, and anything unrecognized all get the same conservative
// multiplier — v1 rows predate the grade column entirely, and "we don't
// know the quality" should size like the worst known quality, not the best.
export function gradeMultiplier(grade) {
  const g = String(grade || "").toUpperCase();
  if (g === "A") return 1.0;
  if (g === "B") return 0.7;
  return 0.45;
}

// `rows` — plain objects with: id, date (YYYY-MM-DD...), symbol, signal_type,
// entry, status, lifecycle_status, exit_price, pnl_pct, closed_at, grade.
// `badgeOf(status, lifecycle)` classifies win/loss/open/expired/cancelled —
// injected rather than imported so this stays DB-free and directly testable.
// `currencyOf(symbol)` is injected for the same reason. It defaults to ₹ so
// existing callers and the test fixtures keep working unchanged.
export function simulateWallet(rows, badgeOf, currencyOf = () => "\u20b9") {
  const events = [];
  const untieredTypes = new Set();
  const trades = [];

  for (const r of rows) {
    const signalType = String(r.signal_type || "");
    const tierKey = tierFor(signalType);
    const badge = badgeOf(r.status, r.lifecycle_status);
    // VOID/CANCELLED never really triggered — no capital was ever at risk,
    // so no allocation, same as they're excluded from every win/loss figure.
    if (badge === "cancelled") continue;
    if (!tierKey) {
      untieredTypes.add(signalType || "(blank)");
      continue;
    }
    const trade = {
      id: Number(r.id),
      date: String(r.date || "").slice(0, 10),
      symbol: String(r.symbol || ""),
      signal_type: signalType,
      tier: tierKey,
      badge,
      grade: r.grade ? String(r.grade) : null,
      entry: r.entry === null || r.entry === undefined ? null : Number(r.entry),
      exit: r.exit_price === null || r.exit_price === undefined ? null : Number(r.exit_price),
      // The wallet is a ₹50L book, but the INSTRUMENT may not be rupee-priced
      // — a US equity or a COMEX contract is sized in rupees and QUOTED in
      // dollars. Carrying the unit per trade is what lets the table print the
      // allocation in ₹ and the entry price in $ without either being a lie.
      currency: currencyOf(r.symbol),
      pnl_pct: r.pnl_pct === null || r.pnl_pct === undefined ? null : Number(r.pnl_pct),
      closed_at: r.closed_at ? String(r.closed_at).slice(0, 10) : null,
      allocated_amount: 0,
      allocated_qty: null,
      realized_pnl: null,
      capital_unavailable: false,
    };
    trades.push(trade);
    events.push({ type: "open", date: trade.date, seq: trade.id, trade });
    if (badge !== "open") {
      // Resolved but no closed_at on record — free the capital on the same
      // day it opened rather than never, which would permanently lock it.
      const closeDate = trade.closed_at || trade.date;
      events.push({ type: "close", date: closeDate, seq: trade.id, trade });
    }
  }

  events.sort((a, b) => {
    const d = a.date < b.date ? -1 : a.date > b.date ? 1 : 0;
    if (d !== 0) return d;
    // Same day: open before close is the conservative ordering — it never
    // credits a trade with capital that a same-day close already freed.
    if (a.type !== b.type) return a.type === "open" ? -1 : 1;
    return a.seq - b.seq;
  });

  const deployedByTier = { long: 0, swing: 0, hf: 0 };
  let deployedTotal = 0;
  let realizedPnl = 0;
  let closedCount = 0;
  let wins = 0;
  let losses = 0;

  for (const ev of events) {
    const tier = TIERS[ev.trade.tier];
    if (ev.type === "open") {
      const desired = CAPITAL * tier.maxPct * gradeMultiplier(ev.trade.grade);
      const categoryHeadroom = CAPITAL * tier.capPct - deployedByTier[ev.trade.tier];
      const globalHeadroom = CAPITAL * GLOBAL_CAP_PCT - deployedTotal;
      const allocated = Math.max(0, Math.min(desired, categoryHeadroom, globalHeadroom));
      ev.trade.allocated_amount = Math.round(allocated);
      ev.trade.capital_unavailable = allocated <= 0;
      ev.trade.allocated_qty =
        ev.trade.entry !== null && ev.trade.entry > 0 ? Math.floor(allocated / ev.trade.entry) : null;
      deployedByTier[ev.trade.tier] += allocated;
      deployedTotal += allocated;
    } else {
      // Free the capital this trade held, then book its realized P&L — the
      // two are separate: freeing capital doesn't depend on whether the
      // trade made or lost money.
      deployedByTier[ev.trade.tier] -= ev.trade.allocated_amount;
      deployedTotal -= ev.trade.allocated_amount;
      const pnl = ev.trade.pnl_pct;
      if (pnl !== null && ev.trade.allocated_amount > 0) {
        const rp = Math.round(ev.trade.allocated_amount * (pnl / 100));
        ev.trade.realized_pnl = rp;
        realizedPnl += rp;
        closedCount += 1;
        if (ev.trade.badge === "win") wins += 1;
        else if (ev.trade.badge === "loss") losses += 1;
      }
    }
  }

  const categories = {};
  for (const [key, tier] of Object.entries(TIERS)) {
    const deployed = Math.round(deployedByTier[key]);
    categories[key] = {
      label: tier.label,
      engines: [...tier.engines],
      max_pct: tier.maxPct,
      cap_pct: tier.capPct,
      deployed_amount: deployed,
      deployed_pct: Number(((deployed / CAPITAL) * 100).toFixed(2)),
      cap_amount: Math.round(CAPITAL * tier.capPct),
    };
  }

  const deployedTotalRounded = Math.round(deployedTotal);
  const decided = wins + losses;

  return {
    capital: CAPITAL,
    start_date: START_DATE,
    global_cap_pct: GLOBAL_CAP_PCT,
    global_cap_amount: Math.round(CAPITAL * GLOBAL_CAP_PCT),
    wallet: {
      deployed_amount: deployedTotalRounded,
      deployed_pct: Number(((deployedTotalRounded / CAPITAL) * 100).toFixed(2)),
      cash_amount: CAPITAL - deployedTotalRounded,
      cash_pct: Number((100 - (deployedTotalRounded / CAPITAL) * 100).toFixed(2)),
      realized_pnl: realizedPnl,
      realized_pnl_pct: Number(((realizedPnl / CAPITAL) * 100).toFixed(2)),
      closed_trades: closedCount,
      wins,
      losses,
      win_rate: decided > 0 ? Number(((wins / decided) * 100).toFixed(1)) : null,
    },
    categories,
    trades: trades.slice().sort((a, b) => b.id - a.id).slice(0, 300),
    untiered_types: [...untieredTypes],
  };
}
