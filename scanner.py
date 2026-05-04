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

NIFTY500_CSV_URL  = "https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv"
NIFTY1000_CSV_URL = "https://nsearchives.nseindia.com/content/indices/ind_nifty1000list.csv"
NIFTY500_CACHE    = "cache/nifty500.csv"
NIFTY1000_CACHE   = "cache/nifty1000.csv"

# NSE F&O eligible stocks (Nifty 200 + major midcap with liquid options)
FNO_ELIGIBLE = {
    "RELIANCE","TCS","HDFCBANK","INFY","ICICIBANK","HINDUNILVR","SBIN","BAJFINANCE",
    "BHARTIARTL","KOTAKBANK","LT","AXISBANK","ASIANPAINT","MARUTI","TITAN","SUNPHARMA",
    "WIPRO","ULTRACEMCO","NESTLEIND","POWERGRID","NTPC","HCLTECH","TECHM","ONGC",
    "JSWSTEEL","TATAMOTORS","TATASTEEL","ADANIPORTS","COALINDIA","BPCL","DIVISLAB",
    "DRREDDY","CIPLA","EICHERMOT","BAJAJFINSV","BAJAJ-AUTO","HEROMOTOCO","M&M",
    "BRITANNIA","GRASIM","HINDALCO","INDUSINDBK","IOC","SHREECEM","SBILIFE","HDFCLIFE",
    "APOLLOHOSP","ADANIENT","LTIM","TATACONSUM","AMBUJACEM","AUROPHARMA","BALKRISIND",
    "BANDHANBNK","BERGEPAINT","BIOCON","BOSCHLTD","CANBK","CHOLAFIN","COLPAL","CONCOR",
    "DABUR","DALBHARAT","DLF","GAIL","GODREJCP","GODREJPROP","HAL","HAVELLS","HDFCAMC",
    "IDFCFIRSTB","IGL","INDHOTEL","INDIGO","INDUSTOWER","IRCTC","JUBLFOOD","LUPIN",
    "MARICO","MPHASIS","MRF","MUTHOOTFIN","NAUKRI","NMDC","OFSS","PAGEIND","PERSISTENT",
    "PETRONET","PIDILITIND","PNB","POLYCAB","SAIL","SBICARD","SRF","TATACOMM",
    "TATAELXSI","TATAPOWER","TRENT","UPL","FEDERALBNK","COFORGE","DEEPAKNTR",
    "ESCORTS","EXIDEIND","FORTIS","GLENMARK","GRANULES","HINDCOPPER","HINDPETRO",
    "LICHSGFIN","MANAPPURAM","MOTHERSON","MFSL","NYKAA","OBEROIRLTY","PEL","PGHH",
    "PIIND","PVRINOX","RBLBANK","RECLTD","STARHEALTH","SUPREMEIND","TORNTPHARM",
    "TORNTPOWER","TRIDENT","VEDL","VOLTAS","ZOMATO","ADANIGREEN","ADANIPOWER",
    "ADANITRANS","AWL","DMART","JKCEMENT","LALPATHLAB","METROPOLIS","NUVOCO",
    "POLICYBZR","CAMPUS","PATANJALI","AARTIIND","ABCAPITAL","ABB","ABFRL","ACC",
    "ALKEM","APLLTD","ASTRAL","ATUL","AUBANK","BSOFT","CANFINHOME","CESC","CROMPTON",
    "DELHIVERY","EMAMILTD","GNFC","GRINDWELL","GSPL","GUJGASLTD","IDEA","IPCALAB",
    "JBCHEPHARM","JSWENERGY","KANSAINER","KARURVYSYA","KEI","LAURUSLABS","LTTS",
    "MAXHEALTH","MCX","NATIONALUM","NBCC","NCC","NHPC","NLCINDIA","NTPCGREEN",
    "NYKAA","ONGC","PATANJALI","PHOENIXLTD","RAJESHEXPO","RAMCOCEM","RATNAMANI",
    "RITES","SJVN","SONACOMS","SYNGENE","TANLA","TATACHEMICALS","TATAINVEST",
    "TATACOMM","TCNSBRANDS","TIINDIA","TIMKEN","TNPL","TRITURBINE","UJJIVANSFB",
    "UNIONBANK","UCOBANK","USHAMART","VGUARD","WHIRLPOOL","WOCKPHARMA","ZEEL",
}


def _next_thursday(weeks_ahead=0):
    """Return date of the Nth upcoming Thursday (NSE weekly expiry)."""
    from datetime import date, timedelta
    today = date.today()
    days_to_thu = (3 - today.weekday()) % 7  # 0=Mon … 6=Sun, Thu=3
    if days_to_thu == 0:
        days_to_thu = 7  # if today IS Thursday, use next one
    first_thu = today + timedelta(days=days_to_thu + weeks_ahead * 7)
    return first_thu

def _last_thursday_of_month():
    """Last Thursday of current month — NSE monthly expiry."""
    from datetime import date, timedelta
    import calendar
    today = date.today()
    # last day of month
    last_day = date(today.year, today.month, calendar.monthrange(today.year, today.month)[1])
    # walk backward to Thursday (weekday 3)
    delta = (last_day.weekday() - 3) % 7
    return last_day - timedelta(days=delta)

def _smart_expiry(score: int, adx: float, setup_type: str, atr_pct: float) -> tuple:
    """
    Expert-grade expiry selection:
    Rule 1: NEVER buy options with < 21 DTE (theta destroys value)
    Rule 2: Match expiry to signal velocity — breakout=biweekly, divergence=monthly
    Rule 3: High ATR (volatile) → ATM (cheaper delta), Low ATR → slight OTM (leverage)
    """
    from datetime import date, timedelta
    today = date.today()

    def _safe_expiry(weeks_ahead):
        """Get expiry with >= 21 DTE guarantee."""
        exp = _next_thursday(weeks_ahead=weeks_ahead)
        days_left = (exp - today).days
        # If less than 21 DTE, roll to next week
        while days_left < 21:
            weeks_ahead += 1
            exp = _next_thursday(weeks_ahead=weeks_ahead)
            days_left = (exp - today).days
            if weeks_ahead > 8:
                break  # safety cap
        return exp, days_left

    if score >= 85 and adx > 28:
        # Strong breakout — biweekly minimum (enough time to play out)
        exp_date, days_left = _safe_expiry(1)
        label = f"Bi-Weekly · Thu {exp_date.strftime('%d %b')}"
        tier  = "biweekly"
    elif score >= 78 or (adx > 22 and setup_type == "breakout"):
        # Good setup — biweekly
        exp_date, days_left = _safe_expiry(1)
        label = f"Bi-Weekly · Thu {exp_date.strftime('%d %b')}"
        tier  = "biweekly"
    elif setup_type == "divergence":
        # Reversal plays need more time — monthly
        exp_date = _last_thursday_of_month()
        days_left = (exp_date - today).days
        if days_left < 21:
            import calendar as _cal
            d = today
            nm = 1 if d.month == 12 else d.month + 1
            ny = d.year + 1 if d.month == 12 else d.year
            last = date(ny, nm, _cal.monthrange(ny, nm)[1])
            delta = (last.weekday() - 3) % 7
            exp_date = last - timedelta(days=delta)
            days_left = (exp_date - today).days
        label = f"Monthly · Thu {exp_date.strftime('%d %b')}"
        tier  = "monthly"
    else:
        # Default — biweekly with 21 DTE guarantee
        exp_date, days_left = _safe_expiry(1)
        label = f"Bi-Weekly · Thu {exp_date.strftime('%d %b')}"
        tier  = "biweekly"

    hold_str = f"{days_left} days"
    # High ATR volatile stock → ATM (reduce gamma risk), stable → slight OTM (leverage)
    opt_type = "ATM" if atr_pct > 3.0 else "OTM"
    return label, hold_str, tier, opt_type


