#!/usr/bin/env python3
"""
momentum_engine.py — cross-sectional quant momentum over the 750-name screen.

WHERE THE FORMULA COMES FROM
----------------------------
Nothing here is invented. Each part traces to a source, and the sources
disagree with each other in one important place, which is stated rather than
smoothed over.

1. THE SCORE — NSE Indices' own construction.
   The Nifty500 Momentum 50 index selects on a "Normalized Momentum Score,
   calculated using six-month and twelve-month price returns adjusted for
   volatility". That is the shape used here: two windows, both risk-adjusted,
   averaged. It is the closest thing to an official Indian definition of the
   factor, and preferring it over something hand-rolled means this engine can
   be argued with rather than defended.

2. THE SKIP MONTH — Jegadeesh & Titman, and Raju's India replication.
   Momentum is measured over 12 months MINUS the most recent month. The last
   month reverses: the same stock that rose over a year tends to give some of
   it back over four weeks, so including it buys the reversal. Raju's India
   papers use the same "12-month-skip-current-month" construction.

3. THE UNIVERSE — Raju, India, Oct 2004 to Apr 2023.
   Universe size is not a detail. Ranked over 750 names momentum returned
   35.12% annualised; over the Nifty 200 it returned 24.52%. Widening to mid
   and small caps is where the Indian premium lives. This site already screens
   750 names, which happens to be exactly the universe that study found best.

4. THE LIQUIDITY FILTER — BacktestIndia, 19-year NSE study.
   The finding that changes the engine most: splitting Nifty 200 Momentum 30 by
   scaled turnover (daily volume / market cap), HIGH-turnover momentum returned
   8.51% net CAGR — BELOW the Nifty 50's 10.41% — while LOW-turnover returned
   19.43%. Their conclusion is that the Indian momentum factor is "a liquidity
   premium in disguise". So scaled turnover is scored, not decorated: a name
   that everyone is already trading has, on that evidence, already paid out.

5. THE CRASH FILTER — Daniel & Moskowitz, and the dual-momentum literature.
   Cross-sectional momentum's characteristic failure is not slow decay, it is a
   crash: it sells the losers hardest just before they rebound. The standard
   defence is an ABSOLUTE momentum gate on top of the relative one — a name
   must be rising on its own terms, not merely rising faster than its peers.
   Here that is 12-month return above zero and price above the 200-day.

THE EVIDENCE AGAINST, WHICH IS NOT BURIED
-----------------------------------------
Sharma, Subramaniam et al. (2021), "Are prominent equity market anomalies in
India fading away?", conclude that momentum "faded and thus does not provide
any superior risk-adjusted returns in the Indian context".

Two of the five sources above are practitioner write-ups rather than peer
review, and the two strongest numbers (35.12% and 19.43%) are backtests, which
are the easiest numbers in finance to produce and the hardest to keep.

So this engine ships as PAPER. It is not cleared for capital and it earns its
way to that like every other engine on this ledger: 30 closed trades at a
t-statistic of 2 or better. On today's evidence it is a hypothesis with good
provenance, which is a different thing from an edge.

WHAT IT DELIBERATELY DOES NOT CLAIM
-----------------------------------
It is asked to serve "swing, short term and long term". The cited work supports
one of those: a monthly-ranked, multi-week-to-multi-month hold. There is no
evidence in any source above for a 12-month momentum score predicting a
three-day move, so no intraday or short-swing variant is emitted. Producing one
would mean inventing the part of the strategy the research does not cover, and
labelling it with the credibility of the part it does.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import statistics
import sys
from datetime import datetime, timezone

log = logging.getLogger("momentum")

# ── PARAMETERS, EACH WITH ITS SOURCE ────────────────────────────────────────
W_12M = 0.5            # NSE weights the two windows equally
W_6M = 0.5
WINSOR_Z = 3.0         # standard practice; one name cannot carry the ranking
MIN_TURNOVER_CR = 1.0  # tradeable at all — below this a stop cannot be worked
TOP_N = 5              # published per run; the ladder is ranked, not exhaustive
STOP_ATR_MULT = 1.5    # matches the floor applied to every other engine today
RR = (1.5, 2.5, 4.0)   # the house ladder, unchanged


def _f(v):
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def skip_month(long_ret, one_month):
    """A 12-month return with the most recent month taken back out.

    (1+r12)/(1+r1) - 1, not r12 - r1. Returns compound; subtracting them is
    wrong by the cross-product, which at Indian mid-cap volatility is not a
    rounding error — on a name up 80% over a year and 20% over the month it is
    the difference between 60.0% and 50.0%.
    """
    if long_ret is None or one_month is None:
        return None
    denom = 1.0 + one_month / 100.0
    if denom <= 0:
        return None
    return ((1.0 + long_ret / 100.0) / denom - 1.0) * 100.0


def zscores(values):
    """Winsorised z-scores. Returns a list aligned with the input."""
    live = [v for v in values if v is not None]
    if len(live) < 20:
        return [None] * len(values)
    mu = statistics.mean(live)
    sd = statistics.pstdev(live)
    if sd <= 0:
        return [0.0 if v is not None else None for v in values]
    out = []
    for v in values:
        if v is None:
            out.append(None)
        else:
            out.append(max(-WINSOR_Z, min(WINSOR_Z, (v - mu) / sd)))
    return out


def score_universe(rows):
    """Rank the screen. Returns rows with a `mom` block attached, best first."""
    # ── raw, per name ────────────────────────────────────────────────────────
    prepared = []
    for r in rows:
        r1m, r6m, r1y = _f(r.get("r1m")), _f(r.get("r6m")), _f(r.get("r1y"))
        atr = _f(r.get("atr_pct"))
        price, sma200 = _f(r.get("price")), _f(r.get("sma200"))
        turn, mcap = _f(r.get("turnover_cr")), _f(r.get("mcap_cr"))
        m12 = skip_month(r1y, r1m)
        m6 = skip_month(r6m, r1m)
        # RISK ADJUSTMENT. NSE says "adjusted for volatility"; ATR% is the
        # volatility this screen actually carries for every name, so it is what
        # the adjustment uses. A 40% run in a name that moves 1% a day is a
        # different fact from the same 40% in one that moves 5%.
        ra12 = (m12 / atr) if (m12 is not None and atr) else None
        ra6 = (m6 / atr) if (m6 is not None and atr) else None
        # Scaled turnover: daily traded value against market cap. Low is the
        # side that paid in the 19-year study.
        scaled_turn = (turn / mcap * 1e4) if (turn is not None and mcap) else None
        prepared.append(dict(row=r, m12=m12, m6=m6, ra12=ra12, ra6=ra6,
                             atr=atr, price=price, sma200=sma200,
                             turn=turn, scaled_turn=scaled_turn))

    z12 = zscores([p["ra12"] for p in prepared])
    z6 = zscores([p["ra6"] for p in prepared])
    # Sign flipped: LOW scaled turnover is the good end, so its z-score is
    # negated before it is added. Getting this backwards would build the
    # portfolio the study measured at 8.51% instead of 19.43%.
    zturn = zscores([p["scaled_turn"] for p in prepared])

    scored = []
    for p, a, b, c in zip(prepared, z12, z6, zturn):
        if a is None or b is None:
            continue
        core = W_12M * a + W_6M * b
        liq = -(c if c is not None else 0.0)
        # The liquidity tilt is a TILT. At a third of the weight it can reorder
        # names of similar momentum without letting an illiquid name with no
        # momentum into the list on liquidity alone.
        total = core + 0.33 * liq
        p["z12"], p["z6"], p["zturn"] = a, b, c
        p["core"], p["liq"], p["score"] = core, liq, total
        scored.append(p)

    scored.sort(key=lambda p: p["score"], reverse=True)
    return scored


def gates(p):
    """Absolute-momentum and tradeability gates. Returns a list of failures."""
    bad = []
    # ABSOLUTE momentum, not merely relative. This is the crash filter: in a
    # falling market the top decile of a relative ranking is still falling, and
    # buying it is how momentum books lose 40% in a quarter.
    if p["m12"] is None or p["m12"] <= 0:
        bad.append("12m return is not positive")
    if p["price"] is None or p["sma200"] is None:
        bad.append("no 200-day to check against")
    elif p["price"] <= p["sma200"]:
        bad.append("price is below its 200-day")
    if p["turn"] is None or p["turn"] < MIN_TURNOVER_CR:
        bad.append(f"turnover under Rs {MIN_TURNOVER_CR}cr — a stop cannot be worked")
    if not p["atr"]:
        bad.append("no ATR, so no stop can be sized")
    return bad


def levels(p):
    """Entry, stop and the target ladder, on the same rules as every engine."""
    price, atr_pct = p["price"], p["atr"]
    risk = price * (STOP_ATR_MULT * atr_pct / 100.0)
    if risk <= 0:
        return None
    return dict(
        entry=round(price, 2),
        sl=round(price - risk, 2),
        target1=round(price + RR[0] * risk, 2),
        target2=round(price + RR[1] * risk, 2),
        target3=round(price + RR[2] * risk, 2),
        rr=round(RR[0], 2),
        risk_pct=round(risk / price * 100, 2),
    )


def build(screen_rows, top_n=TOP_N):
    scored = score_universe(screen_rows)
    # GATE THE WHOLE UNIVERSE, THEN TAKE THE TOP N.
    #
    # The first version counted rejections inside the selection loop and broke
    # out once it had enough picks, so it reported "rejected_by_gates: 0" —
    # true only of the handful of names it happened to look at before filling
    # the list. A diagnostic that counts whatever it saw before it stopped
    # looking is worse than no diagnostic: it says the gates passed everything
    # when it never asked them.
    from collections import Counter
    reasons = Counter()
    eligible = []
    for p in scored:
        bad = gates(p)
        if bad:
            for b in bad:
                reasons[b] += 1
            continue
        if not levels(p):
            reasons["levels could not be sized"] += 1
            continue
        eligible.append(p)

    picks = []
    for p in eligible:
        lv = levels(p)
        r = p["row"]
        picks.append(dict(
            symbol=r.get("sym"), name=r.get("name"), sector=r.get("sector"),
            action="BUY", signal_type="momentum_quant", timeframe="1M",
            score=round(p["score"], 3),
            components=dict(
                mom_12m_skip1=round(p["m12"], 2) if p["m12"] is not None else None,
                mom_6m_skip1=round(p["m6"], 2) if p["m6"] is not None else None,
                z_12m=round(p["z12"], 2), z_6m=round(p["z6"], 2),
                z_scaled_turnover=round(p["zturn"], 2) if p["zturn"] is not None else None,
                atr_pct=p["atr"], scaled_turnover=round(p["scaled_turn"], 3)
                if p["scaled_turn"] is not None else None,
            ),
            **lv,
        ))
        if len(picks) >= top_n:
            break
    return dict(
        ok=True,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        universe=len(screen_rows), ranked=len(scored),
        eligible=len(eligible),
        rejected_by_gates=len(scored) - len(eligible),
        gate_failures=dict(reasons.most_common()), picks=picks,
        method=("Normalised momentum: 12-month and 6-month price returns, each "
                "skipping the most recent month and divided by the name's ATR, "
                "z-scored across the universe and averaged — NSE Indices' own "
                "Nifty500 Momentum 50 construction. Tilted toward low scaled "
                "turnover (BacktestIndia's 19-year NSE study found the Indian "
                "momentum premium lives almost entirely in the low-turnover "
                "half). Gated on absolute momentum: a name must be up over 12 "
                "months and above its 200-day, which is the standard defence "
                "against momentum crashes."),
        caveat=("PAPER ONLY. Sharma et al. (2021) find the Indian momentum "
                "anomaly fading, and the strongest supporting numbers are "
                "backtests. Not cleared for capital until 30 closed trades at "
                "t >= 2."),
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--screen", default="docs/screen.json")
    ap.add_argument("--out", default="")
    ap.add_argument("--top", type=int, default=TOP_N)
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    d = json.load(open(a.screen))
    rows = next((v for v in d.values()
                 if isinstance(v, list) and v and isinstance(v[0], dict)), None)
    if not rows:
        log.error("no rows in %s", a.screen)
        return 1

    res = build(rows, a.top)
    log.info("universe %d · ranked %d · eligible %d · rejected %d · picks %d",
             res["universe"], res["ranked"], res["eligible"],
             res["rejected_by_gates"], len(res["picks"]))
    for k, v in res["gate_failures"].items():
        log.info("   gate: %-46s %4d", k, v)
    for p in res["picks"]:
        c = p["components"]
        log.info("  %-12s score %+.2f  12m %+7.1f%%  6m %+7.1f%%  atr %.2f%%  "
                 "entry %.2f  sl %.2f (%.2f%%)  t1 %.2f",
                 p["symbol"], p["score"], c["mom_12m_skip1"] or 0,
                 c["mom_6m_skip1"] or 0, c["atr_pct"], p["entry"], p["sl"],
                 p["risk_pct"], p["target1"])
    if a.out:
        with open(a.out, "w") as f:
            json.dump(res, f, indent=1)
        log.info("wrote %s", a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
