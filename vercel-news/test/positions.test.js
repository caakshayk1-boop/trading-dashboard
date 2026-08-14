import { test } from "node:test";
import assert from "node:assert/strict";
import {
  computeLadderPlan,
  pnlPct,
  rMultiple,
  sizePosition,
  validateStopTarget,
  nextAction,
  classifyFreshness,
  computeATR,
  computeEMA,
  computeSwingLow,
  computeSwingHigh,
  computeTrailingStop,
  aggregatePortfolioRisk,
  detectSplitRatio,
  adjustForSplit,
  round,
} from "../api/_positions.js";

function flatBars(n, { high, low, close }) {
  return Array.from({ length: n }, () => ({ high, low, close }));
}

function pos(overrides = {}) {
  return {
    status: "active",
    side: "LONG",
    trade_type: "SWING",
    entry_price: 100,
    stop_loss: 90,
    target_price: null,
    current_price: 100,
    original_quantity: 100,
    remaining_quantity: 100,
    ...overrides,
  };
}

test("100@100 -> 130: sells 20 (milestone_30), 80 remain", () => {
  const plan = computeLadderPlan(pos(), 130, new Set());
  assert.equal(plan.length, 1);
  assert.equal(plan[0].reason, "milestone_30");
  assert.equal(plan[0].quantity, 20);
  assert.equal(plan[0].quantity_pct_remaining_after, 80);
  assert.equal(plan[0].execution_price, 130);
  assert.equal(plan[0].trigger_price, 130);
});

test("continuing 130 -> 150 with milestone_30 already fired: sells 40 of the 80 remaining, 40 remain", () => {
  const p = pos({ remaining_quantity: 80 });
  const plan = computeLadderPlan(p, 150, new Set(["milestone_30"]));
  assert.equal(plan.length, 1);
  assert.equal(plan[0].reason, "milestone_50");
  assert.equal(plan[0].quantity, 40);
  assert.equal(plan[0].quantity_pct_remaining_after, 40);
});

test("gap 125 -> 160 in one call: both milestones fire at the SAME execution price, trigger price is the theoretical level not the fill", () => {
  const plan = computeLadderPlan(pos(), 160, new Set());
  assert.equal(plan.length, 2);
  assert.equal(plan[0].reason, "milestone_30");
  assert.equal(plan[0].trigger_price, 130);
  assert.equal(plan[0].execution_price, 160); // never fabricated at 130
  assert.equal(plan[1].reason, "milestone_50");
  assert.equal(plan[1].trigger_price, 150);
  assert.equal(plan[1].execution_price, 160); // never fabricated at 150
  // 20 sold at milestone_30 (20% of 100), then 50% of the resulting 80 = 40
  assert.equal(plan[0].quantity, 20);
  assert.equal(plan[1].quantity, 40);
  assert.equal(plan[1].quantity_pct_remaining_after, 40);
});

test("73 shares, deterministic rounding, no negative/over-sell", () => {
  const p = pos({ original_quantity: 73, remaining_quantity: 73 });
  const first = computeLadderPlan(p, 130, new Set());
  assert.equal(first.length, 1);
  assert.equal(first[0].quantity, 15); // round(73*0.2=14.6) = 15
  const remainingAfterFirst = 73 - 15;
  assert.equal(remainingAfterFirst, 58);

  const p2 = pos({ original_quantity: 73, remaining_quantity: remainingAfterFirst });
  const second = computeLadderPlan(p2, 150, new Set(["milestone_30"]));
  assert.equal(second.length, 1);
  assert.equal(second[0].quantity, 29); // round(58*0.5) = 29
  assert.ok(15 + 29 <= 73, "never exceeds original quantity");
  assert.ok(remainingAfterFirst - 29 >= 0, "never goes negative");
});

test("quantity = 1 never rounds down to 0", () => {
  const p = pos({ original_quantity: 1, remaining_quantity: 1 });
  const plan = computeLadderPlan(p, 130, new Set());
  assert.equal(plan.length, 1);
  assert.equal(plan[0].quantity, 1);
  assert.equal(plan[0].quantity_pct_remaining_after, 0);
});

test("duplicate milestone call: already-fired milestone is not returned again", () => {
  const plan = computeLadderPlan(pos(), 130, new Set(["milestone_30"]));
  assert.equal(plan.length, 0);
});

