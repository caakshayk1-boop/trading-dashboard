"""
vision_scan.py — the two Vision signals, over the stock screen's universe.

Fetches daily and hourly bars for every name on the screen (docs/screen.json,
~1,000 NSE names), runs the two rules in scanner.py — vision_bottom_reversal
and vision_4h_breakout — and writes feeds/vision_signals.json, which the signal
repo mirrors and vision.askakshay.com reads.

WHAT IT KEEPS, AND WHY
----------------------
A signal is FILED once, with its levels fixed at the bar that fired it. A name
that still qualifies the next day is the same setup, not a new one, so it is
not refiled while its first filing is open — the levels a reader acted on do
not quietly move under them. Every filed signal is then GRADED on each run from
the bars after it fired: targets reached, stopped, expired at its horizon. A
bar that touches the stop and a new target is booked as the stop and flagged
(vision_grade says why).

That history is the only record these rules have. They are new and untested;
the feed publishes counts, never a win rate or an expectancy, until there are
enough closed trades for either to mean something (30, the book's own bar).

WHY feeds/ AND NOT docs/
------------------------
Every commit touching docs/ deploys the newspaper on Vercel (vercel.json's
ignoreCommand), and deployments are what filled the 10 GB storage. This runs
twice a trading day; it writes where Vercel does not look.

Usage:
    python3 vision_scan.py              # full screen universe
    python3 vision_scan.py --limit 40   # a slice, for a smoke test
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
FEED = os.path.join(HERE, "feeds", "vision_signals.json")
SCREEN = os.path.join(HERE, "docs", "screen.json")
LOG = os.path.join(HERE, "logs", "vision_scan.log")
HISTORY_CAP = 400

RULES = {
    "bottom": {
        "name": "Bottom reversal", "timeframe": "Daily",
        "rule": ["Close at least 15% above its 52-week low",
                 "Close below the 200-day average",
                 "Close above the 50-day average",
                 "Weekly RSI(14) above 48"],
        "gate": "20-day average turnover at least ₹5 cr",
        "horizon": "30 sessions",
    },
    "brk4h": {
        "name": "4-hour breakout", "timeframe": "4H (09:15–13:15, 13:15–15:30 IST)",
        "rule": ["A completed 4H candle closes above the highest high of the prior 20 candles",
                 "The candle before it had not closed above ITS prior 20-candle high — a fresh break, not a name already extended",
                 "Solid bullish candle: close above open, body ≥ 60% of the range, close in the top quarter",
                 "Range at least 1× the 4H ATR, volume at least 1.5× the prior 20 candles' average"],
        "gate": "Turnover at least ₹5 cr a day",
        "horizon": "20 candles (~10 sessions)",
    },
}
LEVELS = ("Stop: the wider of the 5-bar swing low less 0.25 ATR and 1.41 ATR below entry, capped at 6%. "
          "Targets: 1.6 / 2.5 / 3.3 ATR, snapped to nearby resistance, then lifted so T1 repays at least "
          "1.6× the risk and T2, T3 step 0.9R and 1.7R above it; a first target past 4R is pulled back. "
          "The same functions every other engine in this book uses.")


def _log(msg: str) -> None:
    line = f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} {msg}"
    print(line, flush=True)
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def universe(limit: int | None) -> list[dict]:
    """The screen's rows — the same names every other Vision panel reads."""
    with open(SCREEN, encoding="utf-8") as f:
        rows = json.load(f).get("rows") or []
    out = [{"sym": r["sym"], "name": r.get("name"), "sector": r.get("sector")}
           for r in rows if r.get("sym")]
    return out[:limit] if limit else out


