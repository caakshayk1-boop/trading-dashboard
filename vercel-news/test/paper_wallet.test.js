import { test } from "node:test";
import assert from "node:assert/strict";
import { simulateWallet, tierFor, gradeMultiplier, CAPITAL, TIERS, GLOBAL_CAP_PCT,
         sideOf, directionalPnlPct, ladderedPnlPct } from "../api/_paper_wallet.js";
// From _badge.js specifically, not _db.js — this test runs via plain
// `node --test` with no npm install in newspaper.yml's "Position tracker
// tests" CI step, and _db.js's top-level @libsql/client import would fail
// there with ERR_MODULE_NOT_FOUND. See _badge.js's own comment.
import { badgeOf } from "../api/_badge.js";

function row(overrides) {
  return {
    id: 1,
    date: "2026-08-17",
    symbol: "TEST",
    signal_type: "multibagger",
    entry: 100,
    status: "OPEN",
    lifecycle_status: null,
    exit_price: null,
    pnl_pct: null,
    closed_at: null,
    grade: null,
    ...overrides,
  };
}

test("tierFor maps the real signal_type taxonomy correctly", () => {
  assert.equal(tierFor("multibagger"), "long");
  assert.equal(tierFor("magic"), "long");
  // magicmagic is retired as magic's duplicate, so it is no longer in any tier.
  // magic — the survivor — still is.
  assert.equal(tierFor("magicmagic"), null);
  assert.equal(tierFor("magic"), "long");
  assert.equal(tierFor("ai_longterm"), "long");
  assert.equal(tierFor("breakout"), "swing");
  assert.equal(tierFor("ai_daily"), "swing");

  // Retired on the 2026-08-25 review — measured losers or structurally broken.
  // They keep firing and keep being scored; they no longer receive capital.
  assert.equal(tierFor("equity_measured"), null);   // 0 for 8 at t = -20.17
  assert.equal(tierFor("ohl"), null);               // 0 for 6, every close at the stop
  assert.equal(tierFor("top5_pick"), null);         // 0 for 4, 8 of 25 not Indian listings

  // Out of mandate on instrument or clock, whatever the record says. cf_1h has
  // the best t-statistic of any engine and is still out: none of its 367
  // signals is an Indian listed equity.
  assert.equal(tierFor("cf_1h"), null);
  assert.equal(tierFor("commodity"), null);
  assert.equal(tierFor("intraday"), null);          // 15-minute chart
  assert.equal(tierFor("4h"), null);
  assert.equal(tierFor("ai_4h"), null);
  assert.equal(tierFor("something_unknown"), null);
});

test("gradeMultiplier: A=1.0, B=0.7, C/null/unknown=0.45 (most conservative default)", () => {
  assert.equal(gradeMultiplier("A"), 1.0);
  assert.equal(gradeMultiplier("a"), 1.0);
  assert.equal(gradeMultiplier("B"), 0.7);
  assert.equal(gradeMultiplier("C"), 0.45);
  assert.equal(gradeMultiplier(null), 0.45);
  assert.equal(gradeMultiplier(undefined), 0.45);
  assert.equal(gradeMultiplier("weird"), 0.45);
});

test("single long-horizon grade-A OPEN trade gets the full tier max (5% of capital)", () => {
  const rows = [row({ signal_type: "multibagger", grade: "A", entry: 100 })];
  const out = simulateWallet(rows, badgeOf);
  const t = out.trades[0];
  assert.equal(t.allocated_amount, Math.round(CAPITAL * 0.05));
  assert.equal(t.allocated_qty, Math.floor((CAPITAL * 0.05) / 100));
  assert.equal(t.capital_unavailable, false);
  assert.equal(out.wallet.deployed_amount, t.allocated_amount);
});

test("grade scales the trade size within its tier ceiling", () => {
  const rows = [row({ id: 1, signal_type: "breakout", grade: "C", entry: 50 })];
  const out = simulateWallet(rows, badgeOf);
  // The hf tier is gone: cf_1h, intraday and commodity are all out of mandate,
  // so it reserved 20% of the book for trades that could never happen. Its
  // engines now belong to no tier at all.
  const expected = Math.round(CAPITAL * TIERS.swing.maxPct * 0.45);
  assert.equal(out.trades[0].allocated_amount, expected);
});

