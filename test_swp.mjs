// test_swp.mjs — the SWP drawdown engine, against the cases that break models.
//
// This is a retirement plan. The failure mode is not a crash — it is a plan
// that says "sustainable" when it is not, or renders NaN into a chart, or
// quietly lasts longer on paper than in life because tax was ignored.
//
// The function under test is extracted from the SHIPPED static/app.js rather
// than reimplemented here, so this cannot pass against a copy that has drifted
// from what the page runs.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const SRC = readFileSync(new URL("./static/app.js", import.meta.url), "utf8");
const m = SRC.match(/function drawdown\(c0, basisRatio, o\)\{[\s\S]*?\n    \}/);
assert.ok(m, "drawdown() not found in static/app.js — did it move or get renamed?");
const drawdown = new Function(`${m[0]}; return drawdown;`)();

// Monthly rate from the EFFECTIVE annual rate, exactly as model() computes it.
const monthly = (annualPct) => Math.pow(1 + annualPct / 100, 1 / 12) - 1;

function plan({ corpus = 5_000_000, annual = 8, monthlyDraw = 50_000,
                infl = 6, tax = 12.5, years = 35, basisRatio = 1 } = {}) {
  return drawdown(corpus, basisRatio, {
    years, retAge: 55, mPost: monthly(annual),
    infl: infl / 100, tax: tax / 100, firstNet: monthlyDraw,
  });
}

const finite = (r) =>
  r.rows.every((x) => Number.isFinite(x.corpus) && Number.isFinite(x.flow) && x.corpus >= 0);

// ── The prompt's headline scenario ──────────────────────────────────────────

test("₹50L at 8% drawing ₹50k/month with 6% inflation does NOT survive 35 years", () => {
  // ₹6L a year off ₹50L is 12% against an 8% return, before tax and before
  // inflation raises the cheque every year. Any model that calls this
  // sustainable is broken.
  const r = plan();
  assert.equal(r.survives, false);
  assert.ok(r.deadAt !== null && r.deadAt > 55, `deadAt=${r.deadAt}`);
  assert.ok(finite(r));
});

test("the same corpus drawing ₹10k/month DOES survive", () => {
  // ₹1.2L a year off ₹50L is 2.4% against 8% — the plan must not be
  // pessimistic either, or it is useless in the other direction.
  const r = plan({ monthlyDraw: 10_000 });
  assert.equal(r.survives, true);
  assert.equal(r.deadAt, null);
});

// ── Return edge cases ───────────────────────────────────────────────────────

test("zero return: the corpus can only fall", () => {
  const r = plan({ annual: 0, infl: 0, tax: 0 });
  const c = r.rows.map((x) => x.corpus);
  for (let i = 1; i < c.length; i++) assert.ok(c[i] <= c[i - 1] + 1e-6);
  assert.ok(finite(r));
});

test("negative return does not produce a negative corpus or NaN", () => {
  const r = plan({ annual: -20 });
  assert.ok(finite(r), "a negative return produced a negative or non-finite corpus");
  assert.equal(r.survives, false);
});

test("a −100% return is survived without dividing by zero", () => {
  // mPost = -1 exactly. corpus * (1 + -1) = 0 on the first month.
  const r = plan({ annual: -100 });
  assert.ok(finite(r));
  assert.equal(r.rows[0].corpus, 0);
});

test("a very high return survives and stays finite", () => {
  const r = plan({ annual: 40, monthlyDraw: 50_000 });
  assert.equal(r.survives, true);
  assert.ok(r.rows.every((x) => Number.isFinite(x.corpus)));
});

// ── Withdrawal edge cases ───────────────────────────────────────────────────

test("a withdrawal larger than the whole corpus empties it in year one", () => {
  const r = plan({ corpus: 100_000, monthlyDraw: 500_000 });
  assert.equal(r.deadAt, 56);
  assert.equal(r.rows[0].corpus, 0);
  assert.ok(finite(r));
});

test("zero withdrawal compounds instead of drawing down", () => {
  const r = plan({ monthlyDraw: 0, annual: 8 });
  assert.equal(r.survives, true);
  assert.ok(r.rows[r.rows.length - 1].corpus > 5_000_000);
});

test("zero corpus is dead immediately, not NaN", () => {
  const r = plan({ corpus: 0 });
  assert.equal(r.deadAt, 56);
  assert.ok(finite(r));
  assert.ok(r.rows.every((x) => x.corpus === 0));
});

test("decimal inputs are handled, not rounded into nonsense", () => {
  const r = plan({ corpus: 5_000_000.55, monthlyDraw: 49_999.99, annual: 8.25, infl: 6.4 });
  assert.ok(finite(r));
  assert.ok(r.rows.length > 0);
});

// ── Tax ─────────────────────────────────────────────────────────────────────

test("tax makes the plan strictly shorter, never longer", () => {
  // The gross-up exists because a net cheque needs a bigger redemption. If
  // tax ever LENGTHENED the plan the sign would be inverted somewhere.
  const withTax = plan({ tax: 30, monthlyDraw: 30_000 });
  const noTax = plan({ tax: 0, monthlyDraw: 30_000 });
  const life = (r) => (r.deadAt === null ? Infinity : r.deadAt);
  assert.ok(life(withTax) <= life(noTax),
    `taxed plan outlived the untaxed one: ${life(withTax)} vs ${life(noTax)}`);
});

test("a 100% tax input is guarded rather than dividing by zero", () => {
  const r = plan({ tax: 100, monthlyDraw: 30_000 });
  assert.ok(finite(r), "a 100% tax rate produced a non-finite corpus");
});

test("a fully taxable corpus never draws a bigger gross than the corpus itself", () => {
  const r = plan({ basisRatio: 0, tax: 30, monthlyDraw: 200_000 });
  assert.ok(r.rows.every((x) => x.corpus >= 0));
  assert.ok(finite(r));
});

// ── Inflation ───────────────────────────────────────────────────────────────

test("inflation shortens the plan — the cheque grows every year", () => {
  const hot = plan({ infl: 12, monthlyDraw: 25_000 });
  const cold = plan({ infl: 0, monthlyDraw: 25_000 });
  const life = (r) => (r.deadAt === null ? Infinity : r.deadAt);
  assert.ok(life(hot) <= life(cold));
});

// ── Shape guarantees ────────────────────────────────────────────────────────

test("every row is finite and non-negative across a wide input sweep", () => {
  for (const annual of [-100, -20, 0, 8, 40]) {
    for (const draw of [0, 1, 50_000, 10_000_000]) {
      for (const tax of [0, 12.5, 100]) {
        for (const corpus of [0, 1, 5_000_000]) {
          const r = plan({ annual, monthlyDraw: draw, tax, corpus });
          assert.ok(finite(r), `non-finite at annual=${annual} draw=${draw} tax=${tax} corpus=${corpus}`);
          assert.equal(r.survives, r.deadAt === null);
        }
      }
    }
  }
});

test("deadAt, when set, is the first year the corpus reached zero", () => {
  const r = plan({ corpus: 1_000_000, monthlyDraw: 100_000 });
  assert.ok(r.deadAt !== null);
  const idx = r.rows.findIndex((x) => x.corpus <= 0);
  assert.equal(r.rows[idx].age, r.deadAt);
});
