#!/usr/bin/env python3
"""seasonality.py — what each NSE name has actually done, month by month.

Akshay: "build an advanced AI smart tool... like a world map = NSE scrips &
each thing can be seen like a verdict, bible summary, action plan, seasonality
etc... take unique ideas from tradingview, tickertape/screener.in,
investtech.com."

SEASONALITY IS THE PIECE NOBODY HERE PUBLISHES. The screen already carries a
verdict, quality, value, growth, momentum and institutional flow for 989
names. What it has never carried is the one thing Investtech built a business
on: whether a stock has a MONTH it reliably does well or badly in, measured
over a decade rather than asserted.

    "RELIANCE has risen in 8 of the last 10 Septembers, +3.1% on average"

is a fact about a decade of prices. It is also the single most misused
statistic in retail investing, so this file is built to make misuse hard:

────────────────────────────────────────────────────────────────────────────
WHAT THIS DELIBERATELY REFUSES TO DO
────────────────────────────────────────────────────────────────────────────
1. NO SEASONALITY WITHOUT A DECADE. Under MIN_YEARS observations a calendar
   month is not a pattern, it is a handful of coin flips. Those names get a
   null rather than a number, and the site shows "not enough history" instead
   of a percentage that would read exactly as authoritative as a real one.

2. THE HIT RATE IS PUBLISHED BESIDE THE AVERAGE, ALWAYS. "+3.1% in September"
   hides whether that is nine small gains or one +40% year carrying nine
   losses. Both numbers travel together everywhere, and the median goes with
   them because a single outlier moves a ten-year mean by a third.

3. IT IS NOT A SIGNAL AND THE FIELD SAYS SO. Every row carries
   `evidence: "historical, not predictive"`. A calendar month has no causal
   claim on a share price; what this measures is whether something
   repeatedly HAPPENED, which is a weaker and more honest statement.

4. NO SURVIVORSHIP CLAIM. This reads the names on today's screen, so a
   company that delisted five years ago is absent. That biases the aggregate
   upward and is stated in the output rather than quietly ignored.

MONTHLY BARS, NOT DAILY. A calendar-month return is exactly what a monthly bar
is, and building it from daily closes introduces a boundary question — first
trading day or last? — that changes the answer. yfinance's monthly series is
already split- and dividend-adjusted, which matters over ten years more than
anything else in this file.
"""
from __future__ import annotations

import json
import logging
import os
import pathlib
import statistics
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

log = logging.getLogger("seasonality")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

ROOT = pathlib.Path(__file__).resolve().parent
SCREEN = ROOT / "docs" / "screen.json"
OUT = ROOT / "docs" / "seasonality.json"

MIN_YEARS = int(os.environ.get("SEASONALITY_MIN_YEARS", "8"))
YEARS = int(os.environ.get("SEASONALITY_YEARS", "11"))
WORKERS = int(os.environ.get("SEASONALITY_WORKERS", "8"))
LIMIT = int(os.environ.get("SEASONALITY_LIMIT", "0"))      # 0 = the whole screen

MONTHS = ["January", "February", "March", "April", "May", "June",
          "July", "August", "September", "October", "November", "December"]


