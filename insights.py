#!/usr/bin/env python3
"""
insights.py — findings the datasets already contain and nobody was reading.

Three engines, all DETERMINISTIC. No model writes anything here, and that is
deliberate: every finding below is a rule over published numbers, so it can be
reproduced by hand from the same screen payload. An LLM would add nothing
except the possibility of being wrong.

  hidden_findings(rows)     cross-sectional anomalies in the 750-name screen
  contradictions(breadth,…) where the market's own indicators disagree
  what_changed(payload)     what moved since the previous build

WHAT THESE ARE NOT

Not recommendations. Every finding names companies that share a PROPERTY —
"strong on published accounts, weak on price" is an observation, not a thesis,
and the page labels it that way. The point is to surface what a reader would
never spot scanning 750 rows, then get out of the way.

THE RULES EVERY FINDING OBEYS

1. A finding must name its rule. If the criteria cannot be stated in one line,
   it is not a finding, it is a hunch.
2. A finding with fewer than MIN_HITS companies is suppressed — three names
   sharing a property is a pattern, one is a coincidence.
3. Missing data never counts as passing. A company with no ROCE is not a
   company with low ROCE.
4. Nothing is ranked or scored. The screen already does that; this answers a
   different question.
"""
from __future__ import annotations

# Below this, a "pattern" is noise. Two companies sharing a property is a
# coincidence worth nobody's attention.
MIN_HITS = 3

# How many names a finding lists before it truncates. The count is always the
# true one; only the printed sample is capped.
MAX_NAMES = 8


def _f(row, key):
    """A finite number, or None. Missing is never treated as zero — that is how
    a company with no ROCE becomes a company with terrible ROCE."""
    v = row.get(key)
    if v is None or isinstance(v, bool):
        return None
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return None if v != v or v in (float("inf"), float("-inf")) else v


def _finding(key, title, rule, rows, note=""):
    if len(rows) < MIN_HITS:
        return None
    rows = sorted(rows, key=lambda r: -(_f(r, "comp") or 0))
    return {
        "key": key,
        "title": title,
        # The rule in one line. A finding that cannot state its own criteria
        # is not checkable, and an unfalsifiable finding is decoration.
        "rule": rule,
        "count": len(rows),
        "names": [{"sym": r.get("sym"), "name": r.get("name"),
                   "comp": _f(r, "comp"), "sector": r.get("sector")}
                  for r in rows[:MAX_NAMES]],
        # The COMPLETE list, symbols only.
        #
        # `names` is capped at MAX_NAMES and the page printed "+37 more" beside
        # it — but the other 37 were never serialised, so there was nothing for
        # that label to expand into. It was a dead end rendered as an offer.
        #
        # Symbols only, deliberately: the full dict shape for every hit across
        # six findings adds ~110KB to a server-rendered page. A bare ticker is
        # ~10 bytes, so the entire list costs ~3KB and the reader can actually
        # have all of it. The first MAX_NAMES keep their full detail above.
        "all_syms": [r.get("sym") for r in rows if r.get("sym")],
        "note": note,
    }


# ── 1. Hidden findings ───────────────────────────────────────────────────────

def hidden_findings(rows: list[dict]) -> list[dict]:
    """Unusual combinations across the screened universe."""
    rows = [r for r in (rows or []) if isinstance(r, dict)]
    out = []

    # Strong on the accounts, weak on the tape. The single most useful
    # cross-section a screen can produce, because the screen's own ranking
    # blends the two and therefore hides it.
    out.append(_finding(
        "quality_price_divergence",
        "Strong on the accounts, weak on the tape",
        "quality ≥ 70 and 3-month return ≤ −10%",
        [r for r in rows
         if (_f(r, "q") or 0) >= 70 and (_f(r, "r3m") is not None and _f(r, "r3m") <= -10)],
        "A falling price is not evidence of a worse business. It is also not "
        "evidence of a bargain. This is where to look, not what to buy."))

    # Improving profitability while still cheap against its OWN history.
    out.append(_finding(
        "improving_and_cheap",
        "Improving profitability, still cheap against its own history",
        "margin improving, ROCE trend up, and PE in the cheapest third of its own range",
        [r for r in rows
         if (_f(r, "margin_delta") or 0) > 0
         and str(r.get("roce_trend") or "").lower() in ("up", "rising", "improving")
         and (_f(r, "pe_pctile") is not None and _f(r, "pe_pctile") <= 33)]))

    # Piotroski is a quality screen; pairing it with price weakness is the
    # classic use it was designed for.
    out.append(_finding(
        "fscore_strong_price_weak",
        "F-score 7+ while the price has gone nowhere",
        "F-score ≥ 7 of 9 evaluated, and 6-month return ≤ 0%",
        [r for r in rows
         if (_f(r, "piotroski") or 0) >= 7 and (_f(r, "piotroski_of") or 0) >= 7
         and (_f(r, "r6m") is not None and _f(r, "r6m") <= 0)]))

    # Deterioration is worth surfacing even though nobody enjoys it — a screen
    # that only finds opportunities is a screen selling something.
    out.append(_finding(
        "quality_deterioration",
        "Previously strong, now deteriorating",
        "quality ≥ 60 but margin falling AND ROCE trend down",
        [r for r in rows
         if (_f(r, "q") or 0) >= 60
         and (_f(r, "margin_delta") is not None and _f(r, "margin_delta") < 0)
         and str(r.get("roce_trend") or "").lower() in ("down", "falling", "deteriorating")],
        "Early deterioration in businesses that still score well. The score is "
        "backward-looking; the direction is not."))

    # Volume arriving without price is the market disagreeing with itself.
    out.append(_finding(
        "volume_without_price",
        "Unusual volume, no price response",
        "volume spike ≥ 2× the 20-day average and 1-week move within ±2%",
        [r for r in rows
         if (_f(r, "vol_spike") or 0) >= 2
         and (_f(r, "r1w") is not None and abs(_f(r, "r1w")) <= 2)]))

    # Earnings moved, the price did not.
    out.append(_finding(
        "earnings_price_divergence",
        "Profit up sharply, price has not followed",
        "profit growth ≥ 20% year on year and 3-month return ≤ 0%",
        [r for r in rows
         if (_f(r, "pat_yoy") or 0) >= 20
         and (_f(r, "r3m") is not None and _f(r, "r3m") <= 0)]))

    return [f for f in out if f]


