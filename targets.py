#!/usr/bin/env python3
"""Targets anchored to real resistance, with each level carrying its own odds.

WHY THIS EXISTS. The house ladder is three fixed R-multiples — 1.6 / 2.5 / 3.3 —
snapped to a 10-day and 20-day rolling high. That is a generic target wearing a
structural label: a 20-day rolling max is wherever price happened to be, not a
level anyone defended.

WHAT THE LEDGER SAYS, AND IT POINTS THE OPPOSITE WAY TO INTUITION.
Measured over 453 closed trades, T3 looked reachable 35.8% of the time. That
number is an artifact. 323 of those 453 are `cf_1h`, and its record is FX pairs
carrying stops of 0.08-0.21% of entry against a claimed 21-26% favourable
excursion — EURUSD does not move 21% intraday. Strip cf_1h and its intraday
sibling and the truth is:

    n = 120     T1 reached 19.2%     T2 5.8%     T3 3.3%
    max favourable excursion:  median 0.37R    p75 0.98R    p90 2.28R

So price typically runs about a third of the risk taken. T1 at 1.6R already sits
ABOVE the 75th percentile of what actually happens, and T3 at 3.3R sits beyond
the 90th. The ladder is not too conservative. It is too optimistic, and every
target beyond T1 is mostly decoration.

WHAT THIS MODULE DOES ABOUT IT.
  1. Finds resistance that price actually respected — swing highs (a bar whose
     high exceeds its neighbours on both sides), clustered within an ATR, and
     scored by how many times price turned there. A level touched four times is
     evidence; a 20-day rolling max is not.
  2. Places each target ON a level, and says which one and why. When no level
     exists in range it falls back to an R-multiple and labels it as a floor,
     so the reader can tell a defended level from an arithmetic one.
  3. Publishes the MEASURED reach rate for each target's R distance, from this
     ledger, per engine. A target that has been reached 3% of the time is
     allowed to exist — it is not allowed to look like the others.
  4. Lets a strong business run instead of pushing its target further out. If
     the fundamentals are strong, the honest instrument is a TRAIL after T2,
     not a bigger number on the card: a fixed target caps the upside it was
     supposed to capture, and the ledger says the big fixed numbers do not get
     hit.

Pure stdlib. Callers pass whatever resistance evidence they have.
"""

from __future__ import annotations

# ── Measured reach, from data/all_signals.json, cf_1h and intraday excluded ──
# Refresh with:  python3 targets.py --calibrate
# Keyed by R distance band -> share of closed trades whose max favourable
# excursion reached at least that far. This is what makes a target honest.
REACH_BY_R = {
    0.5: 0.467, 1.0: 0.242, 1.5: 0.183, 2.0: 0.133,
    2.5: 0.067, 3.0: 0.058, 4.0: 0.042, 5.0: 0.033,
}
REACH_SAMPLE_N = 120

# The house floors, unchanged — this module places targets, it does not get to
# quietly re-tune the mandate. signals/indicators.py remains the one place
# those constants live; they are mirrored here only as fallbacks.
R1_FLOOR, R2_FLOOR, R3_FLOOR = 1.6, 2.5, 3.3
MIN_GAP_ATR = 0.5          # two targets closer than this are one target twice
SWING_WINDOW = 3           # bars either side that a swing high must exceed
CLUSTER_ATR = 0.6          # highs within this many ATR are the same level
NEAR_LEVEL_ATR = 0.35      # a target may snap to a level this close


def _num(v):
    if isinstance(v, bool) or v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def reach_pct(r_distance: float) -> float:
    """Measured share of closed trades that ran at least this far, 0-1.

    Linear between the calibration points, flat outside them. Deliberately not
    extrapolated past the last point: nothing in the sample ran beyond it, and
    inventing a number there is how a 4.0R target got published for months.
    """
    r = _num(r_distance)
    if r is None or r <= 0:
        return 0.0
    ks = sorted(REACH_BY_R)
    if r <= ks[0]:
        return REACH_BY_R[ks[0]]
    if r >= ks[-1]:
        return REACH_BY_R[ks[-1]]
    for a, b in zip(ks, ks[1:]):
        if a <= r <= b:
            t = (r - a) / (b - a)
            return REACH_BY_R[a] + t * (REACH_BY_R[b] - REACH_BY_R[a])
    return 0.0