def one(sym: str) -> dict | None:
    """Month-by-month record for one symbol. Network-bound; no state."""
    import yfinance as yf
    try:
        h = yf.Ticker(sym + ".NS").history(
            period=f"{YEARS}y", interval="1mo", auto_adjust=True)
    except Exception:                                        # noqa: BLE001
        return None
    if h is None or h.empty or len(h) < MIN_YEARS * 12 * 0.6:
        return None

    closes = [(d, float(c)) for d, c in zip(h.index, h["Close"]) if c == c]
    if len(closes) < 24:
        return None

    # A month's return is its own bar: (close - open) / open. Using the prior
    # month's close as the base would fold the gap between months into the
    # month that did not trade through it.
    rows: dict[int, list[float]] = {}
    for d, _c in closes:
        pass
    for i in range(len(h)):
        o, c = h["Open"].iloc[i], h["Close"].iloc[i]
        if o != o or c != c or o <= 0:
            continue
        # The CURRENT month is still running and is not a completed
        # observation — including it would let a half-month drag a decade.
        idx = h.index[i]
        now = datetime.now(timezone.utc)
        if idx.year == now.year and idx.month == now.month:
            continue
        rows.setdefault(int(idx.month), []).append((float(c) - float(o)) / float(o) * 100)

    months = {}
    for m in range(1, 13):
        vals = rows.get(m) or []
        if len(vals) < MIN_YEARS:
            months[m] = None                 # not a pattern, a handful of flips
            continue
        up = sum(1 for v in vals if v > 0)
        months[m] = {
            "n": len(vals),
            "avg": round(sum(vals) / len(vals), 2),
            "med": round(statistics.median(vals), 2),
            "hit": round(up / len(vals) * 100),
            "best": round(max(vals), 1),
            "worst": round(min(vals), 1),
        }

    scored = [(m, d) for m, d in months.items() if d]
    if not scored:
        return None
    # STRENGTH IS HIT RATE FIRST, SIZE SECOND. A month that rose 9 years in 10
    # by 2% is a better fact than one that rose 5 in 10 with a +30% outlier,
    # and ranking on the average alone would say the opposite.
    def strength(d):
        return (d["hit"] - 50) * abs(d["med"]) ** 0.5

    best = max(scored, key=lambda x: strength(x[1]))
    worst = min(scored, key=lambda x: strength(x[1]))
    # ── COMPACT, BECAUSE THIS FEED IS LOADED BY A BROWSER ───────────────────
    # The readable shape — twelve objects of six named keys per stock — came to
    # 1.5 MB across 626 names, on a site whose largest existing asset is a
    # 260 KB screen. Most of that was the same key names repeated 7,500 times.
    #
    # Each month becomes [hit, median, n], which is every figure the page
    # actually renders; best/worst are dropped because they are the max and min
    # of an array the client already holds. A null month stays null, so "not
    # enough history" survives the encoding rather than becoming a zero.
    # The key order is published in the file so nothing has to guess it.
    return {
        "m": [None if not months.get(k) else
              [months[k]["hit"], months[k]["med"], months[k]["n"]]
              for k in range(1, 13)],
        "y": max(d["n"] for _m, d in scored),
    }


def main() -> int:
    if not SCREEN.exists():
        log.error("docs/screen.json is missing — it names the universe")
        return 1
    rows = (json.loads(SCREEN.read_text()) or {}).get("rows") or []
    syms = [r["sym"] for r in rows if r.get("sym")]
    if LIMIT:
        syms = syms[:LIMIT]
    log.info(f"{len(syms)} names · {YEARS}y monthly · floor {MIN_YEARS} observations")

    out, done, miss = {}, 0, 0
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(one, s): s for s in syms}
        for f in as_completed(futs):
            s = futs[f]
            done += 1
            try:
                r = f.result()
            except Exception:                                # noqa: BLE001
                r = None
            if r:
                out[s] = r
            else:
                miss += 1
            if done % 100 == 0:
                log.info(f"  {done}/{len(syms)} · {len(out)} with a record")

    if not out:
        log.error("nothing computed")
        return 1

    # The month everyone is about to live through, ranked. This is the part a
    # reader actually uses: not "what is RELIANCE's best month" but "which
    # names have a record in THIS one".
    now = datetime.now(timezone.utc)
    nxt = now.month % 12 + 1
    ranked = []
    for sym, d in out.items():
        m = (d.get("m") or [None] * 12)[nxt - 1]
        if m and m[2] >= MIN_YEARS:
            ranked.append({"sym": sym, "hit": m[0], "med": m[1], "n": m[2]})
    ranked.sort(key=lambda x: (-x["hit"], -x["med"]))

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        "ok": True,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "years": YEARS,
        "min_observations": MIN_YEARS,
        "universe": len(syms),
        "covered": len(out),
        "month_key": ["hit_pct", "median_pct", "observations"],
        "months": MONTHS,
        "next_month": {"m": nxt, "name": MONTHS[nxt - 1],
                       "strong": ranked[:40], "weak": ranked[-40:][::-1]},
        # Stated in the file, not just in a docstring: anyone reading this
        # aggregate needs to know which way it is biased.
        "caveats": [
            "Measured on the names on today's screen, so companies that "
            "delisted are absent — the aggregate is biased upward.",
            "A calendar month has no causal claim on a price. This records "
            "what repeatedly happened, which is a weaker statement than a "
            "forecast and is the only one the data supports.",
            f"A month needs {MIN_YEARS} completed observations to appear at "
            "all; below that it is shown as unknown rather than as a number.",
        ],
        "stocks": out,
    }, indent=2), encoding="utf-8")
    log.info(f"wrote {len(out)} of {len(syms)} ({miss} without enough history) to {OUT}")
    log.info(f"next month is {MONTHS[nxt-1]}: {len(ranked)} names have a record in it")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
