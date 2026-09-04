#!/usr/bin/env python3
"""One verdict per stock, from evidence the screen already computed.

WHAT THIS IS. The 750-name screen produces 89 fields per row and three mode
scores (investor / positional / swing). That is a lot of correct information
and no answer. `setup_label` came closest, but on the 2026-09-04 build 203 of
750 rows returned empty tags AND empty horizons — no reading at all — and
nothing anywhere produced "don't touch this" or "right business, wrong entry".

This module answers four questions and nothing else:

    Is it tradeable at all?            -> AVOID, with the flag that decided it
    If yes, over what horizon?         -> LONG TERM / POSITIONAL / SWING
    Is now the entry?                  -> BUY vs WAIT
    What would change the answer?      -> the trigger

WHAT THIS IS NOT. Not a prediction, not a price target, not a backtested
signal, and it carries no expectancy. It is a rules-based reading of published
fundamentals and price data, with every threshold visible in this file and
every conclusion carrying the number that produced it. The screen's own engines
have no measured edge at the sample sizes available, and a verdict that implied
one would be the same mistake as publishing a profit factor built on stale
entry prices.

DESIGN RULES.
  1. Every verdict cites its numbers. A reason without a figure is not a reason.
  2. Gates are ordered: tradeability, then accounts, then thesis, then entry.
     A name that fails an earlier gate never reaches a later one.
  3. Missing data is never a pass. An unknown ratio blocks the horizon that
     needs it and says so, rather than defaulting to zero.
  4. WAIT is a real answer, not a hedge. It means the thesis holds and the
     entry does not, and it names the level that would change that.

Input is the FINAL published row from screen.json, so the generator, the tests
and the site all read identical keys. No imports beyond the stdlib, so it can
be exercised without a DB or a network.
"""

from __future__ import annotations

# ─────────────────────────────────────────────────────────────────────────────
# THRESHOLDS — every number the verdict can turn on, in one place.
# Changing a value here changes the published verdict, so they are named and
# grouped rather than inlined at the point of use.
# ─────────────────────────────────────────────────────────────────────────────

# Tradeability
MIN_TURNOVER_CR = 1.0      # Rs cr/day. Below this the spread is the trade.
MIN_TURNOVER_SWING_CR = 5.0  # a swing needs to get out in one session

# Accounts (the AVOID gates)
CFO_PAT_FLOOR = 0.0        # profit that never became cash
ICOVER_FLOOR = 1.5         # EBIT/interest below this is distress
DE_DISTRESS = 2.0          # only distress WITH thin cover

# Long-term thesis
LT_ROCE = 15.0
LT_REV_CAGR = 10.0
LT_CFO_PAT = 0.6
LT_DE_MAX = 1.5
LT_VALUE_FLOOR = 25.0      # v score; below this it is expensive vs its peers

# Positional thesis (1-3 months)
POS_RS3M = 0.0             # must be beating the index over 3 months
POS_ABOVE_MAS = 3          # above all three moving averages

# Swing thesis (days to weeks)
SWING_VOL_SPIKE = 1.5
SWING_ATR_MIN = 1.5        # below this there is no range to pay for the risk
SWING_ATR_MAX = 8.0        # above this the stop is wider than the edge

# Entry quality — these turn a BUY into a WAIT
RSI_HOT = 72.0
RSI_VERY_HOT = 78.0
RSI_OVERSOLD = 30.0
EXTENDED_FROM_HIGH = -3.0  # within 3% of the 52w high
EXTENDED_OVER_SMA50 = 15.0  # % above the 50-day

CALLS = ("AVOID", "BUY", "WAIT", "WATCH", "UNRATED")
HORIZONS = ("long term", "positional", "swing")


def _num(v):
    """Return v as a float, or None. Missing data must never read as zero."""
    if isinstance(v, bool) or v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f  # NaN


def _fmt(v, unit="", dp=1):
    n = _num(v)
    return "n/a" if n is None else f"{n:.{dp}f}{unit}"


