// Pure position/ladder logic for the tracker — no db(), no fetch, no I/O.
// Kept separate from tracker.js so the ladder math is unit-testable without
// a live Turso connection (see test/positions.test.js).

// Sell 20% of the ORIGINAL quantity at +30%, then 50% of whatever REMAINS at
// +50%. Order matters — computeLadderPlan evaluates them in this sequence so
// a price gap through both in one refresh applies milestone_30's sale before
// sizing milestone_50 off the resulting remainder.
export const LADDER_MILESTONES = [
  { reason: "milestone_30", triggerPct: 30, sellPctOfOriginal: 0.20 },
  { reason: "milestone_50", triggerPct: 50, sellPctOfRemainderAtTrigger: 0.50 },
];

// INTRADAY: no auto ladder (closed same day, milestone logic doesn't apply).
// INVESTMENT: configurable, off by default — not in this set.
export const LADDER_ELIGIBLE_TRADE_TYPES = new Set(["SWING", "LONG_TERM"]);

export function round(v, d = 2) {
  if (v === null || v === undefined || !Number.isFinite(v)) return null;
  const m = 10 ** d;
  return Math.round(v * m) / m;
}

// Side-aware profit. LONG profits when price rises, SHORT when it falls.
export function pnlPct(position) {
  const entry = position.entry_price;
  const cur = position.current_price;
  if (entry === null || entry === undefined || !entry) return null;
  if (cur === null || cur === undefined) return null;
  const raw = position.side === "SHORT" ? (entry - cur) / entry : (cur - entry) / entry;
  return round(raw * 100, 2);
}

// Risk unit is always |entry - stop|; numerator direction flips with side so
// a SHORT that moves favourably still reports a positive R.
export function rMultiple(position) {
  const entry = position.entry_price;
  const stop = position.stop_loss;
  const cur = position.current_price;
  if (entry === null || stop === null || cur === null || Math.abs(entry - stop) === 0) return null;
  const risk = Math.abs(entry - stop);
  const gain = position.side === "SHORT" ? entry - cur : cur - entry;
  return round(gain / risk, 2);
}

// Stop/target proximity, direction-correct per side. Mirrors the LONG-only
// logic that used to live in tracker.js's decorate(), now branching on side.
export function stopTargetAlert(position) {
  const entry = position.entry_price;
  const cur = position.current_price;
  const stop = position.stop_loss;
  const target = position.target_price;
  if (entry === null || cur === null) return null;
  const short = position.side === "SHORT";

  if (stop !== null && stop > 0) {
    const stopHit = short ? cur >= stop : cur <= stop;
    if (stopHit) return "stop-hit";
    const distanceTotal = short ? stop - entry : entry - stop;
    if (distanceTotal > 0) {
      const travelled = short ? (cur - entry) / distanceTotal : (entry - cur) / distanceTotal;
      if (travelled >= 0.7) return "near-stop";
    }
  }
  if (target !== null && target > 0) {
    const targetHit = short ? cur <= target : cur >= target;
    if (targetHit) return "target-hit";
    const distanceTotal = short ? entry - target : target - entry;
    if (distanceTotal > 0) {
      const travelled = short ? (entry - cur) / distanceTotal : (cur - entry) / distanceTotal;
      if (travelled >= 0.7) return "near-target";
    }
  }
  return null;
}

// Replaces tracker.js's old long-only decorate(). Returns a NEW object
// (does not mutate) with display/derived fields merged in.
export function decoratePosition(position) {
  const cur = position.current_price ?? position.entry_price;
  return {
    ...position,
    current_price: cur,
    pnl_pct: pnlPct({ ...position, current_price: cur }),
    winning: (pnlPct({ ...position, current_price: cur }) ?? 0) >= 0,
    r_multiple: rMultiple({ ...position, current_price: cur }),
    alert: stopTargetAlert({ ...position, current_price: cur }),
  };
}

// LONG: stop < entry < target.  SHORT: target < entry < stop.
// Server-side validation — the frontend must not be the only guard.
export function validateStopTarget({ side, entry, stop, target }) {
  if (entry === null || entry === undefined) return { valid: false, error: "entry_price required" };
  if (stop !== null && stop !== undefined) {
    if (side === "SHORT" ? stop <= entry : stop >= entry) {
      return { valid: false, error: side === "SHORT" ? "stop must be above entry for SHORT" : "stop must be below entry for LONG" };
    }
  }
  if (target !== null && target !== undefined) {
    if (side === "SHORT" ? target >= entry : target <= entry) {
      return { valid: false, error: side === "SHORT" ? "target must be below entry for SHORT" : "target must be above entry for LONG" };
    }
  }
  return { valid: true, error: null };
}

