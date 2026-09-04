#!/usr/bin/env python3
"""Regressions for targets.py.

The two defects that motivated most of these:

  * The house ladder snapped targets to a 10/20-day ROLLING MAX, which is
    wherever price happened to be, not a level anyone defended. Four engines
    were separately found publishing a first target worth less than 1R.
  * A draft of this module allowed a level 10% below the R1 floor, and on the
    first symbol tested that put T1 at 0.88R — reintroducing exactly that bug.
    `test_floor_is_a_floor` is the guard.

Run: python3 test_targets.py
"""
import sys

import targets as T

P = F = 0


def check(label, got, exp):
    global P, F
    if got == exp:
        P += 1
        print(f"  PASS  {label}")
    else:
        F += 1
        print(f"  FAIL  {label} — expected {exp!r}, got {got!r}")


def approx(a, b, tol=0.02):
    return a is not None and b is not None and abs(a - b) <= tol


def main():
    print("── swing highs are turns, not maxima ────────────────────────")
    # A single rising staircase has no swing high: price never turned.
    check("a monotonic rise has no swing high", T.swing_highs(list(range(30))), [])
    # One clear peak in the middle.
    series = [10, 11, 12, 13, 20, 13, 12, 11, 10]
    check("one peak is found", T.swing_highs(series), [20])
    check("a short series yields nothing", T.swing_highs([1, 2, 3]), [])
    check("junk values are skipped", T.swing_highs([1, None, 2, "x", 3]), [])

    print("\n── resistance clusters ──────────────────────────────────────")
    # Three peaks within half an ATR of each other are ONE level tested 3x.
    highs = ([10, 11, 12, 13, 100, 13, 12] + [11, 12, 13, 100.4, 13, 12, 11]
             + [12, 13, 100.2, 13, 12, 11, 10])
    lv = T.resistance_levels(90, atr=2.0, highs=highs)
    check("nearby peaks collapse to one level", len(lv), 1)
    check("...and carry the touch count", lv[0]["touches"], 3)
    check("...and say so", "tested 3x" in lv[0]["basis"], True)

    lv2 = T.resistance_levels(90, atr=2.0, highs=highs, extra=[("52-week high", 100.1)])
    check("a named anchor joins the same cluster", len(lv2), 1)
    check("...and wins the description", lv2[0]["basis"].startswith("52-week high"), True)

    check("levels below price are not resistance",
          T.resistance_levels(200, atr=2.0, highs=highs), [])
    check("no ATR means no levels", T.resistance_levels(90, atr=0, highs=highs), [])

    print("\n── the floor is a floor ─────────────────────────────────────")
    # A wall sits at 0.9R. It must NOT become T1.
    price, stop, atr = 100.0, 90.0, 3.0          # risk = 10
    wall_highs = [90, 95, 100, 109, 100, 95, 90] * 3   # peaks at 109 = 0.9R
    lad = T.build_ladder(price, stop, atr, wall_highs)
    check("T1 clears the 1.6R floor", lad["targets"][0]["r"] >= T.R1_FLOOR, True)
    check("...and the sub-floor wall is reported, not used",
          lad["wall"] is not None and lad["wall"]["r"] < T.R1_FLOOR, True)
    check("...naming its price", approx(lad["wall"]["px"], 109.0, 0.5), True)

    print("\n── ladder shape ─────────────────────────────────────────────")
    lad = T.build_ladder(100.0, 90.0, 3.0, [])
    check("with no levels, three targets still exist", len(lad["targets"]), 3)
    check("T1 falls back to the floor", approx(lad["targets"][0]["r"], 1.6), True)
    check("T2 falls back to the floor", approx(lad["targets"][1]["r"], 2.5), True)
    check("T3 falls back to the floor", approx(lad["targets"][2]["r"], 3.3), True)
    check("...and says it is a floor, not a level",
          "floor" in lad["targets"][0]["basis"], True)
    check("targets ascend", [t["px"] for t in lad["targets"]] ==
          sorted(t["px"] for t in lad["targets"]), True)
    gaps = [lad["targets"][i + 1]["px"] - lad["targets"][i]["px"] for i in range(2)]
    check("no two targets are the same target twice",
          all(g >= T.MIN_GAP_ATR * 3.0 - 1e-9 for g in gaps), True)
    check("rr quotes T1, not the furthest target", lad["rr"], lad["targets"][0]["r"])

    print("\n── odds travel with the target ──────────────────────────────")
    check("every target carries a reach %",
          all(isinstance(t["reach"], int) for t in lad["targets"]), True)
    check("reach falls as the target gets further",
          lad["targets"][0]["reach"] >= lad["targets"][1]["reach"] >=
          lad["targets"][2]["reach"], True)
    check("0.5R reach matches the measured table", T.reach_pct(0.5), T.REACH_BY_R[0.5])
    check("beyond the sample it does not extrapolate",
          T.reach_pct(99.0), T.REACH_BY_R[max(T.REACH_BY_R)])
    check("a zero-distance target has no odds", T.reach_pct(0), 0.0)
    check("reach is monotonic across the table",
          all(T.reach_pct(a) >= T.reach_pct(b)
              for a, b in zip(sorted(T.REACH_BY_R), sorted(T.REACH_BY_R)[1:])), True)

    print("\n── quality trails, it does not stretch ──────────────────────")
    weak = T.build_ladder(100.0, 90.0, 3.0, [], quality={"strong": False})
    strong = T.build_ladder(100.0, 90.0, 3.0, [],
                            quality={"strong": True, "why": "ROCE 22%"})
    check("a strong name does NOT get further targets",
          [t["px"] for t in strong["targets"]], [t["px"] for t in weak["targets"]])
    check("it gets a trail instead", (weak["trail"], strong["trail"]), (False, True))
    check("...and the note says why", "ROCE 22%" in strong["trail_note"], True)

    print("\n── refuses to guess ─────────────────────────────────────────")
    check("no stop, no ladder", T.build_ladder(100.0, None, 3.0, []), None)
    check("no ATR, no ladder", T.build_ladder(100.0, 90.0, None, []), None)
    check("an inverted stop is a bug, not a short",
          T.build_ladder(100.0, 110.0, 3.0, []), None)
    check("a zero-risk stop is rejected", T.build_ladder(100.0, 100.0, 3.0, []), None)

    print(f"\n{P} passed · {F} failed")
    return 1 if F else 0


if __name__ == "__main__":
    sys.exit(main())