test("category cap: a long-horizon grade-A trade is reduced once the category cap is hit", () => {
  // Each grade-A long trade wants maxPct of the book. The category cap divided
  // by that is how many fit at full size; the next one gets whatever headroom
  // is left, which is nothing.
  //
  // Derived from TIERS rather than written as literals: the long cap moved
  // from 25% to 30% when the dead High-frequency tier's reservation was
  // redistributed, and a test carrying "25%" in its name and its arithmetic
  // fails for a reason that has nothing to do with the behaviour under test.
  const perTrade = Math.round(CAPITAL * TIERS.long.maxPct);
  const fit = Math.floor((CAPITAL * TIERS.long.capPct) / perTrade);
  const rows = Array.from({ length: fit + 1 }, (_, i) =>
    row({ id: i + 1, signal_type: "multibagger", grade: "A", entry: 100, date: "2026-08-17" })
  );
  const out = simulateWallet(rows, badgeOf);
  const amounts = out.trades.slice().sort((a, b) => a.id - b.id).map((t) => t.allocated_amount);
  assert.equal(amounts[0], perTrade);
  assert.equal(amounts[fit - 1], perTrade);
  // The one past the cap gets the remaining headroom, which is under a full
  // allocation — and it is MARKED, never silently shrunk.
  assert.ok(amounts[fit] < perTrade);
  assert.equal(out.trades.find((t) => t.id === fit + 1).capital_unavailable, true);
  assert.ok(out.categories.long.deployed_amount <= out.categories.long.cap_amount);
});

test("global cap binds even when a category still has its own headroom", () => {
  // Fill swing to its own cap, then add long trades until the BOOK is at the
  // global ceiling while the long category still has room of its own. The next
  // long trade must be stopped by the global cap, not by its category.
  //
  // Every number is derived from TIERS. Written as literals, this test broke
  // when the tier caps were re-cut and failed for a reason unrelated to the
  // behaviour it exists to prove.
  const swingEach = Math.round(CAPITAL * TIERS.swing.maxPct);
  const longEach = Math.round(CAPITAL * TIERS.long.maxPct);
  const swingFit = Math.floor((CAPITAL * TIERS.swing.capPct) / swingEach);
  const globalRoom = CAPITAL * GLOBAL_CAP_PCT - swingFit * swingEach;
  const longFit = Math.floor(globalRoom / longEach);

  const rows = [];
  let id = 1;
  for (let i = 0; i < swingFit; i++)
    rows.push(row({ id: id++, signal_type: "breakout", grade: "A", entry: 100 }));
  for (let i = 0; i < longFit; i++)
    rows.push(row({ id: id++, signal_type: "multibagger", grade: "A", entry: 100 }));
  const blockedId = id;
  rows.push(row({ id: id++, signal_type: "multibagger", grade: "A", entry: 100 }));

  const out = simulateWallet(rows, badgeOf);
  assert.ok(out.wallet.deployed_pct <= GLOBAL_CAP_PCT * 100 + 0.01,
    `deployed_pct ${out.wallet.deployed_pct} must not exceed the global cap`);
  // Its own category still had headroom — so the global cap is what stopped it.
  assert.ok(out.categories.long.deployed_amount < out.categories.long.cap_amount,
    "long category should still have room, proving the GLOBAL cap bound");
  const blocked = out.trades.find((t) => t.id === blockedId);
  assert.ok(blocked.allocated_amount < longEach,
    "the trade past the global cap must not get a full allocation");
});
test("capital recycles: a trade opened after an earlier same-category trade CLOSED gets full room again", () => {
  const rows = [
    row({ id: 1, signal_type: "multibagger", grade: "A", entry: 100, date: "2026-08-17",
          status: "TARGET_HIT", pnl_pct: 20, closed_at: "2026-08-18" }),
    row({ id: 2, signal_type: "multibagger", grade: "A", entry: 100, date: "2026-08-20" }),
  ];
  const out = simulateWallet(rows, badgeOf);
  const t2 = out.trades.find((t) => t.id === 2);
  // Opened after trade 1's close date — should NOT be blocked by trade 1
  // still "counting" against the category cap.
  assert.equal(t2.allocated_amount, Math.round(CAPITAL * 0.05));
  assert.equal(t2.capital_unavailable, false);
});