def _fno_suggest(symbol, price, bias, atr, score=78, adx=25.0, setup_type="breakout"):
    """
    Expert-grade F&O suggestion:
    • Minimum 21 DTE always (never theta trap)
    • Debit spread strategy: buy ATM, sell 2 strikes OTM (defined risk, better breakeven)
    • Naked option only for highest conviction (score >= 85, ADX > 28)
    • Lot size: max 2% capital risk per trade
    """
    from config import CAPITAL
    direction  = "CALL" if bias == "bullish" else "PUT"
    step       = 50 if price > 5000 else (20 if price > 1000 else (10 if price > 500 else 5))
    atm        = round(price / step) * step
    otm1_strike = atm + step if direction == "CALL" else atm - step     # 1 strike OTM
    otm2_strike = atm + 2*step if direction == "CALL" else atm - 2*step # 2 strikes OTM (spread short leg)
    risk_pts    = round(atr * 1.5, 1)
    atr_pct     = (atr / price * 100) if price > 0 else 2.0

    exp_label, hold_str, tier, opt_type = _smart_expiry(score, adx, setup_type, atr_pct)

    # Strategy selection:
    # High conviction (score >= 85): naked ATM option (aggressive)
    # Standard (score 78-84): bull/bear debit spread (defined risk)
    if score >= 85 and adx > 28:
        strategy    = "Naked Option"
        use_strike  = atm
        spread_note = f"Buy {symbol} {atm} {direction} (ATM)"
        max_risk    = round(atr * 1.5 * 100, 0)  # approx 1.5 ATR in INR per lot
    else:
        strategy    = "Debit Spread"
        use_strike  = atm
        spread_note = (f"Buy {atm} {direction} + Sell {otm2_strike} {direction} "
                       f"(debit spread — capped risk)")
        max_risk    = round(abs(otm2_strike - atm) * 0.6 * 100, 0)  # ~60% of width

    # Capital risk: 2% max per F&O trade
    max_capital_risk = CAPITAL * 0.02
    tier_emoji = {"weekly": "⚡", "biweekly": "📅", "monthly": "📆"}.get(tier, "📅")

    return {
        "direction":    direction,
        "atm_strike":   atm,
        "otm_strike":   otm1_strike,
        "spread_short": otm2_strike,
        "use_strike":   use_strike,
        "opt_type":     opt_type,
        "strategy":     strategy,
        "risk_pts":     risk_pts,
        "expiry":       exp_label,
        "hold_days":    hold_str,
        "tier":         tier,
        "tier_emoji":   tier_emoji,
        "note":         (f"{tier_emoji} {spread_note} | "
                         f"Expiry: {exp_label} | Hold ~{hold_str} | "
                         f"Max risk ~₹{int(max_risk):,}"),
    }

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


def _load_nse_csv(url, cache_path):
    """Download NSE index CSV, cache 24h, return list of symbol.NS strings."""
    try:
        if os.path.exists(cache_path):
            age = time.time() - os.path.getmtime(cache_path)
            if age < 86400:
                df = pd.read_csv(cache_path)
                syms = [s.strip() + ".NS" for s in df["Symbol"].tolist()]
                return syms
        headers = {"User-Agent": "Mozilla/5.0 (compatible)"}
        r = requests.get(url, headers=headers, timeout=20)
        r.raise_for_status()
        with open(cache_path, "wb") as f:
            f.write(r.content)
        df = pd.read_csv(cache_path)
        syms = [s.strip() + ".NS" for s in df["Symbol"].tolist()]
        logging.info(f"NSE download {url}: {len(syms)} symbols")
        return syms
    except Exception as e:
        logging.warning(f"NSE download failed ({url}): {e}")
        return []


def load_nifty500():
    """Load Nifty 1000 universe (try 1000 first, fallback to 500, then static list)."""
    # Try Nifty 1000 first
    syms = _load_nse_csv(NIFTY1000_CSV_URL, NIFTY1000_CACHE)
    if len(syms) >= 800:
        logging.info(f"Universe: Nifty 1000 ({len(syms)} stocks)")
        return syms
    # Fallback: Nifty 500
    syms = _load_nse_csv(NIFTY500_CSV_URL, NIFTY500_CACHE)
    if len(syms) >= 400:
        logging.info(f"Universe: Nifty 500 ({len(syms)} stocks)")
        return syms
    # Static fallback
    logging.warning("Using static fallback universe")
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


# ── 5c Setup 1: Pullback Continuation (Expert Grade) ─────────────────────────
# Logic: 21-EMA pullback entry in confirmed uptrend (institutional magnet zone)
# Entry: Price tags rising 21 EMA within 1.5%, bounces with volume → continuation
# Filters: EMA stack (20>50>200), RSI momentum zone 42-65 (not overbought),
#          ADX>25 confirms trend, vol 2x avg on entry bar, prior structure HH/HL
def setup_pullback(close, high, low, volume):
    e20    = ema(close, 20)
    e50    = ema(close, 50)
    e200   = ema(close, 200)
    cur_adx = float(adx(high, low, close).iloc[-1])
    price  = float(close.iloc[-1])
    avg_v  = float(volume.rolling(20).mean().iloc[-1])
    cur_v  = float(volume.iloc[-1])

    if cur_adx < 25:
        return None, 0

    # Full EMA stack: price > EMA20 > EMA50 > EMA200 (institutional uptrend)
    e20v = float(e20.iloc[-1]); e50v = float(e50.iloc[-1]); e200v = float(e200.iloc[-1])
    if not (e20v > e50v > e200v):
        return None, 0

    # Price within 1.5% of EMA20 — tighter zone, higher-quality entry
    near_ema20 = abs(price - e20v) / price < 0.015
    if not near_ema20:
        return None, 0

    # RSI momentum zone: 42-65 (has momentum, not overbought — institutional)
    cur_rsi = float(rsi(close).iloc[-1])
    if not (42 <= cur_rsi <= 65):
        return None, 0

    # Volume 2x — institutional participation on bounce
    vol_ratio = cur_v / avg_v if avg_v > 0 else 1
    if vol_ratio < 2.0:
        return None, 0

    # Bullish candle close (close > open and close > prior close)
    bullish = float(close.iloc[-1]) > float(close.iloc[-2])
    score = 0
    if bullish:               score += 10
    if vol_ratio >= 2.5:      score += 10
    elif vol_ratio >= 2.0:    score += 7
    if cur_rsi >= 55:         score += 5   # in trending momentum zone
    return "pullback", score