# Sectors where Ind AS 116 capitalises operating leases onto the balance sheet,
# so D/E is inflated by the lease liability and is not comparable to a
# manufacturer's. IndiGo carries D/E 11.15 because its aircraft are there.
# The debt-service gate needs a higher bar here, and must say why.
LEASE_HEAVY = ("airline", "airport", "hotel", "restaurant", "retail",
               "real estate", "reit")
DE_DISTRESS_LEASE_HEAVY = 6.0


def _lease_heavy(row) -> bool:
    s = f"{row.get('sector') or ''} {row.get('ind') or ''}".lower()
    return any(k in s for k in LEASE_HEAVY)


def _is_financial(row) -> bool:
    """Banks and NBFCs carry leverage as inventory. D/E and current ratio are
    not solvency signals for them, so those gates are skipped."""
    s = f"{row.get('sector') or ''} {row.get('ind') or ''}".lower()
    return any(k in s for k in ("bank", "financ", "nbfc", "insur", "capital market"))


# ─────────────────────────────────────────────────────────────────────────────
# GATE 1 — tradeability and accounts. Failing any of these is AVOID.
# ─────────────────────────────────────────────────────────────────────────────

def cash_flag(row):
    """The cash-conversion red flag, or None.

    Reads the LATEST year's absolute CFO (`cfo_cr`), never the CFO/PAT median:
    a ratio whose denominator can be negative cannot be compared to a floor.
    """
    if _is_financial(row):
        return None                      # a lender's CFO is its loan book
    cfo = _num(row.get("cfo_cr"))
    if cfo is None or cfo >= 0:
        return None
    pat_note = ""
    ratio = _num(row.get("cfo_pat"))
    if ratio is not None and ratio < 0:
        pat_note = f", {ratio:.2f}x CFO/PAT across the history"
    return {
        "why": "Operations consumed cash in the latest year",
        "evidence": f"FY operating cash flow {cfo:,.0f} cr{pat_note}",
        "sev": "high",
    }


def _blockers(row) -> list[dict]:
    """Reasons this name should not be acted on AT ALL, each with its figure.

    Deliberately short. A blocker has to be structural -- untradeable, or the
    balance sheet cannot support the business -- not merely unattractive.
    Everything softer is a red flag on the card and a failed thesis, which is
    a different and more honest statement than "ignore this".
    """
    out = []
    fin = _is_financial(row)

    turn = _num(row.get("turnover_cr"))
    if row.get("liquid") is False or (turn is not None and turn < MIN_TURNOVER_CR):
        out.append({
            "why": "Too thin to trade",
            "evidence": f"{_fmt(turn, ' cr/day')} turnover, floor is {MIN_TURNOVER_CR:.0f} cr",
        })

    de = _num(row.get("de"))
    if de is not None and de < 0 and not fin:
        out.append({
            "why": "Negative shareholders' equity",
            "evidence": f"D/E {de:.2f} on a negative net worth",
        })

    # Cash conversion is NOT an AVOID gate. It was, and it was wrong three
    # different ways on the 2026-09-04 build:
    #
    #   INDIGO   CFO +23,470 cr, PAT -2,392 cr -> cfo_pat reads -3.61 purely
    #            because the DENOMINATOR is negative. The strongest cash
    #            generator in the sample was flagged as an accounting risk.
    #   M&M      cfo_pat is a MEDIAN across years. FY23/FY24 were negative,
    #            FY26 is +11,657 cr on 17,099 cr of profit. The company fixed
    #            it; the median had not caught up.
    #   GRASIM   genuinely negative CFO for four straight years -- because
    #            Aditya Birla Capital's loan book consolidates into it, and
    #            loan growth is an operating outflow under Ind AS. A fact
    #            worth showing; not evidence of fraud.
    #
    # So: report the figure, block the long-term thesis (LT_CFO_PAT still
    # applies there), and let the reader see the number. "Ignore this stock"
    # is an inference the data does not support for any of the three.
    # Skipped entirely for lenders: interest expense is a bank's cost of goods,
    # not a fixed charge, and D/E 7 is an ordinary capital structure. Applying
    # this gate to financials marked healthy banks "Ignore" — caught by
    # test_verdict.py, not by reading the live output, because the two banks it
    # would have hit were already failing the return bar for other reasons.
    ic = None if fin else _num(row.get("icover"))
    lease = _lease_heavy(row)
    de_bar = DE_DISTRESS_LEASE_HEAVY if lease else DE_DISTRESS
    if ic is not None and ic < ICOVER_FLOOR and de is not None and de >= de_bar:
        note = (" — note Ind AS 116 puts this sector's leases on the balance "
                "sheet, so D/E overstates borrowings") if lease else ""
        out.append({
            "why": "Cannot service its debt from operating profit",
            "evidence": f"interest cover {ic:.1f}x on D/E {de:.2f}{note}",
        })

    return out