test("a still-open earlier trade DOES block a later same-category trade from full size", () => {
  const rows = [
    row({ id: 1, signal_type: "multibagger", grade: "A", entry: 100, date: "2026-08-17" }), // stays OPEN
    row({ id: 2, signal_type: "multibagger", grade: "A", entry: 100, date: "2026-08-18" }),
  ];
  const out = simulateWallet(rows, badgeOf);
  const t1 = out.trades.find((t) => t.id === 1);
  const t2 = out.trades.find((t) => t.id === 2);
  assert.equal(t1.allocated_amount, Math.round(CAPITAL * 0.05));
  // Still gets some room (25% cap has space for 2 x 5%) but total deployed
  // in that category must never exceed the 25% cap.
  assert.ok(t1.allocated_amount + t2.allocated_amount <= Math.round(CAPITAL * 0.25) + 1);
});

test("CANCELLED/VOID signals are excluded entirely — no allocation, not even a trade record", () => {
  const rows = [
    row({ id: 1, signal_type: "multibagger", status: "VOID" }),
    row({ id: 2, signal_type: "multibagger", status: "CANCELLED" }),
    row({ id: 3, signal_type: "multibagger", status: "OPEN" }),
  ];
  const out = simulateWallet(rows, badgeOf);
  assert.equal(out.trades.length, 1);
  assert.equal(out.trades[0].id, 3);
});

test("unknown/untiered signal_type is excluded and reported, never allocated", () => {
  const rows = [row({ id: 1, signal_type: "some_future_engine" })];
  const out = simulateWallet(rows, badgeOf);
  assert.equal(out.trades.length, 0);
  assert.deepEqual(out.untiered_types, ["some_future_engine"]);
  assert.equal(out.wallet.deployed_amount, 0);
});

test("realized P&L: a WIN books positive P&L on its allocated amount, a LOSS books negative", () => {
  const rows = [
    row({ id: 1, signal_type: "breakout", grade: "A", entry: 100,
          status: "TARGET_HIT", pnl_pct: 10, closed_at: "2026-08-18" }),
    row({ id: 2, signal_type: "breakout", grade: "A", entry: 100,
          status: "SL_HIT", pnl_pct: -4, closed_at: "2026-08-19" }),
  ];
  const out = simulateWallet(rows, badgeOf);
  const alloc = Math.round(CAPITAL * TIERS.swing.maxPct);
  const t1 = out.trades.find((t) => t.id === 1);
  const t2 = out.trades.find((t) => t.id === 2);
  assert.equal(t1.realized_pnl, Math.round(alloc * 0.10));
  assert.equal(t2.realized_pnl, Math.round(alloc * -0.04));
  assert.equal(out.wallet.realized_pnl, t1.realized_pnl + t2.realized_pnl);
  assert.equal(out.wallet.wins, 1);
  assert.equal(out.wallet.losses, 1);
  assert.equal(out.wallet.win_rate, 50);
});

test("win_rate is null with no decided (win/loss) trades yet, not a fake 0%", () => {
  const rows = [row({ id: 1, signal_type: "breakout", status: "OPEN" })];
  const out = simulateWallet(rows, badgeOf);
  assert.equal(out.wallet.win_rate, null);
});

test("deployed + cash always sum to capital", () => {
  const rows = [
    row({ id: 1, signal_type: "multibagger", grade: "A" }),
    row({ id: 2, signal_type: "breakout", grade: "B", date: "2026-08-18" }),
  ];
  const out = simulateWallet(rows, badgeOf);
  assert.equal(out.wallet.deployed_amount + out.wallet.cash_amount, CAPITAL);
});

// ── Currency and price disclosure ───────────────────────────────────────────
// The wallet section published utilisation, tier caps and rule text, but never
// the one thing a reader wants from a ledger: which stock, at what price.
// `entry` was already computed and simply never surfaced (found 2026-08-18).
test("a trade carries the price it was entered at", () => {
  const { trades } = simulateWallet(
    [{ id: 1, date: "2026-08-18", symbol: "IOC", signal_type: "breakout",
       entry: 137.8, status: "OPEN", lifecycle_status: null,
       exit_price: null, pnl_pct: null, closed_at: null, grade: "B" }],
    () => "open"
  );
  assert.equal(trades[0].entry, 137.8);
});