# ── 5c Setup 2: Breakout with Retest (Expert Grade) ──────────────────────────
# Logic: Multi-touch resistance level (3+ tests = strong level, breakout is real)
# Entry: Price closes above resistance on 2x+ volume (institutional buy)
# OR: Retest of breakout level as new support (second entry, lower risk)
# Filters: Resistance tested 3+ times, vol 2x+ mandatory, RSI 50-70
def setup_breakout(close, high, low, volume):
    price  = float(close.iloc[-1])
    avg_v  = float(volume.rolling(20).mean().iloc[-1])
    cur_v  = float(volume.iloc[-1])
    vol_ratio = cur_v / avg_v if avg_v > 0 else 1

    # Mandatory: volume 2x average (institutional breakout, not retail noise)
    if vol_ratio < 2.0:
        return None, 0

    # RSI must be 50-72 (momentum present, not overbought)
    cur_rsi = float(rsi(close).iloc[-1])
    if not (50 <= cur_rsi <= 72):
        return None, 0

    # Find resistance: price level tested ≥3x in last 60 bars (stronger level)
    h60 = high.iloc[-60:]
    resistance_candidates = []
    for i in range(len(h60) - 1):
        level = float(h60.iloc[i])
        touches = sum(1 for j in range(len(h60)) if abs(float(h60.iloc[j]) - level) / level < 0.012)
        if touches >= 3:   # 3+ touches = strong institutional level
            resistance_candidates.append(level)

    if not resistance_candidates:
        return None, 0

    resistance = max(resistance_candidates)
    # Clean breakout: close above resistance (not just wick — use close, not high)
    broke_out = price > resistance and float(close.iloc[-2]) <= resistance * 1.012
    if not broke_out:
        # Retest: price pulling back to resistance as new support (0.5-1.5%)
        retesting = resistance * 0.985 <= price <= resistance * 1.005
        if not retesting:
            return None, 0

    score = 0
    if vol_ratio >= 3.0:      score += 10
    elif vol_ratio >= 2.0:    score += 7
    if broke_out:             score += 10
    if cur_rsi >= 60:         score += 5   # strong momentum
    return "breakout", score


# ── 5c Setup 3: RSI Divergence Reversal (Expert Grade) ───────────────────────
# Logic: Bullish hidden divergence + MACD histogram turning green at support
# Entry: Price makes lower low, RSI makes higher low (momentum not following)
# Confluence: Near EMA50 support + bullish engulfing/hammer candle + MACD turn
# This detects early institutional accumulation before price recovers
def setup_divergence(close, high, low, volume):
    if len(close) < 40:
        return None, 0

    rsi_series  = rsi(close)
    macd_h      = macd_line(close) - macd_signal(close)   # MACD histogram
    price_arr   = close.values
    rsi_arr     = rsi_series.values

    # RSI must be oversold zone (< 50) for reversal setup
    cur_rsi = float(rsi_series.iloc[-1])
    if cur_rsi > 52:
        return None, 0

    # Find last 2 swing price lows
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

    # Divergence must be significant (RSI difference > 5 pts)
    divergence_strength = float(rsi_arr[i2] - rsi_arr[i1])
    if divergence_strength < 5:
        return None, 0

    # Near EMA50 or EMA200 support (within 3%)
    e50   = float(ema(close, 50).iloc[-1])
    e200  = float(ema(close, 200).iloc[-1])
    price = float(close.iloc[-1])
    near_support = (abs(price - e50) / price < 0.03 or
                    abs(price - e200) / price < 0.03)

    # MACD histogram turning from negative to less negative or positive (momentum shift)
    macd_turning = (len(macd_h) >= 3 and
                    float(macd_h.iloc[-1]) > float(macd_h.iloc[-2]))

    # Bullish reversal candle
    bullish_reversal = float(close.iloc[-1]) > float(close.iloc[-2])

    score = 0
    if near_support:              score += 8
    if bullish_reversal:          score += 7
    if macd_turning:              score += 8   # MACD confluence — strongest signal
    if divergence_strength > 10:  score += 5
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

        # All 3 setups are bullish by design — only generate BUY signals.
        # Bearish/short scanner is a separate module.
        if regime["bias"] != "bullish":
            return None

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

        sym_clean = symbol.replace(".NS", "")
        fno = sym_clean in FNO_ELIGIBLE
        fno_suggestion = _fno_suggest(
            sym_clean, data["price"], regime["bias"], data["atr_val"],
            score=data["score"], adx=data["adx_val"], setup_type=best_setup
        ) if fno else None
        return {
            "symbol":         sym_clean,
            "action":         "BUY" if regime["bias"] == "bullish" else "SELL",
            "setup_type":     best_setup,
            "price":          data["price"],
            "score":          data["score"],
            "rsi":            data["rsi_val"],
            "adx":            data["adx_val"],
            "vol_ratio":      data["vol_ratio"],
            "atr":            data["atr_val"],
            "sl1":            data["sl1"],
            "sl2":            data["sl2"],
            "target1":        data["t1"],
            "target2":        data["t2"],
            "target3":        data["t3"],
            "rr1":            data["rr1"],
            "rr2":            data["rr2"],
            "qty":            data["qty"],
            "pe":             round(pe, 1) if pe else "N/A",
            "regime":         regime["tradeable"],
            "bias":           regime["bias"],
            "hh_hl":          hh_hl_count,
            "reasons":        ", ".join(data["reasons"]),
            "tv_link":        f"https://in.tradingview.com/chart/?symbol=NSE:{sym_clean}",
            "fno_eligible":   fno,
            "fno_suggestion": fno_suggestion,
        }
    except Exception as e:
        logging.warning(f"{symbol}: {e}")
        return None


def scan_all(min_score=None):
    from tracker import is_duplicate
    from config import MIN_RR
    universe   = load_nifty500()
    min_score  = min_score or MIN_SIGNAL_SCORE
    nifty_ret  = get_nifty50_return()
    raw        = []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(analyze_stock, sym, nifty_ret): sym for sym in universe}
        for f in as_completed(futures):
            r = f.result()
            if r and r["score"] >= min_score:
                # Quality gate: minimum RR filter
                rr = r.get("rr2") or r.get("rr1") or 0
                if rr < MIN_RR:
                    continue
                raw.append(r)

    raw.sort(key=lambda x: x["score"], reverse=True)

    # Deduplication: skip active trades, keep best signal per symbol, max 5 (quality)
    results      = []
    seen_symbols = set()
    for sig in raw:
        sym_clean = sig["symbol"].replace(".NS", "")
        if sym_clean in seen_symbols:
            continue
        if is_duplicate(sym_clean, "swing"):
            continue
        results.append(sig)
        seen_symbols.add(sym_clean)
        if len(results) >= 8:   # max 8 top-quality signals (expert grade)
            break

    logging.info(f"Scan done: {len(results)} signals from {len(universe)} stocks "
                 f"(score≥{min_score}, RR≥{MIN_RR})")
    return results


