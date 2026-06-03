#!/usr/bin/env python3
"""
global_200ma_screener.py — Weekly candle close above 200-period weekly MA.

Universe: S&P 500 + NASDAQ 100 + major global ETFs + NSE Nifty 500
Fires a Telegram alert for every stock where the LATEST weekly candle
closed ABOVE the 200-week MA for the first time (new crossover) OR
is in a persistent weekly uptrend (price > 200WMA AND was below last week).

Run via GitHub Actions weekly (Monday morning IST).
"""

import os, sys, json, logging, time
import yfinance as yf
import pandas as pd
import requests
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

IST = timezone(timedelta(hours=5, minutes=30))

# ── Universe ─────────────────────────────────────────────────────────────────

SP500_SAMPLE = [
    "AAPL","MSFT","GOOGL","AMZN","NVDA","META","BRK-B","TSLA","UNH","JPM",
    "V","XOM","JNJ","PG","MA","HD","CVX","MRK","ABBV","PEP","KO","AVGO",
    "LLY","TMO","COST","CSCO","ABT","WMT","MCD","CRM","ACN","BAC","NEE",
    "ADBE","LIN","DHR","TXN","VZ","CMCSA","ORCL","PM","RTX","NFLX","AMD",
    "INTC","QCOM","T","HON","AMGN","UPS","CAT","GS","MS","LOW","BLK",
    "SBUX","MDT","AXP","INTU","DE","GE","BKNG","SPGI","ADI","GILD","MMM",
    "ISRG","CB","PLD","ZTS","NOW","REGN","TGT","LRCX","SYK","BSX",
    "PANW","KLAC","SNPS","CDNS","MELI","ASML","TSM","NVO","SAP","SHOP",
]

GLOBAL_ETFS = [
    "QQQ","SPY","IWM","EFA","EEM","VTI","GLD","SLV","USO","TLT",
    "XLK","XLF","XLE","XLV","XLI","XLC","ARKK","SOXX","IBB",
]

NSE_QUALITY = [
    "RELIANCE.NS","TCS.NS","HDFCBANK.NS","INFY.NS","ICICIBANK.NS",
    "HINDUNILVR.NS","SBIN.NS","BAJFINANCE.NS","BHARTIARTL.NS","KOTAKBANK.NS",
    "LT.NS","AXISBANK.NS","ASIANPAINT.NS","MARUTI.NS","TITAN.NS",
    "SUNPHARMA.NS","WIPRO.NS","HCLTECH.NS","TECHM.NS","NESTLEIND.NS",
    "ADANIPORTS.NS","TATAMOTORS.NS","TATASTEEL.NS","JSWSTEEL.NS","ONGC.NS",
    "DIVISLAB.NS","DRREDDY.NS","CIPLA.NS","EICHERMOT.NS","BAJAJFINSV.NS",
    "APOLLOHOSP.NS","DMART.NS","PIDILITIND.NS","COLPAL.NS","MARICO.NS",
    "PERSISTENT.NS","LTIM.NS","COFORGE.NS","MPHASIS.NS","TRENT.NS",
]

UNIVERSE = (
    [s for s in SP500_SAMPLE] +
    GLOBAL_ETFS +
    NSE_QUALITY
)


# ── Core logic ────────────────────────────────────────────────────────────────

def check_weekly_200ma(symbol: str) -> dict | None:
    """
    Returns signal dict if weekly close > 200 WMA (fresh cross or persistent).
    Returns None if no signal or data unavailable.
    """
    try:
        df = yf.Ticker(symbol).history(period="5y", interval="1wk", auto_adjust=True)
        if df is None or len(df) < 205:
            return None

        close  = df["Close"].squeeze()
        wma200 = close.rolling(200).mean()

        cur_close = float(close.iloc[-1])
        cur_ma    = float(wma200.iloc[-1])
        prev_close = float(close.iloc[-2])
        prev_ma    = float(wma200.iloc[-2])

        if cur_ma <= 0 or cur_close <= 0:
            return None

        above_now  = cur_close > cur_ma
        above_prev = prev_close > prev_ma

        # Only signal on NEW crossover (was below, now above) or strong trend
        fresh_cross = above_now and not above_prev
        # Strong trend: >3% above 200WMA and last week was also above
        strong_trend = above_now and above_prev and (cur_close / cur_ma - 1) > 0.03

        if not (fresh_cross or strong_trend):
            return None

        pct_above = round((cur_close / cur_ma - 1) * 100, 1)
        signal_type = "🚀 FRESH CROSS" if fresh_cross else "📈 WEEKLY TREND"

        return {
            "symbol":      symbol,
            "price":       round(cur_close, 4),
            "wma200":      round(cur_ma, 4),
            "pct_above":   pct_above,
            "type":        signal_type,
            "fresh_cross": fresh_cross,
        }
    except Exception as e:
        log.debug(f"{symbol}: {e}")
        return None


def _post(text: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(text)
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"},
            timeout=15,
        )
    except Exception as e:
        log.warning(f"Telegram send error: {e}")


def run():
    log.info(f"Global 200WMA screener — {len(UNIVERSE)} symbols")
    results = []

    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(check_weekly_200ma, sym): sym for sym in UNIVERSE}
        for future in as_completed(futures):
            r = future.result()
            if r:
                results.append(r)
                log.info(f"✅ {r['symbol']} {r['type']} +{r['pct_above']}% above 200WMA")

    if not results:
        log.info("No weekly 200WMA crossovers/trends found.")
        _post("🌍 *Global 200WMA Screener*\nNo new crossovers or strong trends this week.")
        return

    # Sort: fresh crosses first, then by % above
    results.sort(key=lambda x: (-int(x["fresh_cross"]), -x["pct_above"]))

    # Group into batches of 15 for Telegram
    now_ist = datetime.now(IST).strftime("%d %b %Y")
    header  = f"🌍 *Global Weekly 200MA Screener* — {now_ist}\n_{len(results)} signals · weekly close > 200WMA_\n\n"

    lines = []
    for r in results:
        lines.append(
            f"{r['type']} *{r['symbol']}*\n"
            f"Price `{r['price']}` · 200WMA `{r['wma200']}` · *+{r['pct_above']}% above*"
        )

    # Send in chunks of 15
    chunk = 15
    for i in range(0, len(lines), chunk):
        batch = lines[i:i+chunk]
        msg   = (header if i == 0 else f"_(continued {i+1}–{min(i+chunk,len(lines))})_\n\n") + "\n\n".join(batch)
        msg  += "\n\n_Not SEBI/financial advice · @askakshayfinance_"
        _post(msg)
        time.sleep(1)

    # Save to data file
    os.makedirs("data", exist_ok=True)
    with open("data/global_200ma.json", "w") as f:
        json.dump(results, f, indent=2)
    log.info(f"Saved {len(results)} signals to data/global_200ma.json")


if __name__ == "__main__":
    run()
