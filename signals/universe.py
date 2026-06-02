"""signals/universe.py — Stock universe loaders, trading calendar, FnO helpers."""

import os
import logging

log = logging.getLogger(__name__)

# ── NSE Trading Calendar ──────────────────────────────────────────────────────

_NSE_HOLIDAYS_2026 = {
    "26-01-2026","19-02-2026","06-03-2026","31-03-2026","02-04-2026",
    "06-04-2026","10-04-2026","14-04-2026","01-05-2026","25-05-2026",
    "15-08-2026","28-08-2026","02-10-2026","24-10-2026","14-11-2026",
    "25-12-2026",
}


def is_trading_day(dt=None) -> bool:
    import pytz
    from datetime import datetime as _dt
    _IST = pytz.timezone("Asia/Kolkata")
    if dt is None:
        dt = _dt.now(_IST)
    if dt.weekday() >= 5:
        return False
    return dt.strftime("%d-%m-%Y") not in _NSE_HOLIDAYS_2026


def _next_thursday(weeks_ahead: int = 0):
    from datetime import date, timedelta
    today = date.today()
    days_to_thu = (3 - today.weekday()) % 7
    if days_to_thu == 0:
        days_to_thu = 7
    return today + timedelta(days=days_to_thu + weeks_ahead * 7)


def _last_thursday_of_month():
    from datetime import date, timedelta
    import calendar
    today    = date.today()
    last_day = date(today.year, today.month, calendar.monthrange(today.year, today.month)[1])
    delta    = (last_day.weekday() - 3) % 7
    return last_day - timedelta(days=delta)


def _smart_expiry(score: int, adx: float, setup_type: str, atr_pct: float) -> tuple:
    """Returns (label, hold_str, tier, opt_type). Guarantees >= 21 DTE."""
    from datetime import date, timedelta
    today = date.today()

    def _safe_expiry(weeks_ahead):
        exp = _next_thursday(weeks_ahead=weeks_ahead)
        days_left = (exp - today).days
        while days_left < 21 and weeks_ahead <= 8:
            weeks_ahead += 1
            exp = _next_thursday(weeks_ahead=weeks_ahead)
            days_left = (exp - today).days
        return exp, days_left

    if score >= 85 and adx > 28:
        exp_date, days_left = _safe_expiry(1)
        label, tier = f"Bi-Weekly · Thu {exp_date.strftime('%d %b')}", "biweekly"
    elif score >= 78 or (adx > 22 and setup_type == "breakout"):
        exp_date, days_left = _safe_expiry(1)
        label, tier = f"Bi-Weekly · Thu {exp_date.strftime('%d %b')}", "biweekly"
    elif setup_type == "divergence":
        exp_date  = _last_thursday_of_month()
        days_left = (exp_date - today).days
        if days_left < 21:
            import calendar as _cal
            d  = today
            nm = 1 if d.month == 12 else d.month + 1
            ny = d.year + 1 if d.month == 12 else d.year
            last  = date(ny, nm, _cal.monthrange(ny, nm)[1])
            delta = (last.weekday() - 3) % 7
            exp_date  = last - timedelta(days=delta)
            days_left = (exp_date - today).days
        label, tier = f"Monthly · Thu {exp_date.strftime('%d %b')}", "monthly"
    else:
        exp_date, days_left = _safe_expiry(1)
        label, tier = f"Bi-Weekly · Thu {exp_date.strftime('%d %b')}", "biweekly"

    opt_type = "ATM" if atr_pct > 3.0 else "OTM"
    return label, f"{days_left} days", tier, opt_type


