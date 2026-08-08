#!/usr/bin/env python3
"""
cf_engine.py — the single commodity/forex signal engine.

Replaces two divergent implementations that produced different signals and
different prices for the same instrument:
    claude_bot.py::_scan_commodity_forex    (1.5/3.0/5.0R, 5m-bar price)
    scheduled_tasks_runner.py::run_cf_scan  (1.5/2.5/4.0R, fast_info price)

What changed versus those, and why
----------------------------------
1. R:R is now MEASURED, not asserted. The old code set
       t2 = price + 3.0 * risk;  rr = (t2 - price) / risk
   which is 3.0 by construction, so `if rr < 2.5: continue` was dead code and
   every alert printed the same number. Targets now come from real structure
   (swing pivots, day and prior-day levels) and R:R is computed from them, so
   it varies per setup and the gate actually rejects trades.

2. No more selling into exhaustion. `bearish = rsi_4h < 45` had no floor, so
   it shorted CRUDE at 4H RSI 17 and NATGAS at RSI 20. Both sides are bounded.

3. Stops are floored, not tightened. The old code took the TIGHTER of the
   structural and ATR stop (max() for BUY, min() for SELL), which selects for
   stops inside the noise — the NATGAS alert had a stop 1.23x the 1H ATR.
   We take the WIDER, then enforce a hard ATR floor.

4. Volume gates instead of decorating. It was computed and printed but never
   affected the decision.

5. Trend/regime filter — 4H EMA alignment must agree with the direction.

6. RSI/ATR use Wilder's smoothing (EMA, alpha=1/n), matching TradingView.
   The old helpers used rolling().mean() (SMA), so the bot's printed RSI never
   equalled the RSI on the chart being cross-checked.

evaluate() is pure — no network, no Telegram, no DB — so backtest.py can
replay it bar by bar over history with the identical code path the live
scanner uses.
"""

from collections import Counter
from dataclasses import dataclass, asdict, field
import pandas as pd


# ── Tunables. backtest.py sweeps these; do not hardcode them below. ──────────
@dataclass
class Config:
    # 4H RSI regime bands — both sides bounded so we never chase exhaustion.
    rsi_4h_buy:  tuple = (55.0, 75.0)
    rsi_4h_sell: tuple = (25.0, 45.0)
    # 1H entry-timing bands.
    rsi_1h_buy:  tuple = (45.0, 75.0)
    rsi_1h_sell: tuple = (25.0, 55.0)
    # Stop construction.
    atr_mult_sl:  float = 1.5   # ATR-based stop distance
    min_sl_atr:   float = 1.5   # hard floor: stop must be >= this * ATR
    day_buffer:   float = 0.0015
    # Acceptance gates.
    min_rr:       float = 1.8   # measured off structural T2, not asserted
    # T1 must pay back at least the risk. It used to be levels[0] — whatever
    # structural level happened to be nearest — with no distance test at all,
    # so HINDALCO shipped a first target 0.80% above entry against a 4.31%
    # stop: 0.19R. A target you would never take is not a target, and it sat
    # in the table and in Telegram labelled "T1" beside an RR of 2.41 that was
    # quoted off T2. 26 signals across cf_1h, equity_measured, breakout, 4h
    # and commodity carried a T1 closer than their own risk.
    min_rr_t1:    float = 1.0
    min_vol_ratio: float = 1.0  # 1H volume vs 20-bar average (futures volume is
                                # spiky; 1.2 rejected 67% of valid setups)
    require_volume: bool = True
    max_day_move: float = 4.0   # skip if already moved this much today
    # Regime.
    ema_fast: int = 20
    ema_slow: int = 50
    # Structure detection.
    pivot_k:    int = 3         # bars either side for a swing pivot
    level_dedupe: float = 0.003  # merge levels within 0.3%


CONFIG = Config()


# Which gate rejected a candidate, tallied across a run. Lets backtest.py show
# the binding constraint instead of leaving you to guess why nothing fires.
REJECTS = Counter()


def _reject(reason):
    REJECTS[reason] += 1
    return None


# ── Indicators (Wilder's smoothing — matches TradingView) ────────────────────

def rsi(series: pd.Series, n: int = 14) -> pd.Series:
    delta = series.diff()
    gain  = delta.clip(lower=0)
    loss  = (-delta).clip(lower=0)
    avg_g = gain.ewm(alpha=1 / n, adjust=False).mean()
    avg_l = loss.ewm(alpha=1 / n, adjust=False).mean()
    rs    = avg_g / avg_l.replace(0, float("nan"))
    return (100 - 100 / (1 + rs)).fillna(50.0)