def swing_highs(highs, lows=None, window: int = SWING_WINDOW) -> list[float]:
    """Bars whose high exceeds `window` bars either side — a level price turned
    at, rather than a level it merely passed through."""
    hs = [_num(h) for h in (highs or [])]
    hs = [h for h in hs if h is not None]
    out = []
    for i in range(window, len(hs) - window):
        h = hs[i]
        if all(h >= hs[j] for j in range(i - window, i)) and \
           all(h >= hs[j] for j in range(i + 1, i + window + 1)):
            out.append(h)
    return out


def resistance_levels(price: float, atr: float, highs=None,
                      extra: list[tuple] | None = None) -> list[dict]:
    """Resistance ABOVE price, clustered, strongest first.

    `extra` carries anchors a caller knows about that are not swing highs —
    ("52-week high", 1234.0), ("200-day average", 980.0). They join the same
    clustering, so a swing high sitting on the 52-week high becomes one strong
    level rather than two weak ones printed a rupee apart.
    """
    p, a = _num(price), _num(atr)
    if p is None or not a or a <= 0:
        return []

    pts = [(h, "prior swing high") for h in swing_highs(highs) if h > p * 1.001]
    for label, lvl in (extra or []):
        lv = _num(lvl)
        if lv is not None and lv > p * 1.001:
            pts.append((lv, label))
    if not pts:
        return []

    pts.sort(key=lambda t: t[0])
    clusters: list[dict] = []
    for lvl, label in pts:
        if clusters and lvl - clusters[-1]["lo"] <= CLUSTER_ATR * a:
            c = clusters[-1]
            c["hi"] = max(c["hi"], lvl)
            c["touches"] += 1
            c["labels"].append(label)
            c["level"] = sum(c["raw"] + [lvl]) / (len(c["raw"]) + 1)
            c["raw"].append(lvl)
        else:
            clusters.append({"lo": lvl, "hi": lvl, "level": lvl, "touches": 1,
                             "labels": [label], "raw": [lvl]})

    out = []
    for c in clusters:
        named = [l for l in c["labels"] if l != "prior swing high"]
        # A named anchor (the 52-week high) beats "touched twice" as a
        # description, because it is the level other people are watching too.
        if named:
            basis = named[0]
            if c["touches"] > len(named):
                basis += f", tested {c['touches']}x"
        else:
            basis = (f"prior swing high, tested {c['touches']}x"
                     if c["touches"] > 1 else "prior swing high")
        out.append({"level": round(c["level"], 2), "touches": c["touches"],
                    "basis": basis, "named": bool(named)})
    out.sort(key=lambda d: d["level"])
    return out