# ── 1b. Names that clear more than one screen ────────────────────────────────

# Two independent rules landing on the same company is the point. One is a
# property; two is a coincidence worth a look; three is a reason to open the
# accounts.
MIN_OVERLAP = 2


def multi_signal_names(findings: list[dict], rows: list[dict] | None = None) -> list[dict]:
    """Companies that appear in MORE THAN ONE finding above.

    Each finding on its own is a single property — "quality high, price weak"
    is one lens. The question this answers is different and, per name, harder:
    which companies show up under SEVERAL unrelated lenses at once?

    Worked example, and the reason this exists: a company can be simultaneously
    (a) profit up 20%+ year on year, (b) price down over three months, and
    (c) carrying a volume spike with no price response. Each rule finds it
    separately, each buries it in a list of forty, and nothing on the page ever
    said the three were the same company. That intersection is the finding.

    Ranked by how many findings a name clears, then by composite. NOT a
    recommendation and NOT a score — a name clearing three rules is a name
    three rules happened to select, which is a reason to look, not a thesis.
    """
    findings = [f for f in (findings or []) if isinstance(f, dict)]
    meta = {}
    for r in (rows or []):
        if isinstance(r, dict) and r.get("sym"):
            meta[r["sym"]] = r

    hits: dict[str, list[str]] = {}
    for f in findings:
        for sym in (f.get("all_syms") or []):
            hits.setdefault(sym, []).append(f.get("title") or f.get("key") or "")

    out = []
    for sym, titles in hits.items():
        # A rule cannot vouch for a name twice.
        titles = sorted(set(t for t in titles if t))
        if len(titles) < MIN_OVERLAP:
            continue
        r = meta.get(sym) or {}
        out.append({
            "sym": sym,
            "name": r.get("name"),
            "sector": r.get("sector"),
            "comp": _f(r, "comp"),
            "n": len(titles),
            "findings": titles,
        })

    out.sort(key=lambda x: (-x["n"], -(x["comp"] or 0), x["sym"]))
    return out


# ── 1c. Movers inside each sector ────────────────────────────────────────────

# Five each way. Fewer than this many names in a sector and "top five" is just
# "the sector", which tells a reader nothing they could not see by listing it.
MOVERS_N = 5
MIN_SECTOR_SIZE = 8