# ── Privacy: obfuscate indicator names for display ────────────────────────────
_REASON_MAP = [
    ("RSI",       "Momentum Confirmed"),
    ("EMA",       "Trend Aligned"),
    ("ADX",       "Trend Strength"),
    ("volume",    "Volume Surge"),
    ("breakout",  "Resistance Break"),
    ("pullback",  "Pullback Entry"),
    ("divergen",  "Reversal Signal"),
    ("HH/HL",     "Bullish Structure"),
    ("weekly",    "Multi-TF Confirm"),
    ("ATR",       "Volatility Signal"),
]

def obfuscate_reasons(reasons_str):
    """Convert technical indicator names to generic labels."""
    out = set()
    for part in reasons_str.split(","):
        part = part.strip()
        matched = False
        for key, label in _REASON_MAP:
            if key.lower() in part.lower():
                out.add(label)
                matched = True
                break
        if not matched and part:
            out.add("Signal Confirmed")
    return ", ".join(sorted(out))


# ── Breakout Scanner ──────────────────────────────────────────────────────────
def _check_breakouts(df_d, df_w, df_m):
    """Detect confirmed breakout patterns across timeframes."""
    found = []

    if df_d is not None and len(df_d) >= 60:
        close  = df_d["Close"].squeeze()
        high   = df_d["High"].squeeze()
        low    = df_d["Low"].squeeze()
        volume = df_d["Volume"].squeeze()
        avg_vol = volume.rolling(20).mean().iloc[-1]

        # 52-week high breakout (daily close, not intraday)
        high_252 = high.rolling(min(252, len(high)-1)).max().iloc[-2]
        if close.iloc[-1] > high_252 and volume.iloc[-1] > 1.5 * avg_vol:
            found.append(("Daily", "52W Breakout"))

        # Ascending triangle: flat top ±1.5%, rising lows over 20 days
        if len(close) >= 20:
            r_high = high.tail(20)
            r_low  = low.tail(20)
            top    = r_high.max()
            if top > 0 and (r_high.max() - r_high.min()) / top < 0.015:
                slope = np.polyfit(range(20), r_low.values, 1)[0]
                if slope > 0 and close.iloc[-1] >= top * 0.99:
                    found.append(("Daily", "Ascending Triangle"))

        # Bull flag: prior strong move + tight consolidation + breakout
        if len(close) >= 22:
            prior_move = (close.iloc[-11] - close.iloc[-21]) / close.iloc[-21] * 100
            flag_hi    = close.tail(10).max()
            flag_range = (flag_hi - close.tail(10).min()) / close.iloc[-11] * 100
            if prior_move > 8 and flag_range < 5 and close.iloc[-1] >= flag_hi * 0.99:
                found.append(("Daily", "Bull Flag"))

    if df_w is not None and len(df_w) >= 22:
        wclose = df_w["Close"].squeeze()
        whigh  = df_w["High"].squeeze()
        wvol   = df_w["Volume"].squeeze()
        wavg   = wvol.rolling(10).mean().iloc[-1]
        w20hi  = whigh.rolling(20).max().iloc[-2]
        if wclose.iloc[-1] > w20hi and wvol.iloc[-1] > 1.3 * wavg:
            found.append(("Weekly", "20W Breakout"))

        # Cup & handle (weekly): U-shape recovery + consolidation
        if len(wclose) >= 30:
            cup_low  = wclose.iloc[-30:-10].min()
            cup_left = wclose.iloc[-30]
            cup_right = wclose.iloc[-10]
            handle_low = wclose.iloc[-10:].min()
            if (cup_right > cup_low * 1.05 and cup_left > cup_low * 1.05
                    and handle_low > cup_low * 0.95
                    and wclose.iloc[-1] >= cup_right * 0.99):
                found.append(("Weekly", "Cup & Handle"))

    if df_m is not None and len(df_m) >= 8:
        mclose = df_m["Close"].squeeze()
        mhigh  = df_m["High"].squeeze()
        m6hi   = mhigh.rolling(6).max().iloc[-2]
        if mclose.iloc[-1] > m6hi:
            found.append(("Monthly", "6M Breakout"))

    return found


def analyze_breakout(symbol):
    """Full breakout analysis: daily + weekly + monthly."""
    try:
        sym_yf = symbol if symbol.endswith(".NS") else symbol + ".NS"
        df_d = yf.download(sym_yf, period="2y",  interval="1d",  progress=False, auto_adjust=True)
        df_w = yf.download(sym_yf, period="2y",  interval="1wk", progress=False, auto_adjust=True)
        df_m = yf.download(sym_yf, period="3y",  interval="1mo", progress=False, auto_adjust=True)

        if df_d.empty or len(df_d) < 60:
            return None

        patterns = _check_breakouts(df_d, df_w, df_m)
        if not patterns:
            return None

        close = float(df_d["Close"].squeeze().iloc[-1])
        atr_s = ta_lib.volatility.AverageTrueRange(
            df_d["High"].squeeze(), df_d["Low"].squeeze(),
            df_d["Close"].squeeze(), window=14
        ).average_true_range()
        atr = float(atr_s.iloc[-1]) if not atr_s.empty else close * 0.02

        vol    = df_d["Volume"].squeeze()
        avg_v  = float(vol.rolling(20).mean().iloc[-1]) or 1
        vol_r  = round(float(vol.iloc[-1]) / avg_v, 1)

        sym_clean  = symbol.replace(".NS", "")
        best_tf, best_pat = max(patterns, key=lambda x: {"Monthly":3,"Weekly":2,"Daily":1}.get(x[0],0))
        sl = round(close - 1.5 * atr, 1)
        t1 = round(close + 2.0 * atr, 1)
        t2 = round(close + 3.5 * atr, 1)
        t3 = round(close + 5.5 * atr, 1)
        rr = round((t1 - close) / max(close - sl, 0.01), 1)

        return {
            "symbol":      sym_clean,
            "price":       round(close, 1),
            "timeframe":   best_tf,
            "pattern":     best_pat,
            "patterns":    [f"{tf}: {pt}" for tf, pt in patterns],
            "vol_ratio":   vol_r,
            "sl":          sl,
            "target1":     t1,
            "target2":     t2,
            "target3":     t3,
            "rr":          rr,
            "fno":         sym_clean in FNO_ELIGIBLE,
            "tv_link":     f"https://in.tradingview.com/chart/?symbol=NSE:{sym_clean}",
        }
    except Exception as e:
        logging.warning(f"Breakout {symbol}: {e}")
        return None


def scan_breakouts(universe=None):
    """Scan confirmed breakouts: daily, weekly, monthly timeframes."""
    if universe is None:
        raw_uni = load_nifty500()
        universe = [s for s in raw_uni if s.replace(".NS","") in FNO_ELIGIBLE]

    results = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(analyze_breakout, sym): sym for sym in universe}
        for f in as_completed(futures):
            r = f.result()
            if r:
                results.append(r)

    tf_rank = {"Monthly": 3, "Weekly": 2, "Daily": 1}
    results.sort(key=lambda x: (tf_rank.get(x["timeframe"], 0), x["rr"]), reverse=True)
    logging.info(f"Breakout scan: {len(results)} confirmed breakouts")
    return results


