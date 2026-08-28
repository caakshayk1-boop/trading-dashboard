#!/usr/bin/env python3
"""pulse_builder.py — the digest /next.html reads instead of screen.json.

screen.json is 1.26 MB and 88 fields over 750 names. A phone needs none of
that to answer "which sectors moved, who traded abnormally, what broke out" —
it needs the answers, which are a few hundred rows of arithmetic over the same
file. Computing them here means the client downloads ~25 KB instead of 1.26 MB
and does no work at all.

Everything below is a rule over data already published. Nothing is inferred,
nothing is smoothed, and a bucket with too few names to be worth a median says
so by not appearing.
"""
from __future__ import annotations
import statistics


def _num(v):
    try:
        f = float(v)
        return None if f != f else f          # NaN is missing, never zero
    except (TypeError, ValueError):
        return None


def build_pulse(screen: dict, *, min_sector: int = 4) -> dict:
    rows = (screen or {}).get("rows") or []
    out = {
        "built_on": (screen or {}).get("built_on"),
        "universe": len(rows),
        "sectors": [], "movers_up": [], "movers_dn": [],
        "volume": [], "breakouts": [], "breadth": {},
    }
    if not rows:
        return out

    # ── SECTOR HEAT ─────────────────────────────────────────────────────────
    # Median, not mean: one 40% name in a 12-name sector drags a mean into
    # saying something true of nobody. Sectors under `min_sector` names are
    # dropped rather than shown with a median of two.
    by_sector: dict[str, list] = {}
    for r in rows:
        s, v = r.get("sector"), _num(r.get("r1w"))
        if s and v is not None:
            by_sector.setdefault(s, []).append(v)
    # Rows per sector too, so a tile can be OPENED. A heat map that only shows
    # a median answers "which sector moved" and refuses the obvious next
    # question, "which names moved it" — and shipping 1.26 MB to answer that
    # would undo the reason this digest exists. Five each way is ~10 rows per
    # sector: about 6 KB for all eleven.
    rows_by_sector: dict[str, list] = {}
    for r in rows:
        s = r.get("sector")
        if s and _num(r.get("r1w")) is not None:
            rows_by_sector.setdefault(s, []).append(r)

    for s, vals in by_sector.items():
        if len(vals) < min_sector:
            continue
        srt = sorted(rows_by_sector.get(s, []), key=lambda r: -(_num(r.get("r1w")) or 0))
        slim = lambda r: {
            "sym": r.get("sym"), "name": (r.get("name") or "")[:40],
            "price": _num(r.get("price")), "r1w": _num(r.get("r1w")),
            "r1m": _num(r.get("r1m")), "rsi": _num(r.get("rsi")),
        }
        out["sectors"].append({
            "name": s, "n": len(vals),
            "median": round(statistics.median(vals), 2),
            "up": sum(1 for v in vals if v > 0),
            "top": [slim(r) for r in srt[:5]],
            "bottom": [slim(r) for r in srt[-5:][::-1]],
        })
    out["sectors"].sort(key=lambda x: -x["median"])

    def trim(r, extra=()):
        d = {"sym": r.get("sym"), "name": (r.get("name") or "")[:44],
             "sector": r.get("sector"), "price": _num(r.get("price")),
             "r1w": _num(r.get("r1w")), "r1m": _num(r.get("r1m"))}
        for k in extra:
            d[k] = _num(r.get(k)) if k not in ("setup_tags",) else r.get(k)
        return d

    # ── MOVERS ──────────────────────────────────────────────────────────────
    ranked = [r for r in rows if _num(r.get("r1w")) is not None]
    ranked.sort(key=lambda r: -_num(r.get("r1w")))
    out["movers_up"] = [trim(r, ("turnover_cr",)) for r in ranked[:12]]
    out["movers_dn"] = [trim(r, ("turnover_cr",)) for r in ranked[-12:][::-1]]

    # ── WHO ACTUALLY SHOWED UP ──────────────────────────────────────────────
    # vol_spike is a real ratio against the name's own average, not a flag.
    # 2x is the floor: below it "unusual" is just a normal Tuesday.
    vol = [r for r in rows if (_num(r.get("vol_spike")) or 0) >= 2]
    vol.sort(key=lambda r: -(_num(r.get("vol_spike")) or 0))
    out["volume"] = [trim(r, ("vol_spike", "turnover_cr")) for r in vol[:20]]

    # ── BREAKOUTS ───────────────────────────────────────────────────────────
    # A 52-week breakout is the only one worth its own list; a 20-day high is
    # noise on most days. Ordered by turnover, because a breakout nobody
    # traded is a print, not a move.
    brk = [r for r in rows if r.get("brk52w")]
    brk.sort(key=lambda r: -(_num(r.get("turnover_cr")) or 0))
    # Turnover still ORDERS the list — a breakout nobody traded is a print —
    # but it is no longer what the row shows. Where price sits against its own
    # 50 and 200 day, and whether it is stretched, is what changes a decision.
    out["breakouts"] = [trim(r, ("from_high", "rsi", "rsi_m", "sma50", "sma200"))
                        for r in brk[:20]]

    # ── BREADTH ─────────────────────────────────────────────────────────────
    r1w = [_num(r.get("r1w")) for r in rows]
    r1w = [v for v in r1w if v is not None]
    above200 = sum(1 for r in rows
                   if (_num(r.get("price")) or 0) > (_num(r.get("sma200")) or 1e18))
    out["breadth"] = {
        "counted": len(r1w),
        "up": sum(1 for v in r1w if v > 0),
        "down": sum(1 for v in r1w if v < 0),
        "median": round(statistics.median(r1w), 2) if r1w else None,
        "above_200dma": above200,
        "at_52w_high": sum(1 for r in rows if r.get("brk52w")),
    }

    # ── A SECOND HEAT MAP: TODAY, LARGE CAPS ────────────────────────────────
    # The front page showed a WEEK over all 750 names, which answers "what has
    # been happening" — not "what happened today", which is the question
    # someone opens a daily paper with. Micro-caps also dominate a 750-name
    # median: 500 of the 750 are small or micro, and a few thin names swing a
    # sector median most readers will never trade.
    #
    # Scoped to the 250 largest (tier large + mid = the top 250 by market cap,
    # the closest this screen has to a Nifty 200). Labelled by what it IS
    # rather than by an index name it only approximates — the actual
    # constituent list is not in any feed here.
    big = [r for r in rows if r.get("tier") in ("large", "mid")]
    day_by: dict[str, list] = {}
    for r in big:
        sec_, v = r.get("sector"), _num(r.get("r1d"))
        if sec_ and v is not None:
            day_by.setdefault(sec_, []).append((v, r))
    out["sectors_day"] = []
    for sec_, pairs in day_by.items():
        if len(pairs) < min_sector:
            continue
        vals = [v for v, _ in pairs]
        srt = sorted(pairs, key=lambda t: -t[0])
        def slim(r):
            return {"sym": r.get("sym"), "name": (r.get("name") or "")[:40],
                    "price": _num(r.get("price")), "r1d": _num(r.get("r1d")),
                    "r1w": _num(r.get("r1w")), "rsi": _num(r.get("rsi"))}
        out["sectors_day"].append({
            "name": sec_, "n": len(vals),
            "median": round(statistics.median(vals), 2),
            "up": sum(1 for v in vals if v > 0),
            "top": [slim(r) for _, r in srt[:5]],
            "bottom": [slim(r) for _, r in srt[-5:][::-1]],
        })
    out["sectors_day"].sort(key=lambda x: -x["median"])
    out["day_universe"] = len(big)

    return out
