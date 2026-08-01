"""
signals/expectancy.py — per-engine R:R floors derived from the live ledger.

A single global R:R floor is the wrong instrument. Measured over the ledger on
2026-08-02:

    engine      trades   win%    avg R   total R   break-even R:R
    cf_1h          355   41.7%   +0.458   +162.6            1.40
    breakout        64   17.2%   -1.065    -68.2            4.81
    4h              20   15.0%   -0.462     -9.3            5.67
    commodity       13   23.1%   -0.455     -5.9            3.33

A flat 2.0 floor lets the breakout engine keep publishing while it loses 1.065R
per trade — it just loses more slowly. The floor has to come from each engine's
own record, because break-even R:R is fully determined by win rate:

    breakeven_rr = (1 - p) / p

so an engine winning 17% of the time needs 4.8R to not lose money, and no
target-stretching produces that honestly. When the required floor is so high
that the engine cannot realistically produce a qualifying setup, that IS the
answer: the engine is switched off by its own results rather than by opinion.

Floors are recomputed from the ledger and cached for a day. Engines with fewer
than MIN_SAMPLE closed trades keep the conservative default — a 5-trade
engine at 80% is noise, not evidence.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone

CACHE_PATH = "cache/engine_floors.json"
CACHE_TTL_HOURS = 24

MIN_SAMPLE = 25          # below this, win rate is not evidence
DEFAULT_FLOOR = 2.0      # matches config.MIN_RR
SAFETY = 1.15            # 15% margin over measured break-even
FLOOR_CAP = 6.0          # above this the engine is effectively disabled

WIN = ("TARGET_HIT", "T1_HIT", "T2_HIT", "TP1_HIT", "TP2_HIT", "PROFIT")
LOSS = ("SL_HIT", "STOPPED", "STOP_HIT", "LOSS")

log = logging.getLogger(__name__)

_floors = None


def _fresh(payload: dict) -> bool:
    try:
        ts = datetime.fromisoformat(payload["computed_at"])
    except (KeyError, ValueError, TypeError):
        return False
    return datetime.now(timezone.utc) - ts < timedelta(hours=CACHE_TTL_HOURS)


def compute() -> dict:
    """Query the ledger and derive a floor per signal_type. Never raises."""
    import db

    placeholders_w = ",".join("?" * len(WIN))
    placeholders_l = ",".join("?" * len(LOSS))
    sql = f"""
        SELECT signal_type,
               SUM(CASE WHEN upper(COALESCE(status,'')) IN ({placeholders_w})
                        THEN 1 ELSE 0 END) AS wins,
               SUM(CASE WHEN upper(COALESCE(status,'')) IN ({placeholders_l})
                        THEN 1 ELSE 0 END) AS losses
        FROM all_signals
        GROUP BY signal_type
    """
    rows = []
    try:
        with db.connect() as c:
            rows = c.execute(sql, (*WIN, *LOSS)).fetchall()
    except Exception as e:
        log.warning(f"expectancy: ledger query failed ({e}) — using defaults")
        return {"computed_at": datetime.now(timezone.utc).isoformat(),
                "engines": {}, "degraded": True}

    engines = {}
    for r in rows:
        stype, wins, losses = r[0], int(r[1] or 0), int(r[2] or 0)
        closed = wins + losses
        if not stype or closed == 0:
            continue
        p = wins / closed
        if closed < MIN_SAMPLE:
            engines[str(stype)] = {
                "trades": closed, "win_rate": round(p * 100, 1),
                "breakeven_rr": None, "floor": DEFAULT_FLOOR,
                "status": "insufficient-sample",
            }
            continue
        if p <= 0:
            floor, status = FLOOR_CAP, "disabled"
            breakeven = None
        else:
            breakeven = (1 - p) / p
            floor = round(min(max(breakeven * SAFETY, DEFAULT_FLOOR), FLOOR_CAP), 2)
            status = "disabled" if floor >= FLOOR_CAP else "active"
        engines[str(stype)] = {
            "trades": closed,
            "win_rate": round(p * 100, 1),
            "breakeven_rr": round(breakeven, 2) if breakeven else None,
            "floor": floor,
            "status": status,
        }

    return {"computed_at": datetime.now(timezone.utc).isoformat(),
            "engines": engines, "degraded": False}


def _load() -> dict:
    global _floors
    if _floors is not None:
        return _floors
    try:
        with open(CACHE_PATH) as fh:
            payload = json.load(fh)
        if _fresh(payload):
            _floors = payload
            return _floors
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass

    _floors = compute()
    if not _floors.get("degraded"):
        try:
            os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
            with open(CACHE_PATH, "w") as fh:
                json.dump(_floors, fh, indent=1)
        except OSError as e:
            log.warning(f"expectancy: cache write failed — {e}")
    return _floors


def floor_for(engine: str, default: float = DEFAULT_FLOOR) -> float:
    """R:R floor this engine must clear, from its own measured win rate."""
    info = _load().get("engines", {}).get(engine)
    return float(info["floor"]) if info else float(default)


def report() -> dict:
    """Full per-engine picture — for the site and for logging."""
    return _load()


def refresh() -> dict:
    """Force recompute, ignoring the cache."""
    global _floors
    _floors = None
    try:
        os.remove(CACHE_PATH)
    except OSError:
        pass
    return _load()
