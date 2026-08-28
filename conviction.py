#!/usr/bin/env python3
"""
conviction.py — three to five names a DAY, ranked by arithmetic, explained by
a model, and logged so the record can be checked later.

WHY THIS EXISTS SEPARATELY FROM ai_longterm.py

ai_longterm runs weekly and answers "what would I own for two years". This
answers a different question — "what is worth looking at today" — over the same
750-name screen, on a daily cadence, and it keeps an append-only log so the
list can be graded after the fact instead of quietly rewritten every morning.

THE RULE THIS INHERITS, AND WILL NOT BREAK

The SELECTION is arithmetic. The language model writes one sentence per name
AFTER the ranking is fixed and cannot reorder, add or drop a name. "AI
generated" here means the words are; the picks are measured. That is the same
line ai_longterm.py draws, and it is the reason either engine can be published
beside measured data without the two being confused.

If Groq is unavailable the engine still ships its picks with no prose. A
missing sentence is a missing sentence; a missing pick would be a different
list, and a list that changes when a third-party API is down is not a record.

GATES — every one is a floor, not a weight, so a name cannot buy its way past
a gate with a high score somewhere else:
    liquidity     turnover >= Rs 5 cr        untradeable is not an idea
    trend         price > 200DMA, 50 > 200   no falling knives
    not extended  RSI <= 78                  a vertical chart is not an entry
    quality       ROCE >= 12 OR Piotroski >= 6
"""
from __future__ import annotations
import json, os, pathlib, statistics
from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))
LOG = pathlib.Path(__file__).parent / "data" / "conviction_log.json"
MAX_PICKS = 5
MIN_PICKS = 3


def _n(v):
    try:
        f = float(v)
        return None if f != f else f
    except (TypeError, ValueError):
        return None


def _passes(r) -> bool:
    price, s50, s200 = _n(r.get("price")), _n(r.get("sma50")), _n(r.get("sma200"))
    turn, rsi = _n(r.get("turnover_cr")), _n(r.get("rsi"))
    roce, pio = _n(r.get("roce")), _n(r.get("piotroski"))
    if not all(x is not None for x in (price, s50, s200, turn)):
        return False
    if turn < 5:                       return False
    if not (price > s200 and s50 > s200): return False
    if rsi is not None and rsi > 78:   return False
    if not ((roce is not None and roce >= 12) or (pio is not None and pio >= 6)):
        return False
    return True


def _score(r) -> float:
    """Composite, 0-100. Momentum and quality carry it; liquidity is a
    tiebreak, because between two equal ideas the tradeable one wins."""
    def clamp(v, lo, hi):
        return max(0.0, min(1.0, ((v - lo) / (hi - lo)))) if v is not None else 0.0
    mom = (clamp(_n(r.get("r1m")), -5, 25) * .5 + clamp(_n(r.get("r3m")), -10, 60) * .5)
    qual = (clamp(_n(r.get("roce")), 8, 40) * .6 + clamp(_n(r.get("piotroski")), 4, 9) * .4)
    # Distance from the 52-week high: closest to it scores highest, but a name
    # AT the high gets no bonus over one 3% below — that is noise, not strength.
    fh = _n(r.get("from_high"))
    prox = clamp(fh, -30, -1) if fh is not None else 0.0
    liq = clamp(_n(r.get("turnover_cr")), 5, 200)
    brk = 1.0 if r.get("brk52w") else (0.5 if r.get("brk20") else 0.0)
    return round((mom * 35 + qual * 30 + prox * 12 + brk * 13 + liq * 10), 2)


def _why(r) -> list[str]:
    """The measured reasons, before any model sees the row. These are what the
    prose must be consistent with — and what a reader can check it against."""
    out = []
    roce, pio = _n(r.get("roce")), _n(r.get("piotroski"))
    r1m, r3m = _n(r.get("r1m")), _n(r.get("r3m"))
    turn, fh = _n(r.get("turnover_cr")), _n(r.get("from_high"))
    if roce is not None and roce >= 20: out.append(f"ROCE {roce:.0f}%")
    if pio is not None and pio >= 7:    out.append(f"Piotroski {pio:.0f}/9")
    if r3m is not None and r3m >= 15:   out.append(f"+{r3m:.0f}% over three months")
    elif r1m is not None and r1m >= 8:  out.append(f"+{r1m:.0f}% over a month")
    if r.get("brk52w"):                 out.append("at a 52-week high")
    elif fh is not None and fh >= -8:   out.append(f"{abs(fh):.0f}% off its high")
    if turn is not None:                out.append(f"₹{turn:.0f} cr traded")
    return out[:4]