def sector_movers(rows: list[dict], window: str = "r1w") -> list[dict]:
    """Best and worst performers within each sector.

    Deliberately built on the SCREEN's own sector labels rather than on the
    heat map's NSE index tiles, and the two must not be conflated. The tiles
    are NSE sector indices (^CNXIT, ^NSEBANK, ^CNXPSUBANK...); these rows carry
    Yahoo's sector taxonomy. Mapping one onto the other is lossy in one
    direction and plainly wrong in the other — Banking and PSU Bank are
    separate indices that would both collapse into "Financial Services",
    so two tiles would drill into an identical list of names. Better to
    answer the question on a taxonomy that can actually answer it.

    The window is a RETURN ALREADY ON THE ROW (default 1-week), so this costs
    no network call and inherits the screen's build date rather than implying
    it is live. A sector with no reachable return is omitted, never shown flat.
    """
    rows = [r for r in (rows or []) if isinstance(r, dict)]
    buckets: dict[str, list[dict]] = {}
    for r in rows:
        sec = r.get("sector")
        if not sec or _f(r, window) is None:
            continue
        buckets.setdefault(str(sec), []).append(r)

    out = []
    for sec, names in sorted(buckets.items()):
        if len(names) < MIN_SECTOR_SIZE:
            continue
        ranked = sorted(names, key=lambda r: -(_f(r, window) or 0))

        def pack(rs):
            return [{"sym": r.get("sym"), "name": r.get("name"),
                     "move": _f(r, window), "price": _f(r, "price"),
                     "comp": _f(r, "comp")} for r in rs]

        moves = [_f(r, window) for r in names]
        out.append({
            "sector": sec,
            "count": len(names),
            "window": window,
            # The sector's own middle, so a reader can tell a broad move from
            # a couple of names carrying the label.
            "median": round(sorted(moves)[len(moves) // 2], 2),
            "gainers": pack(ranked[:MOVERS_N]),
            "losers": pack(list(reversed(ranked[-MOVERS_N:]))),
        })
    # Strongest sector first, by its median name — not by its best one.
    out.sort(key=lambda x: -(x["median"] or 0))
    return out


# ── 2. Contradiction detector ────────────────────────────────────────────────

def contradictions(breadth: dict, fii: dict | None = None,
                   regime: dict | None = None) -> list[dict]:
    """Where the market's own indicators disagree with each other.

    Only fires on a genuine conflict — an index doing one thing while the
    internals do another. Agreement is not a finding.
    """
    b, out = breadth or {}, []
    nifty_1m = _f(b, "nifty_1m")
    med_1m = _f(b, "median_1m")
    adv, dec = _f(b, "advancing"), _f(b, "declining")
    above200 = _f(b, "above200")

    # The index is carried by a few names while the median stock falls.
    if nifty_1m is not None and med_1m is not None and nifty_1m > med_1m + 2:
        out.append({
            "key": "narrow_index",
            "title": "The index is holding up better than the average stock",
            "detail": f"Nifty {nifty_1m:+.1f}% over a month against a median stock "
                      f"of {med_1m:+.1f}% — a {nifty_1m - med_1m:.1f} point gap.",
            "means": "Strength is concentrated in a few large names rather than broad.",
            "watch": "Whether the median closes the gap, or the index gives way to it.",
        })

    # More falling than rising while the index is up.
    if adv and dec and dec > adv and (nifty_1m or 0) > 0:
        out.append({
            "key": "breadth_conflict",
            "title": "More stocks fell than rose, but the index rose",
            "detail": f"{int(dec)} declining against {int(adv)} advancing, "
                      f"with the index {nifty_1m:+.1f}% over a month.",
            "means": "The move is not broad-based.",
            "watch": "Advance/decline turning positive would confirm the index; "
                     "staying negative would not.",
        })

    # Long-term participation weak while the label says otherwise.
    label = str(b.get("label") or "").upper()
    if above200 is not None and above200 < 50 and label in ("BULLISH", "STRONG"):
        out.append({
            "key": "trend_participation",
            "title": f"Labelled {label}, but under half the market is above its 200-day",
            "detail": f"{above200:.1f}% of {int(_f(b, 'counted') or 0)} names above the 200DMA.",
            "means": "The headline reading is ahead of the participation underneath it.",
            "watch": "Whether the share above the 200-day rises to meet the label.",
        })

    # Foreign money leaving while the index holds.
    fii_cr = _f(fii or {}, "fii_cr")
    if fii_cr is not None and fii_cr < 0 and (nifty_1m or 0) > 0:
        out.append({
            "key": "flow_conflict",
            "title": "Foreign flows negative while the index is up",
            "detail": f"FII net ₹{fii_cr:,.0f} Cr on the last recorded session, "
                      f"index {nifty_1m:+.1f}% over a month.",
            "means": "Domestic buying is absorbing foreign selling.",
            "watch": "Whether domestic flows can keep absorbing it.",
        })

    return out


# ── 3. What changed ──────────────────────────────────────────────────────────

def what_changed(payload: dict) -> dict | None:
    """Movement between this screen build and the previous one.

    Reports the SHAPE of the change, not a list of every mover — 562 rows
    moving says something; naming 562 rows says nothing.
    """
    ch = (payload or {}).get("changes") or {}
    b = (payload or {}).get("breadth") or {}
    if not ch:
        return None
    rows = [r for r in (payload.get("rows") or []) if isinstance(r, dict)]

    def band(lo, hi):
        return sum(1 for r in rows
                   if _f(r, "comp") is not None and lo <= _f(r, "comp") < hi)

    return {
        "compared_with": ch.get("compared_with"),
        "new_names": ch.get("new"),
        "moved": ch.get("moved"),
        "universe": len(rows),
        "breadth": {
            "above200": _f(b, "above200"),
            "advancing": _f(b, "advancing"),
            "declining": _f(b, "declining"),
            "median_1m": _f(b, "median_1m"),
            "label": b.get("label"),
        },
        # Where the universe actually sits, which is what a shifting
        # distribution looks like from the outside.
        "bands": {"80+": band(80, 1e9), "60-79": band(60, 80),
                  "40-59": band(40, 60), "under 40": band(0, 40)},
    }
