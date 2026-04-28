"""Mutual Fund Portfolio Intelligence — AMFI + Google News + Indices"""
import requests, json, os, logging
import pandas as pd
import yfinance as yf
from datetime import datetime, timedelta

os.makedirs("cache", exist_ok=True)
log = logging.getLogger(__name__)

MFAPI          = "https://api.mfapi.in/mf"
PORTFOLIO_FILE = "cache/mf_portfolio.json"

# ── Curated top funds by category (Direct Growth, AMFI scheme codes) ──────────
TOP_FUNDS_DB = {
    "Large Cap": [
        (118825, "Mirae Asset Large Cap"),
        (118632, "Nippon India Large Cap"),
        (120716, "UTI Nifty 50 Index"),
    ],
    "Mid Cap": [
        (120505, "Axis Midcap"),
        (125497, "SBI Small Cap"),   # placeholder
    ],
    "Small Cap": [
        (125497, "SBI Small Cap"),
        (118777, "Nippon India Small Cap"),
        (120164, "Kotak Small Cap"),
        (125354, "Axis Small Cap"),
    ],
    "Flexi Cap": [
        (122639, "Parag Parikh Flexi Cap"),
        (118955, "HDFC Flexi Cap"),
        (120166, "Kotak Flexicap"),
        (151412, "Mirae Asset Flexi Cap"),
    ],
    "ELSS": [
        (135781, "Mirae Asset ELSS Tax Saver"),
        (120847, "Quant ELSS Tax Saver"),
        (119551, "Axis Long Term Equity"),
    ],
    "Hybrid": [
        (118968, "HDFC Balanced Advantage"),
    ],
}

# ── NSE Indices ───────────────────────────────────────────────────────────────
INDICES = {
    "NIFTY 50":   "^NSEI",
    "SENSEX":     "^BSESN",
    "BANK NIFTY": "^NSEBANK",
    "NIFTY IT":   "^CNXIT",
    "NIFTY MID":  "^NSEMDCP150",
    "NIFTY NEXT": "^NSENEXT50",
    "NIFTY PHARMA": "^CNXPHARMA",
}


def get_index_quotes():
    """Fetch live index prices (cached externally via st.cache_data)."""
    out = []
    for name, ticker in INDICES.items():
        try:
            h = yf.Ticker(ticker).history(period="2d", interval="1d", auto_adjust=True)
            if len(h) >= 2:
                prev = float(h["Close"].iloc[-2])
                last = float(h["Close"].iloc[-1])
                chg  = (last - prev) / prev * 100
                out.append({"name": name, "last": round(last, 2),
                            "chg": round(chg, 2), "prev": round(prev, 2)})
        except Exception as e:
            log.warning(f"Index {name}: {e}")
    return out


def search_funds(query):
    try:
        r = requests.get(f"{MFAPI}/search?q={query}", timeout=10)
        return r.json()
    except Exception as e:
        log.warning(f"Fund search: {e}")
        return []


def get_nav_history(scheme_code):
    try:
        r = requests.get(f"{MFAPI}/{scheme_code}", timeout=15)
        data = r.json()
        navs = data.get("data", [])
        if not navs:
            return pd.DataFrame(), {}
        df = pd.DataFrame(navs)
        df["date"] = pd.to_datetime(df["date"], format="%d-%m-%Y")
        df["nav"]  = df["nav"].astype(float)
        df = df.sort_values("date").reset_index(drop=True)
        return df, data.get("meta", {})
    except Exception as e:
        log.warning(f"NAV {scheme_code}: {e}")
        return pd.DataFrame(), {}


def calc_returns(df):
    if df.empty or len(df) < 5:
        return {}
    latest     = float(df["nav"].iloc[-1])
    latest_dt  = df["date"].iloc[-1]
    periods    = {"1M": 30, "3M": 90, "6M": 180, "1Y": 365, "3Y": 1095, "5Y": 1825}
    out = {}
    for label, days in periods.items():
        past = df[df["date"] <= latest_dt - timedelta(days=days)]
        if past.empty:
            continue
        p = float(past["nav"].iloc[-1])
        if p <= 0:
            continue
        if days <= 365:
            out[label] = round((latest - p) / p * 100, 2)
        else:
            out[label] = round(((latest / p) ** (365 / days) - 1) * 100, 2)
    return out


