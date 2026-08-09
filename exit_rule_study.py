#!/usr/bin/env python3
"""
exit_rule_study.py — would a break-even stop actually have helped?

The question
------------
Excursion data says the median LOSING trade was +1.44R in profit before it
reversed, and 59% reached a full +1R first. That looks like a stop problem. A
first pass put a break-even stop after +1R and reported expectancy moving from
-0.060R to +0.318R.

That pass was wrong in a specific, flattering way: it only altered losers.
Winners kept their recorded R. But a break-even stop does not only rescue
losers — it also SCRATCHES winners that dipped back through entry after going
+1R and then went on to reach target. Ignoring that half guarantees the answer
comes out positive.

What this does instead
----------------------
Re-walks every closed trade bar by bar and simulates BOTH rules on the SAME
bars, so the comparison is rule-vs-rule rather than simulation-vs-ledger:

    baseline   entry -> fixed stop / target / time stop
    breakeven  identical, except the stop moves to entry once price has
               traded `trigger` R in favour

Every trade is subject to the rule, in both directions.

Bar resolution
--------------
Daily bars cannot say whether +1R came before or after the return to entry
within one session, and that ordering is the entire question. So each signal is
walked at its own timeframe — 1h for the 1H and 4H engines, daily for the swing
engines. yfinance serves 1h history for ~730 days, which covers the whole
ledger.

Where ambiguity remains inside a single bar, the ADVERSE event is assumed
first. That is the unflattering assumption, and it is applied to both rules
equally so it cannot bias the comparison.

Usage
-----
    python exit_rule_study.py                 # all engines
    python exit_rule_study.py --engine cf_1h  # one engine
"""
from __future__ import annotations

import argparse
import logging
import statistics
from datetime import datetime, timedelta

import pandas as pd
import yfinance as yf

import tracker
from symbols import to_yahoo

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("exits")

CLOSED = ("SL_HIT", "STOPPED", "T1_HIT", "T2_HIT", "TARGET_HIT", "TIME_STOP", "EXPIRED")
TRIGGERS = (0.5, 1.0, 1.5, 2.0)


def _f(v):
    try:
        f = float(v)
        return f if f == f else None
    except (TypeError, ValueError):
        return None


def bars_for(symbol: str, sig_date: str, timeframe: str):
    """Bars from the session after the signal, at the engine's own resolution."""
    tf = (timeframe or "").upper()
    interval = "1h" if tf in ("1H", "4H", "15M") else "1d"
    hold_h = tracker._max_hold_hours(tf)
    try:
        start = datetime.fromisoformat(str(sig_date)[:10]).date() + timedelta(days=1)
    except ValueError:
        return None
    end = min(datetime.now().date(),
              start + timedelta(days=int(hold_h / 24 * 1.6) + 3)) + timedelta(days=1)
    if start >= end:
        return None
    try:
        df = yf.download(to_yahoo(symbol), start=start.isoformat(), end=end.isoformat(),
                         interval=interval, progress=False, auto_adjust=True)
    except Exception:
        return None
    if df is None or df.empty:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df