def atr(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 14) -> pd.Series:
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low  - close.shift()).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def ema(series: pd.Series, n: int) -> pd.Series:
    return series.ewm(span=n, adjust=False).mean()


# ── Structure ────────────────────────────────────────────────────────────────

def swing_levels(high: pd.Series, low: pd.Series, k: int = 3):
    """Local extrema with k bars either side. Returns (highs, lows)."""
    h, l = high.values, low.values
    hs, ls = [], []
    for i in range(k, len(h) - k):
        if h[i] == h[i - k:i + k + 1].max():
            hs.append(float(h[i]))
        if l[i] == l[i - k:i + k + 1].min():
            ls.append(float(l[i]))
    return hs, ls


def _dedupe(levels, tol: float):
    """Collapse levels sitting within `tol` of one another."""
    out = []
    for lv in sorted(levels):
        if not out or abs(lv - out[-1]) / max(out[-1], 1e-9) > tol:
            out.append(lv)
    return out


def targets_from_structure(price, direction, df1h, df4h, df1d, a, cfg):
    """Real price objectives above (BUY) or below (SELL) entry.

    Pulls swing pivots off 1H and 4H plus today's and yesterday's extremes.
    Pads with ATR projections only when structure runs out, and reports which
    source was used so the alert can say so honestly.
    """
    h1, l1 = swing_levels(df1h["High"].squeeze(), df1h["Low"].squeeze(), cfg.pivot_k)
    h4, l4 = swing_levels(df4h["High"].squeeze(), df4h["Low"].squeeze(), cfg.pivot_k)

    dh = df1d["High"].squeeze()
    dl = df1d["Low"].squeeze()
    day_levels = [float(dh.iloc[-1]), float(dl.iloc[-1])]
    if len(dh) >= 2:
        day_levels += [float(dh.iloc[-2]), float(dl.iloc[-2])]

    pool = h1 + h4 + l1 + l4 + day_levels

    if direction == "BUY":
        cand = [x for x in pool if x > price * 1.002]
        cand = _dedupe(cand, cfg.level_dedupe)
    else:
        cand = [x for x in pool if x < price * 0.998]
        cand = sorted(_dedupe(cand, cfg.level_dedupe), reverse=True)

    source = "structure" if len(cand) >= 2 else "atr"
    d = 1 if direction == "BUY" else -1
    for mult in (1.5, 3.0, 5.0, 7.0, 10.0):   # pad only if structure ran out
        if len(cand) >= 5:
            break
        cand.append(price + d * mult * a)

    return cand[:12], source


# ── The signal ───────────────────────────────────────────────────────────────