def get_top_funds_data():
    """Return top funds per category with NAV + returns. Slow — cache externally."""
    result = {}
    for cat, funds in TOP_FUNDS_DB.items():
        rows = []
        for code, short_name in funds:
            df, meta = get_nav_history(code)
            if df.empty:
                continue
            ret = calc_returns(df)
            rows.append({
                "name":        meta.get("scheme_name", short_name),
                "short":       short_name,
                "scheme_code": code,
                "nav":         round(float(df["nav"].iloc[-1]), 4),
                "fund_house":  meta.get("fund_house", ""),
                "returns":     ret,
                "1Y":          ret.get("1Y", None),
                "3Y":          ret.get("3Y", None),
                "5Y":          ret.get("5Y", None),
            })
        rows.sort(key=lambda x: x["1Y"] or -999, reverse=True)
        result[cat] = rows[:5]
    return result


def get_stock_news(symbol, n=5):
    """Google News RSS for a stock symbol."""
    try:
        import feedparser
        q    = f"NSE+{symbol}+stock"
        url  = f"https://news.google.com/rss/search?q={q}&hl=en-IN&gl=IN&ceid=IN:en"
        feed = feedparser.parse(url)
        return [{"title": e.title[:120], "link": e.link,
                 "published": e.get("published", "")[:16]}
                for e in feed.entries[:n]]
    except Exception as e:
        log.warning(f"Stock news {symbol}: {e}")
        return []


def get_corporate_actions(symbol):
    """Dividends, splits, earnings calendar for a stock."""
    try:
        t   = yf.Ticker(symbol + ".NS")
        div = t.dividends
        cal = t.calendar
        actions = []
        if div is not None and not div.empty:
            last_div = div.iloc[-1]
            actions.append(f"Last dividend: ₹{last_div:.2f} ({div.index[-1].strftime('%d %b %Y')})")
        if cal is not None:
            if hasattr(cal, 'items'):
                cal_dict = dict(cal)
            else:
                cal_dict = {}
            earn = cal_dict.get("Earnings Date")
            if earn:
                if hasattr(earn, '__iter__') and not isinstance(earn, str):
                    earn_str = str(list(earn)[0])[:10] if earn else ""
                else:
                    earn_str = str(earn)[:10]
                actions.append(f"Earnings: {earn_str}")
        return actions
    except Exception as e:
        log.warning(f"Corp actions {symbol}: {e}")
        return []


def get_fund_news(fund_name, n=6):
    try:
        import feedparser
        q    = fund_name[:40].replace(" ", "+") + "+mutual+fund"
        url  = f"https://news.google.com/rss/search?q={q}&hl=en-IN&gl=IN&ceid=IN:en"
        feed = feedparser.parse(url)
        return [{"title": e.title[:120], "link": e.link,
                 "published": e.get("published", "")[:16]}
                for e in feed.entries[:n]]
    except Exception as e:
        log.warning(f"Fund news {fund_name}: {e}")
        return []


def load_portfolio():
    if os.path.exists(PORTFOLIO_FILE):
        try:
            with open(PORTFOLIO_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return []


def save_portfolio(portfolio):
    with open(PORTFOLIO_FILE, "w") as f:
        json.dump(portfolio, f, indent=2)


def get_portfolio_summary(portfolio):
    results = []
    for fund in portfolio:
        df, meta = get_nav_history(fund["scheme_code"])
        if df.empty:
            continue
        latest       = float(df["nav"].iloc[-1])
        purchase_nav = float(fund.get("purchase_nav", latest))
        units        = float(fund.get("units", 0))
        invested     = purchase_nav * units
        current      = latest * units
        pnl          = current - invested
        pnl_pct      = (pnl / invested * 100) if invested > 0 else 0
        day_chg      = 0.0
        if len(df) >= 2:
            prev    = float(df["nav"].iloc[-2])
            day_chg = round((latest - prev) / prev * 100, 2)
        results.append({
            "name":         fund.get("name", meta.get("scheme_name", str(fund["scheme_code"]))),
            "scheme_code":  fund["scheme_code"],
            "nav":          round(latest, 4),
            "day_chg":      day_chg,
            "purchase_nav": round(purchase_nav, 4),
            "units":        units,
            "invested":     round(invested),
            "current":      round(current),
            "pnl":          round(pnl),
            "pnl_pct":      round(pnl_pct, 2),
            "returns":      calc_returns(df),
            "fund_house":   meta.get("fund_house", ""),
            "category":     meta.get("scheme_category", ""),
        })
    return results