# ─────────────────────────────────────────────────────────────────────────────
# GATE 2 — the three theses. Each returns (ok, reasons, missing).
# ─────────────────────────────────────────────────────────────────────────────

def _thesis_long(row):
    reasons, fails, missing = [], [], []
    fin = _is_financial(row)

    if row.get("has_stmts") is False or (_num(row.get("fy_count")) or 0) < 2:
        missing.append("fewer than two years of statements")
        return False, reasons, fails, missing

    # ROCE is not meaningful for a lender -- borrowing IS the raw material, so
    # "capital employed" has no clean denominator. 56 of 121 financials in the
    # universe have no ROCE at all, which under the old rule made a long-term
    # verdict structurally unreachable for every bank and NBFC on the screen.
    # ROE is the return measure the sector is actually judged on.
    metric, label = ("roe", "ROE") if fin else ("roce", "ROCE")
    ret = _num(row.get(metric))
    if ret is None:
        missing.append(f"{label} not computable")
    elif ret >= LT_ROCE:
        reasons.append(f"Earns {ret:.1f}% ({label}), above the {LT_ROCE:.0f}% bar")
    else:
        fails.append(f"{label} {ret:.1f}% is below the {LT_ROCE:.0f}% bar")

    cagr = _num(row.get("rev_cagr"))
    if cagr is None:
        missing.append("revenue CAGR not computable")
    elif cagr >= LT_REV_CAGR:
        reasons.append(f"Revenue compounding at {cagr:.1f}%")
    else:
        fails.append(f"revenue CAGR {cagr:.1f}% is below {LT_REV_CAGR:.0f}%")

    # Judge cash on the LATEST year, like cash_flag() does. Using the CFO/PAT
    # median here is what made M&M read "only -0.13x of profit reached cash"
    # in a year it generated 11,657 cr of operating cash on 17,099 cr of
    # profit: FY23 and FY24 were negative and the median had not caught up.
    # The ratio is still reported, because a recovering series is worth
    # seeing -- it just no longer decides the verdict on its own.
    if not fin:
        cfo_cr = _num(row.get("cfo_cr"))
        ratio = _num(row.get("cfo_pat"))
        pat = _num(row.get("pat_yoy"))
        if cfo_cr is None and ratio is None:
            missing.append("cash conversion not computable")
        elif cfo_cr is not None and cfo_cr < 0:
            fails.append(f"operations consumed {abs(cfo_cr):,.0f} cr of cash last year")
        elif ratio is not None and ratio >= LT_CFO_PAT:
            reasons.append(f"Profits convert to cash at {ratio:.2f}x CFO/PAT")
        elif cfo_cr is not None and cfo_cr > 0 and (ratio is None or ratio < LT_CFO_PAT):
            reasons.append(
                f"Generated {cfo_cr:,.0f} cr of operating cash last year"
                + (f" (history reads {ratio:.2f}x CFO/PAT — improving)" if ratio is not None else ""))

    de = _num(row.get("de"))
    if de is not None and not fin:
        if de <= LT_DE_MAX:
            reasons.append(f"Balance sheet carries {de:.2f}x debt to equity")
        else:
            fails.append(f"D/E {de:.2f} above the {LT_DE_MAX} limit")

    v = _num(row.get("v"))
    if v is not None:
        if v >= LT_VALUE_FLOOR:
            pe = _num(row.get("pe"))
            reasons.append(
                f"Not expensive against its industry (value score {v:.0f}"
                + (f", PE {pe:.1f}" if pe is not None else "") + ")")
        else:
            fails.append(f"priced in the expensive tail of its industry (value {v:.0f})")

    if row.get("roce_trend") == "falling":
        fails.append(f"return on capital is falling ({_fmt(row.get('roce'), '%')} "
                     f"vs {_fmt(row.get('roce_med'), '%')} median)")

    return (not fails and not missing and len(reasons) >= 3), reasons, fails, missing