def evaluate(name, df1h, df4h, df1d, price=None, cfg: Config = CONFIG):
    """Return a signal dict, or None if the setup fails any gate.

    Pure: no IO. Reasons for rejection are returned via `reject` on a dict
    when explain=True is not needed — here we simply return None so the live
    path stays simple; backtest.py counts Nones.
    """
    if df1h is None or len(df1h) < 30: return None
    if df4h is None or len(df4h) < max(cfg.ema_slow, 20): return None
    if df1d is None or len(df1d) < 2:  return None

    c1 = df1h["Close"].squeeze()
    h1 = df1h["High"].squeeze()
    l1 = df1h["Low"].squeeze()

    if price is None:
        price = float(c1.iloc[-1])
    if price <= 0:
        return None

    day_high = float(df1d["High"].squeeze().iloc[-1])
    day_low  = float(df1d["Low"].squeeze().iloc[-1])
    prev_cls = float(df1d["Close"].squeeze().iloc[-2])
    if day_high <= 0 or day_high == day_low or prev_cls <= 0:
        return None

    day_chg = (price - prev_cls) / prev_cls * 100
    if abs(day_chg) > cfg.max_day_move:
        return _reject("day_move")                     # already gone, not chasing

    a = float(atr(h1, l1, c1).iloc[-1])
    if a <= 0:
        return None

    rsi_1h = float(rsi(c1).iloc[-1])
    c4     = df4h["Close"].squeeze()
    rsi_4h = float(rsi(c4).iloc[-1])

    # Regime: 4H EMA alignment must agree with the trade direction.
    ef = float(ema(c4, cfg.ema_fast).iloc[-1])
    es = float(ema(c4, cfg.ema_slow).iloc[-1])
    up, down = ef > es, ef < es

    day_mid = (day_high + day_low) / 2
    lo4, hi4 = cfg.rsi_4h_buy
    lo1, hi1 = cfg.rsi_1h_buy
    slo4, shi4 = cfg.rsi_4h_sell
    slo1, shi1 = cfg.rsi_1h_sell

    if   up   and lo4  <= rsi_4h <= hi4  and lo1  <= rsi_1h <= hi1  and price >= day_mid:
        direction = "BUY"
    elif down and slo4 <= rsi_4h <= shi4 and slo1 <= rsi_1h <= shi1 and price <= day_mid:
        direction = "SELL"
    else:
        return _reject("no_setup")

    # Volume must confirm, not decorate.
    vol_ratio = None
    if "Volume" in df1h.columns:
        v = df1h["Volume"].squeeze().replace(0, float("nan"))
        avg = float(v.iloc[-20:].mean())
        cur = float(v.iloc[-1])
        if avg > 0 and cur > 0:
            vol_ratio = cur / avg
            if cfg.require_volume and vol_ratio < cfg.min_vol_ratio:
                return _reject("volume")

    # Stop: WIDER of structural and ATR, then an absolute ATR floor so the
    # stop can never sit inside the bar-to-bar noise.
    if direction == "BUY":
        sl = min(day_low * (1 - cfg.day_buffer), price - cfg.atr_mult_sl * a)
        sl = min(sl, price - cfg.min_sl_atr * a)
    else:
        sl = max(day_high * (1 + cfg.day_buffer), price + cfg.atr_mult_sl * a)
        sl = max(sl, price + cfg.min_sl_atr * a)

    risk = abs(price - sl)
    if risk <= 0:
        return None

    levels, tsrc = targets_from_structure(price, direction, df1h, df4h, df1d, a, cfg)
    if not levels:
        return _reject("no_targets")

    # T2 is the NEAREST level that actually pays for the risk taken — picking a
    # fixed index instead means a wide stop can never qualify, which is what
    # strangled the first run.
    t2 = next((lv for lv in levels if abs(lv - price) / risk >= cfg.min_rr), None)
    if t2 is None:
        return _reject("min_rr")           # no level far enough to be worth it

    # T1 gets the same treatment at a lower bar: the nearest level that at
    # least returns the risk. `levels[0]` was taken blind, which is how a
    # 0.19R "target" reached the ledger. If no structural level is far enough,
    # synthesise one at exactly min_rr_t1 — an honest R-multiple beats a level
    # that happens to be close. t1_source records which, so a reader can tell.
    t1 = next((lv for lv in levels if abs(lv - price) / risk >= cfg.min_rr_t1), None)
    t1_source = "structure"
    if t1 is None:
        t1 = price + cfg.min_rr_t1 * risk if direction == "BUY" \
            else price - cfg.min_rr_t1 * risk
        t1_source = "r_multiple"
    # T1 must never sit beyond T2 — that would invert the scale-out.
    if (direction == "BUY" and t1 > t2) or (direction != "BUY" and t1 < t2):
        t1, t1_source = t2, "clamped_to_t2"

    rr = abs(t2 - price) / risk            # measured off a real level
    beyond = [lv for lv in levels if abs(lv - price) > abs(t2 - price)]
    t3 = beyond[0] if beyond else None

    return {
        "name": name, "bias": direction, "price": round(price, 4),
        "sl": round(sl, 4),
        "t1": round(t1, 4), "t2": round(t2, 4),
        "t3": round(t3, 4) if t3 is not None else None,
        "rr": round(rr, 2),
        # RR has always been measured to T2. That is defensible, but the table
        # shows T1, T2 and one RR, so a reader reasonably reads it against T1.
        # Publish both and the ambiguity disappears.
        "rr_t1": round(abs(t1 - price) / risk, 2),
        "risk_pct": round(risk / price * 100, 2),
        "sl_atr_mult": round(risk / a, 2),
        "target_source": tsrc,
        "t1_source": t1_source,
        "rsi_4h": round(rsi_4h, 1), "rsi_1h": round(rsi_1h, 1),
        "vol_ratio": round(vol_ratio, 2) if vol_ratio else None,
        "day_high": round(day_high, 4), "day_low": round(day_low, 4),
        "day_chg": round(day_chg, 2),
        "atr_1h": round(a, 4),
        "score": _score(rr, vol_ratio, rsi_4h, direction, tsrc, cfg),
    }