def _fno_suggest(symbol, price, bias, atr, score=78, adx=25.0, setup_type="breakout"):
    """Expert-grade F&O suggestion: debit spread default, naked only on high conviction."""
    try:
        from config import CAPITAL
    except ImportError:
        CAPITAL = float(os.environ.get("CAPITAL", 500000))

    direction   = "CALL" if bias == "bullish" else "PUT"
    step        = 50 if price > 5000 else (20 if price > 1000 else (10 if price > 500 else 5))
    atm         = round(price / step) * step
    otm2_strike = atm + 2*step if direction == "CALL" else atm - 2*step
    risk_pts    = round(atr * 1.5, 1)
    atr_pct     = (atr / price * 100) if price > 0 else 2.0
    exp_label, hold_str, tier, opt_type = _smart_expiry(score, adx, setup_type, atr_pct)
    tier_emoji  = {"weekly": "⚡", "biweekly": "📅", "monthly": "📆"}.get(tier, "📅")

    if score >= 85 and adx > 28:
        strategy   = "Naked Option"
        spread_note = f"Buy {symbol} {atm} {direction} (ATM)"
        max_risk   = round(atr * 1.5 * 100, 0)
    else:
        strategy   = "Debit Spread"
        spread_note = f"Buy {atm} {direction} + Sell {otm2_strike} {direction} (debit spread)"
        max_risk   = round(abs(otm2_strike - atm) * 0.6 * 100, 0)

    return {
        "direction": direction, "atm_strike": atm, "otm_strike": atm + step if direction == "CALL" else atm - step,
        "spread_short": otm2_strike, "use_strike": atm, "opt_type": opt_type,
        "strategy": strategy, "risk_pts": risk_pts, "expiry": exp_label,
        "hold_days": hold_str, "tier": tier, "tier_emoji": tier_emoji,
        "note": f"{tier_emoji} {spread_note} | Expiry: {exp_label} | Hold ~{hold_str} | Max risk ~₹{int(max_risk):,}",
    }


# ── Universe Loaders ──────────────────────────────────────────────────────────

NIFTY500_CSV_URL = "https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv"
NIFTY500_CACHE   = "cache/nifty500.csv"
NIFTY200_CSV_URL = "https://nsearchives.nseindia.com/content/indices/ind_nifty200list.csv"
NIFTY200_CACHE   = "cache/nifty200.csv"


def load_nifty500() -> list:
    import requests, pandas as pd
    try:
        if os.path.exists(NIFTY500_CACHE):
            df = pd.read_csv(NIFTY500_CACHE)
        else:
            r = requests.get(NIFTY500_CSV_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
            r.raise_for_status()
            os.makedirs("cache", exist_ok=True)
            with open(NIFTY500_CACHE, "wb") as f:
                f.write(r.content)
            df = pd.read_csv(NIFTY500_CACHE)
        col = next((c for c in df.columns if "symbol" in c.lower()), df.columns[0])
        return [s.strip() + ".NS" for s in df[col].dropna().tolist()]
    except Exception as e:
        log.warning(f"load_nifty500 failed: {e}")
        return []


def load_nifty200() -> list:
    import requests, pandas as pd
    try:
        if os.path.exists(NIFTY200_CACHE):
            df = pd.read_csv(NIFTY200_CACHE)
        else:
            r = requests.get(NIFTY200_CSV_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
            r.raise_for_status()
            os.makedirs("cache", exist_ok=True)
            with open(NIFTY200_CACHE, "wb") as f:
                f.write(r.content)
            df = pd.read_csv(NIFTY200_CACHE)
        col = next((c for c in df.columns if "symbol" in c.lower()), df.columns[0])
        return [s.strip() + ".NS" for s in df[col].dropna().tolist()]
    except Exception as e:
        log.warning(f"load_nifty200 failed: {e}")
        return []


# ── FnO Eligibility ───────────────────────────────────────────────────────────

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
    "PHOENIXLTD","RAJESHEXPO","RAMCOCEM","RATNAMANI","RITES","SJVN","SONACOMS",
    "SYNGENE","TANLA","TATACHEMICALS","TIINDIA","TIMKEN","TRITURBINE","UJJIVANSFB",
    "UNIONBANK","UCOBANK","USHAMART","VGUARD","WOCKPHARMA","ZEEL",
}


__all__ = [
    "is_trading_day", "FNO_ELIGIBLE", "load_nifty500", "load_nifty200",
    "_next_thursday", "_last_thursday_of_month", "_smart_expiry", "_fno_suggest",
]