def walk(df, entry, sl, target, is_long, max_bars, trigger=None):
    """R booked by one rule over one trade.

    trigger=None is the baseline. Otherwise the stop moves to entry once price
    has traded `trigger` R in favour.
    """
    risk = abs(entry - sl)
    if risk <= 0:
        return None
    stop = sl
    armed = False
    n = 0
    last_close = None

    for _ts, bar in df.iterrows():
        hi, lo = float(bar["High"]), float(bar["Low"])
        last_close = float(bar["Close"])
        n += 1

        # Adverse first, always. Within one bar the true order is unknown, and
        # assuming the favourable event came first is how a backtest flatters
        # itself. Applied to both rules, so it cannot skew the comparison.
        hit_stop = (lo <= stop) if is_long else (hi >= stop)
        if hit_stop:
            return (stop - entry) / risk * (1 if is_long else -1)

        hit_target = (hi >= target) if is_long else (lo <= target)
        if hit_target:
            return (target - entry) / risk * (1 if is_long else -1)

        # Arm the break-even stop only AFTER this bar's exits are resolved, so
        # a bar that both triggers and reverses cannot be rescued by a stop
        # that was not yet in place when the reversal happened.
        if trigger is not None and not armed:
            fav = (hi - entry) if is_long else (entry - lo)
            if fav >= trigger * risk:
                armed = True
                stop = entry

        if n >= max_bars:
            break

    if last_close is None:
        return None
    return (last_close - entry) / risk * (1 if is_long else -1)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", default="")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    tracker.init_db()
    in_list = ",".join(f"'{s}'" for s in CLOSED)
    where = f"upper(coalesce(status,'')) IN ({in_list})"
    if args.engine:
        where += f" AND signal_type = '{args.engine}'"
    with tracker._conn() as c:
        rows = pd.read_sql(
            f"SELECT id, symbol, date, action, entry, sl, target1, target2, timeframe, "
            f"signal_type, r_multiple FROM all_signals WHERE {where}", c
        ).to_dict("records")
    if args.limit:
        rows = rows[: args.limit]

    log.info(f"re-walking {len(rows)} closed trades on their own timeframe\n")

    results = {"baseline": []}
    for t in TRIGGERS:
        results[f"be@{t}"] = []
    per_engine: dict[str, dict[str, list]] = {}
    skipped = 0

    for s in rows:
        entry, sl = _f(s["entry"]), _f(s["sl"])
        t2, t1 = _f(s["target2"]), _f(s["target1"])
        target = t2 if t2 is not None else t1
        if entry is None or sl is None or target is None:
            skipped += 1
            continue
        is_long = str(s.get("action", "BUY")).upper() != "SELL"
        df = bars_for(s["symbol"], s["date"], s["timeframe"])
        if df is None or df.empty:
            skipped += 1
            continue

        tf = (s["timeframe"] or "").upper()
        hold_h = tracker._max_hold_hours(tf)
        max_bars = hold_h if tf in ("1H", "4H", "15M") else max(1, int(hold_h / 24))

        base = walk(df, entry, sl, target, is_long, max_bars, None)
        if base is None:
            skipped += 1
            continue
        eng = s["signal_type"] or "other"
        per_engine.setdefault(eng, {k: [] for k in results})
        results["baseline"].append(base)
        per_engine[eng]["baseline"].append(base)
        for t in TRIGGERS:
            r = walk(df, entry, sl, target, is_long, max_bars, t)
            if r is not None:
                results[f"be@{t}"].append(r)
                per_engine[eng][f"be@{t}"].append(r)

    n = len(results["baseline"])
    log.info(f"walked {n} · {skipped} unusable\n")
    if not n:
        return 0

    def line(label, xs):
        if not xs:
            return
        wins = [x for x in xs if x > 0.01]
        scratch = [x for x in xs if -0.01 <= x <= 0.01]
        log.info(f"  {label:12} exp {statistics.mean(xs):+.3f}R   total {sum(xs):+8.1f}R   "
                 f"win {len(wins)/len(xs)*100:5.1f}%   scratch {len(scratch)/len(xs)*100:5.1f}%")

    log.info("── ALL ENGINES ─────────────────────────────────────────────────")
    line("baseline", results["baseline"])
    for t in TRIGGERS:
        line(f"be@{t}R", results[f"be@{t}"])

    base_exp = statistics.mean(results["baseline"])
    best_key = max((k for k in results if k != "baseline"),
                   key=lambda k: statistics.mean(results[k]) if results[k] else -9)
    best_exp = statistics.mean(results[best_key])
    log.info(f"\n  best rule: {best_key}  ({best_exp - base_exp:+.3f}R per trade vs baseline)")

    log.info("\n── BY ENGINE ───────────────────────────────────────────────────")
    for eng, d in sorted(per_engine.items(), key=lambda kv: -len(kv[1]["baseline"])):
        if len(d["baseline"]) < 10:
            continue
        b = statistics.mean(d["baseline"])
        bk = max((k for k in d if k != "baseline"),
                 key=lambda k: statistics.mean(d[k]) if d[k] else -9)
        log.info(f"  {eng:18} n={len(d['baseline']):4}  baseline {b:+.3f}R  "
                 f"best {bk} {statistics.mean(d[bk]):+.3f}R  "
                 f"({statistics.mean(d[bk]) - b:+.3f}R)")

    log.info("\n  Simulated both rules on the same bars, adverse-first inside any bar,")
    log.info("  winners subject to the break-even stop exactly as losers are.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
