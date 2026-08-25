// Pure simulation logic for the paper wallet — no DB, no HTTP, so it can be
// unit-tested directly (paper_wallet.test.js) the same way _positions.js's
// pure functions are tested by positions.test.js. paper_wallet.js is the
// thin HTTP handler that fetches rows and hands them to simulateWallet().

// Rs 1,00,00,000 — the SAME wallet as swing_rulebook.py and Dhruvedge.
//
// This was Rs 50,00,000 while two other books ran at a crore, which is how the
// site showed two capital figures at once. Three wallets against one operator
// is not three strategies; it is three answers to "how much is at risk" and
// none of them was the truth.
export const CAPITAL = 10_000_000;
// Forward-only — confirmed with Akshay 2026-08-16. No historical replay: a
// signal from before this date never enters the simulation at all.
export const START_DATE = "2026-08-17";
export const GLOBAL_CAP_PCT = 0.65;

export const TIERS = {
  long: {
    label: "Long-horizon",
    // magicmagic retired as magic's duplicate — 10 of its 43 rows are the same
    // trade filed twice, and sizing both charged two budgets for one position.
    engines: new Set(["multibagger", "magic", "ai_longterm"]),
    maxPct: 0.05,
    capPct: 0.35,
  },
  swing: {
    label: "Swing/medium",
    // ohl and equity_measured removed on the 2026-08-25 engine review: ohl is
    // 0 for 6 with every close at the stop, equity_measured 0 for 8 at
    // t = -20.17. They keep firing and keep being scored — hiding a losing
    // engine is the one thing this record exists not to do — but they no
    // longer receive capital, on paper or otherwise.
    engines: new Set(["breakout", "ai_daily"]),
    maxPct: 0.03,
    capPct: 0.45,
  },
};

// The High-frequency tier is GONE, not emptied.
//
// It held cf_1h, intraday and commodity and reserved 20% of the book for them.
// All three are out of mandate — cf_1h and commodity are FX and commodities
// this account cannot hold, intraday trades a 15-minute chart — so the tier
// could never fill. The visible symptom was "High-frequency: nil" on every
// build and Rs 10,00,000 of the wallet sitting idle forever, reserved for
// trades that were structurally unable to happen.
//
// A cap that can never be used is not risk control, it is a rounding error
// with a label. The capital it was holding is redistributed to the two tiers
// that actually trade.
//
// The tier caps deliberately SUM ABOVE the global cap: 35% + 45% = 80% against
// a 65% ceiling. That is not an error. Redistributing to exactly 65% was the
// first attempt and it quietly removed a safety property — with the caps
// summing to the global cap, the global cap can never bind before a category
// cap does, so it stops being a constraint and becomes decoration. Tiers cap
// concentration WITHIN a bucket; the global cap is the real ceiling on the
// book. They have to be able to disagree for both to mean anything.

// Engines the ledger has MEASURED as losing. They keep firing, keep being
// logged and keep being scored — hiding a losing engine is the one thing this
// record exists not to do — but they stop receiving capital.
//
// commodity: 20 closed, 25.0% win, -0.511R expectancy, t = -2.48. That clears
// the significance bar in the wrong direction, which makes it a measured loss
// rather than a run of bad luck.
//
// Note what suppressing it does to the published headline: +0.349R to +0.398R,
// t 1.80 to 1.96. Almost nothing. That is the point — this is not about
// flattering the number, it is about not funding a loss. If it were about the
// number it would not be worth doing.
//
// Kept in sync BY HAND with engine_evidence.py, which computes the verdict.
// A serverless function cannot import a Python module, and a second automatic
// path that silently disagreed would be worse than a line that has to be
// changed deliberately.
export const SUPPRESSED_ENGINES = new Set(["commodity"]);

// The file is deliberately import-free (see the header on _levels.js), so it
// carries its own coercion rather than reaching into _db.js for num().
function num(v) {
  if (v === null || v === undefined) return null;
  const x = typeof v === "number" ? v : Number(v);
  return Number.isFinite(x) ? x : null;
}

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

// ── Direction ───────────────────────────────────────────────────────────────
// The ledger stores BUY/SELL in `action`. The wallet never read it, so a short
// and a long rendered identically — and this book carries real shorts: 131 SELL
// signals across Gold, Crude, Natural Gas and Silver. A reader looking at a
// commodity row could not tell whether the position was bought or sold, which
// makes the stop and the targets unreadable too (a short's stop is ABOVE its
// entry). Ordering convention matches tracker.py: SHORT is t2 < t1 < entry < sl.
export function sideOf(action) {
  return String(action || "").toUpperCase() === "SELL" ? "SHORT" : "LONG";
}

// Percentage move in the trade's FAVOUR. A short that falls has made money.
export function directionalPnlPct(side, entry, exit) {
  if (entry === null || exit === null || !(entry > 0)) return null;
  const raw = ((exit - entry) / entry) * 100;
  return side === "SHORT" ? -raw : raw;
}