# ── Forex & Commodities scan ──────────────────────────────────────────────────
FOREX_COMM = {
    "USD/INR":   "INR=X",
    "EUR/INR":   "EURINR=X",
    "GBP/INR":   "GBPINR=X",
    "Gold":      "GC=F",
    "Silver":    "SI=F",
    "Crude Oil": "CL=F",
    "Nat Gas":   "NG=F",
}

# ── 4H Early-Entry Scanner (RSI cross 55 + volume) ───────────────────────────
def analyze_4h(symbol):
    """
    4H Breakout — Expert Grade (Institutional Logic)
    Entry triggers (ALL required):
      1. RSI crosses above 55 (momentum ignition)
      2. Full EMA stack on 4H: price > EMA20 > EMA50 (trend confirmed)
      3. ADX > 20 on 4H (trending market, not ranging)
      4. MACD line crossed above MACD signal (momentum acceleration)
      5. Volume 2x+ average (institutional participation)
    SL: below 4H EMA20 or 1.5×ATR, whichever is tighter to price
    Targets: T1=2×ATR (1:2 RR), T2=3.5×ATR (structural)
    """
    try:
        sym_yf = symbol if symbol.endswith(".NS") else symbol + ".NS"
        df = yf.download(sym_yf, period="90d", interval="4h",
                         progress=False, auto_adjust=True)
        if df is None or df.empty or len(df) < 35:
            return None

        close  = df["Close"].squeeze()
        high   = df["High"].squeeze()
        low    = df["Low"].squeeze()
        volume = df["Volume"].squeeze()

        price   = float(close.iloc[-1])
        avg_v   = float(volume.rolling(20).mean().iloc[-1])
        cur_v   = float(volume.iloc[-1])
        vol_r   = round(cur_v / avg_v, 2) if avg_v > 0 else 1.0

        # Gate 1: Volume 2x+ (institutional participation)
        if vol_r < 2.0:
            return None

        # Gate 2: RSI crossing above 55 (was below, now above = momentum ignition)
        rsi_s   = rsi(close)
        cur_rsi = float(rsi_s.iloc[-1])
        prv_rsi = float(rsi_s.iloc[-2])
        if not (prv_rsi < 55 and cur_rsi >= 55):
            return None

        # Gate 3: Full EMA stack — price > EMA20 > EMA50 on 4H (trend alignment)
        e20v = float(ema(close, 20).iloc[-1])
        e50v = float(ema(close, 50).iloc[-1])
        if not (price > e20v > e50v):
            return None

        # Gate 4: ADX > 20 on 4H (confirms directional trend, not chop)
        cur_adx_4h = float(adx(high, low, close).iloc[-1])
        if cur_adx_4h < 20:
            return None

        # Gate 5: MACD crossover (momentum acceleration confirmation)
        m_line = macd_line(close)
        m_sig  = macd_signal(close)
        macd_crossed = (float(m_line.iloc[-2]) < float(m_sig.iloc[-2]) and
                        float(m_line.iloc[-1]) >= float(m_sig.iloc[-1]))
        # MACD cross required OR at least histogram improving
        macd_hist_improving = float(m_line.iloc[-1] - m_sig.iloc[-1]) > float(m_line.iloc[-2] - m_sig.iloc[-2])
        if not (macd_crossed or macd_hist_improving):
            return None

        # ATR-based SL — tighter of: EMA20 or 1.5×ATR
        cur_atr = float(atr(high, low, close).iloc[-1])
        sl_atr  = price - 1.5 * cur_atr
        sl_ema  = e20v - 0.3 * cur_atr   # just below EMA20
        sl      = round(max(sl_atr, sl_ema), 2)  # use higher SL (tighter, better RR)
        t1      = round(price + 2.0 * cur_atr, 2)
        t2      = round(price + 3.5 * cur_atr, 2)
        risk    = round(price - sl, 2)
        rr      = round((t2 - price) / risk, 1) if risk > 0 else 0

        # Minimum 2:1 RR
        if rr < 2.0:
            return None

        sym_clean = symbol.replace(".NS", "")
        return {
            "symbol":    sym_clean,
            "action":    "BUY",
            "timeframe": "4H",
            "price":     round(price, 2),
            "rsi":       round(cur_rsi, 1),
            "adx":       round(cur_adx_4h, 1),
            "vol_ratio": vol_r,
            "sl":        sl,
            "target1":   t1,
            "target2":   t2,
            "rr":        rr,
            "fno":       sym_clean in FNO_ELIGIBLE,
            "tv_link":   f"https://in.tradingview.com/chart/?symbol=NSE:{sym_clean}",
            "reason":    f"RSI {round(cur_rsi,1)} crossed 55 | MACD ✓ | Vol {vol_r}x | EMA stack ✓ | ADX {round(cur_adx_4h,1)}",
        }
    except Exception as e:
        logging.warning(f"4H {symbol}: {e}")
        return None


def scan_4h(universe=None):
    """Scan 4H setups: RSI cross 55 + volume surge — fires BEFORE daily signal."""
    if universe is None:
        raw_uni = load_nifty500()
        # Limit to F&O stocks for quality + liquidity
        universe = [s for s in raw_uni if s.replace(".NS", "") in FNO_ELIGIBLE]

    results = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(analyze_4h, sym): sym for sym in universe}
        for f in as_completed(futures):
            r = f.result()
            if r:
                results.append(r)

    results.sort(key=lambda x: x["vol_ratio"], reverse=True)
    logging.info(f"4H scan: {len(results)} early signals")
    return results[:15]


def fetch_forex_comm():
    """Fetch live prices + 1-day change for forex & commodities."""
    rows = []
    for name, ticker in FOREX_COMM.items():
        try:
            h = yf.Ticker(ticker).history(period="3d")
            if len(h) >= 2:
                prev = float(h["Close"].iloc[-2])
                last = float(h["Close"].iloc[-1])
                chg  = round((last - prev) / prev * 100, 2)
                rows.append({"Asset": name, "Last": round(last, 2), "Chg%": chg,
                             "Trend": "▲" if chg >= 0 else "▼"})
        except Exception:
            pass
    return rows


# ── Commodity Signal Scanner (Gold/Silver/Oil/Gas — 4H + Daily) ───────────────
COMMODITY_TICKERS = {
    "XAUUSD": ("GC=F",  "Gold Futures (USD/oz)",     "GLD"),
    "XAGUSD": ("SI=F",  "Silver Futures (USD/oz)",   "SLV"),
    "WTIUSD": ("CL=F",  "WTI Crude Oil (USD/bbl)",   "OIL"),
    "BRNUSD": ("BZ=F",  "Brent Crude (USD/bbl)",     "OIL"),
    "NGAS":   ("NG=F",  "Natural Gas (USD/MMBtu)",   "NRG"),
}

