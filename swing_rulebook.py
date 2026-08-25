#!/usr/bin/env python3
"""
swing_rulebook.py — the Rs 1 crore mandate.

One module decides what may be traded, how much of it, and where it is sold.
news.askakshay.com renders from this; nothing else is allowed a second opinion.

THE MANDATE
-----------
Rs 1,00,00,000. Indian listed equity only. No intraday. Three horizons, each
with a return band and a holding window:

    SWING    25-60%   2 weeks to 1 month
    MEDIUM   35-75%   1 to 6 months
    LONG     40-90%   6 months to 1 year

Every position is unwound on the same ladder: 20% at T1, then half of what is
left at T2 (40% of the original), then the remainder at T3.

WHY THE ENGINE LIST LOOKS THE WAY IT DOES
-----------------------------------------
The bands are the filter, and they are brutal. Measured over the 370 NSE rows
in the feed, this is the median distance from entry to the FINAL target:

    ai_longterm   75.0%      multibagger    47.3%      magicmagic  31.8%
    top5_pick     40.0%      magic          23.7%      ai_daily    22.9%
    ai_4h         14.2%      equity_measured 14.0%     breakout    10.4%
    ohl            8.6%      4h              5.0%      intraday     0.8%
    sip_bucket     0.2%

Six engines cannot reach the bottom of the SWING band on their own targets. It
does not matter what their expectancy is — they are not aiming where the
mandate points. `equity_measured`, which was until now the only engine cleared
for capital anywhere in this stack, tops out at a 14% target. It was never
capable of this mandate.

`magic` and `magicmagic` are byte-identical engines filing the same trades
twice. One survives; the other is mapped to it so the duplicate can be named in
the reject log rather than silently disappearing.

WHAT THIS DOES NOT CLAIM
------------------------
The engines that reach the bands are also the ones with the thinnest and worst
live records — `top5_pick` is 0 for 4, `magic` 0 for 2. That is a real tension
and it is not resolved by ignoring it. Clearance is therefore reported per
horizon and the caller decides whether a horizon funds real money or paper.
Nothing here promotes an engine on the strength of its target size alone.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict
from typing import Optional

# ── Capital and caps ────────────────────────────────────────────────────────

CAPITAL = 10_000_000  # Rs 1,00,00,000

RISK = {
    # Rs 75,000 a trade. Unchanged as a percentage from the Rs 50L book, so the
    # mandate can be re-sized again without re-deriving anything here.
    "risk_per_trade_pct": 0.0075,
    # Rs 10,00,000 in one name. Tighter than the old 15% because a LONG-horizon
    # position is held for up to a year and cannot be trimmed on a bad week.
    "max_name_pct": 0.10,
    # Rs 6,00,000 of open risk, about eight concurrent positions. Raised from
    # 3% because three horizons run concurrently and a 3% ceiling meant the LONG
    # book alone could exhaust it and block every swing for months. It is set
    # equal to the drawdown halt on purpose: a total wipeout of open risk is
    # exactly the event that stops new entries.
    "max_heat_pct": 0.06,
    # Rs 2,00,000 of risk in one sector.
    "max_sector_heat_pct": 0.02,
    # Cash is a position, but 40% deployed cannot put Rs 1 crore to work across
    # holds measured in months. 75% leaves a real reserve without idling most
    # of the mandate.
    "max_deployed_pct": 0.75,
    "drawdown_halt_pct": 0.06,
}

# ── Horizons ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Horizon:
    key: str
    label: str
    min_gain: float          # % move to the final target, lower bound
    max_gain: float          # upper bound; above this the target is not credible
    min_days: int
    max_days: int
    max_stop_pct: float      # how far the stop may sit below entry
    timeframes: frozenset    # feed `timeframe` values that belong to this horizon

HORIZONS = {
    "SWING": Horizon("SWING", "Swing", 25.0, 60.0, 14, 31, 10.0,
                     frozenset({"1W", "WEEKLY"})),
    "MEDIUM": Horizon("MEDIUM", "Medium term", 35.0, 75.0, 31, 183, 15.0,
                      frozenset({"1W", "WEEKLY", "1M", "MONTHLY"})),
    "LONG": Horizon("LONG", "Long term", 40.0, 90.0, 183, 365, 25.0,
                    frozenset({"LONG", "1M", "MONTHLY"})),
}

# Max 8 alert types. Four are mapped; the cap is a ceiling, not a quota, and
# filling it with engines that cannot reach a band would defeat the mandate.
ENGINE_HORIZON = {
    "top5_pick":   "SWING",
    "magicmagic":  "SWING",
    "multibagger": "MEDIUM",
    "ai_longterm": "LONG",
}
MAX_ALERT_TYPES = 8

# Engines that file the same trade as another engine. Named rather than deleted
# so a dropped row can say which engine it duplicated.
DUPLICATE_OF = {"magic": "magicmagic"}

# Everything else, with the reason it is out. Rendered verbatim.
OUT_OF_MANDATE = {
    "intraday":        "15m signals — the mandate excludes intraday",
    "4h":              "4H signals — intraday timeframe, and a 5.0% final target",
    "ohl":             "final target 8.6% — below the swing band",
    "breakout":        "final target 10.4% — below the swing band",
    "equity_measured": "final target 14.0% — below the swing band",
    "ai_4h":           "4H signals — intraday timeframe",
    "ai_daily":        "final target 22.9% — below the swing band",
    "sip_bucket":      "accumulation bucket, 0.2% targets — not a trade",
    "cf_1h":           "FX and commodities — not Indian equity",
    "commodity":       "commodities — not Indian equity",
}

# ── Exit ladder ─────────────────────────────────────────────────────────────
#
# 20% at T1, half of the remainder at T2, the rest at T3. Expressed as
# fractions of the ORIGINAL position so the three always sum to 1.0 and no
# rounding path can leave a stranded share.
LADDER = (("T1", 0.20), ("T2", 0.40), ("T3", 0.40))


# ── What counts as an Indian listing ────────────────────────────────────────
#
# The feed's own `market` field CANNOT be trusted for this. MSFT, SNOW, MDB and
# NET are all tagged `market: "NSE"`, and SMCI appears under both "NSE" and
# "US" on different days. Sized as rupees, MSFT at "entry 499.99" bought 1,875
# shares of a $937,000 position against a Rs 1 crore book.
#
# docs/screen.json carries the NSE Nifty Total Market universe — 750 symbols,
# rebuilt weekly by stock_screen.py. Membership of that list is the test. A
# symbol this file cannot positively recognise as an Indian listing is
# rejected, on the same principle as an unrecognised engine: absence of
# evidence is not evidence of a domestic listing.

_UNIVERSE: Optional[frozenset] = None


def nse_universe(path: str = "docs/screen.json") -> frozenset:
    """The 750-name NSE universe. Cached; empty set if the screen has not run."""
    global _UNIVERSE
    if _UNIVERSE is None:
        try:
            import json
            with open(path, encoding="utf-8") as fh:
                rows = json.load(fh).get("rows") or []
            _UNIVERSE = frozenset(
                normalize_symbol(r.get("sym")) for r in rows if r.get("sym")
            )
        except Exception:
            _UNIVERSE = frozenset()
    return _UNIVERSE


def is_indian_equity(symbol: str, universe: Optional[frozenset] = None) -> bool:
    u = universe if universe is not None else nse_universe()
    if not u:
        # No universe loaded — fall back to the suffix, which is the only other
        # positive signal available. Never default to True.
        return str(symbol or "").upper().endswith((".NS", ".BO"))
    return normalize_symbol(symbol) in u


def _f(v) -> Optional[float]:
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


# ── Picking rules ───────────────────────────────────────────────────────────
#
# Built only on fields the feed actually populates. `turnover_cr` is present on
# 34 of 370 NSE rows and `breakeven_wr` on the same 34, so neither can carry a
# liquidity or a quality floor — a filter that silently passes 91% of rows
# because the field is null is not a filter. What is universally present:
# entry, sl, target1..3, action, timeframe, market, signal_type, date. `score`
# is populated on every engine mapped to a horizon and zero on every engine
# that is not, so it is usable exactly where it is needed.

REJECT_LABELS = {
    "NOT_EQUITY_INDIA": "Not an Indian listed equity",
    "OUT_OF_MANDATE":   "Engine is outside the mandate",
    "DUPLICATE_ENGINE": "Duplicate of another engine's signal",
    "WRONG_TIMEFRAME":  "Timeframe does not belong to this horizon",
    "BELOW_BAND":       "Final target below the horizon's return band",
    "ABOVE_BAND":       "Final target above the band — not a credible move",
    "STOP_TOO_WIDE":    "Stop further from entry than the horizon allows",
    "POOR_REWARD":      "Reward to risk below the floor",
    "MISSING_LEVELS":   "No entry, stop or target to work from",
    "INVERTED_RISK":    "Stop on the wrong side of entry",
    "LOW_SCORE":        "Score below the floor for this horizon",
    "DUPLICATE_NAME":   "Same name already on the list",
    "BELOW_MIN_SIZE":   "Cannot buy one share inside the name cap",
}

# Reward:risk floor. A 25% move behind a 10% stop is 2.5:1; anything under that
# does not pay for the ladder, which gives up the tail of the winner by design.
MIN_REWARD_RISK = 2.5

# Score floor. The four mandated engines all score, and the distribution across
# them sits between 40 and 95; 55 removes the bottom without pretending to a
# precision the field does not have.
MIN_SCORE = 55.0


@dataclass
class Leg:
    label: str
    price: float
    qty: int
    gain_pct: float
    r_multiple: float


@dataclass
class Ticket:
    id: int
    symbol: str
    engine: str
    horizon: str
    horizon_label: str
    sector: str
    date: str
    entry: float
    stop: float
    stop_pct: float
    targets: list
    final_gain_pct: float
    risk_per_share: float
    qty: int
    notional: int
    risk_amount: int
    risk_pct: float
    notional_pct: float
    reward_risk: float
    score: Optional[float]
    hold_days: str
    legs: list
    trail_note: str
    plan: str


@dataclass
class Rejected:
    id: int
    symbol: str
    engine: str
    date: str
    reason: str
    detail: str


def sector_of(symbol: str, sectors: dict) -> str:
    s = normalize_symbol(symbol)
    return sectors.get(s) or f"UNMAPPED:{s}"


def normalize_symbol(symbol) -> str:
    """Strip the yfinance exchange suffix. COFORGE.NS and COFORGE are one name;
    leaving the suffix on gave each a private sector bucket, so the sector cap
    stopped binding, and made one company look like two to the duplicate guard."""
    return str(symbol or "").upper().replace(".NSE", "").replace(".BSE", "") \
        .replace(".NS", "").replace(".BO", "")


def horizon_of(engine: str) -> Optional[Horizon]:
    key = ENGINE_HORIZON.get(engine)
    return HORIZONS.get(key) if key else None


def build_ladder(entry: float, stop: float, targets: list, qty: int, long: bool) -> list:
    """
    20% at T1, 40% at T2, 40% at T3, as fractions of the original position.

    The last leg takes the rounding remainder so the legs always sum to qty —
    flooring all three strands shares, and a stranded share has no exit.
    """
    legs, placed = [], 0
    rps = (entry - stop) if long else (stop - entry)

    # Collapse repeated prices. The feed frequently files target2 == target3 —
    # PAYTM, COFORGE and BAJFINANCE all carry the same number twice — and two
    # legs at one price is not a ladder, it is one exit printed twice with the
    # quantities split for no reason. Keep the first occurrence and let the
    # remaining fractions redistribute onto it.
    seen_prices, deduped = set(), []
    for t in targets:
        if t is None or t in seen_prices:
            deduped.append(None)
            continue
        seen_prices.add(t)
        deduped.append(t)
    targets = deduped

    usable = [(lbl, t) for (lbl, _frac), t in zip(LADDER, targets) if t is not None]
    for n, ((label, frac), price) in enumerate(zip(LADDER, targets), 1):
        if price is None:
            continue
        last = n == len(usable)
        q = qty - placed if last else int(qty * frac)
        if q < 1:
            continue
        placed += q
        gain = ((price - entry) if long else (entry - price)) / entry * 100
        legs.append(Leg(label, round(price, 2), q, round(gain, 1),
                        round((abs(price - entry)) / rps, 2) if rps > 0 else 0.0))
    return legs


def size_signal(sig: dict, sectors: dict, capital: float = CAPITAL):
    """
    One signal against the whole rulebook. Returns (Ticket, None) or (None, Rejected).

    Order matters: mandate membership is checked before geometry, so a rejected
    row says "outside the mandate" rather than "target too small", which is the
    more useful sentence when an engine is never going to qualify.
    """
    sym = normalize_symbol(sig.get("symbol"))
    engine = str(sig.get("signal_type") or "unknown")
    date = str(sig.get("date") or "")
    sid = int(sig.get("id") or 0)

    def no(reason, detail):
        return None, Rejected(sid, sym, engine, date, reason, detail)

    if not is_indian_equity(sym):
        return no("NOT_EQUITY_INDIA",
                  f"{sym} is not in the NSE universe — the feed calls it "
                  f"'{sig.get('market') or 'unstated'}', which it also does for MSFT and SNOW")
    if engine in DUPLICATE_OF:
        return no("DUPLICATE_ENGINE", f"{engine} files the same trades as {DUPLICATE_OF[engine]}")
    hz = horizon_of(engine)
    if hz is None:
        return no("OUT_OF_MANDATE", OUT_OF_MANDATE.get(engine, f"{engine} is not mapped to a horizon"))

    tf = str(sig.get("timeframe") or "").upper()
    if tf not in hz.timeframes:
        return no("WRONG_TIMEFRAME", f"{tf or 'no timeframe'} is not a {hz.label} timeframe")

    entry, stop = _f(sig.get("entry")), _f(sig.get("sl"))
    if not entry or entry <= 0 or not stop or stop <= 0:
        return no("MISSING_LEVELS", f"entry={sig.get('entry')} sl={sig.get('sl')}")

    long = str(sig.get("action") or "BUY").upper() != "SELL"
    rps = (entry - stop) if long else (stop - entry)
    if rps <= 0:
        return no("INVERTED_RISK",
                  f"{'LONG' if long else 'SHORT'} with entry {entry} and stop {stop}")

    stop_pct = rps / entry * 100
    if stop_pct > hz.max_stop_pct:
        return no("STOP_TOO_WIDE",
                  f"stop is {stop_pct:.1f}% from entry, {hz.label} allows {hz.max_stop_pct:.0f}%")

    targets = [_f(sig.get(k)) for k in ("target1", "target2", "target3")]
    usable = [t for t in targets if t and t > 0]
    if not usable:
        return no("MISSING_LEVELS", "no usable target")
    final = usable[-1]
    gain = ((final - entry) if long else (entry - final)) / entry * 100
    if gain < hz.min_gain:
        return no("BELOW_BAND",
                  f"final target is {gain:.1f}% — {hz.label} needs {hz.min_gain:.0f}%")
    if gain > hz.max_gain:
        return no("ABOVE_BAND",
                  f"final target is {gain:.1f}% — above the {hz.max_gain:.0f}% band ceiling")

    rr = gain / stop_pct
    if rr < MIN_REWARD_RISK:
        return no("POOR_REWARD", f"{rr:.1f}:1 against a {MIN_REWARD_RISK:.1f}:1 floor")

    score = _f(sig.get("score"))
    if score is not None and score > 0 and score < MIN_SCORE:
        return no("LOW_SCORE", f"score {score:.0f} against a floor of {MIN_SCORE:.0f}")

    qty = min(int(capital * RISK["risk_per_trade_pct"] / rps),
              int(capital * RISK["max_name_pct"] / entry))
    if qty < 1:
        return no("BELOW_MIN_SIZE", f"one share is {entry:.0f}, name cap is "
                                    f"{capital * RISK['max_name_pct']:,.0f}")

    legs = build_ladder(entry, stop, targets, qty, long)
    notional = round(entry * qty)
    risk_amt = round(rps * qty)

    # The trail engages only AFTER T2 — that is, after 60% of the position is
    # already booked. Tightening a stop earlier was tested on 2026-08-10 against
    # both rules on the same bars and made expectancy worse, and this feed
    # cannot re-test it because MFE and MAE are stored without their order.
    # Ratcheting the runner's stop up to T1 once T2 has printed cannot touch the
    # pre-T1 outcome the evidence covers, and it stops a 40% winner round-tripping.
    t1 = legs[0].price if legs else None
    trail = (f"Stop stays at {stop:,.2f} until T2 prints. After T2, the final "
             f"{legs[-1].qty if legs else 0} trail to {t1:,.2f} (T1) and never lower."
             if t1 and len(legs) > 2 else
             f"Stop stays at {stop:,.2f}. Too few legs to trail.")

    plan = " · ".join(f"{l.label} sell {l.qty} @ {l.price:,.2f} (+{l.gain_pct}%)" for l in legs)

    return Ticket(
        id=sid, symbol=sym, engine=engine, horizon=hz.key, horizon_label=hz.label,
        sector=sector_of(sym, sectors), date=date, entry=entry, stop=round(stop, 2),
        stop_pct=round(stop_pct, 1), targets=[round(t, 2) for t in usable],
        final_gain_pct=round(gain, 1), risk_per_share=round(rps, 2), qty=qty,
        notional=notional, risk_amount=risk_amt,
        risk_pct=round(risk_amt / capital * 100, 2),
        notional_pct=round(notional / capital * 100, 2),
        reward_risk=round(rr, 1), score=score,
        hold_days=f"{hz.min_days}-{hz.max_days} days",
        legs=[asdict(l) for l in legs], trail_note=trail, plan=plan,
    ), None


def build_book(signals: list, sectors: dict, capital: float = CAPITAL) -> dict:
    """
    The whole book: what to place, what is waiting on a cap, what was dropped.

    Caps are applied per horizon AND across the whole book, so a long-horizon
    position held for a year cannot quietly consume the swing budget.
    """
    tickets, rejected, dupes = [], [], []
    for s in signals:
        t, r = size_signal(s, sectors, capital)
        (tickets if t else rejected).append(t or r)

    tickets.sort(key=lambda t: (-(t.score or 0), t.date), reverse=False)
    tickets.sort(key=lambda t: -(t.score or 0))

    heat_cap = capital * RISK["max_heat_pct"]
    sect_cap = capital * RISK["max_sector_heat_pct"]
    depl_cap = capital * RISK["max_deployed_pct"]

    heat = deployed = 0.0
    sect: dict = {}
    seen: set = set()
    admitted, deferred = [], []

    for t in tickets:
        if t.symbol in seen:
            dupes.append(Rejected(t.id, t.symbol, t.engine, t.date, "DUPLICATE_NAME",
                                  f"{t.symbol} is already on the list from a {t.engine} signal"))
            continue
        seen.add(t.symbol)
        if heat + t.risk_amount > heat_cap:
            deferred.append((t, "HEAT")); continue
        if sect.get(t.sector, 0) + t.risk_amount > sect_cap:
            deferred.append((t, "SECTOR")); continue
        if deployed + t.notional > depl_cap:
            deferred.append((t, "DEPLOYED")); continue
        heat += t.risk_amount
        deployed += t.notional
        sect[t.sector] = sect.get(t.sector, 0) + t.risk_amount
        admitted.append(t)

    by_h: dict = {}
    for t in admitted:
        b = by_h.setdefault(t.horizon, {"count": 0, "notional": 0, "risk": 0})
        b["count"] += 1; b["notional"] += t.notional; b["risk"] += t.risk_amount

    return {
        "capital": capital,
        "admitted": [asdict(t) for t in admitted],
        "deferred": [{"ticket": asdict(t), "cap": c} for t, c in deferred],
        "rejected": [asdict(r) for r in rejected],
        "duplicates": [asdict(d) for d in dupes],
        "by_horizon": by_h,
        "state": {
            "heat": round(heat), "heat_pct": round(heat / capital * 100, 2),
            "deployed": round(deployed), "deployed_pct": round(deployed / capital * 100, 2),
            "cash": round(capital - deployed),
            "heat_cap": round(heat_cap), "deployed_cap": round(depl_cap),
            "at_capacity": heat >= heat_cap or deployed >= depl_cap,
        },
        "alert_types": sorted(ENGINE_HORIZON),
        "max_alert_types": MAX_ALERT_TYPES,
    }