test("a closed trade carries its exit price too", () => {
  const { trades } = simulateWallet(
    [{ id: 2, date: "2026-08-17", symbol: "IOC", signal_type: "breakout",
       entry: 100, status: "CLOSED", lifecycle_status: null,
       exit_price: 112.5, pnl_pct: 12.5, closed_at: "2026-08-18", grade: "A" }],
    () => "win"
  );
  assert.equal(trades[0].exit, 112.5);
});

test("the instrument's currency travels with the trade, not the book's", () => {
  // The wallet is a ₹50L book. A US equity inside it is SIZED in rupees and
  // QUOTED in dollars; printing the entry as ₹ would restate a $ price as an
  // INR one, which is the bug currencyOf() was fixed for.
  const { trades } = simulateWallet(
    [{ id: 3, date: "2026-08-18", symbol: "SNOW", signal_type: "breakout",
       entry: 214.3, status: "OPEN", lifecycle_status: null,
       exit_price: null, pnl_pct: null, closed_at: null, grade: "A" },
     { id: 4, date: "2026-08-18", symbol: "IOC", signal_type: "breakout",
       entry: 137.8, status: "OPEN", lifecycle_status: null,
       exit_price: null, pnl_pct: null, closed_at: null, grade: "A" }],
    () => "open",
    (sym) => (sym === "SNOW" ? "$" : "₹")
  );
  assert.equal(trades.find((t) => t.symbol === "SNOW").currency, "$");
  assert.equal(trades.find((t) => t.symbol === "IOC").currency, "₹");
});

test("currencyOf is optional — omitting it defaults to rupees, never undefined", () => {
  const { trades } = simulateWallet(
    [{ id: 5, date: "2026-08-18", symbol: "IOC", signal_type: "breakout",
       entry: 1, status: "OPEN", lifecycle_status: null,
       exit_price: null, pnl_pct: null, closed_at: null, grade: "A" }],
    () => "open"
  );
  assert.equal(trades[0].currency, "₹");
});

// ── Collapsed target ladders ────────────────────────────────────────────────
import { distinctTargets } from "../api/_levels.js";

test("the TECHM ladder drops the duplicate target instead of rewriting it", () => {
  // entry 1592, stop 1568.12, T1 1673.09, T2 1678.17 — 0.2R apart. Ten of 157
  // open signals had this. The stored row is left alone; the display blanks
  // the one that was never distinct.
  assert.deepEqual(
    distinctTargets(1592, 1568.12, 1673.09, 1678.17, 1744.57),
    [1673.09, null, 1744.57]
  );
});

test("the INNER target survives — it is the one anchored to resistance", () => {
  const [t1] = distinctTargets(1592, 1568.12, 1673.09, 1678.17, 1744.57);
  assert.equal(t1, 1673.09);
});

test("a healthy ladder is left completely alone", () => {
  assert.deepEqual(distinctTargets(100, 90, 110, 125, 140), [110, 125, 140]);
});

test("a target exactly at the floor is kept, not dropped", () => {
  // risk 10, floor 5. T2 at 115 is exactly 5 away from T1.
  assert.deepEqual(distinctTargets(100, 90, 110, 115, 130), [110, 115, 130]);
});

test("no stop-loss means no risk to measure against — nothing is dropped", () => {
  // Inventing a floor from a missing stop would blank real targets.
  assert.deepEqual(distinctTargets(100, null, 110, 111, 112), [110, 111, 112]);
});

test("a null target stays null and does not shift the ones after it", () => {
  assert.deepEqual(distinctTargets(100, 90, 110, null, 140), [110, null, 140]);
});

test("three collapsed targets leave exactly one", () => {
  assert.deepEqual(distinctTargets(100, 90, 110, 110.5, 111), [110, null, null]);
});

// ── News event clustering ───────────────────────────────────────────────────
import { clusterByEvent, sameEvent, tokens } from "../api/_cluster.js";

test("one RBI rate decision told four ways becomes one card", () => {
  const items = [
    { title: "RBI Panel Watches Inflation as One Member Eyes Hike This Year - Bloomberg.com", source: "Google Markets" },
    { title: "RBI MPC minutes: Policy panel signals rate hike risk as inflation rises - indianexpress.com", source: "Google India" },
    { title: "India rate panel signals impending hikes, eyes inflation path for timing - reuters", source: "Reuters" },
    { title: "India's central bank signals possible rate hikes amid inflation risks - CNBC", source: "CNBC" },
  ];
  const out = clusterByEvent(items);
  assert.equal(out.length, 1);
  assert.equal(out[0].also, 3);
});

