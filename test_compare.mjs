// test_compare.mjs — "where each wins", against the shipped code.
//
// The failure mode is quiet and total: a naive max() hands the win to whoever
// has the HIGHEST P/E and the MOST debt, and the table still looks right.
// Direction is the whole feature.
//
// cmpWinner and CMP_METRICS are extracted from static/app.js rather than
// reimplemented, so this cannot pass against a copy that has drifted.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const SRC = readFileSync(new URL("./static/app.js", import.meta.url), "utf8");

function grab(re, what) {
  const m = SRC.match(re);
  assert.ok(m, `${what} not found in static/app.js — renamed or moved?`);
  return m[0];
}
const METRICS = grab(/var CMP_METRICS = \[[\s\S]*?\n    \];/, "CMP_METRICS");
const WINNER = grab(/function cmpWinner\(rows, m\)\{[\s\S]*?\n    \}/, "cmpWinner");
const TALLY = grab(/function cmpTally\(rows\)\{[\s\S]*?\n    \}/, "cmpTally");
const { cmpWinner, cmpTally, CMP_METRICS } =
  new Function(`${METRICS}${WINNER}${TALLY}
     return { cmpWinner, cmpTally, CMP_METRICS };`)();

const M = (k) => CMP_METRICS.find((x) => x.k === k);

test("higher wins where higher is better", () => {
  const rows = [{ sym: "A", roce: 28 }, { sym: "B", roce: 14 }];
  assert.equal(cmpWinner(rows, M("roce")), "A");
});

test("LOWER wins on P/E — the check the whole feature turns on", () => {
  const rows = [{ sym: "A", pe: 48 }, { sym: "B", pe: 12 }];
  assert.equal(cmpWinner(rows, M("pe")), "B");
});

test("LOWER wins on debt-to-equity", () => {
  const rows = [{ sym: "A", de: 1.8 }, { sym: "B", de: 0.2 }];
  assert.equal(cmpWinner(rows, M("de")), "B");
});

test("every metric declares a direction", () => {
  for (const m of CMP_METRICS) {
    assert.ok([1, -1, 0].includes(m.dir), `${m.k} has dir ${m.dir}`);
    assert.ok(m.label && m.fmt, `${m.k} missing label/fmt`);
  }
});

test("RSI is shown but never scored — it has no better end", () => {
  const rows = [{ sym: "A", rsi: 71 }, { sym: "B", rsi: 44 }];
  assert.equal(cmpWinner(rows, M("rsi")), null);
});

test("a tie is not a win", () => {
  const rows = [{ sym: "A", q: 80 }, { sym: "B", q: 80 }];
  assert.equal(cmpWinner(rows, M("q")), null);
});

test("a metric only one company reports is not compared", () => {
  // Otherwise the company that failed to disclose loses by default, and the
  // one that disclosed 'wins' against nobody.
  const rows = [{ sym: "A", roce: 22 }, { sym: "B" }];
  assert.equal(cmpWinner(rows, M("roce")), null);
});

test("missing values are never read as zero", () => {
  // A null ROCE beating a real 14% would be the worst kind of wrong: it
  // rewards non-disclosure.
  const rows = [{ sym: "A", roce: null }, { sym: "B", roce: 14 }, { sym: "C", roce: 9 }];
  assert.equal(cmpWinner(rows, M("roce")), "B");
});

test("NaN, Infinity and strings are treated as missing", () => {
  for (const bad of [NaN, Infinity, -Infinity, "22", undefined]) {
    const rows = [{ sym: "A", roce: bad }, { sym: "B", roce: 14 }, { sym: "C", roce: 9 }];
    assert.equal(cmpWinner(rows, M("roce")), "B", `failed for ${String(bad)}`);
  }
});

test("a negative return still beats a worse negative return", () => {
  const rows = [{ sym: "A", r1y: -30 }, { sym: "B", r1y: -8 }];
  assert.equal(cmpWinner(rows, M("r1y")), "B");
});

test("the tally counts wins and names what they were won on", () => {
  const rows = [
    { sym: "A", q: 90, pe: 40, roce: 30 },
    { sym: "B", q: 60, pe: 11, roce: 12 },
  ];
  const t = cmpTally(rows);
  assert.equal(t.A.wins, 2);                     // quality + ROCE
  assert.equal(t.B.wins, 1);                     // cheaper P/E
  assert.ok(t.B.on.includes("P/E"));
  assert.ok(t.A.on.includes("Quality"));
});

test("a company that leads on nothing is reported as leading on nothing", () => {
  const rows = [{ sym: "A", q: 90, roce: 30 }, { sym: "B", q: 10, roce: 4 }];
  assert.equal(cmpTally(rows).B.wins, 0);
});

test("comparing three or more works, and only one can win a metric", () => {
  const rows = [{ sym: "A", q: 70 }, { sym: "B", q: 90 }, { sym: "C", q: 80 }];
  assert.equal(cmpWinner(rows, M("q")), "B");
  const t = cmpTally(rows);
  assert.equal(t.A.wins + t.B.wins + t.C.wins, 1);
});