test("position already closed: no plan regardless of price", () => {
  const p = pos({ status: "exited" });
  const plan = computeLadderPlan(p, 200, new Set());
  assert.equal(plan.length, 0);
});

test("INTRADAY trade_type: ladder never fires even at +50%", () => {
  const p = pos({ trade_type: "INTRADAY" });
  const plan = computeLadderPlan(p, 160, new Set());
  assert.equal(plan.length, 0);
});

test("LONG entry100/stop90/current130: positive profit and R", () => {
  const p = pos({ current_price: 130 });
  assert.equal(pnlPct(p), 30);
  assert.ok(rMultiple(p) > 0);
});

test("SHORT entry100/stop110/current70: positive profit (not inverted)", () => {
  const p = pos({ side: "SHORT", stop_loss: 110, current_price: 70 });
  assert.equal(pnlPct(p), 30);
  assert.ok(rMultiple(p) > 0);
});

test("sizePosition: basic risk-based sizing", () => {
  const r = sizePosition({ capital: 500000, riskPct: 1, entry: 100, stop: 90 });
  assert.equal(r.risk_per_share, 10);
  assert.equal(r.risk_budget, 5000);
  assert.equal(r.suggested_quantity, 500);
  assert.equal(r.max_loss, 5000);
});

test("validateStopTarget rejects a SHORT with stop below entry", () => {
  const r = validateStopTarget({ side: "SHORT", entry: 100, stop: 90, target: 80 });
  assert.equal(r.valid, false);
});

test("validateStopTarget accepts a correctly-configured LONG", () => {
  const r = validateStopTarget({ side: "LONG", entry: 100, stop: 90, target: 130 });
  assert.equal(r.valid, true);
});

test("nextAction: active position with no quantity on record is NOT mislabeled as closed", () => {
  const p = pos({ remaining_quantity: null, original_quantity: null, current_price: 105 });
  const result = nextAction(p, { firedMilestones: new Set() });
  assert.notEqual(result.action, "NO_ACTION");
  assert.notEqual(result.reason, "Position closed");
});

test("nextAction: a truly closed position is NO_ACTION regardless of quantity", () => {
  const p = pos({ status: "exited" });
  const result = nextAction(p, { firedMilestones: new Set() });
  assert.equal(result.action, "NO_ACTION");
});

test("classifyFreshness: LIVE within 90s, DELAYED to 5min, STALE beyond, OFFLINE when unknown", () => {
  assert.equal(classifyFreshness(0), "LIVE");
  assert.equal(classifyFreshness(90), "LIVE");
  assert.equal(classifyFreshness(91), "DELAYED");
  assert.equal(classifyFreshness(300), "DELAYED");
  assert.equal(classifyFreshness(301), "STALE");
  assert.equal(classifyFreshness(null), "OFFLINE");
  assert.equal(classifyFreshness(undefined), "OFFLINE");
  assert.equal(classifyFreshness(Infinity), "OFFLINE");
});

test("computeATR: constant high-low range gives an exact, stable ATR", () => {
  // TR = max(H-L, |H-prevClose|, |L-prevClose|) = max(2,1,1) = 2 for every
  // bar after the first, regardless of period smoothing — a flat series
  // never has a bar taller than its own H-L, so ATR settles at exactly 2.
  const bars = flatBars(30, { high: 102, low: 100, close: 101 });
  assert.equal(computeATR(bars, 14), 2);
});

test("computeATR: too few bars returns null rather than a bad number", () => {
  const bars = flatBars(10, { high: 102, low: 100, close: 101 });
  assert.equal(computeATR(bars, 14), null);
});

test("computeEMA: constant series converges to the constant", () => {
  const closes = flatBars(30, { high: 0, low: 0, close: 100 }).map((b) => b.close);
  assert.equal(computeEMA(closes, 20), 100);
});

test("computeSwingLow/High: min/max over the lookback window only", () => {
  const bars = [
    ...flatBars(40, { high: 110, low: 90, close: 100 }),   // outside a 10-bar lookback
    ...flatBars(10, { high: 105, low: 95, close: 100 }),   // inside it
  ];
  assert.equal(computeSwingLow(bars, 10), 95);   // not 90 — that bar is outside the window
  assert.equal(computeSwingHigh(bars, 10), 105); // not 110
});

