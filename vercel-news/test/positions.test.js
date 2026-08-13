import { test } from "node:test";
import assert from "node:assert/strict";
import {
  computeLadderPlan,
  pnlPct,
  rMultiple,
  sizePosition,
  validateStopTarget,
  nextAction,
} from "../api/_positions.js";

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

// Documented gap: everything above proves the pure ladder/P&L math. It does
// NOT prove the DB-level idempotency guarantee (the UNIQUE INDEX +
// INSERT OR IGNORE race protection in tracker.js) — there is no local Turso
// instance to integration-test against in this environment. That layer is
// verified by a manual smoke test against the live (empty) database instead,
// not claimed as covered here.