test("a DIFFERENT RBI story is not swallowed by the rate-decision cluster", () => {
  // The failure that matters. Merging on the loudest shared token would hide a
  // real story behind an unrelated one — worse than a duplicate, and far
  // harder to notice, because nothing looks wrong.
  const items = [
    { title: "RBI MPC minutes: Policy panel signals rate hike risk as inflation rises", source: "A" },
    { title: "RBI issues major warning on KYC fraud, scammers can access bank OTP", source: "B" },
  ];
  assert.equal(clusterByEvent(items).length, 2);
});

test("Armenia's central bank does not join India's rate decision", () => {
  // The exact false merge that ruled out a looser threshold (0.35).
  assert.equal(
    sameEvent(
      "RBI MPC minutes: Policy panel signals rate hike risk as inflation rises",
      "Central Bank of Armenia: exchange rates and prices of precious metals"
    ),
    false
  );
});

test("acronyms survive tokenisation — they are the identifying word", () => {
  // A four-character floor drops RBI, SEC, FED, IMF, GDP, IPO: precisely the
  // token that says WHICH event a headline is about.
  const t = tokens("RBI MPC minutes signal a rate hike");
  assert.ok(t.has("rbi") && t.has("mpc"));
});

test("plural and singular of the event word do not split a cluster", () => {
  const t1 = tokens("India rate panel signals impending hikes on inflation path");
  const t2 = tokens("India rate panel signal impending hike on inflation path");
  assert.deepEqual([...t1].sort(), [...t2].sort());
});

test("the publisher suffix is not part of the event", () => {
  assert.deepEqual(
    [...tokens("Nifty ends lower on IT drag - Reuters")].sort(),
    [...tokens("Nifty ends lower on IT drag")].sort()
  );
});

test("the primary keeps its own source out of also_sources", () => {
  const out = clusterByEvent([
    { title: "RBI MPC minutes signal rate hike risk as inflation rises", source: "Reuters" },
    { title: "RBI MPC minutes signal rate hike risk amid inflation", source: "Reuters" },
    { title: "RBI MPC minutes signal a rate hike as inflation rises further", source: "CNBC" },
  ]);
  assert.equal(out.length, 1);
  assert.deepEqual(out[0].also_sources, ["CNBC"]);
});

test("unrelated stories are never merged", () => {
  const items = [
    { title: "Moderna, Merck cancer vaccine shows promise in late-stage trial", source: "CNBC" },
    { title: "Israel confirms it opened fire on vehicle carrying a child in Gaza", source: "BBC" },
    { title: "Nifty 50 profit growth faces global headwinds", source: "Investing" },
  ];
  assert.equal(clusterByEvent(items).length, 3);
});

test("an empty or single-item list is handled", () => {
  assert.deepEqual(clusterByEvent([]), []);
  assert.deepEqual(clusterByEvent(null), []);
  assert.equal(clusterByEvent([{ title: "x", source: "s" }]).length, 1);
});

// ── Direction ───────────────────────────────────────────────────────────────
// This book carries real shorts — 131 SELL signals across Gold, Crude, Natural
// Gas and Silver. The wallet never read `action`, so a short and a long
// rendered identically and the stop looked broken (a short's stop is ABOVE
// its entry).

test("sideOf: SELL is a short, everything else is a long", () => {
  assert.equal(sideOf("SELL"), "SHORT");
  assert.equal(sideOf("sell"), "SHORT");
  assert.equal(sideOf("BUY"), "LONG");
  assert.equal(sideOf(null), "LONG");
  assert.equal(sideOf(""), "LONG");
});

test("directionalPnlPct: a short that FALLS has made money", () => {
  assert.equal(directionalPnlPct("SHORT", 100, 90), 10);
  assert.equal(directionalPnlPct("SHORT", 100, 110), -10);
  assert.equal(directionalPnlPct("LONG", 100, 110), 10);
  assert.equal(directionalPnlPct("LONG", 100, 90), -10);
});

