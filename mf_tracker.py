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


# ── Fund Holdings (approximate, as of last monthly AMC disclosure) ─────────────
FUND_HOLDINGS = {
    122639: {  # Parag Parikh Flexi Cap
        "sectors": {"Financial Svcs": 28.5, "IT": 22.3, "Consumer": 12.1, "International": 18.5, "Pharma": 5.2, "Energy": 4.8, "Others": 8.6},
        "top_scripts": [("HDFC Bank", 8.2), ("Bajaj Holdings", 6.8), ("Coal India", 5.9), ("ITC", 5.1), ("Power Grid", 4.8), ("Alphabet US", 4.5), ("Microsoft US", 3.9), ("Amazon US", 3.6)],
    },
    118825: {  # Mirae Asset Large Cap
        "sectors": {"Financial Svcs": 35.2, "IT": 18.4, "Consumer": 9.8, "Energy": 8.5, "Pharma": 6.2, "Industrials": 7.3, "Others": 14.6},
        "top_scripts": [("HDFC Bank", 9.8), ("ICICI Bank", 9.1), ("Infosys", 8.3), ("Reliance", 7.2), ("TCS", 6.5), ("Kotak Bank", 4.8), ("Axis Bank", 4.1), ("L&T", 3.6)],
    },
    118632: {  # Nippon India Large Cap
        "sectors": {"Financial Svcs": 32.1, "IT": 16.5, "Consumer": 10.2, "Energy": 9.8, "Pharma": 7.3, "Industrials": 8.1, "Others": 16.0},
        "top_scripts": [("HDFC Bank", 9.2), ("Infosys", 8.1), ("ICICI Bank", 7.8), ("Reliance", 7.5), ("TCS", 5.9), ("L&T", 4.8), ("HUL", 3.9), ("Kotak Bank", 3.6)],
    },
    120716: {  # UTI Nifty 50 Index
        "sectors": {"Financial Svcs": 36.4, "IT": 13.2, "Energy": 10.5, "Consumer": 8.7, "Industrials": 6.8, "Pharma": 5.9, "Others": 18.5},
        "top_scripts": [("HDFC Bank", 13.5), ("Reliance", 9.8), ("ICICI Bank", 8.4), ("Infosys", 6.8), ("TCS", 4.6), ("Bharti Airtel", 3.9), ("L&T", 3.8), ("ITC", 3.4)],
    },
    120505: {  # Axis Midcap
        "sectors": {"Financial Svcs": 22.3, "Consumer": 16.8, "Industrials": 14.5, "IT": 12.1, "Pharma": 9.8, "Materials": 8.4, "Others": 16.1},
        "top_scripts": [("Cholamandalam Fin", 4.8), ("Max Healthcare", 4.2), ("Persistent Sys", 3.9), ("Astral", 3.6), ("Divi's Labs", 3.4), ("Gujarat Gas", 3.1), ("CG Power", 3.0), ("Whirlpool", 2.8)],
    },
    125497: {  # SBI Small Cap
        "sectors": {"Industrials": 22.5, "Consumer": 18.3, "Financial Svcs": 12.8, "Materials": 11.4, "IT": 8.2, "Pharma": 7.6, "Others": 19.2},
        "top_scripts": [("Aegis Logistics", 3.8), ("Kirloskar Bros", 3.5), ("Blue Star", 3.2), ("Welspun India", 3.0), ("Techno Electric", 2.9), ("JK Tyre", 2.7), ("SJ Logistics", 2.5), ("KPIT Tech", 2.4)],
    },
    118777: {  # Nippon India Small Cap
        "sectors": {"Consumer": 18.5, "Industrials": 17.2, "Financial Svcs": 15.3, "Materials": 12.8, "IT": 9.1, "Pharma": 8.4, "Others": 18.7},
        "top_scripts": [("KPIT Technologies", 3.2), ("Tube Investments", 2.9), ("Dixon Tech", 2.8), ("MCX", 2.6), ("Kaynes Tech", 2.5), ("Apar Industries", 2.4), ("Blue Star", 2.3), ("Akzo Nobel", 2.1)],
    },
    120164: {  # Kotak Small Cap
        "sectors": {"Industrials": 24.3, "Consumer": 16.8, "Financial Svcs": 13.5, "Materials": 11.2, "IT": 8.9, "Pharma": 7.8, "Others": 17.5},
        "top_scripts": [("Blue Star", 4.1), ("Apar Industries", 3.8), ("Carborundum Univ", 3.4), ("Century Ply", 3.2), ("Techno Electric", 3.0), ("JK Paper", 2.8), ("Hawkins Cookers", 2.6), ("Elgi Equipments", 2.4)],
    },
    125354: {  # Axis Small Cap
        "sectors": {"Consumer": 22.1, "Industrials": 20.4, "Financial Svcs": 14.6, "IT": 10.2, "Materials": 9.8, "Pharma": 8.3, "Others": 14.6},
        "top_scripts": [("Campus Activewear", 3.9), ("Lemon Tree Hotels", 3.6), ("Dalmia Bharat Sugar", 3.3), ("Zydus Wellness", 3.1), ("Minda Corp", 2.9), ("Aavas Financiers", 2.7), ("KPR Mill", 2.5), ("Praj Industries", 2.4)],
    },
    118955: {  # HDFC Flexi Cap
        "sectors": {"Financial Svcs": 30.5, "IT": 15.8, "Industrials": 13.2, "Consumer": 10.4, "Energy": 9.1, "Pharma": 7.2, "Others": 13.8},
        "top_scripts": [("HDFC Bank", 9.5), ("ICICI Bank", 8.2), ("Infosys", 6.8), ("Kotak Bank", 5.4), ("Axis Bank", 4.9), ("L&T", 4.6), ("HCL Tech", 3.8), ("Sun Pharma", 3.2)],
    },
    120166: {  # Kotak Flexicap
        "sectors": {"Financial Svcs": 31.2, "IT": 16.4, "Consumer": 12.3, "Industrials": 10.5, "Energy": 8.8, "Pharma": 7.1, "Others": 13.7},
        "top_scripts": [("HDFC Bank", 8.9), ("Infosys", 7.5), ("ICICI Bank", 7.2), ("Reliance", 6.8), ("TCS", 5.6), ("Kotak Bank", 5.1), ("L&T", 4.3), ("HUL", 3.7)],
    },
    151412: {  # Mirae Asset Flexi Cap
        "sectors": {"Financial Svcs": 29.8, "IT": 18.5, "Consumer": 11.2, "Industrials": 10.4, "Energy": 8.3, "Pharma": 7.6, "Others": 14.2},
        "top_scripts": [("HDFC Bank", 9.2), ("Infosys", 7.8), ("ICICI Bank", 7.5), ("Reliance", 7.0), ("TCS", 5.8), ("Axis Bank", 4.5), ("L&T", 4.2), ("Sun Pharma", 3.8)],
    },
    135781: {  # Mirae Asset ELSS
        "sectors": {"Financial Svcs": 34.2, "IT": 17.8, "Consumer": 10.5, "Energy": 9.2, "Pharma": 6.8, "Industrials": 8.1, "Others": 13.4},
        "top_scripts": [("HDFC Bank", 10.2), ("ICICI Bank", 8.9), ("Infosys", 8.1), ("Reliance", 7.4), ("TCS", 6.2), ("Kotak Bank", 5.0), ("L&T", 4.5), ("HUL", 3.8)],
    },
    120847: {  # Quant ELSS
        "sectors": {"Energy": 22.4, "Financial Svcs": 18.6, "Consumer": 15.3, "Materials": 12.8, "IT": 8.5, "Pharma": 7.2, "Others": 15.2},
        "top_scripts": [("Reliance", 8.5), ("ONGC", 7.8), ("ITC", 6.9), ("HPCL", 6.2), ("SBI", 5.8), ("Adani Ports", 5.1), ("Coal India", 4.8), ("BPCL", 4.5)],
    },
    119551: {  # Axis Long Term Equity ELSS
        "sectors": {"Financial Svcs": 28.5, "IT": 18.2, "Consumer": 14.8, "Pharma": 10.2, "Industrials": 9.1, "Materials": 7.4, "Others": 11.8},
        "top_scripts": [("Bajaj Finance", 8.2), ("HDFC Bank", 7.8), ("TCS", 7.1), ("Infosys", 6.4), ("Kotak Bank", 5.8), ("Pidilite", 5.2), ("Avenue Supermarts", 4.9), ("Divi's Labs", 4.3)],
    },
    118968: {  # HDFC Balanced Advantage
        "sectors": {"Financial Svcs": 28.4, "IT": 13.2, "Debt/Money Mkt": 18.5, "Consumer": 9.8, "Energy": 8.5, "Industrials": 7.6, "Others": 14.0},
        "top_scripts": [("HDFC Bank", 8.5), ("ICICI Bank", 7.2), ("Infosys", 5.8), ("Reliance", 5.4), ("Kotak Bank", 4.8), ("L&T", 4.2), ("TCS", 3.8), ("Bharti Airtel", 3.2)],
    },
}


def get_fund_holdings(scheme_code):
    return FUND_HOLDINGS.get(int(scheme_code))


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
