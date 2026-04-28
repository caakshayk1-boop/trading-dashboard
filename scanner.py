"""
Signal engine: Regime Filter → Structure → 3 Setups → Score → Dedup
PDF spec: Part 5–7 of SwingTrading_BuildGuide
"""
import yfinance as yf
import ta as ta_lib
import pandas as pd
import numpy as np
import requests, os, time, logging, functools
from concurrent.futures import ThreadPoolExecutor, as_completed
from config import (MIN_SIGNAL_SCORE, MIN_PRICE, MIN_AVG_VOLUME,
                    ENABLE_WEEKLY_CONFIRM, MAX_PE, MAX_WORKERS, CAPITAL, RISK_PER_TRADE)

os.makedirs("logs", exist_ok=True)
os.makedirs("cache", exist_ok=True)
logging.basicConfig(
    filename="logs/scanner.log", level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

NIFTY500_CSV_URL = "https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv"
NIFTY500_CACHE   = "cache/nifty500.csv"

FALLBACK_NIFTY500 = [
    "RELIANCE.NS","TCS.NS","HDFCBANK.NS","INFY.NS","ICICIBANK.NS",
    "HINDUNILVR.NS","SBIN.NS","BAJFINANCE.NS","BHARTIARTL.NS","KOTAKBANK.NS",
    "LT.NS","AXISBANK.NS","ASIANPAINT.NS","MARUTI.NS","TITAN.NS",
    "SUNPHARMA.NS","WIPRO.NS","ULTRACEMCO.NS","NESTLEIND.NS","POWERGRID.NS",
    "NTPC.NS","HCLTECH.NS","TECHM.NS","ONGC.NS","JSWSTEEL.NS",
    "TATAMOTORS.NS","TATASTEEL.NS","ADANIPORTS.NS","COALINDIA.NS","BPCL.NS",
    "DIVISLAB.NS","DRREDDY.NS","CIPLA.NS","EICHERMOT.NS","BAJAJFINSV.NS",
    "BAJAJ-AUTO.NS","HEROMOTOCO.NS","M&M.NS","BRITANNIA.NS","GRASIM.NS",
    "HINDALCO.NS","INDUSINDBK.NS","IOC.NS","SHREECEM.NS","SBILIFE.NS",
    "HDFCLIFE.NS","APOLLOHOSP.NS","ADANIENT.NS","LTIM.NS","TATACONSUM.NS",
    "AMBUJACEM.NS","AUROPHARMA.NS","BALKRISIND.NS","BANDHANBNK.NS","BERGEPAINT.NS",
    "BIOCON.NS","BOSCHLTD.NS","CANBK.NS","CHOLAFIN.NS","COLPAL.NS",
    "CONCOR.NS","DABUR.NS","DALBHARAT.NS","DLF.NS","GAIL.NS",
    "GODREJCP.NS","GODREJPROP.NS","HAL.NS","HAVELLS.NS","HDFCAMC.NS",
    "IDFCFIRSTB.NS","IGL.NS","INDHOTEL.NS","INDIGO.NS","INDUSTOWER.NS",
    "IRCTC.NS","JUBLFOOD.NS","LUPIN.NS","MARICO.NS","MPHASIS.NS",
    "MRF.NS","MUTHOOTFIN.NS","NAUKRI.NS","NMDC.NS","OBEROIRLTY.NS",
    "OFSS.NS","PAGEIND.NS","PERSISTENT.NS","PETRONET.NS","PIDILITIND.NS",
    "PNB.NS","POLYCAB.NS","SAIL.NS","SBICARD.NS","SRF.NS",
    "TATACOMM.NS","TATAELXSI.NS","TATAPOWER.NS","TRENT.NS","UPL.NS",
    "AARTIIND.NS","ABCAPITAL.NS","ABFRL.NS","ACC.NS","ALKEM.NS",
    "APLLTD.NS","APLAPOLLO.NS","ASTRAL.NS","ATUL.NS","AUBANK.NS",
    "BSOFT.NS","CANFINHOME.NS","CESC.NS","COFORGE.NS","CROMPTON.NS",
    "DEEPAKNTR.NS","DELHIVERY.NS","EMAMILTD.NS","ESCORTS.NS","EXIDEIND.NS",
    "FEDERALBNK.NS","FORTIS.NS","GLENMARK.NS","GNFC.NS","GRANULES.NS",
    "GRINDWELL.NS","GSPL.NS","GUJGASLTD.NS","HINDCOPPER.NS","HINDPETRO.NS",
    "HONAUT.NS","IEX.NS","IPCALAB.NS","IRFC.NS","JBCHEPHARM.NS",
    "JKCEMENT.NS","JSWENERGY.NS","KAJARIACER.NS","KANSAINER.NS","KEI.NS",
    "KPITTECH.NS","LALPATHLAB.NS","LAURUSLABS.NS","LICHSGFIN.NS","LICI.NS",
    "LINDEINDIA.NS","MANAPPURAM.NS","MCDOWELL-N.NS","METROPOLIS.NS","MFSL.NS",
    "NATIONALUM.NS","NATCOPHARM.NS","NAVINFLUOR.NS","NHPC.NS","NYKAA.NS",
    "OLECTRA.NS","PGHH.NS","PHOENIXLTD.NS","PIIND.NS","PVR.NS",
    "RAMCOCEM.NS","REDINGTON.NS","RITES.NS","RVNL.NS","SJVN.NS",
    "SOLARINDS.NS","SONACOMS.NS","STARHEALTH.NS","SUPREMEIND.NS","SYNGENE.NS",
    "THERMAX.NS","TIINDIA.NS","TORNTPHARM.NS","TORNTPOWER.NS","TVSMOTOR.NS",
    "UBL.NS","VBL.NS","VEDL.NS","VOLTAS.NS","ZOMATO.NS",
    "ZYDUSLIFE.NS","CHAMBLFERT.NS","COROMANDEL.NS","CUMMINSIND.NS","DIXON.NS",
    "KALYANKJIL.NS","KNRCON.NS","NLCINDIA.NS","RELAXO.NS","SCHAEFFLER.NS",
    "SUMICHEM.NS","SUNTV.NS","TRIDENT.NS","UJJIVANSFB.NS","VIPIND.NS",
]


# ── Retry decorator ────────────────────────────────────────────────────────────
def with_retry(max_retries=3):
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            for attempt in range(max_retries):
                try:
                    return fn(*args, **kwargs)
                except Exception as e:
                    if attempt == max_retries - 1:
                        raise
                    time.sleep(2 ** attempt)
        return wrapper
    return decorator


def load_nifty500():
    try:
        if os.path.exists(NIFTY500_CACHE):
            age = time.time() - os.path.getmtime(NIFTY500_CACHE)
            if age < 86400:
                df = pd.read_csv(NIFTY500_CACHE)
                syms = [s.strip() + ".NS" for s in df["Symbol"].tolist()]
                logging.info(f"Cache: {len(syms)} symbols")
                return syms
        headers = {"User-Agent": "Mozilla/5.0"}
        r = requests.get(NIFTY500_CSV_URL, headers=headers, timeout=15)
        r.raise_for_status()
        with open(NIFTY500_CACHE, "wb") as f:
            f.write(r.content)
        df = pd.read_csv(NIFTY500_CACHE)
        syms = [s.strip() + ".NS" for s in df["Symbol"].tolist()]
        logging.info(f"NSE download: {len(syms)} symbols")
        return syms
    except Exception as e:
        logging.warning(f"NSE download failed ({e}), using fallback")
        seen = set()
        return [s for s in FALLBACK_NIFTY500 if not (s in seen or seen.add(s))]


def get_nifty50_return():
    try:
        df = yf.download("^NSEI", period="6mo", interval="1d",
                         progress=False, auto_adjust=True)
        c = df["Close"].squeeze()
        return float(c.iloc[-1] / c.iloc[-20] - 1)
    except Exception:
        return 0.0


# ── Indicator helpers (PDF Part 6 — swappable wrappers) ──────────────────────
def ema(series, n):
    return ta_lib.trend.EMAIndicator(series, window=n).ema_indicator()

def rsi(series, n=14):
    return ta_lib.momentum.RSIIndicator(series, window=n).rsi()

def adx(high, low, close, n=14):
    return ta_lib.trend.ADXIndicator(high, low, close, window=n).adx()

def atr(high, low, close, n=14):
    return ta_lib.volatility.AverageTrueRange(high, low, close, window=n).average_true_range()

def macd_line(series):
    return ta_lib.trend.MACD(series).macd()

def macd_signal(series):
    return ta_lib.trend.MACD(series).macd_signal()

def obv(close, volume):
    return ta_lib.volume.OnBalanceVolumeIndicator(close, volume).on_balance_volume()


# ── 5a. Regime Filter (PDF Part 7) ────────────────────────────────────────────
def regime_filter(close, high, low):
    cur_adx   = float(adx(high, low, close).iloc[-1])
    cur_ema200 = float(ema(close, 200).iloc[-1])
    cur_price  = float(close.iloc[-1])

    if cur_adx < 20:
        return None, cur_adx          # flat/choppy — skip entirely
    if cur_adx < 30:
        tradeable = "selective"       # only highest-score setups
    else:
        tradeable = "strong"

    bias = "bullish" if cur_price > cur_ema200 else "bearish"
    return {"tradeable": tradeable, "bias": bias, "adx": round(cur_adx, 1)}, cur_adx


# ── 5b. Structure Detection (fractal swing highs/lows) ────────────────────────
def count_hh_hl(high, low, lookback=40):
    h = high.iloc[-lookback:].values
    l = low.iloc[-lookback:].values
    swing_highs, swing_lows = [], []
    for i in range(2, len(h) - 2):
        if h[i] > h[i-1] and h[i] > h[i-2] and h[i] > h[i+1] and h[i] > h[i+2]:
            swing_highs.append(h[i])
        if l[i] < l[i-1] and l[i] < l[i-2] and l[i] < l[i+1] and l[i] < l[i+2]:
            swing_lows.append(l[i])

    hh_count = sum(1 for i in range(1, len(swing_highs)) if swing_highs[i] > swing_highs[i-1])
    hl_count  = sum(1 for i in range(1, len(swing_lows))  if swing_lows[i]  > swing_lows[i-1])
    return min(hh_count, hl_count), swing_highs, swing_lows


# ── 5c Setup 1: Pullback Continuation ─────────────────────────────────────────
def setup_pullback(close, high, low, volume):
    e20    = ema(close, 20)
    e50    = ema(close, 50)
    cur_adx = float(adx(high, low, close).iloc[-1])
    price  = float(close.iloc[-1])
    avg_v  = float(volume.rolling(20).mean().iloc[-1])
    cur_v  = float(volume.iloc[-1])

    if cur_adx < 25:
        return None, 0
    # Price within 2% of EMA20 (pullback zone)
    near_ema20 = abs(price - float(e20.iloc[-1])) / price < 0.02
    if not near_ema20:
        return None, 0
    # Trend: EMA20 > EMA50
    if float(e20.iloc[-1]) <= float(e50.iloc[-1]):
        return None, 0
    # Volume spike on last candle (entry trigger)
    vol_ratio = cur_v / avg_v if avg_v > 0 else 1
    if vol_ratio < 1.5:
        return None, 0
    # Bullish candle
    bullish = float(close.iloc[-1]) > float(close.iloc[-2])
    score = 0
    if bullish:         score += 10
    if vol_ratio >= 2:  score += 10
    else:               score += 5
    return "pullback", score


# ── 5c Setup 2: Breakout with Retest ──────────────────────────────────────────
def setup_breakout(close, high, low, volume):
    price  = float(close.iloc[-1])
    avg_v  = float(volume.rolling(20).mean().iloc[-1])
    cur_v  = float(volume.iloc[-1])
    vol_ratio = cur_v / avg_v if avg_v > 0 else 1

    # Find resistance: price level tested ≥2x in last 50 bars
    h50 = high.iloc[-50:]
    resistance_candidates = []
    for i in range(len(h50) - 1):
        level = float(h50.iloc[i])
        touches = sum(1 for j in range(len(h50)) if abs(float(h50.iloc[j]) - level) / level < 0.015)
        if touches >= 2:
            resistance_candidates.append(level)

    if not resistance_candidates:
        return None, 0

    resistance = max(resistance_candidates)
    # Price broke above resistance with volume
    broke_out = price > resistance and float(close.iloc[-2]) <= resistance * 1.015
    if not broke_out:
        # Check if retesting (within 1.5% of resistance from above)
        retesting = resistance * 0.985 <= price <= resistance * 1.015
        if not retesting or vol_ratio < 1.5:
            return None, 0

    score = 0
    if vol_ratio >= 2:  score += 10
    elif vol_ratio >= 1.5: score += 6
    if broke_out:       score += 10
    return "breakout", score


# ── 5c Setup 3: RSI Divergence Reversal ───────────────────────────────────────
def setup_divergence(close, high, low, volume):
    if len(close) < 30:
        return None, 0

    rsi_series = rsi(close)
    price_arr  = close.values
    rsi_arr    = rsi_series.values

    # Find last 2 price lows
    lows_idx = []
    for i in range(2, len(price_arr) - 2):
        if price_arr[i] < price_arr[i-1] and price_arr[i] < price_arr[i+1]:
            lows_idx.append(i)
    if len(lows_idx) < 2:
        return None, 0

    i1, i2 = lows_idx[-2], lows_idx[-1]
    # Bullish divergence: price lower low, RSI higher low
    price_ll = price_arr[i2] < price_arr[i1]
    rsi_hl   = rsi_arr[i2]   > rsi_arr[i1]
    if not (price_ll and rsi_hl):
        return None, 0

    # Near support (within 3% of EMA50)
    e50   = float(ema(close, 50).iloc[-1])
    price = float(close.iloc[-1])
    near_support = abs(price - e50) / price < 0.03

    # Reversal candle
    bullish_reversal = float(close.iloc[-1]) > float(close.iloc[-2])

    score = 0
    if near_support:       score += 8
    if bullish_reversal:   score += 7
    divergence_strength = (rsi_arr[i2] - rsi_arr[i1])
    if divergence_strength > 5: score += 5
    return "divergence", score


# ── 5d. Scoring Engine (PDF spec, 0–100) ──────────────────────────────────────
def compute_full_score(close, high, low, volume, setup_type, setup_score,
                       regime, nifty_ret, hh_hl_count):
    score = 0
    reasons = []

    # 1. Trend Strength (20 pts)
    cur_adx = regime["adx"]
    if cur_adx >= 30:
        score += 20; reasons.append(f"Strong trend ADX {cur_adx}")
    elif cur_adx >= 20:
        score += 10; reasons.append(f"Moderate trend ADX {cur_adx}")

    # 2. Structure Clarity (20 pts)
    if hh_hl_count >= 3:
        score += 20; reasons.append("3+ HH/HL swings")
    elif hh_hl_count == 2:
        score += 10; reasons.append("2 HH/HL swings")
    elif hh_hl_count == 1:
        score += 5;  reasons.append("1 HH/HL swing")

    # 3. Volume Confirmation (15 pts)
    avg_v    = float(volume.rolling(20).mean().iloc[-1])
    cur_v    = float(volume.iloc[-1])
    vol_ratio = cur_v / avg_v if avg_v > 0 else 1
    if vol_ratio >= 2.0:
        score += 15; reasons.append(f"Vol {vol_ratio:.1f}x")
    elif vol_ratio >= 1.5:
        score += 10; reasons.append(f"Vol {vol_ratio:.1f}x")

    # 4. Setup Quality (20 pts) — from setup-specific checklist
    score += min(setup_score, 20)
    reasons.append(f"Setup: {setup_type}")

    # 5. Risk/Reward Ratio (15 pts)
    price   = float(close.iloc[-1])
    cur_atr = float(atr(high, low, close).iloc[-1])
    sl2     = price - 1.5 * cur_atr
    t1      = price + cur_atr           # 1R
    t2      = price + 2 * cur_atr       # 2R
    t3      = price + 3.5 * cur_atr     # structural
    risk    = price - sl2
    rr      = (t2 - price) / risk if risk > 0 else 0
    if rr >= 2.5:   score += 15; reasons.append(f"RR {rr:.1f}:1")
    elif rr >= 2.0: score += 10; reasons.append(f"RR {rr:.1f}:1")
    elif rr >= 1.5: score += 5;  reasons.append(f"RR {rr:.1f}:1")

    # 6. Market Condition — relative strength vs Nifty (10 pts)
    stock_ret = float(close.iloc[-1] / close.iloc[-20] - 1) if len(close) >= 20 else 0
    if stock_ret > nifty_ret + 0.03:
        score += 10; reasons.append(f"RS +{(stock_ret-nifty_ret)*100:.1f}% vs Nifty")
    elif stock_ret > nifty_ret:
        score += 5;  reasons.append("Outperforming Nifty")

    # Selective regime: minimum 85 required instead of 70
    min_required = 85 if regime["tradeable"] == "selective" else MIN_SIGNAL_SCORE

    qty  = int((CAPITAL * RISK_PER_TRADE) / risk) if risk > 0 else 0
    rr1  = round((t1 - price) / risk, 2) if risk > 0 else 0
    rr2  = round((t2 - price) / risk, 2) if risk > 0 else 0

    # Structural SL: just below recent swing low
    sl1 = float(low.rolling(10).min().iloc[-1])

    return {
        "score":       score,
        "min_required": min_required,
        "reasons":     reasons,
        "vol_ratio":   round(vol_ratio, 2),
        "rsi_val":     round(float(rsi(close).iloc[-1]), 1),
        "adx_val":     round(cur_adx, 1),
        "atr_val":     round(cur_atr, 2),
        "price":       round(price, 2),
        "sl1":         round(sl1, 2),
        "sl2":         round(sl2, 2),
        "t1":          round(t1, 2),
        "t2":          round(t2, 2),
        "t3":          round(t3, 2),
        "rr1":         rr1,
        "rr2":         rr2,
        "qty":         qty,
    }


def check_weekly_trend(symbol):
    try:
        wdf = yf.download(symbol, period="1y", interval="1wk",
                          progress=False, auto_adjust=True)
        if wdf.empty or len(wdf) < 20:
            return True
        wc   = wdf["Close"].squeeze()
        return float(ema(wc, 10).iloc[-1]) > float(ema(wc, 20).iloc[-1])
    except Exception:
        return True


@with_retry(max_retries=3)
def fetch_data(symbol):
    try:
        from upstox_provider import fetch_ohlcv, is_authenticated
        if is_authenticated():
            df = fetch_ohlcv(symbol, period="1y", interval="1d")
            if df is not None and not df.empty:
                return df
    except Exception:
        pass
    # fallback
    return yf.download(symbol, period="1y", interval="1d",
                       progress=False, auto_adjust=True)


def analyze_stock(symbol, nifty_ret=0.0):
    try:
        df = fetch_data(symbol)
        if df.empty or len(df) < 60:
            return None

        close  = df["Close"].squeeze()
        high   = df["High"].squeeze()
        low    = df["Low"].squeeze()
        volume = df["Volume"].squeeze()

        price = float(close.iloc[-1])
        avg_v = float(volume.rolling(20).mean().iloc[-1])

        if price < MIN_PRICE or avg_v < MIN_AVG_VOLUME:
            return None

        # 5a. Regime filter
        regime, cur_adx = regime_filter(close, high, low)
        if regime is None:
            return None  # ADX < 20, skip

        # 5b. Structure
        hh_hl_count, _, _ = count_hh_hl(high, low)

        # 5c. Run all 3 setups, pick best
        results = [
            setup_pullback(close, high, low, volume),
            setup_breakout(close, high, low, volume),
            setup_divergence(close, high, low, volume),
        ]
        best_setup, best_score = max(results, key=lambda x: x[1])
        if not best_setup:
            return None

        # 5d. Full score
        data = compute_full_score(
            close, high, low, volume,
            best_setup, best_score,
            regime, nifty_ret, hh_hl_count
        )

        if data["score"] < data["min_required"]:
            return None

        # Weekly trend confirmation
        if ENABLE_WEEKLY_CONFIRM and not check_weekly_trend(symbol):
            return None

        # Optional P/E filter
        pe = None
        if MAX_PE > 0:
            try:
                pe = yf.Ticker(symbol).fast_info.pe_ratio
                if pe and pe > MAX_PE:
                    return None
            except Exception:
                pass

        return {
            "symbol":     symbol.replace(".NS", ""),
            "action":     "BUY" if regime["bias"] == "bullish" else "SELL",
            "setup_type": best_setup,
            "price":      data["price"],
            "score":      data["score"],
            "rsi":        data["rsi_val"],
            "adx":        data["adx_val"],
            "vol_ratio":  data["vol_ratio"],
            "atr":        data["atr_val"],
            "sl1":        data["sl1"],
            "sl2":        data["sl2"],
            "target1":    data["t1"],
            "target2":    data["t2"],
            "target3":    data["t3"],
            "rr1":        data["rr1"],
            "rr2":        data["rr2"],
            "qty":        data["qty"],
            "pe":         round(pe, 1) if pe else "N/A",
            "regime":     regime["tradeable"],
            "bias":       regime["bias"],
            "hh_hl":      hh_hl_count,
            "reasons":    ", ".join(data["reasons"]),
            "tv_link":    f"https://in.tradingview.com/chart/?symbol=NSE:{symbol.replace('.NS','')}",
        }
    except Exception as e:
        logging.warning(f"{symbol}: {e}")
        return None


def scan_all(min_score=None):
    from tracker import is_duplicate
    universe   = load_nifty500()
    min_score  = min_score or MIN_SIGNAL_SCORE
    nifty_ret  = get_nifty50_return()
    raw        = []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(analyze_stock, sym, nifty_ret): sym for sym in universe}
        for f in as_completed(futures):
            r = f.result()
            if r and r["score"] >= min_score:
                raw.append(r)

    raw.sort(key=lambda x: x["score"], reverse=True)

    # 5f. Deduplication rules (PDF spec)
    results       = []
    sectors_used  = set()
    for sig in raw:
        if is_duplicate(sig["symbol"]):
            continue
        sector = sig.get("setup_type", "misc")   # using setup_type as proxy
        if sector in sectors_used:
            continue
        results.append(sig)
        sectors_used.add(sector)
        if len(results) >= 5:   # max 5 signals per session
            break

    logging.info(f"Scan done: {len(results)} signals from {len(universe)} stocks")
    return results