// Pure — computes which (if any) ladder milestones are newly triggered at
// currentPrice, given which have already fired. Never mutates its inputs,
// never touches a database. The caller (tracker.js) is responsible for
// persisting the returned plan idempotently.
//
// Handles: qty=1 (rounds to a minimum of 1, never 0, never sells more than
// what remains), a price gap crossing both milestones in one call (both are
// evaluated against the SAME currentPrice as execution_price — the trigger
// price recorded per milestone is the theoretical threshold level, not a
// fabricated fill), and closed/ineligible/already-fired positions (returns []).
export function computeLadderPlan(position, currentPrice, firedMilestones = new Set()) {
  if (position.status !== "active") return [];
  if (!LADDER_ELIGIBLE_TRADE_TYPES.has(position.trade_type)) return [];
  const original = position.original_quantity;
  let remaining = position.remaining_quantity;
  const entry = position.entry_price;
  if (!original || !remaining || remaining <= 0 || !entry || currentPrice === null) return [];

  const short = position.side === "SHORT";
  const profitPct = short ? ((entry - currentPrice) / entry) * 100 : ((currentPrice - entry) / entry) * 100;

  const plan = [];
  for (const m of LADDER_MILESTONES) {
    if (firedMilestones.has(m.reason)) continue;
    if (profitPct < m.triggerPct) break; // milestones are in ascending trigger order
    if (remaining <= 0) break;

    const rawQty = m.sellPctOfOriginal !== undefined
      ? original * m.sellPctOfOriginal
      : remaining * m.sellPctOfRemainderAtTrigger;
    const qty = Math.min(Math.max(Math.round(rawQty), 1), remaining);

    const triggerPrice = short ? entry * (1 - m.triggerPct / 100) : entry * (1 + m.triggerPct / 100);
    const realizedDelta = short ? (entry - currentPrice) * qty : (currentPrice - entry) * qty;
    const remainingAfter = round(remaining - qty, 6);

    plan.push({
      reason: m.reason,
      trigger_price: round(triggerPrice, 2),
      execution_price: round(currentPrice, 2),
      quantity: qty,
      quantity_pct_remaining_after: round((remainingAfter / original) * 100, 2),
      realized_pnl_delta: round(realizedDelta, 2),
      stop_before: position.stop_loss,
      // Only the first milestone moves the stop, and only to breakeven —
      // the trailing-stop engine (structure/ATR-based) is explicitly deferred.
      stop_after: m.reason === "milestone_30" ? entry : position.stop_loss,
    });

    remaining = remainingAfter;
  }
  return plan;
}

// Decision-support only — never calls anything, never executes a trade.
// If the ladder is eligible, milestone recommendations reflect what
// computeLadderPlan would still do (useful pre-refresh); once a milestone
// has actually fired (present in firedMilestones) it's suppressed here too.
export function nextAction(position, { firedMilestones = new Set() } = {}) {
  if (position.status !== "active") {
    return { action: "NO_ACTION", reason: "Position closed", trigger: null };
  }
  if (position.entry_price === null || position.current_price === null) {
    return { action: "WAIT_FOR_DATA", reason: "Missing market price", trigger: null };
  }
  const alert = stopTargetAlert(position);
  if (alert === "stop-hit") return { action: "EXIT", reason: "Stop-loss hit", trigger: position.stop_loss };
  if (alert === "target-hit") return { action: "EXIT", reason: "Target reached", trigger: position.target_price };

  // Active but no quantity on record (e.g. a row inserted by an older/other
  // client that never set original/remaining_quantity) — real, not closed,
  // just outside what the ladder can size. Say so rather than guessing.
  if (!position.remaining_quantity || position.remaining_quantity <= 0) {
    return { action: "HOLD", reason: "No quantity on record — the profit ladder needs one to run", trigger: null };
  }

  const profitPct = pnlPct(position);
  if (profitPct !== null) {
    for (const m of LADDER_MILESTONES) {
      if (firedMilestones.has(m.reason)) continue;
      if (profitPct >= m.triggerPct) {
        return {
          action: m.reason === "milestone_30" ? "SELL_20" : "SELL_PARTIAL",
          reason: `+${m.triggerPct}% milestone reached`,
          trigger: m.triggerPct,
        };
      }
    }
  }
  return { action: "HOLD", reason: LADDER_ELIGIBLE_TRADE_TYPES.has(position.trade_type) ? "No milestone reached" : "Ladder not enabled for this trade type", trigger: null };
}

// ACCUMULATION → PROTECTED (milestone_30 fired) → COMPOUNDING (milestone_50
// fired) → THREATENED overrides both when price is at/near the stop.
export function deriveBattleStatus(position, { firedMilestones = new Set() } = {}) {
  if (position.status !== "active") return "CLOSED";
  const alert = stopTargetAlert(position);
  if (alert === "stop-hit" || alert === "near-stop") return "THREATENED";
  if (firedMilestones.has("milestone_50")) return "COMPOUNDING";
  if (firedMilestones.has("milestone_30")) return "PROTECTED";
  return "ACCUMULATION";
}

// Risk-based sizing calculator. Decision support only — does not place or
// record a trade.
export function sizePosition({ capital, riskPct, entry, stop, side }) {
  if (!capital || capital <= 0) return { error: "capital must be positive" };
  if (!riskPct || riskPct <= 0) return { error: "riskPct must be positive" };
  if (entry === null || stop === null || entry === undefined || stop === undefined) {
    return { error: "entry and stop are required" };
  }
  const riskPerShare = Math.abs(entry - stop);
  if (riskPerShare <= 0) return { error: "entry and stop cannot be equal" };

  const riskBudget = round(capital * (riskPct / 100), 2);
  const quantity = Math.floor(riskBudget / riskPerShare);
  const maxLoss = round(quantity * riskPerShare, 2);

  return {
    capital, riskPct, entry, stop, side: side || "LONG",
    risk_per_share: round(riskPerShare, 2),
    risk_budget: riskBudget,
    suggested_quantity: quantity,
    max_loss: maxLoss,
  };
}
