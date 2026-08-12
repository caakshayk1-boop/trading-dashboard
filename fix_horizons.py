#!/usr/bin/env python3
"""
fix_horizons.py — reopen positions the time stop closed before their horizon.

Why this exists
---------------
`standalone_scan.MAX_HOLD_HOURS` was keyed by TIMEFRAME, and two of the values
the ledger actually writes were missing from it:

    "1W"    56 rows
    "LONG"  15 rows

Both fell through to `_DEFAULT_MAX_HOLD_HOURS`, which was 20 days. So every
signal filed on a weekly bar was force-closed after three weeks — including the
multibagger engine, whose documented horizon is 6–12 months, and ai_longterm,
whose horizon is 2–3 years.

    GLAND, filed 2026-07-11 as a multibagger with metadata
    {"engine": "multibagger", "horizon": "6-12 months"}, timeframe "1W",
    was booked EXPIRED on 2026-08-11 at +14.86% / +0.93R.

The row declared its own horizon and the time stop read a lookup table instead.
That is now fixed at source (`_max_hold_hours` takes engine and horizon first,
and refuses to close anything whose horizon it cannot establish). This script
repairs the rows the old behaviour already closed.

What it does NOT do
-------------------
It does not touch a row that reached a level. SL_HIT, T1_HIT, T2_HIT and
TARGET_HIT are real outcomes and stay exactly as they are, whatever their
horizon was. Only EXPIRED rows are candidates, and only those whose CORRECT
horizon had not yet elapsed at the moment they were closed.

Reopened rows have their outcome fields cleared, because a P&L booked at an
arbitrary date is not information — keeping the number while reopening the
position would leave a stale +14.86% attached to a live idea.

The published win rate is unaffected either way: /api/stats excludes
ai_longterm and multibagger from win rate and expectancy (NON_TRADING), so
these rows never fed the headline numbers. They were still wrong on the page.

Usage
-----
    python fix_horizons.py            # report only
    python fix_horizons.py --apply    # write corrections
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone

import standalone_scan as S

IST = timezone(timedelta(hours=5, minutes=30))

# Outcomes that mean "price did something", which the horizon never overrides.
REAL_OUTCOMES = ("SL_HIT", "STOPPED", "T1_HIT", "T2_HIT", "TARGET_HIT",
                 "CANCELLED", "VOID", "OPEN")


def _parse(ts):
    if not ts:
        return None
    txt = str(ts)
    try:
        dt = datetime.fromisoformat(txt.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=IST)
    except ValueError:
        pass
    for fmt, n in (("%Y-%m-%d %H:%M:%S", 19), ("%Y-%m-%dT%H:%M:%S", 19),
                   ("%Y-%m-%d", 10)):
        try:
            return datetime.strptime(txt[:n], fmt).replace(tzinfo=IST)
        except ValueError:
            continue
    return None


def audit(rows: list[dict]) -> list[dict]:
    """Rows that were expired before their real horizon. Newest first."""
    wrong = []
    for r in rows:
        if (r.get("status") or "").upper() != "EXPIRED":
            continue
        meta = r.get("metadata")
        if isinstance(meta, str):
            try:
                meta = json.loads(meta or "{}")
            except json.JSONDecodeError:
                meta = {}
        meta = meta or {}

        tf = r.get("timeframe") or ""
        engine = str(r.get("signal_type") or meta.get("engine") or "")
        horizon = str(meta.get("horizon") or "")
        limit_h = S._max_hold_hours(tf, engine=engine, horizon=horizon)

        opened = _parse(r.get("entry_triggered_at") or r.get("date")
                        or r.get("generated_at"))
        closed = _parse(r.get("closed_at"))
        if not opened or not closed:
            continue
        held_h = (closed - opened).total_seconds() / 3600.0

        # limit_h None = horizon unknown = should never have been time-stopped.
        early = limit_h is None or held_h < limit_h
        if not early:
            continue
        wrong.append({
            "id": r.get("id"), "symbol": r.get("symbol"), "engine": engine,
            "tf": tf, "horizon": horizon,
            "held_days": round(held_h / 24, 1),
            "allowed_days": None if limit_h is None else round(limit_h / 24, 1),
            "opened": str(opened)[:10], "closed": str(closed)[:10],
            "pnl_pct": r.get("pnl_pct"), "r_multiple": r.get("r_multiple"),
        })
    wrong.sort(key=lambda x: x["closed"], reverse=True)
    return wrong


def main() -> int:
    apply = "--apply" in sys.argv
    import tracker
    tracker.init_db()

    with tracker._conn() as c:
        c.row_factory = None
        cur = c.execute(
            "SELECT id, symbol, signal_type, timeframe, status, metadata, date, "
            "generated_at, entry_triggered_at, closed_at, pnl_pct, r_multiple "
            "FROM all_signals WHERE status = 'EXPIRED'")
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, v)) for v in cur.fetchall()]

    print(f"{len(rows)} EXPIRED rows in the ledger")
    wrong = audit(rows)
    if not wrong:
        print("none were closed before their horizon — nothing to do")
        return 0

    print(f"\n{len(wrong)} closed BEFORE their real horizon:\n")
    print(f"  {'ID':>5}  {'SYMBOL':<13}{'ENGINE':<14}{'TF':<8}"
          f"{'HELD':>7}{'ALLOWED':>9}  {'CLOSED':<11}{'BOOKED':>9}")
    for w in wrong:
        allowed = "never" if w["allowed_days"] is None else f"{w['allowed_days']:.0f}d"
        pnl = "—" if w["pnl_pct"] is None else f"{w['pnl_pct']:+.1f}%"
        print(f"  {str(w['id']):>5}  {w['symbol']:<13}{(w['engine'] or '—'):<14}"
              f"{w['tf']:<8}{w['held_days']:>6.0f}d{allowed:>9}  "
              f"{w['closed']:<11}{pnl:>9}")

    by_engine: dict = {}
    for w in wrong:
        by_engine[w["engine"] or "—"] = by_engine.get(w["engine"] or "—", 0) + 1
    print("\n  by engine:", ", ".join(f"{k}={v}" for k, v in sorted(by_engine.items())))

    if not apply:
        print("\nDRY RUN — re-run with --apply to reopen these")
        return 0

    # Back up before writing, same discipline as the 2026-08-08 re-grade.
    stamp = datetime.now(IST).strftime("%Y-%m-%d")
    path = f"data/backups/all_signals_pre_horizon_fix_{stamp}.json"
    try:
        import os
        os.makedirs("data/backups", exist_ok=True)
        with open(path, "w") as fh:
            json.dump(rows, fh, indent=1, default=str)
        print(f"\nbacked up {len(rows)} EXPIRED rows to {path}")
    except OSError as e:
        print(f"BACKUP FAILED ({e}) — refusing to write")
        return 1

    ids = [w["id"] for w in wrong]
    with tracker._conn() as c:
        c.executemany(
            # Outcome fields cleared: a P&L booked on an arbitrary date is not
            # information, and leaving it on a reopened position is worse than
            # having none.
            "UPDATE all_signals SET status='OPEN', exit_price=NULL, pnl_pct=NULL, "
            "r_multiple=NULL, closed_at=NULL, exit_ambiguous=0 WHERE id=?",
            [(i,) for i in ids])
        c.commit()
    try:
        import db as _db
        _db.sync(c)
    except Exception:
        pass
    print(f"reopened {len(ids)} positions")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