def _thesis_positional(row):
    reasons, fails, missing = [], [], []

    above = _num(row.get("above_mas"))
    if above is None:
        missing.append("moving averages unavailable")
    elif row.get("stack") and above >= POS_ABOVE_MAS:
        reasons.append("Trading above the 20, 50 and 200-day, stacked in order")
    else:
        fails.append(f"trend not intact — above {int(above or 0)} of 3 moving averages")

    rs = _num(row.get("rs3m"))
    if rs is None:
        missing.append("3-month relative strength unavailable")
    elif rs > POS_RS3M:
        reasons.append(f"Beating the index by {rs:.1f}pt over three months")
    else:
        fails.append(f"lagging the index by {abs(rs):.1f}pt over three months")

    if row.get("em_label") == "accelerating":
        reasons.append(f"Earnings momentum accelerating ({_fmt(row.get('em'))} score)")
    elif row.get("em_label") == "decelerating":
        fails.append("earnings momentum decelerating")

    return (not fails and not missing and len(reasons) >= 2), reasons, fails, missing


def _thesis_swing(row):
    reasons, fails, missing = [], [], []

    turn = _num(row.get("turnover_cr"))
    if turn is None:
        missing.append("turnover unavailable")
    elif turn < MIN_TURNOVER_SWING_CR:
        fails.append(f"{turn:.1f} cr/day is too thin to exit in one session")

    brk = next((lbl for key, lbl in (("brk52w", "52-week"), ("brk50", "50-day"),
                                     ("brk20", "20-day")) if row.get(key)), None)
    if brk:
        reasons.append(f"Broke its {brk} high")
    else:
        fails.append("no breakout — nothing has triggered")

    vol = _num(row.get("vol_spike"))
    if vol is None:
        missing.append("volume ratio unavailable")
    elif vol >= SWING_VOL_SPIKE:
        reasons.append(f"On {vol:.1f}x normal volume")
    else:
        fails.append(f"volume only {vol:.1f}x normal — the move is unconfirmed")

    atr = _num(row.get("atr_pct"))
    if atr is None:
        missing.append("ATR unavailable, so no stop can be sized")
    elif atr < SWING_ATR_MIN:
        fails.append(f"daily range {atr:.1f}% is too small to pay for the risk")
    elif atr > SWING_ATR_MAX:
        fails.append(f"daily range {atr:.1f}% puts the stop wider than the edge")
    else:
        reasons.append(f"Daily range {atr:.1f}% sizes a workable stop")

    return (not fails and not missing and len(reasons) >= 3), reasons, fails, missing


# ─────────────────────────────────────────────────────────────────────────────
# GATE 3 — entry. A held thesis with a poor entry is WAIT, not BUY.
# ─────────────────────────────────────────────────────────────────────────────

def _entry_problem(row, horizon):
    """Return (why, trigger) when the thesis holds but this is not the entry."""
    rsi = _num(row.get("rsi"))
    from_high = _num(row.get("from_high"))
    price = _num(row.get("price"))
    sma50 = _num(row.get("sma50"))

    if rsi is not None and rsi >= RSI_VERY_HOT:
        return (f"RSI {rsi:.0f} — bought out in the short run",
                f"a pullback toward the 20-day ({_fmt(row.get('sma20'), '', 0)}) "
                f"or RSI back under {RSI_HOT:.0f}")

    if horizon == "long term":
        over = None
        if price and sma50:
            over = (price - sma50) / sma50 * 100
        if over is not None and over > EXTENDED_OVER_SMA50:
            return (f"Price is {over:.0f}% above its 50-day — paying up for a "
                    f"thesis that does not need today's price",
                    f"a retest of the 50-day near {sma50:.0f}")
        if rsi is not None and rsi >= RSI_HOT and from_high is not None \
                and from_high >= EXTENDED_FROM_HIGH:
            return (f"At the 52-week high on RSI {rsi:.0f}",
                    "a consolidation, or the next results print")

    if horizon == "positional" and rsi is not None and rsi >= RSI_HOT:
        return (f"RSI {rsi:.0f} into resistance",
                f"RSI back under {RSI_HOT:.0f} with the 20-day holding")

    return None, None