test("directionalPnlPct: no entry, no number — never a fabricated 0", () => {
  assert.equal(directionalPnlPct("LONG", null, 90), null);
  assert.equal(directionalPnlPct("LONG", 100, null), null);
  assert.equal(directionalPnlPct("LONG", 0, 90), null);
});

// ── Partial booking ─────────────────────────────────────────────────────────

test("T2_HIT books half at T1 and half at T2, not all of it at T2", () => {
  // entry 100, T1 110, T2 130. Full-position would be +30%.
  // The ladder is half at +10 and half at +30 = +20%.
  assert.equal(ladderedPnlPct("T2_HIT", "LONG", 100, 95, 110, 130), 20);
});

test("T1_HIT books the whole position at T1", () => {
  assert.equal(ladderedPnlPct("T1_HIT", "LONG", 100, 95, 110, 130), 10);
});

test("TARGET_HIT does not say which target, so it books at T1", () => {
  // The conservative reading, and the only one that cannot overstate.
  assert.equal(ladderedPnlPct("TARGET_HIT", "LONG", 100, 95, 110, 130), 10);
});

test("SL_HIT books the whole position at the stop", () => {
  assert.equal(ladderedPnlPct("SL_HIT", "LONG", 100, 95, 110, 130), -5);
});

test("a SHORT ladder runs downward and profits", () => {
  // SHORT: t2 < t1 < entry < sl (tracker.py's ordering).
  // entry 100, T1 90, T2 70, stop 105. Half at +10, half at +30 = +20%.
  assert.equal(ladderedPnlPct("T2_HIT", "SHORT", 100, 105, 90, 70), 20);
  assert.equal(ladderedPnlPct("SL_HIT", "SHORT", 100, 105, 90, 70), -5);
});

test("a collapsed target pair books one leg, never an invented second exit", () => {
  // distinctTargets() nulls the duplicate rather than inventing a level.
  assert.equal(ladderedPnlPct("T2_HIT", "LONG", 100, 95, 110, null), 10);
});

test("an open trade has no ladder", () => {
  assert.equal(ladderedPnlPct("OPEN", "LONG", 100, 95, 110, 130), null);
});

test("no levels means fall back to the ledger, not a fabricated ladder", () => {
  assert.equal(ladderedPnlPct("T2_HIT", "LONG", 100, null, null, null), null);
});

// ── End to end through simulateWallet ───────────────────────────────────────

test("simulateWallet carries side and levels onto every trade", () => {
  const out = simulateWallet(
    [row({ id: 7, symbol: "GOLD", signal_type: "breakout", action: "SELL",
           entry: 100, sl: 105, target1: 90, target2: 70 })],
    badgeOf);
  const t = out.trades.find((x) => x.id === 7);
  assert.equal(t.side, "SHORT");
  assert.equal(t.action, "SELL");
  assert.equal(t.sl, 105);
  assert.equal(t.target1, 90);
  assert.equal(t.target2, 70);
});

test("simulateWallet books a T2_HIT on the ladder, and says so", () => {
  const out = simulateWallet(
    [row({ id: 8, status: "T2_HIT", exit_price: 130, pnl_pct: 30,
           closed_at: "2026-08-19", entry: 100, sl: 95,
           target1: 110, target2: 130 })],
    badgeOf);
  const t = out.trades.find((x) => x.id === 8);
  assert.equal(t.pnl_basis, "partial_booking");
  assert.equal(t.booked_pnl_pct, 20);      // ladder
  assert.equal(t.ledger_pnl_pct, 30);      // what the ledger recorded
  // The wallet's realized rupees follow the ladder, not the full position.
  assert.equal(t.realized_pnl, Math.round(t.allocated_amount * 0.20));
});

test("a closed trade with no levels falls back to the ledger figure", () => {
  // A real resolved status, but the row carries no stop — older rows predate
  // the levels columns. The ladder cannot be built, so the ledger's own
  // number stands rather than a fabricated one.
  const out = simulateWallet(
    [row({ id: 9, status: "SL_HIT", exit_price: 92, pnl_pct: -8,
           closed_at: "2026-08-19", entry: 100, sl: null,
           target1: null, target2: null })],
    badgeOf);
  const t = out.trades.find((x) => x.id === 9);
  assert.equal(t.pnl_basis, "ledger");
  assert.equal(t.booked_pnl_pct, -8);
});
