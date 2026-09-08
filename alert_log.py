#!/usr/bin/env python3
"""
alert_log.py — one durable, de-duplicated record of every alert a scan raised.

WHY A SEPARATE FILE AND NOT THE SCAN'S OWN OUTPUT
docs/buoy.json is a SNAPSHOT: it is overwritten on every run and says what is
true now. That is right for a watchlist and useless as a record. Its `history`
array was a half-measure — it kept the last sixty runs, so a name alerted on
Monday fell out of the file by Thursday, and it stored only symbols, so nothing
could be reconciled against the ledger later.

This is the other thing: an append-only log, one row per DISTINCT alert, that
survives every scan and can be counted.

WHAT COUNTS AS A DUPLICATE
The rule that matters, because it is the one that quietly inflates a record.
An engine that fires on the same name for six consecutive scans has found ONE
setup, not six — the scan runs twice a day and a 4H reclaim stays true for
days. Counting each scan as an alert would multiply this book's forward sample
by however often the cron happens to run, which is a number about the schedule,
not about the market.

So the key is (engine, lane, symbol, the level it fired at) and an alert is new
only when that key has not been seen within DEDUPE_DAYS. The level is rounded
before it enters the key: the same setup re-detected a day later prices the
entry a few paise differently, and two rows that differ by 0.3% are the same
observation.

A re-alert AFTER the window is a genuinely separate event — the name came back,
which is information — so it gets its own row and a `repeat_of` pointer to the
first, rather than being silently merged or silently duplicated.

WHAT IT DOES NOT DO
It does not grade anything. Whether an alert worked is the ledger's question,
and answering it here would create a second record that could disagree with the
first. This says only: this engine said this, about this name, at this level,
at this time, and it had not said it recently.
"""
from __future__ import annotations
import json, math, os, sys
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(HERE, "docs", "alerts_log.json")
DEDUPE_DAYS = 10          # a 4H reclaim stays true for about this long
LEVEL_TOL = 0.005         # 0.5% — the same setup, re-priced
MAX_ROWS = 5000


def _key(engine, lane, symbol):
    """Identity of the SETUP, without the price.

    The price is deliberately NOT in the key. Two attempts to put it there both
    failed, and the way they failed is the reason this is a proximity test now:

      · `e / (e * LEVEL_TOL)` — the price cancels out, so every level on every
        stock produced the same bucket and two alerts 18% apart were recorded
        as one setup.
      · `log(e) / log(1 + LEVEL_TOL)` — correct spacing, but a GRID: two prices
        0.3% apart straddle a bucket edge often enough to matter, so the same
        setup re-priced overnight logged twice.

    "Within half a percent" is a distance, and a grid cannot express a distance.
    So the key identifies the engine, lane and name, and `_same_level` decides
    whether a candidate is the same setup as one already recorded.
    """
    return f"{engine}|{lane or '-'}|{str(symbol).upper()}"


def _same_level(a, b) -> bool:
    """True when two entry prices are the same setup re-priced."""
    try:
        x, y = float(a), float(b)
    except (TypeError, ValueError):
        return False
    if x <= 0 or y <= 0:
        return False
    return abs(x - y) / max(x, y) <= LEVEL_TOL


def load():
    if not os.path.exists(LOG):
        return {"ok": True, "rows": [], "counts": {}}
    try:
        d = json.load(open(LOG))
        if isinstance(d, dict) and isinstance(d.get("rows"), list):
            return d
    except Exception:
        # A corrupt log is not a reason to lose the scan. It is renamed rather
        # than overwritten, because a file that will not parse may still be the
        # only copy of what happened.
        os.replace(LOG, LOG + ".corrupt")
    return {"ok": True, "rows": [], "counts": {}}


def record(alerts, engine, now=None):
    """Append the alerts that are actually new. Returns (added, skipped)."""
    now = now or datetime.now(timezone.utc)
    d = load()
    rows = d["rows"]
    cutoff = now - timedelta(days=DEDUPE_DAYS)

    recent = []
    for r in rows:
        try:
            seen = datetime.fromisoformat(r["at"])
        except Exception:
            continue
        if seen.tzinfo is None:
            seen = seen.replace(tzinfo=timezone.utc)
        if seen >= cutoff:
            recent.append(r)

    added, skipped = [], 0
    for a in alerts:
        k = _key(engine, a.get("lane"), a.get("symbol"))
        dup = next((r for r in recent
                    if r["key"] == k and _same_level(r.get("entry"), a.get("entry"))), None)
        if dup is not None:
            skipped += 1
            dup["last_seen"] = now.isoformat(timespec="seconds")
            dup["seen_count"] = int(dup.get("seen_count", 1)) + 1
            continue
        # An older row with the same key is a genuine RE-alert, not a duplicate.
        prior = next((r for r in reversed(rows)
                      if r["key"] == k and _same_level(r.get("entry"), a.get("entry"))), None)
        row = {
            "id": f"{engine}-{str(a.get('symbol')).upper()}-{now.strftime('%Y%m%dT%H%M%S')}",
            "key": k, "at": now.isoformat(timespec="seconds"),
            "last_seen": now.isoformat(timespec="seconds"), "seen_count": 1,
            "engine": engine, "lane": a.get("lane"),
            "symbol": str(a.get("symbol", "")).upper(),
            "entry": a.get("entry"), "sl": a.get("sl"),
            "target1": a.get("target1"), "target2": a.get("target2"),
            "target3": a.get("target3"),
            "risk_pct": a.get("risk_pct"), "rsi": a.get("rsi"),
            "from_high_pct": a.get("from_high_pct"),
        }
        if prior:
            row["repeat_of"] = prior["id"]
            row["days_since_last"] = round(
                (now - datetime.fromisoformat(prior["at"]).replace(tzinfo=timezone.utc)).total_seconds() / 86400, 1)
        rows.append(row)
        added.append(row)

    rows.sort(key=lambda r: r["at"])
    if len(rows) > MAX_ROWS:
        rows[:] = rows[-MAX_ROWS:]

    counts = {}
    for r in rows:
        counts[r["engine"]] = counts.get(r["engine"], 0) + 1
    out = {"ok": True, "generated_at": now.isoformat(timespec="seconds"),
           "dedupe_days": DEDUPE_DAYS, "level_tolerance_pct": LEVEL_TOL * 100,
           "total": len(rows), "counts": counts,
           "note": ("One row per DISTINCT alert. An engine that fires on the same name at "
                    "the same level in consecutive scans has found one setup, not one per "
                    "scan — those increment seen_count instead of adding a row. A re-alert "
                    "more than "
                    f"{DEDUPE_DAYS} days later is a separate event and gets its own row, "
                    "pointing back at the first."),
           "rows": rows}
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    json.dump(out, open(LOG, "w"), indent=1)
    return added, skipped


if __name__ == "__main__":
    d = load()
    print(f"{d.get('total', len(d.get('rows', [])))} alerts logged")
    for r in d.get("rows", [])[-15:]:
        rep = f"  (repeat of {r['repeat_of']}, {r.get('days_since_last')}d later)" if r.get("repeat_of") else ""
        print(f"  {r['at'][:16]}  {r['engine']:<8} {r.get('lane') or '-':<8} "
              f"{r['symbol']:<12} entry {r.get('entry')}  seen {r.get('seen_count', 1)}x{rep}")