// ── Partial booking ─────────────────────────────────────────────────────────
// Half off at T1, the rest runs to T2. The ledger records the FULL-position
// outcome, so a T2_HIT was previously banked as if the entire position had run
// to the far target — which overstates a real ladder every time, because half
// of it came off lower.
//
// T2 cannot be reached without passing through T1 (T1 sits between entry and
// T2 by construction, both directions), so a T2_HIT implies both legs.
// TARGET_HIT does not say WHICH target, so it books at T1 — the conservative
// reading, and the only one that cannot overstate.
export const T1_FRACTION = 0.5;

export function bookingLegs(status, side, entry, sl, t1, t2) {
  const st = String(status || "").toUpperCase();
  const at = (px, frac) =>
    px === null || px === undefined || !isFinite(px) ? null : { px: Number(px), frac };

  if (st === "SL_HIT" || st === "STOPPED" || st === "STOP_HIT") {
    return [at(sl, 1)].filter(Boolean);
  }
  if (st === "T2_HIT" || st === "TP2_HIT") {
    const a = at(t1, T1_FRACTION), b = at(t2, 1 - T1_FRACTION);
    // A collapsed target pair leaves only one real level — distinctTargets()
    // nulls the duplicate rather than inventing a second exit. One leg then.
    if (a && b) return [a, b];
    return [at(t2, 1) || at(t1, 1)].filter(Boolean);
  }
  if (st === "T1_HIT" || st === "TP1_HIT" || st === "TARGET_HIT" || st === "PROFIT") {
    return [at(t1, 1)].filter(Boolean);
  }
  return [];
}

// Weighted P&L across the ladder, in the trade's own direction. Returns null
// when the ladder cannot be built — the caller then falls back to the ledger's
// own pnl_pct rather than inventing a number.
export function ladderedPnlPct(status, side, entry, sl, t1, t2) {
  const legs = bookingLegs(status, side, entry, sl, t1, t2);
  if (!legs.length || entry === null || !(entry > 0)) return null;
  let acc = 0;
  for (const leg of legs) {
    const p = directionalPnlPct(side, entry, leg.px);
    if (p === null) return null;
    acc += p * leg.frac;
  }
  return Number(acc.toFixed(4));
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
      // Direction, and the levels that only mean anything once you know it.
      // A short's stop sits ABOVE its entry; printing 2,412 as "stop" beside a
      // 2,380 entry reads as a broken row until the side is on the card.
      side: sideOf(r.action),
      action: r.action ? String(r.action).toUpperCase() : null,
      sl: num(r.sl),
      target1: num(r.target1),
      target2: num(r.target2),
      pnl_pct: r.pnl_pct === null || r.pnl_pct === undefined ? null : Number(r.pnl_pct),
      closed_at: r.closed_at ? String(r.closed_at).slice(0, 10) : null,
      status_raw: String(r.status || ""),
      allocated_amount: 0,
      allocated_qty: null,
      realized_pnl: null,
      suppressed: false,
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
      // A suppressed engine is sized at zero, and says why. Not dropped from
      // the table: the reader should see that the signal fired and that no
      // capital followed it, which is a different and more useful fact than
      // the row simply not being there.
      const suppressed = SUPPRESSED_ENGINES.has(
        String(ev.trade.signal_type || "").toLowerCase());
      const desired = suppressed
        ? 0
        : CAPITAL * tier.maxPct * gradeMultiplier(ev.trade.grade);
      const categoryHeadroom = CAPITAL * tier.capPct - deployedByTier[ev.trade.tier];
      const globalHeadroom = CAPITAL * GLOBAL_CAP_PCT - deployedTotal;
      const allocated = Math.max(0, Math.min(desired, categoryHeadroom, globalHeadroom));
      ev.trade.allocated_amount = Math.round(allocated);
      ev.trade.capital_unavailable = allocated <= 0 && !suppressed;
      ev.trade.suppressed = suppressed;
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
      // Half off at T1, the rest to T2. The ledger's pnl_pct is the
      // FULL-position outcome and banks a T2_HIT as though none of it came
      // off lower, which overstates every laddered win. Fall back to the
      // ledger figure when the ladder cannot be built — never invent one.
      const ladder = ladderedPnlPct(
        ev.trade.status_raw, ev.trade.side, ev.trade.entry,
        ev.trade.sl, ev.trade.target1, ev.trade.target2);
      ev.trade.pnl_basis = ladder === null ? "ledger" : "partial_booking";
      ev.trade.ledger_pnl_pct = ev.trade.pnl_pct;
      const pnl = ladder === null ? ev.trade.pnl_pct : ladder;
      ev.trade.booked_pnl_pct = pnl;
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