def _comm_weekly_bias(ticker):
    """Get weekly trend bias for a commodity — daily must align with weekly."""
    try:
        wk = yf.download(ticker, period="1y", interval="1wk",
                         progress=False, auto_adjust=True)
        if wk is None or wk.empty or len(wk) < 20:
            return None
        wc = wk["Close"].squeeze()
        we20 = float(ema(wc, 10).iloc[-1])  # 10-week EMA (20-week equiv on weekly)
        we50 = float(ema(wc, 20).iloc[-1])  # 20-week EMA (50-week equiv)
        wp   = float(wc.iloc[-1])
        if wp > we20 > we50:
            return "bullish"
        elif wp < we20 < we50:
            return "bearish"
        return None  # no clear weekly trend
    except Exception:
        return None


def _comm_signal(name, ticker, label, interval="1d", period="180d"):
    """
    Commodity Signal — Expert Grade (Inter-market / Multi-TF)
    Rules:
      1. Daily must align with weekly trend (no counter-trend trades)
      2. ADX > 25 (stronger filter — commodities trend hard or chop wildly)
      3. RSI zones: BUY 45-65 (momentum, not overbought), SELL 35-55
      4. EMA stack: daily EMA20 > EMA50 (BUY) or < (SELL)
      5. SL: 2×ATR for commodities (wider stops needed — commodity whipsaws)
      6. Minimum 2:1 RR (T2 vs SL)
    """
    try:
        df = yf.download(ticker, period=period, interval=interval,
                         progress=False, auto_adjust=True)
        if df is None or df.empty or len(df) < 40:
            return None

        close = df["Close"].squeeze()
        high  = df["High"].squeeze()
        low   = df["Low"].squeeze()

        price   = float(close.iloc[-1])
        e20     = float(ema(close, 20).iloc[-1])
        e50     = float(ema(close, 50).iloc[-1])
        cur_rsi = float(rsi(close).iloc[-1])
        cur_atr = float(atr(high, low, close).iloc[-1])
        cur_adx = float(adx(high, low, close).iloc[-1])

        # Gate 1: ADX > 25 (strong trend, not choppy noise)
        if cur_adx < 25:
            return None

        # Gate 2: Weekly trend alignment (no counter-trend signals)
        weekly_bias = _comm_weekly_bias(ticker)
        # Allow signal only if weekly confirms direction

        # Gate 3: Bias determination with tighter RSI zones
        if (price > e20 > e50 and 45 <= cur_rsi <= 65
                and (weekly_bias == "bullish" or weekly_bias is None)):
            bias = "BUY"
            # Wider SL for commodities (2×ATR to avoid whipsaw)
            sl   = round(price - 2.0 * cur_atr, 2)
            t1   = round(price + 1.5 * cur_atr, 2)
            t2   = round(price + 3.0 * cur_atr, 2)
            t3   = round(price + 5.0 * cur_atr, 2)
        elif (price < e20 < e50 and 35 <= cur_rsi <= 55
                and (weekly_bias == "bearish" or weekly_bias is None)):
            bias = "SELL"
            sl   = round(price + 2.0 * cur_atr, 2)
            t1   = round(price - 1.5 * cur_atr, 2)
            t2   = round(price - 3.0 * cur_atr, 2)
            t3   = round(price - 5.0 * cur_atr, 2)
        else:
            return None

        risk = abs(price - sl)
        rr   = round(abs(t2 - price) / risk, 1) if risk > 0 else 0

        # Gate 4: Minimum 2:1 RR
        if rr < 2.0:
            return None

        return {
            "symbol":        name,
            "ticker":        ticker,
            "label":         label,
            "action":        bias,
            "interval":      interval,
            "price":         round(price, 2),
            "sl":            sl,
            "target1":       t1,
            "target2":       t2,
            "target3":       t3,
            "rr":            rr,
            "rsi":           round(cur_rsi, 1),
            "adx":           round(cur_adx, 1),
            "atr":           round(cur_atr, 2),
            "weekly_trend":  weekly_bias or "neutral",
        }
    except Exception as e:
        logging.warning(f"Commodity {name}: {e}")
        return None


def scan_commodities():
    """
    Scan Gold/Silver/Oil/Gas for swing signals (daily) + 4H momentum.
    Returns list of signals sorted by ADX strength.
    """
    results = []
    for name, (ticker, label, _) in COMMODITY_TICKERS.items():
        # Daily swing signal
        sig = _comm_signal(name, ticker, label, interval="1d", period="180d")
        if sig:
            sig["timeframe"] = "Daily"
            results.append(sig)

        # 4H momentum (RSI > 55 or < 45)
        try:
            df4 = yf.download(ticker, period="60d", interval="4h",
                              progress=False, auto_adjust=True)
            if df4 is not None and not df4.empty and len(df4) >= 20:
                c4 = df4["Close"].squeeze()
                h4 = df4["High"].squeeze()
                l4 = df4["Low"].squeeze()
                r4 = float(rsi(c4).iloc[-1])
                r4_prev = float(rsi(c4).iloc[-2])
                a4 = float(atr(h4, l4, c4).iloc[-1])
                p4 = float(c4.iloc[-1])
                e4 = float(ema(c4, 20).iloc[-1])

                if r4_prev < 55 <= r4 and p4 > e4:
                    results.append({
                        "symbol": name, "ticker": ticker, "label": label,
                        "action": "BUY", "interval": "4H", "timeframe": "4H",
                        "price": round(p4, 2),
                        "sl":      round(p4 - 1.5 * a4, 2),
                        "target1": round(p4 + 1.0 * a4, 2),
                        "target2": round(p4 + 2.0 * a4, 2),
                        "target3": round(p4 + 3.5 * a4, 2),
                        "rr":      2.0,
                        "rsi":     round(r4, 1),
                        "adx":     0, "atr": round(a4, 2),
                    })
                elif r4_prev > 45 >= r4 and p4 < e4:
                    results.append({
                        "symbol": name, "ticker": ticker, "label": label,
                        "action": "SELL", "interval": "4H", "timeframe": "4H",
                        "price": round(p4, 2),
                        "sl":      round(p4 + 1.5 * a4, 2),
                        "target1": round(p4 - 1.0 * a4, 2),
                        "target2": round(p4 - 2.0 * a4, 2),
                        "target3": round(p4 - 3.5 * a4, 2),
                        "rr":      2.0,
                        "rsi":     round(r4, 1),
                        "adx":     0, "atr": round(a4, 2),
                    })
        except Exception as e:
            logging.warning(f"Commodity 4H {name}: {e}")

    results.sort(key=lambda x: x.get("adx", 0), reverse=True)
    logging.info(f"Commodity scan: {len(results)} signals")
    return results


# ── Trendline Channel Breakout (TLM) Scanner ─────────────────────────────────
# Python equivalent of Pine Script TLM indicator:
#   - Pivot span trendline: upper (pivot highs) + lower (pivot lows)
#   - 5-point OLS regression channel
#   - Signal fires when price breaks the upper channel band (bullish breakout)
#   - 4H: immediate signal | Daily EOD: confirmation

def _find_pivots(series: pd.Series, span: int = 5) -> tuple:
    """Find pivot highs and lows within a rolling window of `span` bars each side."""
    highs, lows = [], []
    arr = series.values
    for i in range(span, len(arr) - span):
        window_h = arr[i - span:i + span + 1]
        window_l = arr[i - span:i + span + 1]
        if arr[i] == window_h.max():
            highs.append((i, arr[i]))
        if arr[i] == window_l.min():
            lows.append((i, arr[i]))
    return highs, lows


