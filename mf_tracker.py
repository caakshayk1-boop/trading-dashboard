"""Mutual Fund Portfolio Intelligence — AMFI + Google News"""
import requests, json, os, logging
import pandas as pd
from datetime import datetime, timedelta

os.makedirs("cache", exist_ok=True)
log = logging.getLogger(__name__)

MFAPI        = "https://api.mfapi.in/mf"
PORTFOLIO_FILE = "cache/mf_portfolio.json"


def search_funds(query):
    try:
        r = requests.get(f"{MFAPI}/search?q={query}", timeout=10)
        return r.json()
    except Exception as e:
        log.warning(f"Fund search failed: {e}")
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
        log.warning(f"NAV fetch {scheme_code}: {e}")
        return pd.DataFrame(), {}


def calc_returns(df):
    if df.empty or len(df) < 2:
        return {}
    latest_nav  = df["nav"].iloc[-1]
    latest_date = df["date"].iloc[-1]
    periods = {"1M": 30, "3M": 90, "6M": 180, "1Y": 365, "3Y": 1095, "5Y": 1825}
    out = {}
    for label, days in periods.items():
        past = df[df["date"] <= latest_date - timedelta(days=days)]
        if past.empty:
            continue
        p_nav = past["nav"].iloc[-1]
        if p_nav <= 0:
            continue
        if days <= 365:
            out[label] = round((latest_nav - p_nav) / p_nav * 100, 2)
        else:
            years = days / 365
            out[label] = round(((latest_nav / p_nav) ** (1 / years) - 1) * 100, 2)
    return out


def get_fund_news(fund_name, max_items=6):
    try:
        import feedparser
        q   = fund_name.replace(" ", "+") + "+mutual+fund"
        url = f"https://news.google.com/rss/search?q={q}&hl=en-IN&gl=IN&ceid=IN:en"
        feed = feedparser.parse(url)
        return [
            {"title": e.title, "link": e.link,
             "published": e.get("published", "")[:16]}
            for e in feed.entries[:max_items]
        ]
    except Exception as e:
        log.warning(f"News fetch {fund_name}: {e}")
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
        latest_nav   = float(df["nav"].iloc[-1])
        purchase_nav = float(fund.get("purchase_nav", latest_nav))
        units        = float(fund.get("units", 0))
        invested     = purchase_nav * units
        current      = latest_nav * units
        pnl          = current - invested
        pnl_pct      = (pnl / invested * 100) if invested > 0 else 0
        returns      = calc_returns(df)

        # 1-day change
        day_chg = 0.0
        if len(df) >= 2:
            prev_nav = float(df["nav"].iloc[-2])
            day_chg  = round((latest_nav - prev_nav) / prev_nav * 100, 2)

        results.append({
            "name":         fund.get("name", meta.get("scheme_name", str(fund["scheme_code"]))),
            "scheme_code":  fund["scheme_code"],
            "nav":          round(latest_nav, 4),
            "day_chg":      day_chg,
            "purchase_nav": round(purchase_nav, 4),
            "units":        units,
            "invested":     round(invested),
            "current":      round(current),
            "pnl":          round(pnl),
            "pnl_pct":      round(pnl_pct, 2),
            "returns":      returns,
            "fund_house":   meta.get("fund_house", ""),
            "category":     meta.get("scheme_category", ""),
        })
    return results
