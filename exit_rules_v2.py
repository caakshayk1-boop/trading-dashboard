#!/usr/bin/env python3
"""
exit_rules_v2.py — is the stop too tight, or is the exit missing?

THE QUESTION, AND WHY THE OBVIOUS ANSWER IS SUSPECT
---------------------------------------------------
89 of 113 closed trades are SL_HIT and every winner is a T2_HIT. Read quickly
that says "the stops are too close". Read against the excursion data it says
something else: exit_rule_study.py records that the median LOSING trade was
+1.44R in profit before it reversed, and 59% reached a full +1R first.

A stop that is genuinely too tight is hit BEFORE the trade works. These trades
worked, gave the profit back, and then stopped. That is not a stop-distance
problem, it is the absence of any rule that banks a gain.

Which does not make "widen the stop" wrong — it makes it a hypothesis to test
rather than assume, so it is tested here alongside the alternatives.

WHY WIDENING IS NOT FREE, AND HOW THAT IS ENFORCED HERE
-------------------------------------------------------
A wider stop with the same position size is simply a bigger bet, and comparing
its rupees to the baseline's would flatter it. Risk per trade is what is held
constant in the real book, so a 2x wider stop means half the position — and a
win that reaches the same target price then pays HALF the R.

So R is always measured against the risk THAT RULE took:

    R = (exit - entry) / (mult * |entry - sl|)

That is the whole reason wide stops rarely rescue a losing system: they convert
some losers into winners and shrink every winner that already worked.

WHAT IS SIMULATED
-----------------
  baseline    entry -> fixed stop, target T2, time stop
  t1_only     same, but the target is T1 (T2 is reached 15 times in 113)
  partial50   half off at T1, stop to entry, remainder runs to T2
  wide1.5     stop 1.5x further out, target T2, R vs the wider risk
  wide2.0     stop 2.0x further out, target T2, R vs the wider risk

Every rule walks the SAME bars at the engine's own resolution, adverse event
first inside any bar. That assumption is unflattering and is applied to all
five rules equally, so it cannot decide the comparison.

Bars, timeframes and the trade list are taken from exit_rule_study.py rather
than reimplemented: a second copy of "which bars belong to this trade" is a
second thing to get wrong.

    python exit_rules_v2.py                 # every engine
    python exit_rules_v2.py --engine ohl    # one
"""
from __future__ import annotations

import argparse
import logging
import statistics
import sys

import pandas as pd

import tracker
from exit_rule_study import CLOSED, _f, bars_for

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("exit2")


def walk_rule(df, entry, sl, t1, t2, is_long, max_bars,
              stop_mult=1.0, target="t2", partial_at=None, partial_frac=0.5):
    """R booked by one rule over one trade.

    stop_mult    widens the stop away from entry; risk widens with it, so the
                 R denominator widens too. This is what stops a wider stop
                 from looking free.
    target       't1' or 't2' — which level ends the trade.
    partial_at   if set, book `partial_frac` of the position at this price and
                 move the stop to entry for the remainder.
    """
    risk0 = abs(entry - sl)
    if risk0 <= 0:
        return None
    risk = risk0 * stop_mult                     # the risk THIS rule takes
    stop = entry - risk if is_long else entry + risk
    exit_target = t2 if target == "t2" else t1
    if exit_target is None:
        return None

    booked = 0.0          # R already banked by a partial
    remaining = 1.0       # fraction of the position still open
    n = 0
    last_close = None

    for _ts, bar in df.iterrows():
        hi, lo = float(bar["High"]), float(bar["Low"])
        last_close = float(bar["Close"])
        n += 1

        # ADVERSE FIRST. Within one bar the true order is unknown, and assuming
        # the favourable event came first is how a backtest flatters itself.
        hit_stop = (lo <= stop) if is_long else (hi >= stop)
        if hit_stop:
            return booked + remaining * ((stop - entry) / risk * (1 if is_long else -1))

        # The partial is checked BEFORE the final target so a bar that clears
        # both books the partial at T1 and the rest at T2, which is what the
        # rule actually does.
        if partial_at is not None and remaining == 1.0:
            hit_p = (hi >= partial_at) if is_long else (lo <= partial_at)
            if hit_p:
                booked += partial_frac * ((partial_at - entry) / risk * (1 if is_long else -1))
                remaining = 1.0 - partial_frac
                stop = entry            # the remainder can no longer lose

        hit_target = (hi >= exit_target) if is_long else (lo <= exit_target)
        if hit_target:
            return booked + remaining * ((exit_target - entry) / risk * (1 if is_long else -1))

        if n >= max_bars:
            break

    if last_close is None:
        return None
    return booked + remaining * ((last_close - entry) / risk * (1 if is_long else -1))