test("computeTrailingStop: unavailable with too little history — never invents a value", () => {
  const bars = flatBars(10, { high: 102, low: 100, close: 101 });
  const r = computeTrailingStop(pos(), bars);
  assert.equal(r.available, false);
  assert.equal(r.stop, null);
});

test("computeTrailingStop LONG: moves the stop UP when the computed level beats the previous stop", () => {
  const bars = flatBars(60, { high: 102, low: 99, close: 100 });
  // candidate = max(swing_low=99, ema20(100) - 1.5*atr(2)) = max(99, 97) = 99
  const r = computeTrailingStop(pos({ stop_loss: 50 }), bars);
  assert.equal(r.available, true);
  assert.equal(r.stop, 99);
});

test("computeTrailingStop LONG: never lowers the stop below what it already was", () => {
  const bars = flatBars(60, { high: 102, low: 99, close: 100 });
  // Same candidate (99) as above, but previous_stop (105) is already higher.
  const r = computeTrailingStop(pos({ stop_loss: 105 }), bars);
  assert.equal(r.stop, 105);
});

test("computeTrailingStop SHORT: never raises the stop above what it already was", () => {
  const bars = flatBars(60, { high: 101, low: 98, close: 100 });
  // candidate = min(swing_high=101, ema20(100) + 1.5*atr(2)) = min(101, 103) = 101
  const r = computeTrailingStop(pos({ side: "SHORT", stop_loss: 95 }), bars);
  assert.equal(r.available, true);
  assert.equal(r.stop, 95); // 95 already tighter (lower) than the 101 candidate
});

test("computeTrailingStop SHORT: moves the stop DOWN when the computed level is tighter", () => {
  const bars = flatBars(60, { high: 101, low: 98, close: 100 });
  const r = computeTrailingStop(pos({ side: "SHORT", stop_loss: 150 }), bars);
  assert.equal(r.stop, 101);
});

test("aggregatePortfolioRisk: a null stop_loss is EXCLUDED, not coerced to 0-risk", () => {
  // If null coerced to 0 in arithmetic (JS does this), this position would
  // silently contribute nothing to open risk instead of being flagged as
  // unmeasurable — the exact bug a design review caught before this shipped.
  const noStop = pos({ stop_loss: null, current_price: 130, remaining_quantity: 100 });
  const r = aggregatePortfolioRisk([noStop], 500000);
  assert.equal(r.open_risk_amount, 0);
  assert.equal(r.risk_excluded_count, 1);
  assert.equal(r.total_positions, 1);
});

test("aggregatePortfolioRisk: null current_price and null/zero remaining_quantity are also excluded", () => {
  const noPrice = pos({ current_price: null });
  const noQty = pos({ remaining_quantity: null });
  const zeroQty = pos({ remaining_quantity: 0 });
  const r = aggregatePortfolioRisk([noPrice, noQty, zeroQty], 500000);
  assert.equal(r.open_risk_amount, 0);
  assert.equal(r.risk_excluded_count, 3);
});

test("aggregatePortfolioRisk: sums current-price-to-stop risk across measurable positions", () => {
  const a = pos({ entry_price: 100, stop_loss: 90, current_price: 110, remaining_quantity: 100 }); // risk 20*100=2000
  const b = pos({ entry_price: 50, stop_loss: 45, current_price: 55, remaining_quantity: 200 });   // risk 10*200=2000
  const r = aggregatePortfolioRisk([a, b], 500000);
  assert.equal(r.open_risk_amount, 4000);
  assert.equal(r.risk_excluded_count, 0);
  assert.equal(r.open_risk_pct, round(4000 / 500000 * 100, 2));
});

test("aggregatePortfolioRisk: risk bands are GREEN <10%, AMBER 10-15%, RED >15%", () => {
  const mk = (riskAmt) => pos({ entry_price: 100, stop_loss: 100 - riskAmt, current_price: 100, remaining_quantity: 1 });
  assert.equal(aggregatePortfolioRisk([mk(9)], 100).risk_level, "GREEN");
  assert.equal(aggregatePortfolioRisk([mk(10)], 100).risk_level, "AMBER");
  assert.equal(aggregatePortfolioRisk([mk(15)], 100).risk_level, "AMBER");
  assert.equal(aggregatePortfolioRisk([mk(16)], 100).risk_level, "RED");
});