def _ols_trendline(points: list) -> tuple:
    """Fit OLS line through (index, price) points. Returns (slope, intercept)."""
    if len(points) < 2:
        return None, None
    xs = np.array([p[0] for p in points], dtype=float)
    ys = np.array([p[1] for p in points], dtype=float)
    slope, intercept = np.polyfit(xs, ys, 1)
    return float(slope), float(intercept)


def _channel_value(slope, intercept, idx: int) -> float:
    return slope * idx + intercept


def analyze_tlm(symbol: str, interval: str = "4h", period: str = "60d",
                pivot_span: int = 5, n_pivots: int = 5) -> dict | None:
    """
    TLM: Trendline Channel Breakout detection.
    1. Find recent pivot highs (upper TL) and pivot lows (lower TL)
    2. Fit OLS regression through last n_pivots of each
    3. Compute channel bandwidth (ATR-normalised)
    4. Signal: close > upper channel + channel bandwidth > min_width
    Returns signal dict or None.
    """
    try:
        sym_yf = symbol if symbol.endswith(".NS") else symbol + ".NS"
        df = yf.download(sym_yf, period=period, interval=interval,
                         progress=False, auto_adjust=True)
        if df is None or df.empty or len(df) < 40:
            return None

        close  = df["Close"].squeeze()
        high   = df["High"].squeeze()
        low    = df["Low"].squeeze()
        volume = df["Volume"].squeeze()
        n      = len(close)

        cur_atr   = float(atr(high, low, close).iloc[-1])
        price     = float(close.iloc[-1])
        prev_close= float(close.iloc[-2])

        # Pivot detection on last 50 bars
        h_series = high.iloc[-50:]
        l_series = low.iloc[-50:]
        ph, pl   = _find_pivots(h_series, span=pivot_span)
        if len(ph) < 2 or len(pl) < 2:
            return None

        # Last n_pivots for regression
        ph = ph[-n_pivots:]
        pl = pl[-n_pivots:]

        h_slope, h_int = _ols_trendline(ph)
        l_slope, l_int = _ols_trendline(pl)
        if h_slope is None or l_slope is None:
            return None

        # Project to current bar (bar index = last index in h_series)
        last_idx = len(h_series) - 1
        upper_band = _channel_value(h_slope, h_int, last_idx)
        lower_band = _channel_value(l_slope, l_int, last_idx)
        prev_upper = _channel_value(h_slope, h_int, last_idx - 1)

        channel_width = upper_band - lower_band
        # Channel must have meaningful width (at least 1.5× ATR)
        if channel_width < 1.5 * cur_atr:
            return None

        # Upward trendline: upper band must be declining or flat (compression)
        # or price must be coiling inside channel before breakout
        # Breakout condition: prev close was inside/below upper, current close above
        if not (prev_close <= prev_upper and price > upper_band):
            return None

        # Volume confirmation: current bar > 1.3x avg
        avg_v   = float(volume.rolling(20).mean().iloc[-1])
        cur_v   = float(volume.iloc[-1])
        vol_r   = round(cur_v / avg_v, 2) if avg_v > 0 else 1.0
        if vol_r < 1.3:
            return None

        # Trend context: price above EMA20
        e20 = float(ema(close, 20).iloc[-1])
        if price < e20 * 0.99:
            return None

        cur_rsi = float(rsi(close).iloc[-1])
        if cur_rsi < 45:
            return None  # RSI too weak for breakout

        # Targets & SL
        sl   = round(lower_band - 0.3 * cur_atr, 2)  # below lower channel
        t1   = round(price + (price - sl) * 1.5, 2)
        t2   = round(price + (price - sl) * 2.5, 2)
        risk = round(price - sl, 2)
        rr   = round((t2 - price) / risk, 1) if risk > 0 else 0

        sym_clean = symbol.replace(".NS", "")
        return {
            "symbol":        sym_clean,
            "action":        "BUY",
            "timeframe":     "4H" if interval == "4h" else "Daily",
            "pattern":       "AI Channel Breakout",
            "price":         round(price, 2),
            "upper_band":    round(upper_band, 2),
            "lower_band":    round(lower_band, 2),
            "channel_width": round(channel_width, 2),
            "rsi":           round(cur_rsi, 1),
            "vol_ratio":     vol_r,
            "sl":            sl,
            "target1":       t1,
            "target2":       t2,
            "rr":            rr,
            "atr":           round(cur_atr, 2),
            "fno":           sym_clean in FNO_ELIGIBLE,
            "tv_link":       f"https://in.tradingview.com/chart/?symbol=NSE:{sym_clean}",
            "reason":        (f"TL Channel Breakout | Upper: ₹{round(upper_band,1)} broken | "
                              f"Vol {vol_r}x | RSI {round(cur_rsi,1)} | Width: {round(channel_width,1)}"),
        }
    except Exception as e:
        logging.warning(f"TLM {symbol}: {e}")
        return None


def scan_tlm_breakouts(universe=None, interval: str = "4h") -> list:
    """
    Scan F&O universe for TLM channel breakouts.
    interval: "4h" (intraday signals) or "1d" (EOD confirmation)
    Returns top 15 sorted by channel_width/ATR ratio.
    """
    if universe is None:
        raw_uni = load_nifty500()
        universe = [s for s in raw_uni if s.replace(".NS", "") in FNO_ELIGIBLE]

    period = "60d" if interval == "4h" else "180d"
    results = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(analyze_tlm, sym, interval, period): sym for sym in universe}
        for f in as_completed(futures):
            r = f.result()
            if r:
                results.append(r)

    # Sort: vol * rr as quality score
    results.sort(key=lambda x: x["vol_ratio"] * x["rr"], reverse=True)
    logging.info(f"TLM scan ({interval}): {len(results)} breakouts")
    return results[:15]