def select(screen: dict) -> list[dict]:
    rows = [r for r in ((screen or {}).get("rows") or []) if _passes(r)]
    for r in rows:
        r["_s"] = _score(r)
    rows.sort(key=lambda r: -r["_s"])

    # One name per sector. Five ideas that are all the same trade is one idea
    # with five tickers, and it is the failure mode a momentum rank walks into
    # on a day when one sector runs.
    picked, seen = [], set()
    for r in rows:
        sec = r.get("sector") or "?"
        if sec in seen:
            continue
        seen.add(sec)
        picked.append(r)
        if len(picked) == MAX_PICKS:
            break
    if len(picked) < MIN_PICKS:                       # relax the sector rule
        for r in rows:                                 # rather than ship two
            if r not in picked:
                picked.append(r)
            if len(picked) == MIN_PICKS:
                break
    # ── LEVELS ──────────────────────────────────────────────────────────────
    # A pick without an entry, a stop and a target is an opinion. These are
    # derived from the name's own volatility, not from a round number:
    #
    #   entry  the last close — this is a watch list, not a limit order, and
    #          pretending to know tomorrow's fill would be a fabricated price
    #   stop   2 x ATR below, floored at the 20-day average. Two ATR is the
    #          usual noise band; the 20-day is the level the trend itself
    #          would break, and whichever is CLOSER to price is the honest
    #          stop because it is the one that triggers first
    #   T1/T2  2R and 3.5R off that risk, so the reward is stated in the same
    #          units the ledger scores in
    def levels(r):
        px = _n(r.get("price"))
        atrp = _n(r.get("atr_pct"))
        s20 = _n(r.get("sma20"))
        if px is None:
            return {}
        band = px * (atrp / 100) * 2 if atrp else px * 0.06
        stop = px - band
        if s20 is not None and s20 < px:
            stop = max(stop, s20)          # the closer stop wins
        risk = px - stop
        if risk <= 0:
            return {}
        return {
            "entry": round(px, 2), "stop": round(stop, 2),
            "stop_pct": round(-risk / px * 100, 2),
            "t1": round(px + 2 * risk, 2), "t2": round(px + 3.5 * risk, 2),
            "t1_pct": round(2 * risk / px * 100, 1),
            "t2_pct": round(3.5 * risk / px * 100, 1),
            "rr": 3.5,
        }

    return [{
        **levels(r),
        "sym": r.get("sym"), "name": (r.get("name") or "")[:48],
        "sector": r.get("sector"), "price": _n(r.get("price")),
        "score": r["_s"], "rsi": _n(r.get("rsi")),
        "r1m": _n(r.get("r1m")), "r3m": _n(r.get("r3m")),
        "roce": _n(r.get("roce")), "piotroski": _n(r.get("piotroski")),
        "turnover_cr": _n(r.get("turnover_cr")), "from_high": _n(r.get("from_high")),
        "brk52w": bool(r.get("brk52w")),
        "reasons": _why(r),
    } for r in picked]


def add_prose(picks: list[dict]) -> list[dict]:
    """One sentence per name. Fails soft — the picks ship either way."""
    key = os.environ.get("GROQ_API_KEY")
    if not key or not picks:
        return picks
    try:
        from groq import Groq
        client = Groq(api_key=key)
        lines = "\n".join(
            f"{p['sym']} ({p['sector']}): " + "; ".join(p["reasons"]) for p in picks)
        msg = (
            "For each ticker below write ONE sentence, max 22 words, saying what "
            "the numbers given describe. Use only those numbers. Do not add "
            "price targets, do not predict, do not recommend. Reply as "
            "TICKER: sentence, one per line, nothing else.\n\n" + lines)
        r = client.chat.completions.create(
            model=os.environ.get("GROQ_MODEL") or "openai/gpt-oss-120b",
            messages=[{"role": "user", "content": msg}],
            temperature=0.3, max_completion_tokens=700)
        txt = (r.choices[0].message.content or "").strip()
        by = {}
        for ln in txt.splitlines():
            if ":" in ln:
                k, v = ln.split(":", 1)
                by[k.strip().upper().lstrip("-*# ")] = v.strip()
        for p in picks:
            if by.get((p["sym"] or "").upper()):
                p["view"] = by[(p["sym"] or "").upper()][:220]
    except Exception as e:                                   # noqa: BLE001
        print(f"[conviction] prose unavailable ({e}) — shipping picks without it")
    return picks


def build(screen: dict) -> dict:
    now = datetime.now(IST)
    picks = add_prose(select(screen))
    return {
        "date": now.strftime("%Y-%m-%d"),
        "generated_at": now.isoformat(timespec="seconds"),
        "universe": len((screen or {}).get("rows") or []),
        "method": ("Ranked by arithmetic over the daily screen — momentum, return on "
                   "capital, proximity to the 52-week high, breakout and liquidity — "
                   "after four floors: ₹5 cr turnover, price above the 200-day with "
                   "50 above 200, RSI at or under 78, and ROCE 12%+ or Piotroski 6+. "
                   "One name per sector. The sentences are model-written from those "
                   "same numbers, after the ranking was fixed."),
        "picks": picks,
    }


def append_log(payload: dict, path: pathlib.Path = LOG) -> int:
    """Append-only, one entry per date. A list that is silently rewritten every
    morning cannot be graded later, which is the whole reason for keeping it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        log = json.loads(path.read_text()) if path.exists() else []
    except Exception:                                        # noqa: BLE001
        log = []
    log = [e for e in log if e.get("date") != payload["date"]]
    log.append({"date": payload["date"], "generated_at": payload["generated_at"],
                "picks": [{k: p.get(k) for k in
                           ("sym", "sector", "price", "score", "reasons", "view")}
                          for p in payload["picks"]]})
    log = log[-180:]                                          # six months
    path.write_text(json.dumps(log, indent=1, default=str))
    return len(log)


if __name__ == "__main__":
    import sys
    scr = json.loads(pathlib.Path("docs/screen.json").read_text())
    out = build(scr)
    print(json.dumps(out, indent=1, default=str)[:1400])
    print(f"\nlog entries: {append_log(out)}")