def _near_miss(row):
    """A swing that is forming but has not triggered — the honest WAIT case."""
    price, sma20 = _num(row.get("price")), _num(row.get("sma20"))
    vol = _num(row.get("vol_spike"))
    high52 = _num(row.get("high52"))
    from_high = _num(row.get("from_high"))

    if row.get("stack") and price and sma20 and price > sma20 \
            and (vol is None or vol < SWING_VOL_SPIKE) \
            and from_high is not None and -12 <= from_high <= -2:
        return (f"Trend is intact and price sits {abs(from_high):.0f}% under the "
                f"52-week high, but nothing has broken and volume is "
                f"{_fmt(vol, 'x')} normal",
                f"a close above {high52:.0f} on {SWING_VOL_SPIKE:.1f}x volume"
                if high52 else "a breakout on volume")
    return None, None


# ─────────────────────────────────────────────────────────────────────────────
# THE VERDICT
# ─────────────────────────────────────────────────────────────────────────────

def verdict(row: dict) -> dict:
    """One verdict for one published screen row."""
    red = _blockers(row)
    cash = cash_flag(row)
    if cash and not red:
        red_soft = [cash]
    else:
        red_soft = []
    if red:
        return {
            "call": "AVOID",
            "label": "Ignore",
            "horizon": None,
            # The evidence is rendered separately in the red-flag block below
            # the headline, so repeating it here printed the same sentence
            # twice on every AVOID card.
            "one_line": red[0]["why"],
            "because": [],
            "against": [f"{b['why']} ({b['evidence']})" for b in red],
            "red_flags": red + ([cash] if cash else []),
            "trigger": None,
            "confidence": "high",
        }

    theses = {
        "long term": _thesis_long(row),
        "positional": _thesis_positional(row),
        "swing": _thesis_swing(row),
    }
    mode_key = {"long term": "m_inv", "positional": "m_pos", "swing": "m_swing"}
    passing = [h for h in HORIZONS if theses[h][0]]

    if passing:
        # Several theses can hold at once. The headline goes to the one the
        # screen already scores highest, so the verdict and the mode scores on
        # the same card cannot disagree.
        best = max(passing, key=lambda h: _num(row.get(mode_key[h])) or 0)
        ok, reasons, fails, missing = theses[best]
        why, trigger = _entry_problem(row, best)
        others = [h for h in passing if h != best]

        if why:
            return {
                "call": "WAIT",
                "label": f"Good {best} case, wrong entry",
                "horizon": best,
                "one_line": why,
                "because": reasons,
                "against": [why],
                "red_flags": red_soft,
                "trigger": trigger,
                "confidence": _confidence(row, best),
                "also": others,
            }

        return {
            "call": "BUY",
            "label": {"long term": "Long term", "positional": "Positional (1-3m)",
                      "swing": "Swing (days-weeks)"}[best],
            "horizon": best,
            "one_line": reasons[0] if reasons else "",
            "because": reasons,
            "against": [],
            "red_flags": red_soft,
            "trigger": None,
            "confidence": _confidence(row, best),
            "also": others,
        }

    # Nothing passes. Is anything close?
    why, trigger = _near_miss(row)
    if why:
        return {
            "call": "WAIT",
            "label": "Setting up, not triggered",
            "horizon": "swing",
            "one_line": why,
            "because": [],
            "against": [why],
            "red_flags": red_soft,
            "trigger": trigger,
            "confidence": "medium",
        }

    # Report the thesis that came closest, so "no" still says what is missing.
    closest = max(HORIZONS, key=lambda h: len(theses[h][1]))
    _, reasons, fails, missing = theses[closest]
    short = (fails + missing)[:3]
    return {
        "call": "WATCH" if reasons else "UNRATED" if missing and not fails else "WATCH",
        "label": "Nothing to act on",
        "horizon": None,
        "one_line": ("Closest to a " + closest + " case, held back by "
                     + short[0]) if short else "No thesis holds today",
        "because": reasons,
        "against": short,
        "red_flags": red_soft,
        "trigger": None,
        "confidence": "low" if missing else "medium",
    }