# THE RULE ACTUALLY SHIPPED, RECONSTRUCTED PER TRADE.
#
# wide1.5/wide2.0 are uniform multiples of the RECORDED stop. The change made
# to scanner.py is not that: it sets the stop from the name's own ATR, scaled
# by the holding horizon, and re-derives the targets from it to hold the
# 1.5/2.5/4.0 ladder. For ohl that lands near wide1.8; for breakout it is about
# 4x, well outside the range those two columns cover — so reading breakout off
# them would be extrapolation dressed as measurement.
#
# ATR comes from screen.json, which is TODAY's value, not the value on the
# signal date. That is an approximation and it is the honest limit of this
# column: it assumes a name's volatility regime has not changed much over a
# few weeks. It is applied identically to every trade.
ATR_PCT = {}
def _load_atr():
    import json
    try:
        d = json.load(open("/Users/akshaykumarkothari/Workspace/Apps/Websites/"
                           "signal/public/screen.json"))
        rows = next(v for v in d.values() if isinstance(v, list) and v
                    and isinstance(v[0], dict))
        for r in rows:
            if r.get("atr_pct") is not None:
                ATR_PCT[str(r["sym"]).upper()] = float(r["atr_pct"])
    except Exception as e:
        log.warning(f"no ATR reference ({e}) — the shipped-rule column is skipped")
_HZ = {"MONTHLY": 22 ** 0.5, "WEEKLY": 5 ** 0.5, "1W": 5 ** 0.5, "1M": 22 ** 0.5}


def shipped_levels(sym, tf, entry, is_long):
    """(sl, t1, t2) under the rule now in scanner.py, or None if no ATR."""
    a = ATR_PCT.get(str(sym).replace(".NS", "").upper())
    if not a:
        return None
    hz = _HZ.get(str(tf or "").upper(), 1.0)
    stop_atr = 1.5 * hz
    risk = entry * (stop_atr * a / 100.0)
    cap = min(0.06 * hz, 0.20) * entry
    risk = min(risk, cap)
    if risk <= 0:
        return None
    sl = entry - risk if is_long else entry + risk
    t1 = entry + 1.5 * risk if is_long else entry - 1.5 * risk
    t2 = entry + 2.5 * risk if is_long else entry - 2.5 * risk
    return sl, t1, t2


