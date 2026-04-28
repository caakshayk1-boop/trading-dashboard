"""
Upstox API v2 data provider.
Replaces yfinance for historical OHLCV data.
Token auth flow: browser login once → token cached → auto-refreshed daily.
"""
import os, json, time, logging, requests, webbrowser
from datetime import datetime, timedelta, date
from urllib.parse import urlencode
import pandas as pd

from config import (UPSTOX_API_KEY, UPSTOX_API_SECRET,
                    UPSTOX_REDIRECT_URL, UPSTOX_TOKEN_FILE)

os.makedirs("cache", exist_ok=True)
log = logging.getLogger(__name__)

BASE   = "https://api.upstox.com/v2"
AUTH   = "https://api.upstox.com/v2/login/authorization/dialog"
TOKEN  = "https://api.upstox.com/v2/login/authorization/token"

INTERVAL_MAP = {
    "1d":  "day",
    "1wk": "week",
    "1mo": "month",
}


# ── Token management ───────────────────────────────────────────────────────────
def _load_token():
    if os.path.exists(UPSTOX_TOKEN_FILE):
        with open(UPSTOX_TOKEN_FILE) as f:
            data = json.load(f)
        # Token valid if saved today
        if data.get("date") == str(date.today()):
            return data.get("access_token")
    return None


def _save_token(access_token):
    with open(UPSTOX_TOKEN_FILE, "w") as f:
        json.dump({"access_token": access_token, "date": str(date.today())}, f)


def get_auth_url():
    params = {
        "response_type": "code",
        "client_id":     UPSTOX_API_KEY,
        "redirect_uri":  UPSTOX_REDIRECT_URL,
    }
    return AUTH + "?" + urlencode(params)


def exchange_code_for_token(code):
    r = requests.post(TOKEN, data={
        "code":          code,
        "client_id":     UPSTOX_API_KEY,
        "client_secret": UPSTOX_API_SECRET,
        "redirect_uri":  UPSTOX_REDIRECT_URL,
        "grant_type":    "authorization_code",
    }, timeout=15)
    r.raise_for_status()
    token = r.json()["access_token"]
    _save_token(token)
    log.info("Upstox token saved")
    return token


def get_token():
    """Return cached token or None (caller must trigger login flow)."""
    return _load_token()


def is_authenticated():
    return _load_token() is not None


# ── Instrument key lookup ──────────────────────────────────────────────────────
_INSTRUMENT_CACHE = {}

def get_instrument_key(symbol):
    """
    Convert NSE symbol (e.g. RELIANCE) → Upstox instrument key (NSE_EQ|INE002A01018).
    Uses Upstox instruments master CSV (downloaded once per day).
    """
    global _INSTRUMENT_CACHE
    if _INSTRUMENT_CACHE:
        return _INSTRUMENT_CACHE.get(symbol)

    master_file = "cache/upstox_instruments.csv"
    if not os.path.exists(master_file) or \
       (time.time() - os.path.getmtime(master_file)) > 86400:
        try:
            url = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.csv.gz"
            r = requests.get(url, timeout=30)
            r.raise_for_status()
            with open(master_file + ".gz", "wb") as f:
                f.write(r.content)
            import gzip, shutil
            with gzip.open(master_file + ".gz", "rb") as f_in:
                with open(master_file, "wb") as f_out:
                    shutil.copyfileobj(f_in, f_out)
            log.info("Upstox instruments master downloaded")
        except Exception as e:
            log.warning(f"Instrument master download failed: {e}")
            return None

    try:
        df = pd.read_csv(master_file)
        # Filter NSE equity
        eq = df[(df["exchange"] == "NSE_EQ") & (df["instrument_type"] == "EQUITY")]
        for _, row in eq.iterrows():
            _INSTRUMENT_CACHE[str(row["tradingsymbol"])] = str(row["instrument_key"])
    except Exception as e:
        log.warning(f"Instrument parse failed: {e}")

    return _INSTRUMENT_CACHE.get(symbol)


# ── OHLCV fetch ────────────────────────────────────────────────────────────────
def fetch_ohlcv(symbol, period="1y", interval="1d"):
    """
    Fetch OHLCV from Upstox.
    Returns DataFrame with columns: Open, High, Low, Close, Volume
    Falls back to yfinance if token missing or fetch fails.
    """
    token = get_token()
    if not token:
        log.warning(f"No Upstox token — falling back to yfinance for {symbol}")
        return _yfinance_fallback(symbol, period, interval)

    ikey = get_instrument_key(symbol.replace(".NS", ""))
    if not ikey:
        log.warning(f"No instrument key for {symbol} — falling back to yfinance")
        return _yfinance_fallback(symbol, period, interval)

    # Date range
    to_date   = date.today()
    from_date = to_date - timedelta(days=365 if period == "1y" else 180)

    upstox_interval = INTERVAL_MAP.get(interval, "day")

    url = f"{BASE}/historical-candle/{ikey}/{upstox_interval}/{to_date}/{from_date}"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    try:
        r = requests.get(url, headers=headers, timeout=15)
        if r.status_code == 401:
            log.warning(f"Upstox token expired for {symbol} — falling back to yfinance")
            return _yfinance_fallback(symbol, period, interval)
        r.raise_for_status()
        candles = r.json().get("data", {}).get("candles", [])
        if not candles:
            return _yfinance_fallback(symbol, period, interval)

        df = pd.DataFrame(candles,
                          columns=["datetime","Open","High","Low","Close","Volume","OI"])
        df["datetime"] = pd.to_datetime(df["datetime"])
        df = df.set_index("datetime").sort_index()
        df = df[["Open","High","Low","Close","Volume"]].astype(float)
        log.info(f"Upstox: {symbol} {len(df)} candles")
        return df

    except Exception as e:
        log.warning(f"Upstox fetch failed for {symbol}: {e} — falling back to yfinance")
        return _yfinance_fallback(symbol, period, interval)


def _yfinance_fallback(symbol, period, interval):
    import yfinance as yf
    df = yf.download(symbol, period=period, interval=interval,
                     progress=False, auto_adjust=True)
    return df