def _confidence(row, horizon) -> str:
    """How much the underlying data supports the call — not how likely it is
    to work. Driven by the screen's own per-block confidences."""
    keys = {"long term": ("q_conf", "g_conf", "v_conf"),
            "positional": ("tech_conf", "em_conf"),
            "swing": ("tech_conf",)}[horizon]
    vals = [c for c in (_num(row.get(k)) for k in keys) if c is not None]
    if not vals:
        return "low"
    avg = sum(vals) / len(vals)
    return "high" if avg >= 0.9 else "medium" if avg >= 0.7 else "low"


def _compact(v: dict) -> dict:
    """Trim the verdict for publication.

    screen.json is already a 230 KB download and the site fetches it on the
    first paint. A full verdict on 750 rows adds ~350 KB, which is a real cost
    on a phone. Three reasons is what a reader acts on; the rest is in the
    numbers already on the card. Empty keys are dropped rather than published
    as nulls.
    """
    out = {"c": v["call"], "l": v["label"], "o": v["one_line"]}
    # `because` / `against` are deliberately NOT published. The card already
    # renders why_now and risk.flags from this same row, and the verdict's
    # reasons restate them — carrying both put 251 KB onto a 230 KB file that
    # the front page fetches on first paint. The headline, the horizon, the
    # confidence and the trigger are what the reader cannot get elsewhere.
    if v.get("horizon"):
        out["h"] = v["horizon"]
    if v.get("confidence"):
        out["k"] = v["confidence"]
    if v.get("trigger"):
        out["t"] = v["trigger"]
    if v.get("red_flags"):
        out["f"] = [{"w": f["why"], "e": f["evidence"]} for f in v["red_flags"][:2]]
    if v.get("also"):
        out["a"] = v["also"]
    return out


def annotate(rows: list[dict], compact: bool = True) -> dict:
    """Attach `vd` to every row in place; return a tally for the header."""
    tally = {c: 0 for c in CALLS}
    for r in rows:
        v = verdict(r)
        # One key only. An alias here looks free in Python (same object) and
        # is NOT free in JSON — json.dumps serialises it twice, which is how a
        # 112 KB addition measured 222 KB on disk.
        r["vd"] = _compact(v) if compact else v
        tally[v["call"]] = tally.get(v["call"], 0) + 1
    return tally


if __name__ == "__main__":
    import json
    import sys
    from collections import Counter

    path = sys.argv[1] if len(sys.argv) > 1 else "docs/screen.json"
    rows = json.load(open(path))["rows"]
    tally = annotate(rows)
    print(f"{len(rows)} rows\n")
    for call, n in sorted(tally.items(), key=lambda kv: -kv[1]):
        if n:
            print(f"  {call:8} {n:4d}  {n / len(rows) * 100:5.1f}%")
    print("\n  by horizon:",
          dict(Counter(r["vd"].get("h") for r in rows
                       if r["vd"]["c"] in ("BUY", "WAIT"))))
    print("\nSamples:")
    for call in ("BUY", "WAIT", "AVOID", "WATCH"):
        ex = next((r for r in rows if r["vd"]["c"] == call), None)
        if ex:
            v = ex["vd"]
            print(f"\n  [{call}] {ex['sym']} — {v['l']}  ({v.get('k')} confidence)")
            print(f"    {v['o']}")
            for b in v.get("y", []):
                print(f"      + {b}")
            for a in v.get("n", []):
                print(f"      - {a}")
            if v.get("t"):
                print(f"    trigger: {v['t']}")