# ── Potential Multibaggers Scanner (Weekly — Saturday only) ──────────────────
def _analyze_multibagger(symbol: str, nifty_13w: float = 0.0) -> dict | None:
    """
    Multibagger Scanner — Expert Grade (CAN SLIM + Darvas Box)
    Institutional criteria (William O'Neil / Mark Minervini methodology):

    PRICE ACTION (required):
      • In upper 30% of 52W range (pos_52 >= 0.70) — near highs, not cheap trash
      • Within 15% of 52W high (Darvas Box: price consolidating near ATH)
      • NOT extended: 13-week (3M) return < 60% (avoid chasing extended moves)
      • Weekly EMA stack: price > EMA10 > EMA20 > EMA50 (full weekly uptrend)

    MOMENTUM (required):
      • Weekly RSI 55-75 (strong momentum, not overbought — Minervini zone)
      • Weekly ADX > 22 (stock is in a true trend, not drifting)
      • MACD positive histogram on weekly (momentum accelerating)

    VOLUME (required):
      • Recent 4-week avg volume > 20-week avg × 1.3 (institutional accumulation)

    RELATIVE STRENGTH:
      • 13W return must outperform Nifty 50 by at least 5% (leaders, not laggards)

    TARGETS (6-12 month horizon):
      • T1: 52W range × 0.30 above current price
      • T2: 52W range × 0.65 (potential double from breakout)
      • T3: 52W range × 1.10 (full multibagger move)

    SCORE (0-100):
      • 52W position: 30 pts
      • RSI momentum: 25 pts
      • ADX trend strength: 20 pts
      • Relative strength vs Nifty: 15 pts
      • Volume expansion: 10 pts
    """
    try:
        sym_yf = symbol if symbol.endswith(".NS") else symbol + ".NS"
        wk = yf.download(sym_yf, period="3y", interval="1wk",
                         progress=False, auto_adjust=True)
        if wk is None or wk.empty or len(wk) < 52:
            return None

        wk_c = wk["Close"].squeeze()
        wk_h = wk["High"].squeeze()
        wk_l = wk["Low"].squeeze()
        wk_v = wk["Volume"].squeeze()

        price    = float(wk_c.iloc[-1])
        h52      = float(wk_h.rolling(52).max().iloc[-1])
        l52      = float(wk_l.rolling(52).min().iloc[-1])
        range_52 = max(h52 - l52, 1)

        # ── GATE 1: 52W position >= 70% (in upper 30% of range — Minervini Stage 2) ──
        pos_52 = (price - l52) / range_52
        if pos_52 < 0.70:
            return None

        # ── GATE 2: Within 15% of 52W high (Darvas Box — consolidating near ATH) ──
        if price < h52 * 0.85:
            return None

        # ── GATE 3: Not extended — 13W (3M) return < 60% ──
        ret_13w = 0.0
        if len(wk_c) >= 14:
            prev_13 = float(wk_c.iloc[-14])
            ret_13w = (price - prev_13) / prev_13 if prev_13 > 0 else 0
        if ret_13w > 0.60:
            return None   # already extended, late entry, skip

        # ── GATE 4: Full weekly EMA stack ──
        we10 = float(ema(wk_c, 10).iloc[-1])
        we20 = float(ema(wk_c, 20).iloc[-1])
        we50 = float(ema(wk_c, 50).iloc[-1])
        if not (price > we10 > we20 > we50):
            return None

        # ── GATE 5: Weekly RSI 55-75 (momentum without overbought) ──
        wr = float(rsi(wk_c).iloc[-1])
        if not (55 <= wr <= 75):
            return None

        # ── GATE 6: Weekly ADX > 22 ──
        wa = float(adx(wk_h, wk_l, wk_c).iloc[-1])
        if wa < 22:
            return None

        # ── GATE 7: Volume expansion ≥ 1.3x (institutional accumulation) ──
        avg20v = float(wk_v.rolling(20).mean().iloc[-1])
        rec4v  = float(wk_v.iloc[-4:].mean())
        vol_r  = round(rec4v / avg20v, 2) if avg20v > 0 else 1.0
        if vol_r < 1.3:
            return None

        # ── GATE 8: Relative strength vs Nifty (pre-fetched, passed in) ──
        rs_vs_nifty = ret_13w - nifty_13w

        # Must outperform Nifty by at least 5% over 13 weeks
        if rs_vs_nifty < 0.05:
            return None

        # ── LEVELS ──
        support1 = round(we20, 2)
        support2 = round(float(wk_l.rolling(8).min().iloc[-1]), 2)
        sl       = round(max(support1, support2), 2)

        t1 = round(price + range_52 * 0.30, 2)
        t2 = round(price + range_52 * 0.65, 2)
        t3 = round(price + range_52 * 1.10, 2)

        risk = round(price - sl, 2)
        rr   = round((t2 - price) / risk, 1) if risk > 0 else 0

        # ── SCORE (0-100) ──
        score = round(
            min(pos_52, 1.0)              * 30 +   # 52W position
            min((wr - 55) / 20, 1.0)      * 25 +   # RSI momentum (55→75 maps to 0→25)
            min(wa / 45, 1.0)             * 20 +   # ADX trend strength
            min(rs_vs_nifty / 0.30, 1.0)  * 15 +   # Relative strength vs Nifty
            min(vol_r / 3.0, 1.0)         * 10,    # Volume expansion
            1
        )

        # ── PE (best effort) ──
        pe = None
        try:
            info = yf.Ticker(sym_yf).info
            pe_v = float(info.get("trailingPE", 0) or 0)
            pe   = round(pe_v, 1) if pe_v > 0 else None
        except Exception:
            pass

        # Skip extremely overvalued stocks (PE > 100) — momentum not supported by earnings
        if pe and pe > 100:
            return None

        sym_clean = symbol.replace(".NS", "")
        return {
            "symbol":       sym_clean,
            "price":        round(price, 2),
            "high_52w":     round(h52, 2),
            "low_52w":      round(l52, 2),
            "range_pos":    round(pos_52 * 100, 1),
            "dist_from_ath": round((price / h52 - 1) * 100, 1),
            "ret_13w":      round(ret_13w * 100, 1),
            "rs_vs_nifty":  round(rs_vs_nifty * 100, 1),
            "wk_rsi":       round(wr, 1),
            "wk_adx":       round(wa, 1),
            "vol_ratio":    vol_r,
            "sl":           sl,
            "support1":     support1,
            "support2":     support2,
            "target1":      t1,
            "target2":      t2,
            "target3":      t3,
            "rr":           rr,
            "score":        score,
            "pe":           pe,
            "fno":          sym_clean in FNO_ELIGIBLE,
            "tv_link":      f"https://in.tradingview.com/chart/?symbol=NSE:{sym_clean}",
            "reason":       (f"Stage 2 | RSI {round(wr,1)} | ADX {round(wa,1)} | "
                             f"RS +{round(rs_vs_nifty*100,1)}% vs Nifty | "
                             f"Vol {vol_r}x | {round(pos_52*100,1)}% of 52W range"),
        }
    except Exception as e:
        logging.warning(f"Multibagger {symbol}: {e}")
        return None


def scan_multibaggers(universe=None, top_n=15) -> list:
    """
    Scan Nifty 1000 for potential multibagger candidates (weekly timeframe).
    Runs Saturday only — results valid for the week.
    Returns top_n sorted by composite score.
    """
    if universe is None:
        universe = load_nifty500()

    # Pre-fetch Nifty 13W return ONCE (not inside parallel threads)
    nifty_13w = 0.0
    try:
        ndf = yf.download("^NSEI", period="6mo", interval="1wk",
                          progress=False, auto_adjust=True)
        if not ndf.empty and len(ndf) >= 14:
            nc = ndf["Close"].squeeze()
            nifty_13w = float(nc.iloc[-1] / nc.iloc[-14] - 1)
        logging.info(f"Nifty 13W return: {nifty_13w:.2%}")
    except Exception as e:
        logging.warning(f"Nifty 13W fetch failed: {e}")

    results = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(_analyze_multibagger, sym, nifty_13w): sym for sym in universe}
        for f in as_completed(futures):
            r = f.result()
            if r:
                results.append(r)

    results.sort(key=lambda x: x["score"], reverse=True)
    logging.info(f"Multibagger scan: {len(results)} candidates → top {top_n}")
    return results[:top_n]
