#!/usr/bin/env python3
"""
dedupe_positions.py — one open position per symbol per engine, keeping the first.

Why this exists
---------------
The ledger held 84 OPEN rows across 59 symbols. OFSS had five, GLAND, VIJAYA,
EXIDEIND, LUPIN and SHRIRAMFIN three each. Two separate causes:

  1. EXACT DOUBLE-WRITES. OFSS 591/584, LUPIN 592/585 and SHRIRAMFIN 594/587
     are same-day, same-engine rows with byte-identical entry and stop. One
     physical signal recorded twice.

  2. WEEKLY RE-FILES. `tracker.duplicate_symbols` skipped a symbol only if it
     had an OPEN row filed within FIVE DAYS. multibagger rescans weekly and
     holds for 6-12 months, so every re-detection landed 7 days later — outside
     the window — and passed the guard. GLAND was filed 2026-06-13, 07-11 and
     07-25 and all three stayed open.

Both are fixed at source (the OPEN check no longer carries a date window). This
repairs the rows already written.

The rule, and why
-----------------
ONE open position per (symbol, signal_type), keeping the EARLIEST.

  · Per ENGINE, not per symbol. A 6-12 month multibagger thesis and a 2-3 year
    ai_longterm thesis on the same company are genuinely different ideas with
    different stops and different horizons. Collapsing those would be wrong.
    OFSS legitimately keeps one of each.

  · The EARLIEST, not the latest. The first firing is the entry a reader would
    actually be holding, at the price it actually fired. Keeping the newest
    would silently re-enter at a later price and restart the clock, which
    flatters the record every time the position has moved in favour — exactly
    the direction a ledger must not be wrong in.

Superseded rows are marked VOID, not deleted. VOID already means "never managed
as a position, carries no usable outcome" and is excluded from expectancy by
signal_report.NOT_CLOSED and by /api/stats. Deleting would destroy the evidence
that the engine re-detected the name, which is itself information.

Usage
-----
    python dedupe_positions.py            # report only
    python dedupe_positions.py --apply    # write
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))


def plan(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """(keep, void). Groups OPEN rows by (symbol, engine)."""
    groups: dict = {}
    for r in rows:
        if (r.get("status") or "").upper() != "OPEN":
            continue
        key = (str(r.get("symbol") or "").upper(),
               str(r.get("signal_type") or "").lower())
        groups.setdefault(key, []).append(r)

    keep, void = [], []
    for key, rs in sorted(groups.items()):
        if len(rs) == 1:
            keep.append(rs[0])
            continue
        # Earliest date wins; ties broken by the lower id, which is the row that
        # was physically written first.
        rs.sort(key=lambda r: (str(r.get("date") or "9999"), int(r.get("id") or 0)))

        # ...but only among rows still INSIDE their horizon. "Keep the earliest"
        # is right when both are legitimately open, and wrong when the earliest
        # is open only because it should already have been time-stopped: that
        # would void a fresh signal in favour of one the grader is about to
        # close, leaving the symbol with no position at all. Not currently
        # triggered by any live row (HINDALCO's earlier equity_measured is 6
        # days into a 20-day horizon), so this is a guard rather than a fix.
        live = [r for r in rs if _within_horizon(r)]
        winner = live[0] if live else rs[0]

        keep.append(winner)
        for r in rs:
            if r is winner:
                continue
            r["_superseded_by"] = winner.get("id")
            r["_same_day"] = str(r.get("date")) == str(winner.get("date"))
            r["_kept_later"] = winner is not rs[0]
            void.append(r)
    return keep, void


def _within_horizon(r: dict) -> bool:
    """Is this row still inside the horizon its own engine declares?

    True when the horizon cannot be established — an unknown horizon must never
    be a reason to discard a position, exactly as in standalone_scan.
    """
    try:
        import standalone_scan as S
        meta = r.get("metadata")
        if isinstance(meta, str):
            meta = json.loads(meta or "{}")
        meta = meta or {}
        limit_h = S._max_hold_hours(
            str(r.get("timeframe") or ""),
            engine=str(r.get("signal_type") or meta.get("engine") or ""),
            horizon=str(meta.get("horizon") or ""))
        if limit_h is None:
            return True
        d = str(r.get("date") or "")[:10]
        if not d:
            return True
        opened = datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=IST)
        return (datetime.now(IST) - opened).total_seconds() / 3600.0 <= limit_h
    except Exception:
        return True


def main() -> int:
    apply = "--apply" in sys.argv
    import tracker
    tracker.init_db()

    with tracker._conn() as c:
        cur = c.execute(
            "SELECT id, symbol, signal_type, timeframe, status, date, entry, sl, "
            "metadata FROM all_signals WHERE UPPER(COALESCE(status,'OPEN'))='OPEN'")
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, v)) for v in cur.fetchall()]

    keep, void = plan(rows)
    print(f"{len(rows)} OPEN rows -> {len(keep)} kept, {len(void)} superseded\n")
    if not void:
        print("one open position per symbol per engine already — nothing to do")
        return 0

    same_day = sum(1 for r in void if r.get("_same_day"))
    print(f"  {same_day} are same-day exact double-writes")
    print(f"  {len(void) - same_day} are later re-files of a still-open position\n")
    print(f"  {'ID':>5}  {'SYMBOL':<13}{'ENGINE':<14}{'DATE':<12}"
          f"{'ENTRY':>10}  SUPERSEDED BY")
    for r in sorted(void, key=lambda x: (x["symbol"], str(x.get("date")))):
        print(f"  {str(r['id']):>5}  {r['symbol']:<13}"
              f"{(r.get('signal_type') or '—'):<14}{str(r.get('date')):<12}"
              f"{(r.get('entry') if r.get('entry') is not None else 0):>10.2f}"
              f"  id={r['_superseded_by']}"
              f"{'  (same-day duplicate)' if r.get('_same_day') else ''}")

    kept_by_engine: dict = {}
    for r in keep:
        e = (r.get("signal_type") or "—")
        kept_by_engine[e] = kept_by_engine.get(e, 0) + 1
    print("\n  kept, by engine:",
          ", ".join(f"{k}={v}" for k, v in sorted(kept_by_engine.items())))

    if not apply:
        print("\nDRY RUN — re-run with --apply to VOID the superseded rows")
        return 0

    stamp = datetime.now(IST).strftime("%Y-%m-%d")
    path = f"data/backups/all_signals_pre_dedupe_{stamp}.json"
    try:
        import os
        os.makedirs("data/backups", exist_ok=True)
        with open(path, "w") as fh:
            json.dump(rows, fh, indent=1, default=str)
        print(f"\nbacked up {len(rows)} OPEN rows to {path}")
    except OSError as e:
        print(f"BACKUP FAILED ({e}) — refusing to write")
        return 1

    now = datetime.now(IST).isoformat()
    with tracker._conn() as c:
        for r in void:
            # VOID, not deleted, and the reason is recorded in metadata so the
            # ledger can explain itself later.
            try:
                meta = json.loads(r.get("metadata") or "{}")
            except (TypeError, ValueError):
                meta = {}
            meta["voided_reason"] = ("duplicate open position — superseded by "
                                     f"id {r['_superseded_by']}")
            meta["voided_at"] = now
            c.execute(
                "UPDATE all_signals SET status='VOID', closed_at=?, metadata=? "
                "WHERE id=?", (now, json.dumps(meta), r["id"]))
        c.commit()
    try:
        import db as _db
        _db.sync(c)
    except Exception:
        pass
    print(f"voided {len(void)} superseded rows; "
          f"{len(keep)} open positions remain")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