RULES = {
    "baseline":  dict(),
    "t1_only":   dict(target="t1"),
    "partial50": dict(partial_at="T1"),
    "wide1.5":   dict(stop_mult=1.5),
    "wide2.0":   dict(stop_mult=2.0),
    "shipped":   dict(),          # levels rebuilt per trade, handled below
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", default="")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--source",
                    default="https://signal.askakshay.com/api/signals?limit=1000")
    args = ap.parse_args()

    # THE LEDGER IS IN TURSO, NOT IN THE LOCAL signals.db.
    #
    # exit_rule_study.py reads tracker._conn(), which falls back to local
    # sqlite when TURSO_URL is unset — and that file is empty on this machine,
    # so the query returned 0 rows and the study cheerfully reported nothing.
    # A study that silently walks zero trades is worse than one that fails.
    #
    # /api/signals serves the same table and needs no credentials, so the
    # trades come from there and the run is reproducible by anyone.
    import json as _json
    import urllib.request as _url
    if args.source.startswith("http"):
        with _url.urlopen(args.source, timeout=90) as r:
            payload = _json.load(r)
    else:
        payload = _json.load(open(args.source))
    rows = [r for r in payload.get("signals", [])
            if str(r.get("status", "")).upper() in CLOSED]
    if args.engine:
        rows = [r for r in rows if r.get("signal_type") == args.engine]
    if not rows:
        log.error("no closed trades matched — refusing to report on an empty set")
        return 1
    if args.limit:
        rows = rows[: args.limit]

    _load_atr()
    log.info(f"re-walking {len(rows)} closed trades on their own timeframe\n")

    results = {k: [] for k in RULES}
    per_engine: dict[str, dict[str, list]] = {}
    skipped = 0

    for s in rows:
        entry, sl = _f(s["entry"]), _f(s["sl"])
        t1, t2 = _f(s["target1"]), _f(s["target2"])
        if entry is None or sl is None or (t1 is None and t2 is None):
            skipped += 1
            continue
        # A trade with no T2 still has a T1; treat T1 as the exit for both.
        if t2 is None:
            t2 = t1
        if t1 is None:
            t1 = t2
        is_long = str(s.get("action", "BUY")).upper() != "SELL"
        # NO HORIZON, NO WALK. _max_hold_hours returns None for tf='1M'
        # (sip_bucket carries levels and no time stop), and bars_for divides by
        # it — so these crashed the run rather than being excluded from it.
        # They are genuinely outside this question: a monthly SIP allocation
        # has no exit rule to compare.
        if tracker._max_hold_hours((s["timeframe"] or "").upper()) is None:
            skipped += 1
            continue
        df = bars_for(s["symbol"], s["date"], s["timeframe"])
        if df is None or df.empty:
            skipped += 1
            continue

        tf = (s["timeframe"] or "").upper()
        hold_h = tracker._max_hold_hours(tf)
        max_bars = hold_h if tf in ("1H", "4H", "15M") else max(1, int(hold_h / 24))

        eng = s["signal_type"] or "other"
        got = {}
        for name, kw in RULES.items():
            if name == "shipped":
                lv = shipped_levels(s["symbol"], s["timeframe"], entry, is_long)
                got[name] = (walk_rule(df, entry, lv[0], lv[1], lv[2], is_long, max_bars)
                             if lv else None)
                continue
            kw = dict(kw)
            if kw.get("partial_at") == "T1":
                kw["partial_at"] = t1
            got[name] = walk_rule(df, entry, sl, t1, t2, is_long, max_bars, **kw)

        # All-or-nothing per trade: a trade that one rule cannot price must not
        # appear under another, or the columns stop being the same population.
        if any(v is None for v in got.values()):
            skipped += 1
            continue
        per_engine.setdefault(eng, {k: [] for k in RULES})
        for name, v in got.items():
            results[name].append(v)
            per_engine[eng][name].append(v)

    n = len(results["baseline"])
    log.info(f"walked {n} · {skipped} unusable\n")
    if not n:
        return 0

    def line(label, xs, base=None):
        if not xs:
            return
        wins = [x for x in xs if x > 0.01]
        m = statistics.mean(xs)
        sd = statistics.pstdev(xs) if len(xs) > 1 else 0.0
        se = (sd / len(xs) ** 0.5) if len(xs) > 1 else 0.0
        t = (m / se) if se else 0.0
        d = ""
        if base is not None and base:
            d = f"   vs base {m - statistics.mean(base):+.3f}R"
        log.info(f"  {label:11} exp {m:+.3f}R   total {sum(xs):+8.1f}R   "
                 f"win {len(wins) / len(xs) * 100:5.1f}%   t {t:+5.2f}{d}")

    log.info("ALL ENGINES")
    for name in RULES:
        line(name, results[name], None if name == "baseline" else results["baseline"])

    log.info("\nBY ENGINE")
    for eng, d in sorted(per_engine.items(), key=lambda kv: -len(kv[1]["baseline"])):
        if len(d["baseline"]) < 4:
            continue
        log.info(f"\n  {eng}  (n={len(d['baseline'])})")
        for name in RULES:
            line("    " + name, d[name], None if name == "baseline" else d["baseline"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