def _score(rr, vol_ratio, rsi_4h, direction, tsrc, cfg):
    """0–100 conviction. Unlike the old constant R:R, this actually varies."""
    s = 0.0
    s += min(rr / 4.0, 1.0) * 40                                    # reward
    s += min((vol_ratio or 1.0) / 2.5, 1.0) * 20                    # participation
    mid = 65 if direction == "BUY" else 35                          # sweet spot
    s += max(0.0, 1 - abs(rsi_4h - mid) / 25) * 25                  # momentum quality
    s += 15 if tsrc == "structure" else 5                           # real levels
    return round(s)


# ── Live scan (the only IO in this module) ───────────────────────────────────

CF_SYMBOLS = ["GOLD", "SILVER", "CRUDE", "NATGAS"]

# Yahoo serves COMEX front-month futures for the metals, which trade above
# spot XAU/USD — ~$59 on gold as of 2026-07-29. Say so in the alert rather
# than let the number look wrong against TradingView. Setting
# TWELVEDATA_API_KEY will let a future spot feed replace this.
_FUTURES_NOTE = {"GOLD", "SILVER", "CRUDE", "NATGAS"}


def fetch(symbol: str):
    """Pull the frames evaluate() needs, plus a live price. None if unusable."""
    import yfinance as yf
    from symbols import to_yahoo

    t = to_yahoo(symbol)
    d1h = yf.download(t, period="7d",  interval="1h", progress=False, auto_adjust=True)
    d4h = yf.download(t, period="60d", interval="4h", progress=False, auto_adjust=True)
    d1d = yf.download(t, period="5d",  interval="1d", progress=False, auto_adjust=True)
    if any(d is None or d.empty for d in (d1h, d4h, d1d)):
        return None

    # 5m close is steadier than fast_info.last_price, which can serve a stale
    # or contract-rolled tick. The two old engines disagreed on exactly this.
    price = None
    try:
        d5 = yf.download(t, period="1d", interval="5m",
                         progress=False, auto_adjust=True)
        if d5 is not None and not d5.empty:
            price = float(d5["Close"].to_numpy().ravel()[-1])
    except Exception:
        pass
    if not price or price <= 0:
        price = float(d1h["Close"].to_numpy().ravel()[-1])
    return d1h, d4h, d1d, price


def scan(symbols=None, cfg: Config = CONFIG):
    """Fetch + evaluate each symbol. Returns signals sorted by conviction."""
    import logging
    out = []
    for s in (symbols or CF_SYMBOLS):
        try:
            frames = fetch(s)
            if frames is None:
                logging.warning(f"cf_engine: no data for {s}")
                continue
            d1h, d4h, d1d, price = frames
            sig = evaluate(s, d1h, d4h, d1d, price=price, cfg=cfg)
            if sig:
                out.append(sig)
        except Exception as e:
            logging.warning(f"cf_engine: {s} failed — {e}")
    return sorted(out, key=lambda x: -x["score"])


def format_alert(sig: dict) -> str:
    """One signal as a Telegram Markdown block."""
    arrow = "📈" if sig["bias"] == "BUY" else "📉"
    dec   = 4 if sig["price"] < 100 else 2
    f     = lambda v: f"{v:,.{dec}f}"
    vol   = f" · Vol `{sig['vol_ratio']}x`" if sig.get("vol_ratio") else ""
    t3    = f"\n*T3:* `{f(sig['t3'])}`" if sig.get("t3") else ""
    src   = "structural levels" if sig["target_source"] == "structure" else "ATR projection"
    note  = "\n_COMEX front-month futures — trades above spot_" if sig["name"] in _FUTURES_NOTE else ""

    return (
        f"━━━━━━━━━━━━━━\n"
        f"{arrow} *{sig['name']}* | *{sig['bias']}*  ·  score `{sig['score']}/100`\n"
        f"Price `{f(sig['price'])}` ({sig['day_chg']:+.2f}% day){note}\n"
        f"Day H/L `{f(sig['day_high'])}` / `{f(sig['day_low'])}`\n"
        f"4H RSI `{sig['rsi_4h']}` · 1H RSI `{sig['rsi_1h']}`{vol}\n\n"
        f"*Entry:* `{f(sig['price'])}`\n"
        f"*SL:*    `{f(sig['sl'])}`  _({sig['risk_pct']}% · {sig['sl_atr_mult']}× ATR)_\n"
        f"*T1:* `{f(sig['t1'])}`\n"
        f"*T2:* `{f(sig['t2'])}`  _(R:R {sig['rr']}:1)_{t3}\n"
        f"_Targets from {src}_"
    )