test("aggregatePortfolioRisk: no capital set means no fake percentage, not a guess", () => {
  const a = pos({ current_price: 110, remaining_quantity: 100 });
  const r = aggregatePortfolioRisk([a], null);
  assert.equal(r.open_risk_pct, null);
  assert.equal(r.risk_level, null);
  assert.equal(r.capital, null);
  assert.ok(r.open_risk_amount > 0); // absolute amount still reported
});

test("aggregatePortfolioRisk: largest_position_risk_pct guards divide-by-zero when nothing is measurable", () => {
  const r = aggregatePortfolioRisk([pos({ stop_loss: null })], 500000);
  assert.equal(r.largest_position_risk_pct, null);
});

test("aggregatePortfolioRisk: closed positions don't count toward totals or risk", () => {
  const closed = pos({ status: "exited", current_price: 200, remaining_quantity: 100, stop_loss: 100 });
  const r = aggregatePortfolioRisk([closed], 500000);
  assert.equal(r.total_positions, 0);
  assert.equal(r.open_risk_amount, 0);
});

test("aggregatePortfolioRisk: protected_capital sums realized_pnl only for PROTECTED/COMPOUNDING", () => {
  const protectedPos = pos({ battle_status: "PROTECTED", realized_pnl: 500 });
  const compounding = pos({ battle_status: "COMPOUNDING", realized_pnl: 300 });
  const accumulating = pos({ battle_status: "ACCUMULATION", realized_pnl: 999 }); // not counted
  const r = aggregatePortfolioRisk([protectedPos, compounding, accumulating], 500000);
  assert.equal(r.protected_capital, 800);
  assert.equal(r.protected_positions, 1);
  assert.equal(r.compounding_positions, 1);
});

test("detectSplitRatio: a clean 2:1 forward split (price halves) is detected as ratio=2", () => {
  assert.equal(detectSplitRatio(200, 100), 2);
});

test("detectSplitRatio: a 1:2 reverse split (price doubles) is detected as ratio=0.5", () => {
  assert.equal(detectSplitRatio(100, 200), 0.5);
});

test("detectSplitRatio: within tolerance of a factor still matches (real quotes aren't exact)", () => {
  assert.equal(detectSplitRatio(200, 102), 2); // 200/102 = 1.96, within 3% of 2
});

test("detectSplitRatio: an ordinary large trading move is NOT flagged as a split", () => {
  assert.equal(detectSplitRatio(100, 80), null); // -20%, below the 35% magnitude floor
});

test("detectSplitRatio: a big move that doesn't land near any common factor is NOT flagged", () => {
  assert.equal(detectSplitRatio(100, 57), null); // -43%, but 100/57=1.75, not near 2, 1.5, or 3
});

test("detectSplitRatio: null/zero/missing prices never fabricate a ratio", () => {
  assert.equal(detectSplitRatio(null, 100), null);
  assert.equal(detectSplitRatio(100, null), null);
  assert.equal(detectSplitRatio(0, 100), null);
});

test("adjustForSplit: preserves economic equivalence (qty*price unchanged) for a 2:1 split", () => {
  const p = pos({ entry_price: 200, stop_loss: 180, target_price: 260, original_quantity: 50, remaining_quantity: 50 });
  const adj = adjustForSplit(p, 2);
  assert.equal(adj.entry_price, 100);
  assert.equal(adj.stop_loss, 90);
  assert.equal(adj.target_price, 130);
  assert.equal(adj.original_quantity, 100);
  assert.equal(adj.remaining_quantity, 100);
  assert.equal(adj.entry_price * adj.original_quantity, p.entry_price * p.original_quantity);
});

test("adjustForSplit: a null stop/target stays null, not coerced to 0", () => {
  const p = pos({ stop_loss: null, target_price: null, original_quantity: 10, remaining_quantity: 10 });
  const adj = adjustForSplit(p, 3);
  assert.equal(adj.stop_loss, null);
  assert.equal(adj.target_price, null);
});

// Documented gap: everything above proves the pure ladder/P&L math. It does
// NOT prove the DB-level idempotency guarantee (the UNIQUE INDEX +
// INSERT OR IGNORE race protection in tracker.js) — there is no local Turso
// instance to integration-test against in this environment. That layer is
// verified by a manual smoke test against the live (empty) database instead,
// not claimed as covered here.