def _load_prev() -> dict:
    try:
        with open(FEED, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def file_signals(history: list, found: list, now_iso: str) -> tuple[list, int]:
    """File what fired, newest first. A name already open on the same engine is
    the same setup, so its first filing — and the levels a reader acted on —
    stands. The same BAR is the same setup even after it closed: a later run
    must not refile a signal because the first filing was stopped in between."""
    history = list(history)
    open_keys = {(h["engine"], h["sym"]) for h in history
                 if (h.get("grade") or {}).get("status", "open") == "open"}
    seen_bars = {(h["engine"], h["sym"], h["fired_at"]) for h in history}
    new = 0
    for sig in found:
        key = (sig["engine"], sig["sym"])
        if key in open_keys or (*key, sig["fired_at"]) in seen_bars:
            continue
        history.insert(0, dict(sig, filed_at=now_iso))
        open_keys.add(key)
        seen_bars.add((*key, sig["fired_at"]))
        new += 1
    return history, new


def grade_history(history: list, daily: dict, hourly: dict, now_iso: str, now=None) -> None:
    """Grade every open filing on the completed bars after the one that fired it.
    A closed grade does not reopen; a name with no bars this run keeps its last grade."""
    import scanner
    from signals.buoy import to_4h
    for h in history:
        g = h.get("grade") or {}
        if g.get("status", "open") != "open":
            continue
        if h["engine"] == "bottom":
            bars = daily.get(h["sym"])
            if not bars:
                continue
            after = scanner._vision_complete_daily(
                [r for r in bars if scanner._vist(r[0]).date().isoformat() > h["fired_at"][:10]], now)
        else:
            bars = hourly.get(h["sym"])
            if not bars:
                continue
            fired = datetime.fromisoformat(h["fired_at"])
            after = [c for c in scanner._vision_complete_4h(to_4h(bars), now) if scanner._vist(c[0]) > fired]
        h["grade"] = scanner.vision_grade(h, after, scanner.VISION_HORIZON[h["engine"]])
        h["graded_at"] = now_iso


def tally(history: list) -> dict:
    """Counts per engine. Every filing lands in exactly one of open / stopped /
    expired / t3, so the four always sum to `filed`."""
    counts = {}
    for h in history:
        e = counts.setdefault(h["engine"], {"filed": 0, "open": 0, "t3": 0, "stopped": 0, "expired": 0,
                                            "t1_or_better": 0, "ambiguous": 0})
        g = h.get("grade") or {}
        st = g.get("status", "open")
        e["filed"] += 1
        e["stopped" if st.startswith("stopped") else st] += 1
        e["t1_or_better"] += g.get("targets_hit", 0) >= 1
        e["ambiguous"] += bool(g.get("ambiguous"))
    return counts


def run(limit: int | None = None, now=None) -> int:
    import harvest_bars
    import scanner

    names = universe(limit)
    if not names:
        _log("vision: the screen universe is empty — nothing to scan, feed left as it was")
        return 1
    syms = [n["sym"] for n in names]
    meta = {n["sym"]: n for n in names}
    _log(f"vision: {len(syms)} names")

    harvest_bars.RANGES["vision_d"] = {"interval": "1d", "period": "2y", "min_bars": 210}
    harvest_bars.RANGES["vision_h"] = {"interval": "1h", "period": "60d", "min_bars": 150}
    t0 = time.time()
    daily, dcov = harvest_bars.harvest(syms, "vision_d")
    hourly, hcov = harvest_bars.harvest(syms, "vision_h")
    _log(f"vision: daily {dcov['got']}/{dcov['asked']}, hourly {hcov['got']}/{hcov['asked']} in {time.time() - t0:.0f}s")
    if not daily or not hourly:
        _log("vision: a fetch came back empty — NOT overwriting the feed with a scan that saw nothing")
        return 1

    found = []
    for s in syms:
        for fn, bars in ((scanner.vision_bottom_reversal, daily.get(s)),
                         (scanner.vision_4h_breakout, hourly.get(s))):
            if not bars:
                continue
            try:
                sig = fn(bars, now) if now else fn(bars)
            except Exception as e:                              # noqa: BLE001
                _log(f"vision: {fn.__name__} {s}: {type(e).__name__}: {e}")
                continue
            if sig:
                sig.update({"sym": s, "name": meta[s].get("name"), "sector": meta[s].get("sector")})
                found.append(sig)

    prev = _load_prev()
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    history, new = file_signals(prev.get("history") or [], found, now_iso)
    grade_history(history, daily, hourly, now_iso, now)
    history = history[:HISTORY_CAP]
    counts = tally(history)

    feed = {
        "ok": True,
        "generated_at": now_iso,
        "universe": len(syms),
        "coverage": {"daily": {"asked": dcov["asked"], "got": dcov["got"]},
                     "hourly": {"asked": hcov["asked"], "got": hcov["got"]}},
        "rules": RULES,
        "levels": LEVELS,
        "note": ("Two new rules, untested against history. They are not filed to the signal ledger, "
                 "not sent to Telegram, and carry no win rate or expectancy until 30 have closed."),
        # What qualifies on this run, shown with the levels it was FILED at —
        # the newest filing for that name — not today's recomputed ones.
        "today": [next(h for h in history if (h["engine"], h["sym"]) == (s["engine"], s["sym"]))
                  for s in found if any((h["engine"], h["sym"]) == (s["engine"], s["sym"]) for h in history)],
        "counts": counts,
        "history": history,
    }
    os.makedirs(os.path.dirname(FEED), exist_ok=True)
    tmp = FEED + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(feed, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, FEED)
    _log(f"vision: {len(found)} matching now ({sum(1 for s in found if s['engine'] == 'bottom')} bottom, "
         f"{sum(1 for s in found if s['engine'] == 'brk4h')} 4H), {new} newly filed, {len(history)} in history → {FEED}")
    return 0


if __name__ == "__main__":
    a = sys.argv[1:]
    lim = int(a[a.index("--limit") + 1]) if "--limit" in a else None
    raise SystemExit(run(lim))