def build_ladder(price: float, stop: float, atr: float, highs=None,
                 extra: list[tuple] | None = None,
                 quality: dict | None = None) -> dict | None:
    """Three targets, each on a level where one exists, each carrying its odds.

    `quality` is the fundamental read for the name — {"strong": bool, "why":
    str}. It does NOT push targets further out. It turns on a trail after T2,
    which is the instrument that actually captures an extended move: the
    ledger's own p90 excursion is 2.28R, so a bigger fixed number is a number
    that does not get hit.
    """
    p, s, a = _num(price), _num(stop), _num(atr)
    if p is None or s is None or a is None or a <= 0:
        return None
    risk = p - s
    if risk <= 0:
        return None                      # long-only; an inverted stop is a bug

    levels = resistance_levels(p, a, highs, extra)
    used: list[float] = []
    targets = []

    for idx, floor_r in enumerate((R1_FLOOR, R2_FLOOR, R3_FLOOR), start=1):
        floor_px = p + floor_r * risk
        gap = MIN_GAP_ATR * a
        # The floor is a FLOOR. An earlier draft allowed a level 10% under it,
        # and on the first symbol tested that put T1 at 0.88R — below the 1.6R
        # mandate, which is precisely the defect the house ladder was written
        # to stop (four engines were once found publishing a first target
        # worth less than R1). A wall below the floor is real and worth
        # naming, but it is not a target.
        lo = max(floor_px, (used[-1] + gap) if used else 0.0)

        # The nearest level at or above the floor, but not so far past it that
        # the ladder stops being a ladder.
        cand = [L for L in levels
                if L["level"] >= lo and L["level"] not in used
                and L["level"] <= floor_px + 2.0 * a]
        if cand:
            cand.sort(key=lambda L: (-L["touches"], L["level"]))
            pick = cand[0]
            px, basis = pick["level"], pick["basis"]
        else:
            px, basis = floor_px, f"{floor_r:.1f}R floor — no level in range"

        if used and px - used[-1] < gap:
            px = used[-1] + gap
            basis = f"spaced {MIN_GAP_ATR}xATR off T{idx - 1}"
        px = round(px, 2)
        used.append(px)
        r = (px - p) / risk
        targets.append({
            "t": idx, "px": px, "r": round(r, 2),
            "basis": basis,
            "reach": round(reach_pct(r) * 100),
        })

    # Resistance BELOW the first target is not a target, but it is the thing
    # standing between entry and the first target — the reader should know the
    # trade has to get through it.
    first_t = targets[0]["px"] if targets else None
    below = [L for L in levels if first_t and L["level"] < first_t]
    wall = None
    if below:
        w = max(below, key=lambda L: L["level"])
        wall = {"px": w["level"], "r": round((w["level"] - p) / risk, 2),
                "basis": w["basis"]}

    q = quality or {}
    trail = bool(q.get("strong"))
    return {
        "entry": round(p, 2), "stop": round(s, 2),
        "risk_pct": round(risk / p * 100, 2),
        "targets": targets,
        "wall": wall,
        # The one number that matters for position sizing, quoted against the
        # target most likely to fill rather than the furthest one. rr measured
        # to T2 is how the multibagger engine published a 0.80R T1 as a good
        # trade — see project_house_ladder.
        "rr": targets[0]["r"],
        "trail": trail,
        "trail_note": (
            f"Fundamentals are strong ({q.get('why', 'quality gate passed')}), "
            "so ratchet a stop under each higher low after T2 instead of "
            "capping the exit. A fixed target cannot capture an extended move, "
            "and this ledger's 90th-percentile excursion is 2.28R."
            if trail else
            "No trail: the fundamental case is not strong enough to justify "
            "holding past the last target."),
        "sample": REACH_SAMPLE_N,
    }


def _calibrate(path="data/all_signals.json"):
    """Recompute REACH_BY_R from the ledger. cf_1h and intraday are EXCLUDED —
    their max_profit_pct is not credible (FX pairs, 0.08% stops, 21% claimed
    excursions), and they are 71% of the closed sample, so leaving them in
    inverts the answer."""
    import json
    rows = json.load(open(path))
    rows = rows if isinstance(rows, list) else rows.get("signals", [])
    mfe = []
    for r in rows:
        if r.get("signal_type") in ("cf_1h", "intraday"):
            continue
        if (r.get("status") or "").upper() in ("OPEN", ""):
            continue
        m, e, s = _num(r.get("max_profit_pct")), _num(r.get("entry")), _num(r.get("sl"))
        if m is None or not e or s is None:
            continue
        risk = abs(e - s) / e * 100
        if risk > 0:
            mfe.append(m / risk)
    if not mfe:
        return None, 0
    return {k: round(sum(1 for x in mfe if x >= k) / len(mfe), 3)
            for k in sorted(REACH_BY_R)}, len(mfe)


if __name__ == "__main__":
    import sys
    if "--calibrate" in sys.argv:
        table, n = _calibrate()
        print(f"REACH_BY_R from {n} closed trades (cf_1h + intraday excluded):")
        for k, v in (table or {}).items():
            print(f"  {k:>4}R  {v*100:5.1f}%")
        print("\nPaste into REACH_BY_R above if this differs from the committed table.")
        sys.exit(0)

    # A worked example against real cached bars.
    import pickle
    bars = pickle.load(open("cache/rebuild_bars.pkl", "rb"))
    (sym, _), df = next(iter(bars.items()))
    highs = [float(x) for x in df["High"].tolist()]
    price = float(df["Close"].iloc[-1])
    atr = float((df["High"] - df["Low"]).tail(14).mean())
    lad = build_ladder(price, price - 1.41 * atr, atr, highs,
                       extra=[("52-week high", max(highs))],
                       quality={"strong": True, "why": "ROCE 22%, D/E 0.3"})
    print(f"{sym}  entry {lad['entry']}  stop {lad['stop']}  risk {lad['risk_pct']}%")
    for t in lad["targets"]:
        print(f"  T{t['t']}  {t['px']:>10}  {t['r']:>5.2f}R  reached {t['reach']:>2}%  · {t['basis']}")
    print(f"  trail: {lad['trail']}")
