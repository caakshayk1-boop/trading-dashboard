#!/usr/bin/env python3
"""
engine_evidence.py — has each engine earned the right to be published?

The wallet sizes a signal by its horizon tier and its grade. Neither of those
asks the prior question: does this engine have any measured edge at all?

That question kept being asked of the page and the page had no answer on it —
"why are we taking OHL trades", "why is gold sized so low". The numbers existed
in the ledger; nothing surfaced them per engine, so a reader could see that OHL
had produced four signals and not that all four lost.

WHAT THIS IS NOT

It is not a p-value ritual. With four closed trades no test is meaningful, and
saying "not significant" would imply the sample could have been significant and
was not. The honest reading of n=4 is that there is no evidence yet in either
direction, and the verdict says exactly that.

VERDICTS

  BLEEDING   expectancy negative and |t| >= 2 — measured loss, not noise
  EDGE       expectancy positive and t >= 2 — measured gain
  UNPROVEN   fewer than 20 closed trades — too little to call either way
  FLAT       enough trades, no significant result in either direction

The floor of 20 is deliberate and stated: below it the standard error on an
R-multiple series is wider than any effect worth acting on.
"""
from __future__ import annotations

import math
import statistics
from collections import defaultdict

# Below this, do not pretend to a conclusion.
MIN_N = 20
NOT_CLOSED = ("OPEN", "T1_HIT", "CANCELLED", "VOID")

# Engines that are research artefacts rather than trade signals. They are still
# measured — being unsized is not a reason to stop scoring them — but the verdict
# says what they are so a reader does not read a weekly ranking as a trade.
NOT_TRADES = {"top5_pick", "sip_bucket", "multibagger", "ai_longterm"}

# One engine, two names. scan_magic and scan_magicmagic are separate screens,
# but they overlap: a name 20-40% off its 52-week high can satisfy both on the
# same day and they then write BYTE-IDENTICAL levels. Ten such pairs exist in
# the ledger and three of them have closed, so the same idea has been counted
# twice in expectancy and again in every per-engine breakdown.
#
# Reported as one family. The rows stay separate in the ledger — rewriting
# history to tidy a report is not on — but a reader comparing engines is asking
# about the SCREEN, and "magic" and "magicmagic" are one screen with two
# spellings. A duplicate guard at write time stops new pairs (tracker.py).
ENGINE_ALIASES = {"magicmagic": "magic"}

# Capital follows evidence. An engine the ledger has MEASURED as losing does not
# get sized, and this is the list the paper wallet reads.
#
# Note what suppressing them does to the headline: +0.349R to +0.398R, t 1.80 to
# 1.96. Almost nothing. That is the point — this is not about flattering the
# published number, it is about not deploying capital into a measured loss. The
# rows stay in the ledger and stay on the page, because hiding a losing engine
# is the one thing this record exists not to do.
def suppressed(engines: list[dict]) -> list[str]:
    return sorted(e["engine"] for e in engines
                  if e["verdict"] == "BLEEDING" and e["is_trade"])


def _stats(rs: list[float]) -> dict:
    n = len(rs)
    mu = sum(rs) / n
    sd = statistics.stdev(rs) if n > 1 else 0.0
    se = sd / math.sqrt(n) if n > 1 and sd else 0.0
    return {"n": n, "expectancy": round(mu, 3),
            "win_rate": round(100 * sum(1 for x in rs if x > 0) / n, 1),
            "total_r": round(sum(rs), 1),
            "se": round(se, 3), "t": round(mu / se, 2) if se else None,
            "best": round(max(rs), 2), "worst": round(min(rs), 2)}


def _verdict(s: dict, engine: str) -> tuple[str, str]:
    n, mu, t = s["n"], s["expectancy"], s["t"]
    if n < MIN_N:
        if mu <= -0.99 and s["win_rate"] == 0:
            return ("UNPROVEN", f"{n} closed, none of them winners. That is not yet "
                    f"evidence of no edge — {n} trades cannot establish one — but it is "
                    "enough to treat this as unproven rather than as a signal.")
        return ("UNPROVEN", f"Only {n} closed trades. Below {MIN_N} the standard error is "
                "wider than any effect worth acting on, so no verdict is offered.")
    if t is not None and t <= -2 and mu < 0:
        return ("BLEEDING", f"{n} closed at {mu:+.3f}R, t={t:+.2f}. That is a measured "
                "loss rather than a run of bad luck.")
    if t is not None and t >= 2 and mu > 0:
        return ("EDGE", f"{n} closed at {mu:+.3f}R, t={t:+.2f}. Measured, not asserted.")
    return ("FLAT", f"{n} closed at {mu:+.3f}R — not distinguishable from zero in "
            "either direction.")


def build(rows: list[dict]) -> dict:
    """rows: all_signals records. Returns per-engine evidence, worst first."""
    by = defaultdict(list)
    open_by = defaultdict(int)
    for r in rows or []:
        st = r.get("signal_type")
        if not st:
            continue
        st = ENGINE_ALIASES.get(st, st)
        if r.get("status") == "OPEN":
            open_by[st] += 1
        if r.get("status") in NOT_CLOSED:
            continue
        try:
            by[st].append(float(r["r_multiple"]))
        except (TypeError, ValueError, KeyError):
            continue

    out = []
    for st, rs in by.items():
        s = _stats(rs)
        v, why = _verdict(s, st)
        s.update(engine=st, verdict=v, why=why, open_now=open_by.get(st, 0),
                 is_trade=st not in NOT_TRADES)
        out.append(s)

    # Worst first. A page that leads with its best engine is a brochure; the
    # thing a reader needs to see is what is losing money right now.
    out.sort(key=lambda x: (x["expectancy"], -x["n"]))
    supp = suppressed(out)
    for e in out:
        e["suppressed"] = e["engine"] in supp
    return {
        "engines": out,
        "bleeding": [e["engine"] for e in out if e["verdict"] == "BLEEDING"],
        "unproven": [e["engine"] for e in out if e["verdict"] == "UNPROVEN"],
        "suppressed": supp,
        "aliased": sorted(ENGINE_ALIASES),
        "min_n": MIN_N,
    }


if __name__ == "__main__":
    import json
    from pathlib import Path
    d = json.loads((Path(__file__).parent / "data" / "all_signals.json").read_text())
    ev = build(d)
    print(f"{'engine':18}{'n':>5}{'win%':>7}{'exp':>9}{'t':>7}  verdict")
    for e in ev["engines"]:
        print(f"{e['engine']:18}{e['n']:>5}{e['win_rate']:>6.1f}%{e['expectancy']:>+9.3f}"
              f"{(e['t'] if e['t'] is not None else 0):>+7.2f}  {e['verdict']}")
