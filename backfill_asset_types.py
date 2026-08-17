#!/usr/bin/env python3
"""
backfill_asset_types.py — set all_signals.market / .asset_type from the symbol.

Why this is needed
------------------
Both columns were declared with schema defaults ('NSE' / 'Equity') and NO
writer in the codebase ever set them. Every commodity and forex row therefore
claimed to be an NSE equity.

That is not cosmetic. `/api/ticker` selects open rows with
`market='NSE' AND asset_type='Equity'` and appends ".NS" to quote them, so a
SILVER row was quoted as **SILVER.NS — a real, unrelated NSE company at ₹233**
while silver itself traded at $66/oz. The site published ₹233 as the last price
of a silver trade. GOLD.NS and CRUDE.NS merely 404 and fall back to blank,
which is why only silver was visibly wrong; the tagging was equally broken for
every non-equity.

A wrong mark price is dangerous rather than untidy — one has already closed
metals longs at their stop (the APEX phantom stop-out).

Classification comes from symbols.classify(), which reads the same NON_EQUITY
map as to_yahoo(), so the ledger and the quote layer cannot disagree.

Usage
-----
    python3 backfill_asset_types.py            # dry run, prints what changes
    python3 backfill_asset_types.py --apply    # writes

Safe to re-run: it only touches rows whose stored pair differs from the
computed one, so a second run is a no-op.
"""
from __future__ import annotations

import sys

import db as _db
from symbols import classify


def main() -> int:
    apply = "--apply" in sys.argv
    with _db.connect() as c:
        rows = c.execute(
            "SELECT id, symbol, market, asset_type FROM all_signals"
        ).fetchall()

        changes = []
        for row in rows:
            rid, symbol, market, atype = row[0], row[1], row[2], row[3]
            want_m, want_a = classify(symbol or "")
            if (market or "") != want_m or (atype or "") != want_a:
                changes.append((rid, symbol, market, atype, want_m, want_a))

        print(f"{len(rows)} rows scanned, {len(changes)} need correcting\n")
        if not changes:
            print("nothing to do")
            return 0

        # Group for a readable summary — 14 commodity rows should not print as
        # 14 near-identical lines when what matters is the per-symbol verdict.
        by_symbol: dict[str, list] = {}
        for ch in changes:
            by_symbol.setdefault(ch[1], []).append(ch)
        for symbol, group in sorted(by_symbol.items()):
            _, _, m, a, wm, wa = group[0]
            print(f"  {symbol:12} {len(group):3} row(s)  "
                  f"{m}/{a}  ->  {wm}/{wa}")

        if not apply:
            print("\nDRY RUN — re-run with --apply to write")
            return 0

        for rid, _sym, _m, _a, want_m, want_a in changes:
            c.execute(
                "UPDATE all_signals SET market=?, asset_type=? WHERE id=?",
                (want_m, want_a, rid))
        c.commit()
        _db.sync(c)

        print(f"\napplied {len(changes)} update(s)")

        bad = [r for r in c.execute(
            "SELECT symbol, market, asset_type FROM all_signals").fetchall()
            if classify(r[0] or "") != ((r[1] or ""), (r[2] or ""))]
        if bad:
            print(f"VERIFY FAILED — {len(bad)} row(s) still wrong")
            return 1
        print("verified: every row's market/asset_type now matches its symbol")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
