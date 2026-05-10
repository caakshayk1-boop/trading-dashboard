import streamlit as st
import streamlit.components.v1 as _stc
import plotly.graph_objects as go
import plotly.express as px
import pandas as pd
import yfinance as yf
import ta as ta_lib
from datetime import datetime, date
import pytz, os, json, requests

from scanner import fetch_forex_comm, obfuscate_reasons, scan_ohl_oll
from tracker import (get_performance, get_history, get_active_signals, init_db,
                     get_breakouts, get_4h_signals, get_commodity_signals,
                     get_last_scan, get_signals_display)
from config import MIN_SIGNAL_SCORE, CAPITAL

_GH_RAW = "https://raw.githubusercontent.com/caakshayk1-boop/trading-dashboard/main/data"

@st.cache_data(ttl=60)
def _fetch_json(name: str):
    try:
        r = requests.get(f"{_GH_RAW}/{name}.json", timeout=10)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None

def _gh_signals_display(days=3, min_score=0):
    from datetime import timedelta
    data = _fetch_json("signals")
    if not data:
        return []
    cutoff = str(date.today() - timedelta(days=days))
    result = []
    for row in data:
        if row.get("date","") < cutoff:
            continue
        if int(row.get("score",0)) < min_score:
            continue
        try:
            meta = json.loads(row.get("metadata") or "{}")
        except Exception:
            meta = {}
        entry = float(row.get("entry", 0))
        sl2   = float(row.get("sl2") or entry * 0.96)
        t1    = float(row.get("target1", 0))
        t2    = float(row.get("target2", 0))
        t3    = float(row.get("target3", 0))
        risk  = max(entry - sl2, 0.01)
        result.append({
            "symbol":      row.get("symbol",""),
            "action":      row.get("action","BUY"),
            "setup_type":  row.get("setup_type",""),
            "price":       entry,
            "sl1":         float(row.get("sl1") or sl2),
            "sl2":         sl2,
            "target1":     t1, "target2": t2, "target3": t3,
            "score":       int(row.get("score",0)),
            "status":      row.get("status","OPEN"),
            "date":        row.get("date",""),
            "rsi":         meta.get("rsi", 0),
            "adx":         meta.get("adx", 0),
            "vol_ratio":   meta.get("vol_ratio", 1.0),
            "regime":      meta.get("regime",""),
            "reasons":     meta.get("reasons",""),
            "fno_eligible":meta.get("fno", False),
            "rr1":         meta.get("rr1") or round((t1-entry)/risk,2),
            "rr2":         meta.get("rr2") or round((t2-entry)/risk,2),
            "qty":         meta.get("qty",0),
            "atr":         meta.get("atr",0),
            "tv_link":     meta.get("tv_link") or f"https://in.tradingview.com/chart/?symbol=NSE:{row.get('symbol','')}",
            "bias":        meta.get("bias","bullish"),
            "fno_suggestion": meta.get("fno_suggestion"),
        })
    result.sort(key=lambda x: x["score"], reverse=True)
    return result

def _gh_breakouts(days=3):
    from datetime import timedelta
    data = _fetch_json("breakouts")
    if not data:
        return pd.DataFrame()
    cutoff = str(date.today() - timedelta(days=days))
    rows = [r for r in data if r.get("date","") >= cutoff]
    df = pd.DataFrame(rows)
    if not df.empty and "patterns" in df.columns:
        df["patterns"] = df["patterns"].apply(
            lambda x: json.loads(x) if isinstance(x, str) and x else [])
    return df

def _gh_4h_signals(days=1):
    from datetime import timedelta
    data = _fetch_json("signals_4h")
    if not data:
        return pd.DataFrame()
    cutoff = str(date.today() - timedelta(days=days))
    rows = [r for r in data if r.get("date","") >= cutoff]
    return pd.DataFrame(rows)

def _gh_commodity_signals(days=1):
    from datetime import timedelta
    data = _fetch_json("commodity_signals")
    if not data:
        return pd.DataFrame()
    cutoff = str(date.today() - timedelta(days=days))
    rows = [r for r in data if r.get("date","") >= cutoff]
    return pd.DataFrame(rows)

def _gh_last_scan():
    data = _fetch_json("scan_meta")
    if data:
        return data.get("ts"), data.get("slot"), data.get("counts",{})
    return None, None, {}

def _gh_multibaggers(days=7):
    from datetime import timedelta
    data = _fetch_json("multibaggers")
    if not data:
        return pd.DataFrame()
    cutoff = str(date.today() - timedelta(days=days))
    rows = [r for r in data if r.get("date","") >= cutoff]
    return pd.DataFrame(rows)

def _gh_all_signals(days=9999):
    from datetime import timedelta
    data = _fetch_json("all_signals")
    if not data:
        return pd.DataFrame()
    if days >= 9999:
        return pd.DataFrame(data)
    cutoff = str(date.today() - timedelta(days=days))
    rows = [r for r in data if r.get("date","") >= cutoff]
    return pd.DataFrame(rows)

def _get_ai_signals(days=3):
    from datetime import timedelta
    cutoff = str(date.today() - timedelta(days=days))
    if IS_LOCAL:
        df = get_breakouts(days=days)
        if df.empty:
            return []
        rows = df.to_dict("records")
    else:
        data = _fetch_json("breakouts")
        if not data:
            return []
        rows = [r for r in data if r.get("date","") >= cutoff]
    ai = [r for r in rows if "AI Channel" in str(r.get("pattern","")) or "Channel Breakout" in str(r.get("pattern",""))]
    return ai

def _gh_intraday_signals(days=1):
    from datetime import timedelta
    data = _fetch_json("all_signals")
    if not data:
        return []
    cutoff = str(date.today() - timedelta(days=days))
    return [r for r in data if r.get("signal_type") == "intraday" and r.get("date","") >= cutoff]

from mf_tracker import (search_funds, get_nav_history, calc_returns, get_fund_news,
                         load_portfolio, save_portfolio, get_portfolio_summary,
                         get_index_quotes, get_top_funds_data, get_stock_news,
                         get_corporate_actions, get_fund_holdings, get_fund_meta)

# ─────────────────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="TradeFlow — NSE Scanner",
    layout="wide", page_icon="📈",
    initial_sidebar_state="expanded",
)
IST = pytz.timezone("Asia/Kolkata")
IS_LOCAL = (
    not os.path.exists("/mount/src") and
    os.getenv("GITHUB_ACTIONS") != "true" and
    os.getenv("STREAMLIT_SHARING_MODE") != "true"
)
try:
    init_db()
except Exception:
    pass

# ─── CSS ─────────────────────────────────────────────────────────────────────
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&display=swap');

/* ── Base — white Groww theme ── */
html, body, [data-testid="stApp"] {
  background: #f7f8fa !important;
  color: #1a1a1a !important;
  font-family: 'Inter', system-ui, sans-serif !important;
  -webkit-font-smoothing: antialiased;
}
[data-testid="stAppViewContainer"], .stApp { background: #f7f8fa !important; }
[data-testid="stSidebar"] {
  background: #ffffff !important;
  border-right: 1px solid #e8eaed !important;
  box-shadow: 2px 0 12px rgba(0,0,0,.05);
}
[data-testid="stSidebar"] * { font-family: 'Inter', sans-serif !important; }

/* Hide chrome */
#MainMenu, footer, [data-testid="stToolbar"], header { display: none !important; }
.block-container { padding: 20px 28px 40px !important; max-width: 1400px !important; }
[data-testid="stVerticalBlock"] > div { gap: 0 !important; }

/* Tabs */
[data-testid="stTabs"] [data-baseweb="tab-list"] {
  background: #ffffff !important;
  border-bottom: 2px solid #e8eaed !important;
  border-radius: 12px 12px 0 0 !important;
  gap: 0 !important; padding: 0 4px !important;
}
[data-testid="stTabs"] [data-baseweb="tab"] {
  background: transparent !important;
  border: none !important;
  color: #9ca3af !important;
  font-size: 13px !important;
  font-weight: 600 !important;
  padding: 12px 20px !important;
  border-bottom: 2px solid transparent !important;
  margin-bottom: -2px !important;
  transition: color .2s;
}
[data-testid="stTabs"] [data-baseweb="tab"][aria-selected="true"] {
  color: #00d09c !important;
  border-bottom-color: #00d09c !important;
  text-shadow: 0 0 16px rgba(0,208,156,.5) !important;
}
[data-testid="stTabs"] [data-baseweb="tab-highlight"] { display: none !important; }

/* Metrics */
[data-testid="metric-container"] {
  background: #ffffff !important;
  border: 1px solid #e8eaed !important;
  border-radius: 12px !important;
  padding: 14px 16px !important;
  box-shadow: 0 1px 4px rgba(0,0,0,.06) !important;
}
[data-testid="stMetricLabel"] { color: #9ca3af !important; font-size: 11px !important; font-weight: 600 !important; }
[data-testid="stMetricValue"] { color: #1a1a1a !important; font-size: 22px !important; font-weight: 800 !important; }

/* Selectbox / inputs */
[data-testid="stSelectbox"] > div > div,
[data-testid="stTextInput"] > div > div > input {
  background: #ffffff !important;
  border: 1px solid #e8eaed !important;
  border-radius: 8px !important;
  color: #1a1a1a !important;
  font-size: 13px !important;
}
[data-testid="stSelectbox"] label, [data-testid="stTextInput"] label {
  color: #9ca3af !important; font-size: 12px !important; font-weight: 600 !important;
}
[data-baseweb="select"] [data-testid="stMarkdownContainer"] p { font-size: 13px !important; color: #1a1a1a !important; }
[data-baseweb="popover"] { background: #ffffff !important; border: 1px solid #e8eaed !important; }
[role="option"] { color: #1a1a1a !important; }
[role="option"]:hover { background: #f0fdf9 !important; }

/* Buttons */
[data-testid="stButton"] > button {
  background: #ffffff !important;
  border: 1px solid #e8eaed !important;
  border-radius: 8px !important;
  color: #1a1a1a !important;
  font-size: 13px !important;
  font-weight: 600 !important;
  padding: 6px 14px !important;
  box-shadow: 0 1px 3px rgba(0,0,0,.06) !important;
}
[data-testid="stButton"] > button:hover {
  border-color: #00d09c !important;
  color: #00d09c !important;
  box-shadow: 0 0 10px rgba(0,208,156,.2) !important;
}

/* Slider */
[data-testid="stSlider"] label { color: #9ca3af !important; font-size: 12px !important; }
[data-testid="stSlider"] [data-testid="stThumbValue"] { color: #00d09c !important; }
[data-testid="stSlider"] [data-baseweb="slider"] div[role="slider"] {
  background: #00d09c !important;
  box-shadow: 0 0 8px rgba(0,208,156,.5) !important;
}

/* Expander */
[data-testid="stExpander"] {
  background: #ffffff !important;
  border: 1px solid #e8eaed !important;
  border-radius: 12px !important;
  box-shadow: 0 1px 4px rgba(0,0,0,.05) !important;
}
[data-testid="stExpander"] summary { color: #6b7280 !important; font-size: 13px !important; font-weight: 600 !important; }

/* Dataframe */
[data-testid="stDataFrame"] { border-radius: 12px !important; overflow: hidden !important; border: 1px solid #e8eaed !important; }
.dvn-scroller { background: #ffffff !important; }

/* Divider */
hr { border-color: #e8eaed !important; margin: 16px 0 !important; }

/* Scrollbar */
::-webkit-scrollbar { width: 5px; height: 5px; }
::-webkit-scrollbar-track { background: #f7f8fa; }
::-webkit-scrollbar-thumb { background: #d1d5db; border-radius: 3px; }

/* ── Cards ── */
.card {
  background: #ffffff;
  border: 1px solid #e8eaed;
  border-left: 3px solid #00d09c;
  border-radius: 12px;
  padding: 16px 18px;
  margin-bottom: 10px;
  box-shadow: 0 1px 6px rgba(0,0,0,.05);
  transition: box-shadow .2s;
}
.card:hover { box-shadow: 0 4px 16px rgba(0,208,156,.12); }
.card.sell  { border-left-color: #e8192c; }
.card.sell:hover { box-shadow: 0 4px 16px rgba(232,25,44,.1); }
.card.neutral { border-left-color: #f59e0b; }

/* ── KV grid ── */
.kv-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(80px, 1fr));
  gap: 8px;
  margin-top: 12px;
}
.kv-cell {
  background: #f7f8fa;
  border: 1px solid #e8eaed;
  border-radius: 8px;
  padding: 8px 10px;
  text-align: center;
}
.kv-label {
  font-size: 10px;
  color: #9ca3af;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: .06em;
  margin-bottom: 3px;
}
.kv-val { font-size: 14px; font-weight: 700; color: #1a1a1a; }
.kv-val.green { color: #00d09c; text-shadow: 0 0 10px rgba(0,208,156,.4); }
.kv-val.red   { color: #e8192c; text-shadow: 0 0 10px rgba(232,25,44,.3); }
.kv-val.blue  { color: #3b82f6; text-shadow: 0 0 10px rgba(59,130,246,.3); }
.kv-val.amber { color: #f59e0b; text-shadow: 0 0 10px rgba(245,158,11,.3); }

/* ── Section title ── */
.section-title {
  font-size: 11px;
  font-weight: 700;
  color: #9ca3af;
  text-transform: uppercase;
  letter-spacing: .1em;
  padding: 16px 0 10px;
  border-bottom: 1px solid #e8eaed;
  margin-bottom: 14px;
}

/* ── Badges ── */
.badge {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 4px;
  font-size: 10px;
  font-weight: 700;
  letter-spacing: .04em;
}
.badge.buy    { background: rgba(0,208,156,.1);  color: #00a87e; border: 1px solid rgba(0,208,156,.3); }
.badge.sell   { background: rgba(232,25,44,.08); color: #e8192c; border: 1px solid rgba(232,25,44,.25); }
.badge.fno    { background: rgba(59,130,246,.08);color: #3b82f6; border: 1px solid rgba(59,130,246,.25); }
.badge.weekly { background: rgba(245,158,11,.08);color: #d97706; border: 1px solid rgba(245,158,11,.25); }
.badge.monthly{ background: rgba(139,92,246,.08);color: #7c3aed; border: 1px solid rgba(139,92,246,.25); }
.badge.daily  { background: rgba(0,208,156,.08); color: #00a87e; border: 1px solid rgba(0,208,156,.2); }
.badge.intra  { background: rgba(251,191,36,.08);color: #d97706; border: 1px solid rgba(251,191,36,.25); }

/* ── Misc ── */
@keyframes pulse { 0%,100%{opacity:1;} 50%{opacity:.4;} }
.muted { color: #9ca3af; font-size: 12px; }
.sym { font-size: 18px; font-weight: 800; color: #1a1a1a; }
.price-big { font-size: 20px; font-weight: 800; color: #1a1a1a; }

/* ── Neon glow on key numbers ── */
.neon-green { color: #00d09c; text-shadow: 0 0 14px rgba(0,208,156,.6), 0 0 28px rgba(0,208,156,.2); font-weight: 800; }
.neon-red   { color: #e8192c; text-shadow: 0 0 14px rgba(232,25,44,.5), 0 0 28px rgba(232,25,44,.15); font-weight: 800; }

/* ── Fund cards ── */
.gw-fund-card {
  background: #ffffff; border: 1px solid #e8eaed;
  border-radius: 14px; padding: 18px 20px; margin-bottom: 12px;
  box-shadow: 0 1px 6px rgba(0,0,0,.05);
}
.gw-card-head { display: flex; align-items: flex-start; gap: 16px; margin-bottom: 14px; }
.gw-fund-name { font-size: 14px; font-weight: 700; color: #1a1a1a; line-height: 1.4; }
.gw-fund-amc  { font-size: 11px; color: #9ca3af; margin-top: 2px; }
.gw-chips { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 6px; }
.gw-chip { font-size: 10px; font-weight: 600; padding: 2px 8px; border-radius: 99px; }
.gw-chip.cat  { background: rgba(59,130,246,.08); color: #3b82f6; border: 1px solid rgba(59,130,246,.2); }
.risk-low  { background: rgba(0,208,156,.08); color: #00a87e; border: 1px solid rgba(0,208,156,.2); }
.risk-mod  { background: rgba(59,130,246,.08);color: #3b82f6; border: 1px solid rgba(59,130,246,.2); }
.risk-mh   { background: rgba(245,158,11,.08);color: #d97706; border: 1px solid rgba(245,158,11,.2); }
.risk-high { background: rgba(251,113,74,.08);color: #ea580c; border: 1px solid rgba(251,113,74,.2); }
.risk-vh   { background: rgba(232,25,44,.08); color: #e8192c; border: 1px solid rgba(232,25,44,.2); }
.gw-nav-block { text-align: right; flex-shrink: 0; }
.gw-nav-price { font-size: 16px; font-weight: 800; color: #1a1a1a; }
.gw-nav-chg   { font-size: 11px; font-weight: 600; margin-top: 2px; }
.gw-returns-bar { display: flex; gap: 6px; margin-bottom: 14px; flex-wrap: wrap; }
.gw-ret-cell { flex: 1; min-width: 48px; background: #f7f8fa; border: 1px solid #e8eaed; border-radius: 6px; padding: 6px 8px; text-align: center; }
.gw-ret-period { font-size: 9px; color: #9ca3af; font-weight: 600; margin-bottom: 3px; }
.gw-ret-val    { font-size: 13px; font-weight: 800; }
.gw-inv-band   { display: flex; gap: 0; border-top: 1px solid #e8eaed; padding-top: 12px; }
.gw-inv-cell   { flex: 1; text-align: center; border-right: 1px solid #e8eaed; }
.gw-inv-cell:last-child { border-right: none; }
.gw-inv-label  { font-size: 10px; color: #9ca3af; font-weight: 600; margin-bottom: 4px; }
.gw-inv-val    { font-size: 14px; font-weight: 700; color: #1a1a1a; }
.gw-fund-details { display: flex; gap: 0; flex-wrap: wrap; }
.gw-detail-cell { flex: 1; min-width: 120px; padding: 10px 14px; border-right: 1px solid #e8eaed; }
.gw-detail-cell:last-child { border-right: none; }
.gw-detail-label { font-size: 10px; color: #9ca3af; font-weight: 600; margin-bottom: 4px; }
.gw-detail-val   { font-size: 13px; font-weight: 700; color: #1a1a1a; }
.gw-rom { text-align: center; }
</style>
""", unsafe_allow_html=True)

# ─── Auto-refresh ─────────────────────────────────────────────────────────────
try:
    from streamlit_autorefresh import st_autorefresh
    st_autorefresh(interval=60_000, key="live_refresh")
except ImportError:
    pass

# ─── Cached fetchers ──────────────────────────────────────────────────────────
@st.cache_data(ttl=300)
def _forex():
    return fetch_forex_comm()

@st.cache_data(ttl=1800)
def _market_regime():
    try:
        from scanner import fetch_market_regime
        return fetch_market_regime()
    except Exception:
        return {"regime":"UNKNOWN","vix":None,"nifty":None,"nifty_change":None,
                "trend":"NEUTRAL","adx":None,"regime_detail":"—"}

@st.cache_data(ttl=60)
def _index_quotes():
    return get_index_quotes()

@st.cache_data(ttl=3600)
def _top_funds():
    return get_top_funds_data()

@st.cache_data(ttl=600)
def _mf_summary(portfolio_json):
    return get_portfolio_summary(json.loads(portfolio_json))

@st.cache_data(ttl=900)
def _ohl_oll_scan():
    return scan_ohl_oll()

# ─── Helpers ──────────────────────────────────────────────────────────────────
_now_hdr = datetime.now(IST)
_now_h, _now_m = _now_hdr.hour, _now_hdr.minute

try:
    from scanner import is_trading_day as _itd
    _is_trading = _itd(_now_hdr)
except Exception:
    _is_trading = _now_hdr.weekday() < 5

def _kv(label, val, cls=""):
    return f'<div class="kv-cell"><div class="kv-label">{label}</div><div class="kv-val {cls}">{val}</div></div>'

def _card_open(action="BUY"):
    cls = "" if action == "BUY" else "sell"
    return f'<div class="card {cls}">'

def _card_close():
    return '</div>'

def _tv_btn(link):
    link = (link or "").replace("&", "%26")
    return f'<a href="{link}" target="_blank" style="font-size:11px;font-weight:600;color:#00d09c;text-decoration:none;padding:4px 10px;border:1px solid rgba(0,208,156,.25);border-radius:6px">Chart ↗</a>'

def _grade(b):
    vol = float(b.get("vol_ratio",1)); rr = float(b.get("rr",1))
    tf  = b.get("timeframe","Daily")
    pts = 0
    if vol >= 5:   pts += 3
    elif vol >= 3: pts += 2
    elif vol >= 2: pts += 1
    if rr >= 2.5:  pts += 2
    elif rr >= 1.8: pts += 1
    if tf == "Monthly": pts += 3
    elif tf == "Weekly": pts += 2
    return {5:"S",4:"S",3:"A",2:"A",1:"B",0:"B"}.get(pts, "C")

# ─── Sidebar ──────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown('<div style="font-size:16px;font-weight:800;color:#1a1a1a;padding:4px 0 16px">📈 TradeFlow</div>', unsafe_allow_html=True)

    # Market regime
    _rd = _market_regime()
    _reg = _rd.get("regime","UNKNOWN").replace("_"," ")
    _nifty = _rd.get("nifty")
    _nc    = _rd.get("nifty_change", 0) or 0
    _vix   = _rd.get("vix")
    _nc_col = "#00a87e" if _nc >= 0 else "#e8192c"
    st.markdown(f"""
<div style="background:#f7f8fa;border:1px solid #e8eaed;border-radius:12px;padding:14px 16px;margin-bottom:16px">
  <div style="font-size:10px;color:#9ca3af;font-weight:600;text-transform:uppercase;letter-spacing:.08em;margin-bottom:8px">Market</div>
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
    <span style="font-size:13px;font-weight:700;color:#1a1a1a">NIFTY</span>
    <span style="font-size:13px;font-weight:700;color:{_nc_col};text-shadow:0 0 8px {'rgba(0,208,156,.4)' if _nc>=0 else 'rgba(232,25,44,.3)'}">{f'{_nifty:,.0f}' if _nifty else '—'} <small>({_nc:+.2f}%)</small></span>
  </div>
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
    <span style="font-size:12px;color:#9ca3af">VIX</span>
    <span style="font-size:12px;font-weight:600;color:#1a1a1a">{f'{_vix:.1f}' if _vix else '—'}</span>
  </div>
  <div style="display:flex;justify-content:space-between;align-items:center">
    <span style="font-size:12px;color:#9ca3af">Regime</span>
    <span style="font-size:11px;font-weight:700;color:#f59e0b">{_reg}</span>
  </div>
</div>
""", unsafe_allow_html=True)

    # Scan schedule
    _SLOTS = [("9:20 AM","4H + Commodity",9,20),("11:42 AM","Swing + F&O",11,42),("4:30 PM","Breakouts EOD",16,30),("8:00 PM","Multibagger",20,0)]
    def _slot_st(h, m):
        nm = _now_h*60+_now_m; sm = h*60+m
        if not _is_trading: return "#d1d5db"
        if nm > sm+10: return "#00a87e"
        if abs(nm-sm) <= 10: return "#f59e0b"
        return "#9ca3af"

    st.markdown('<div style="font-size:10px;color:#9ca3af;font-weight:600;text-transform:uppercase;letter-spacing:.08em;margin-bottom:8px">Auto-Scan (IST)</div>', unsafe_allow_html=True)
    for t, label, h, m in _SLOTS:
        col = _slot_st(h, m)
        st.markdown(f'<div style="display:flex;justify-content:space-between;padding:5px 0;border-bottom:1px solid #e8eaed"><span style="font-size:12px;color:{col};font-weight:600">{t}</span><span style="font-size:11px;color:#9ca3af">{label}</span></div>', unsafe_allow_html=True)

    st.markdown('<div style="font-size:9px;color:#9ca3af;margin-top:20px;line-height:1.6">Not SEBI-registered. For research only. Trade at your own risk.</div>', unsafe_allow_html=True)

# Default filter values (no sidebar filters — inline per tab)
_days_n   = 3
_min_score = 70

# ─── Header bar ───────────────────────────────────────────────────────────────
_scan_ts, _scan_slot, _scan_counts = _gh_last_scan()
_now_str = _now_hdr.strftime("%d %b %Y · %I:%M %p IST")

# Prefetch data needed for header counts
_signals_hdr = _gh_signals_display(days=_days_n, min_score=_min_score) if not IS_LOCAL else get_signals_display(days=_days_n, min_score=_min_score)
_bos_df = _gh_breakouts(days=_days_n) if not IS_LOCAL else get_breakouts(days=_days_n)

_sig_count = len(_signals_hdr)
_bo_count  = len(_bos_df) if not _bos_df.empty else 0

try:
    _perf = get_performance() if IS_LOCAL else {}
    _wr_hdr = _perf.get("win_rate", 0) if _perf else 0
    _trades_hdr = _perf.get("closed_trades", 0) if _perf else 0
except Exception:
    _wr_hdr = 0; _trades_hdr = 0

_mkt_open = 9 <= _now_h < 16
st.markdown(f"""
<div style="display:flex;align-items:center;justify-content:space-between;
  padding:14px 0 18px;border-bottom:2px solid #e8eaed;margin-bottom:20px;flex-wrap:wrap;gap:10px">
  <div>
    <div style="font-size:24px;font-weight:900;color:#1a1a1a;line-height:1;letter-spacing:-.5px">TradeFlow</div>
    <div style="font-size:12px;color:#9ca3af;margin-top:3px">NSE Nifty 500 · {_now_str}</div>
  </div>
  <div style="display:flex;gap:10px;flex-wrap:wrap">
    <div style="background:#ffffff;border:1px solid #e8eaed;border-radius:12px;padding:10px 18px;text-align:center;box-shadow:0 1px 4px rgba(0,0,0,.06)">
      <div style="font-size:10px;color:#9ca3af;font-weight:600;margin-bottom:3px">Signals</div>
      <div style="font-size:22px;font-weight:900;color:#00d09c;text-shadow:0 0 14px rgba(0,208,156,.5)">{_sig_count}</div>
    </div>
    <div style="background:#ffffff;border:1px solid #e8eaed;border-radius:12px;padding:10px 18px;text-align:center;box-shadow:0 1px 4px rgba(0,0,0,.06)">
      <div style="font-size:10px;color:#9ca3af;font-weight:600;margin-bottom:3px">Breakouts</div>
      <div style="font-size:22px;font-weight:900;color:#7c3aed;text-shadow:0 0 14px rgba(124,58,237,.4)">{_bo_count}</div>
    </div>
    <div style="background:#ffffff;border:1px solid #e8eaed;border-radius:12px;padding:10px 18px;text-align:center;box-shadow:0 1px 4px rgba(0,0,0,.06)">
      <div style="font-size:10px;color:#9ca3af;font-weight:600;margin-bottom:3px">Win Rate</div>
      <div style="font-size:22px;font-weight:900;color:{'#00a87e' if _wr_hdr>=55 else '#1a1a1a'};{'text-shadow:0 0 14px rgba(0,208,156,.5)' if _wr_hdr>=55 else ''}">{_wr_hdr:.0f}%</div>
    </div>
    <div style="background:{'#f0fdf9' if _mkt_open else '#fafafa'};border:1px solid {'#00d09c' if _mkt_open else '#e8eaed'};border-radius:12px;padding:10px 18px;text-align:center;{'box-shadow:0 0 12px rgba(0,208,156,.2)' if _mkt_open else ''}">
      <div style="font-size:10px;color:#9ca3af;font-weight:600;margin-bottom:3px">Market</div>
      <div style="font-size:13px;font-weight:800;color:{'#00a87e' if _mkt_open else '#9ca3af'};margin-top:2px">{'● OPEN' if _mkt_open else '○ CLOSED'}</div>
    </div>
  </div>
</div>
""", unsafe_allow_html=True)

# ─── Disclaimer ───────────────────────────────────────────────────────────────
st.markdown('<div style="background:rgba(245,158,11,.06);border:1px solid rgba(245,158,11,.2);border-radius:8px;padding:8px 14px;font-size:11px;color:#6b7280;margin-bottom:20px">⚠ <b style="color:#9ca3af">Research only.</b> Not SEBI-registered. Not financial advice. Yahoo Finance data (15-min delay). Past performance ≠ future results.</div>', unsafe_allow_html=True)

# ─── TABS ─────────────────────────────────────────────────────────────────────
tab_sig, tab_bo, tab_intra, tab_fno, tab_mf, tab_watch, tab_hist = st.tabs([
    "📈 Signals", "🚀 Breakouts", "⚡ Intraday",
    "📊 F&O", "💰 Mutual Funds", "💎 Watchlist", "📋 History"
])


# ══════════════════════════════════════════════════════════════════════════════
# TAB: SIGNALS
# ══════════════════════════════════════════════════════════════════════════════
with tab_sig:
    signals = _signals_hdr

    if not signals:
        st.markdown("""
<div style="text-align:center;padding:60px 20px">
  <div style="font-size:36px;margin-bottom:12px">🔍</div>
  <div style="font-size:15px;font-weight:700;color:#e8eaed;margin-bottom:6px">No setups matching filters</div>
  <div style="font-size:12px;color:#6b7280;line-height:1.7">Scanned 500 stocks · Adjust score filter or lookback in sidebar</div>
</div>""", unsafe_allow_html=True)
    else:
        # KPI row
        c1,c2,c3,c4,c5 = st.columns(5)
        c1.metric("Signals",    len(signals))
        c2.metric("Top Score",  f"{signals[0]['score']}/100")
        c3.metric("Avg Score",  f"{round(sum(s['score'] for s in signals)/len(signals),1)}")
        c4.metric("F&O Ready",  sum(1 for s in signals if s.get("fno_eligible")))
        rr_vals = [s['rr1'] for s in signals if s.get('rr1',0) > 0]
        c5.metric("Avg RR", f"1:{round(sum(rr_vals)/len(rr_vals),1)}" if rr_vals else "—")

        st.markdown("<div style='height:16px'></div>", unsafe_allow_html=True)

        # Filters
        fc1,fc2,fc3,fc4,fc5 = st.columns(5)
        _f_action = fc1.selectbox("Action",    ["All","BUY","SELL"],                       key="sf_act")
        _f_score  = fc2.selectbox("Min Score", ["Any","70+","80+","85+","90+"],             key="sf_scr")
        _f_setup  = fc3.selectbox("Setup",     ["All","Breakout","Pullback","Divergence"],  key="sf_set")
        _f_fno    = fc4.selectbox("F&O",       ["All","F&O Only"],                          key="sf_fno")
        _f_vol    = fc5.selectbox("Volume",    ["All","1.5x+","2.5x+"],                     key="sf_vol")

        _sg = {"Any":0,"70+":70,"80+":80,"85+":85,"90+":90}.get(_f_score, 0)
        _vg = {"All":0,"1.5x+":1.5,"2.5x+":2.5}.get(_f_vol, 0)

        def _apply(sigs):
            out = []
            for s in sigs:
                if _f_action != "All" and s.get("action","BUY") != _f_action: continue
                if s.get("score",0) < _sg: continue
                if _f_setup != "All" and _f_setup.lower() not in s.get("setup_type","").lower(): continue
                if _f_fno == "F&O Only" and not s.get("fno_eligible"): continue
                if s.get("vol_ratio",1.0) < _vg: continue
                out.append(s)
            return out

        sigs_f = _apply(sorted(signals, key=lambda x: (x.get("date",""), x.get("score",0)), reverse=True))

        if not sigs_f:
            st.info("No signals match current filters.")
        else:
            for s in sigs_f:
                action = s.get("action","BUY")
                price  = float(s.get("price",0))
                sl     = float(s.get("sl2",0) or s.get("sl1",0))
                t1     = float(s.get("target1",0))
                t2     = float(s.get("target2",0))
                t3     = float(s.get("target3",0))
                rr     = s.get("rr1",0)
                vol    = s.get("vol_ratio",0)
                score  = s.get("score",0)
                rsi    = s.get("rsi",0)
                tv     = s.get("tv_link") or f"https://in.tradingview.com/chart/?symbol=NSE:{s['symbol']}"
                fno_b  = '<span class="badge fno" style="margin-left:6px">F&O</span>' if s.get("fno_eligible") else ""
                act_cls= "" if action=="BUY" else "sell"
                act_col= "#00d09c" if action=="BUY" else "#eb5757"

                st.markdown(f"""
<div class="card {act_cls}">
  <div style="display:flex;justify-content:space-between;align-items:flex-start">
    <div>
      <span class="sym">{s['symbol']}</span>
      <span class="badge {'buy' if action=='BUY' else 'sell'}" style="margin-left:8px">{action}</span>
      {fno_b}
      <div style="font-size:11px;color:#6b7280;margin-top:4px">{s.get('setup_type','').title()} · Score {score}/100 · {s.get('date','')}</div>
    </div>
    <div style="text-align:right">
      <div class="price-big" style="color:#e8eaed">₹{price:,.2f}</div>
      <div style="font-size:11px;color:#6b7280;margin-top:2px">Vol {vol:.1f}x · RSI {rsi:.0f}</div>
    </div>
  </div>
  <div class="kv-grid">
    {_kv("Entry", f"₹{price:,.2f}")}
    {_kv("Stop",  f"₹{sl:,.2f}",  "red")}
    {_kv("T1",    f"₹{t1:,.2f}",  "green")}
    {_kv("T2",    f"₹{t2:,.2f}",  "green")}
    {_kv("T3",    f"₹{t3:,.2f}",  "green") if t3 > 0 else ""}
    {_kv("RR",    f"1:{rr}",       "blue")}
  </div>
  <div style="margin-top:12px;display:flex;align-items:center;justify-content:space-between">
    <div style="font-size:11px;color:#6b7280">{s.get('reasons','')[:80]}</div>
    {_tv_btn(tv)}
  </div>
</div>""", unsafe_allow_html=True)

        # Trade Planner
        with st.expander("🧮 Trade Planner — Position Sizing"):
            tp_c1,tp_c2,tp_c3 = st.columns(3)
            tp_cap   = tp_c1.number_input("Capital (₹)",      10000, 50000000, 100000, 5000, key="tp_cap")
            tp_risk  = tp_c2.slider("Risk per trade (%)",     0.5, 5.0, 1.0, 0.25,           key="tp_rsk")
            tp_entry = tp_c3.number_input("Entry (₹)",        1.0,   value=500.0, step=1.0,  key="tp_ent")
            tp_sl    = tp_c1.number_input("Stop Loss (₹)",    1.0,   value=480.0, step=1.0,  key="tp_sl")
            tp_t1    = tp_c2.number_input("Target 1 (₹)",     1.0,   value=540.0, step=1.0,  key="tp_t1")
            tp_t2    = tp_c3.number_input("Target 2 (₹)",     1.0,   value=570.0, step=1.0,  key="tp_t2")
            tp_per   = abs(tp_entry - tp_sl)
            tp_qty   = int(tp_cap * tp_risk / 100 / tp_per) if tp_per > 0 else 0
            tp_inv   = tp_qty * tp_entry
            tp_g1    = tp_qty * (tp_t1 - tp_entry)
            tp_g2    = tp_qty * (tp_t2 - tp_entry)
            tp_loss  = tp_qty * tp_per
            tp_rr1   = round(tp_g1/tp_loss,2) if tp_loss>0 else 0
            mc1,mc2,mc3,mc4,mc5 = st.columns(5)
            mc1.metric("Qty",         tp_qty)
            mc2.metric("Invested",    f"₹{tp_inv:,.0f}")
            mc3.metric("Max Risk",    f"₹{tp_loss:,.0f}")
            mc4.metric("Gain @ T1",   f"₹{tp_g1:,.0f}")
            mc5.metric("RR",          f"1:{tp_rr1}")


# ══════════════════════════════════════════════════════════════════════════════
# TAB: BREAKOUTS  (confirmed breakouts + 4H early + AI channel)
# ══════════════════════════════════════════════════════════════════════════════
with tab_bo:
    # ── Confirmed multi-TF breakouts ────────────────────────────────────────
    st.markdown('<div class="section-title">Confirmed Breakouts · Daily / Weekly / Monthly</div>', unsafe_allow_html=True)

    breakouts_df = _bos_df
    if breakouts_df.empty:
        st.info("No breakouts yet. Auto-scan runs 4:30 PM IST on trading days.")
    else:
        bos_list = breakouts_df.to_dict("records")
        for b in bos_list: b["_grade"] = _grade(b)
        tf_order = {"Monthly":0,"Weekly":1,"Daily":2}
        bos_list.sort(key=lambda b: (tf_order.get(b.get("timeframe","Daily"),3), -float(b.get("vol_ratio",1))))
        _by_tf = {"Monthly":[],"Weekly":[],"Daily":[]}
        for b in bos_list:
            tf = b.get("timeframe","Daily")
            if tf in _by_tf: _by_tf[tf].append(b)
        bos_list = _by_tf["Monthly"][:5] + _by_tf["Weekly"][:5] + _by_tf["Daily"][:10]

        bc1,bc2,bc3,bc4 = st.columns(4)
        bc1.metric("Total",   len(bos_list))
        bc2.metric("Monthly", sum(1 for b in bos_list if b.get("timeframe")=="Monthly"))
        bc3.metric("Weekly",  sum(1 for b in bos_list if b.get("timeframe")=="Weekly"))
        bc4.metric("Daily",   sum(1 for b in bos_list if b.get("timeframe")=="Daily"))
        st.markdown("<div style='height:12px'></div>", unsafe_allow_html=True)

        # Filters
        bf1,bf2,bf3 = st.columns(3)
        _btf  = bf1.selectbox("Timeframe", ["All","Monthly","Weekly","Daily"], key="bf_tf")
        _bgr  = bf2.selectbox("Grade",     ["All","S","A","B"],                key="bf_gr")
        _bfno = bf3.selectbox("F&O",       ["All","F&O Only"],                 key="bf_fno")

        bos_f = [b for b in bos_list if
                 (_btf=="All" or b.get("timeframe")==_btf) and
                 (_bgr=="All" or b.get("_grade")==_bgr) and
                 (_bfno=="All" or b.get("fno"))]

        TF_COL = {"Monthly":"#a78bfa","Weekly":"#f59e0b","Daily":"#00d09c"}
        for b in bos_f:
            tf     = b.get("timeframe","Daily")
            tfc    = TF_COL.get(tf,"#00d09c")
            grade  = b.get("_grade","B")
            fno_b  = '<span class="badge fno" style="margin-left:6px">F&O</span>' if b.get("fno") else ""
            raw_p  = b.get("patterns",[])
            if isinstance(raw_p, str):
                try: raw_p = json.loads(raw_p)
                except: raw_p = []
            pats   = " · ".join(raw_p) if raw_p else b.get("pattern","")
            tv     = b.get("tv_link") or f"https://in.tradingview.com/chart/?symbol=NSE:{b['symbol']}"
            p      = float(b.get("price",0))
            sl     = float(b.get("sl",0))
            t1     = float(b.get("target1",0))
            t2     = float(b.get("target2",0))
            t3     = float(b.get("target3",t2))
            st.markdown(f"""
<div class="card" style="border-left-color:{tfc}">
  <div style="display:flex;justify-content:space-between;align-items:flex-start">
    <div>
      <span class="sym">{b['symbol']}</span>
      <span style="font-size:10px;font-weight:700;padding:2px 8px;border-radius:4px;
        background:rgba(0,0,0,.3);color:{tfc};border:1px solid {tfc}44;margin-left:8px">{tf.upper()}</span>
      <span style="font-size:10px;font-weight:800;padding:2px 8px;border-radius:4px;
        background:rgba(0,0,0,.3);color:#9ca3af;border:1px solid #1e2025;margin-left:4px">GRADE {grade}</span>
      {fno_b}
      <div style="font-size:11px;color:#6b7280;margin-top:4px">{pats}</div>
    </div>
    <div style="text-align:right">
      <div class="price-big" style="color:#e8eaed">₹{p:,.1f}</div>
      <div style="font-size:11px;color:#6b7280;margin-top:2px">Vol {b.get('vol_ratio',0)}x</div>
    </div>
  </div>
  <div class="kv-grid">
    {_kv("Entry", f"₹{p:,.1f}")}
    {_kv("Stop",  f"₹{sl:,.1f}", "red")}
    {_kv("T1",    f"₹{t1:,.1f}", "green")}
    {_kv("T2",    f"₹{t2:,.1f}", "green")}
    {_kv("T3",    f"₹{t3:,.1f}", "green")}
    {_kv("RR",    f"1:{b.get('rr',0)}", "blue")}
  </div>
  <div style="margin-top:12px">{_tv_btn(tv)}</div>
</div>""", unsafe_allow_html=True)

    # ── 4H Early-Entry Signals ───────────────────────────────────────────────
    st.markdown('<div class="section-title">⚡ 4H Early-Entry · RSI 55 + Volume Surge</div>', unsafe_allow_html=True)
    df_4h = get_4h_signals(days=_days_n) if IS_LOCAL else _gh_4h_signals(days=_days_n)
    if df_4h.empty:
        st.caption("No 4H signals today. Next scan: 9:20 AM IST.")
    else:
        for b in df_4h.to_dict("records"):
            p   = float(b.get("price",0))
            sl  = float(b.get("sl",0))
            t1  = float(b.get("target1",0))
            t2  = float(b.get("target2",0))
            tv4 = b.get("tv_link") or f"https://in.tradingview.com/chart/?symbol=NSE:{b['symbol']}"
            fno_b = '<span class="badge fno" style="margin-left:6px">F&O</span>' if b.get("fno") else ""
            st.markdown(f"""
<div class="card neutral">
  <div style="display:flex;justify-content:space-between;align-items:flex-start">
    <div>
      <span class="sym">{b['symbol']}</span>
      <span class="badge intra" style="margin-left:8px">4H · EARLY</span>
      {fno_b}
      <div style="font-size:11px;color:#6b7280;margin-top:4px">{b.get('reason','RSI 55 cross + Volume surge')}</div>
    </div>
    <div class="price-big" style="color:#e8eaed">₹{p:,.2f}</div>
  </div>
  <div class="kv-grid">
    {_kv("Entry", f"₹{p:,.2f}")}
    {_kv("Stop",  f"₹{sl:,.2f}", "red")}
    {_kv("T1",    f"₹{t1:,.2f}", "green")}
    {_kv("T2",    f"₹{t2:,.2f}", "green")}
    {_kv("RR",    f"1:{b.get('rr',0)}", "blue")}
    {_kv("Vol",   f"{b.get('vol_ratio',0)}x", "amber")}
  </div>
  <div style="margin-top:12px">{_tv_btn(tv4)}</div>
</div>""", unsafe_allow_html=True)

    # ── AI Channel Breakouts ─────────────────────────────────────────────────
    st.markdown('<div class="section-title">🤖 AI Channel Breakouts · OLS Regression</div>', unsafe_allow_html=True)
    ai_sigs = _get_ai_signals(days=_days_n)
    if not ai_sigs:
        st.caption("No AI channel breakouts today.")
    else:
        # Filter
        af1,af2 = st.columns(2)
        _atf  = af1.selectbox("Timeframe", ["All","4H","Daily","Weekly"], key="ai_tf")
        _afno = af2.selectbox("F&O",       ["All","F&O Only"],            key="ai_fno")
        ai_f  = [s for s in ai_sigs if (_atf=="All" or s.get("timeframe")==_atf) and (_afno=="All" or s.get("fno"))]

        for b in ai_f:
            sym    = b.get("symbol","")
            p      = float(b.get("price",0))
            sl     = float(b.get("sl",0))
            t1     = float(b.get("target1",0))
            t2     = float(b.get("target2",0))
            t3     = float(b.get("target3",t2))
            rr     = b.get("rr",0)
            vol_r  = float(b.get("vol_ratio",1))
            tf     = b.get("timeframe","4H")
            fno_b  = '<span class="badge fno" style="margin-left:6px">F&O</span>' if b.get("fno") else ""
            tv     = b.get("tv_link") or f"https://in.tradingview.com/chart/?symbol=NSE:{sym}"
            st.markdown(f"""
<div class="card" style="border-left-color:#a78bfa">
  <div style="display:flex;justify-content:space-between;align-items:flex-start">
    <div>
      <span class="sym">{sym}</span>
      <span style="font-size:10px;font-weight:700;padding:2px 8px;border-radius:4px;
        background:rgba(167,139,250,.1);color:#a78bfa;border:1px solid rgba(167,139,250,.3);margin-left:8px">AI · {tf}</span>
      {fno_b}
      <div style="font-size:11px;color:#6b7280;margin-top:4px">{b.get('pattern','TL Channel Breakout')} · Vol {vol_r:.1f}×</div>
    </div>
    <div class="price-big" style="color:#e8eaed">₹{p:,.2f}</div>
  </div>
  <div class="kv-grid">
    {_kv("Entry", f"₹{p:,.2f}")}
    {_kv("Stop",  f"₹{sl:,.2f}", "red")}
    {_kv("T1",    f"₹{t1:,.2f}", "green")}
    {_kv("T2",    f"₹{t2:,.2f}", "green")}
    {_kv("T3",    f"₹{t3:,.1f}", "green")}
    {_kv("RR",    f"1:{rr}", "blue")}
  </div>
  <div style="margin-top:12px">{_tv_btn(tv)}</div>
</div>""", unsafe_allow_html=True)

    # ── Commodity Signals ────────────────────────────────────────────────────
    st.markdown('<div class="section-title">🥇 Commodity Signals · Gold / Silver / Crude / Nat Gas</div>', unsafe_allow_html=True)
    df_comm = get_commodity_signals(days=_days_n) if IS_LOCAL else _gh_commodity_signals(days=_days_n)
    if df_comm.empty:
        # Live fallback
        _comm_ov = [("Gold","GC=F","🥇"),("Silver","SI=F","🥈"),("Crude","CL=F","🛢️"),("Nat Gas","NG=F","🔥")]
        try:
            _tks = " ".join(t for _,t,_ in _comm_ov)
            _cd  = yf.download(_tks, period="2d", interval="1d", group_by="ticker", progress=False, auto_adjust=True, timeout=10)
            lines_html = ""
            for name, ticker, icon in _comm_ov:
                try:
                    _df  = _cd[ticker] if len(_comm_ov)>1 else _cd
                    _p   = float(_df["Close"].iloc[-1])
                    _p0  = float(_df["Close"].iloc[-2])
                    _chg = round((_p-_p0)/_p0*100,2)
                    _col = "#00d09c" if _chg>=0 else "#eb5757"
                    lines_html += f'<div style="display:flex;justify-content:space-between;padding:10px 14px;border-bottom:1px solid #1e2025"><span style="color:#e8eaed;font-weight:600">{icon} {name}</span><span style="color:#e8eaed;font-weight:700">${_p:,.2f}</span><span style="color:{_col};font-weight:700">{_chg:+.2f}%</span></div>'
                except Exception: pass
            if lines_html:
                st.markdown(f'<div style="background:#161618;border:1px solid #1e2025;border-radius:10px;overflow:hidden">{lines_html}</div>', unsafe_allow_html=True)
        except Exception: pass
        st.caption("No active commodity signals. Next scan: 9:20 AM IST.")
    else:
        for b in df_comm.to_dict("records"):
            action = b.get("action","BUY")
            ac     = "#00d09c" if action=="BUY" else "#eb5757"
            p      = float(b.get("price",0))
            sl     = float(b.get("sl",0))
            t1     = float(b.get("target1",0))
            t2     = float(b.get("target2",0))
            st.markdown(f"""
<div class="card {'sell' if action=='SELL' else ''}">
  <div style="display:flex;justify-content:space-between;align-items:center">
    <div>
      <span class="sym">{b['symbol']}</span>
      <span style="font-size:11px;color:#6b7280;margin-left:8px">{b.get('label','')} · {b.get('timeframe','Daily')}</span>
    </div>
    <span style="font-size:14px;font-weight:800;color:{ac}">{'▲ BUY' if action=='BUY' else '▼ SELL'}</span>
  </div>
  <div class="kv-grid">
    {_kv("Price", f"${p:,.2f}")}
    {_kv("Stop",  f"${sl:,.2f}", "red")}
    {_kv("T1",    f"${t1:,.2f}", "green")}
    {_kv("T2",    f"${t2:,.2f}", "green")}
    {_kv("RR",    f"1:{b.get('rr',0)}", "blue")}
    {_kv("RSI",   str(b.get('rsi',0)))}
  </div>
</div>""", unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════════════════════
# TAB: INTRADAY
# ══════════════════════════════════════════════════════════════════════════════
with tab_intra:
    st.markdown('<div class="section-title">⚡ Intraday Momentum · 15m · VWAP + RSI55 + Vol 2.5x</div>', unsafe_allow_html=True)
    _intra = _gh_intraday_signals(days=1)
    if not _intra:
        st.markdown("""
<div style="text-align:center;padding:60px 0">
  <div style="font-size:36px;margin-bottom:12px">⚡</div>
  <div style="font-size:14px;font-weight:600;color:#9ca3af">No intraday signals today</div>
  <div style="font-size:12px;color:#6b7280;margin-top:6px">Scans every 30 min · 10:00 AM – 2:30 PM IST</div>
</div>""", unsafe_allow_html=True)
    else:
        st.caption(f"{len(_intra)} signal(s) today · Exit by 3:15 PM IST")
        for s in _intra:
            ia  = s.get("action","BUY")
            ie  = float(s.get("entry",0) or s.get("price",0))
            isl = float(s.get("sl",0) or ie*0.98)
            it1 = float(s.get("target1",0) or ie*1.02)
            it2 = float(s.get("target2",0) or it1*1.01)
            irr = s.get("rr",0) or s.get("rr1",0)
            try:
                ivol = s.get("vol_ratio",0) or json.loads(s.get("metadata") or "{}").get("vol_ratio",0)
            except Exception:
                ivol = 0
            st.markdown(f"""
<div class="card" style="border-left-color:#fbbf24">
  <div style="display:flex;justify-content:space-between;align-items:flex-start">
    <div>
      <span class="sym">{s['symbol']}</span>
      <span class="badge intra" style="margin-left:8px">15m</span>
      <span class="badge {'buy' if ia=='BUY' else 'sell'}" style="margin-left:4px">{ia}</span>
    </div>
    <div class="price-big" style="color:#e8eaed">₹{ie:,.2f}</div>
  </div>
  <div class="kv-grid">
    {_kv("Entry", f"₹{ie:,.2f}")}
    {_kv("Stop",  f"₹{isl:,.2f}", "red")}
    {_kv("T1",    f"₹{it1:,.2f}", "green")}
    {_kv("T2",    f"₹{it2:,.2f}", "green")}
    {_kv("RR",    f"1:{irr}", "blue")}
    {_kv("Vol",   f"{ivol}x", "amber")}
  </div>
  <div style="margin-top:10px;font-size:11px;color:#6b7280">⚠ Exit before 3:15 PM IST · Intraday only</div>
</div>""", unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════════════════════
# TAB: F&O
# ══════════════════════════════════════════════════════════════════════════════
with tab_fno:
    st.markdown('<div class="section-title">📊 F&O Watchlist · Breakout + 4H + Swing signals on F&O stocks</div>', unsafe_allow_html=True)

    # Merge F&O signals from all sources
    _fno_bo  = [b for b in (_bos_df.to_dict("records") if not _bos_df.empty else []) if b.get("fno")]
    _df_4h_f = get_4h_signals(days=_days_n) if IS_LOCAL else _gh_4h_signals(days=_days_n)
    _fno_4h  = [b for b in (_df_4h_f.to_dict("records") if not _df_4h_f.empty else []) if b.get("fno")]
    _fno_sw  = [s for s in _signals_hdr if s.get("fno_eligible")]

    all_fno = _fno_bo + _fno_4h + _fno_sw
    _seen_f = set(); fno_dedup = []
    for b in all_fno:
        sym = b.get("symbol","")
        if sym not in _seen_f:
            _seen_f.add(sym); fno_dedup.append(b)

    if not fno_dedup:
        st.info("No F&O-eligible signals today. Auto-scan: 9:20 AM · 11:45 AM · 4:30 PM IST.")
    else:
        # Filters
        ff1,ff2 = st.columns(2)
        _fsrc = ff1.selectbox("Source", ["All","Breakout","4H","Swing"], key="fno_src")
        _ftf  = ff2.selectbox("Timeframe", ["All","Monthly","Weekly","Daily","4H"], key="fno_tf")

        fa,fb,fc_col = st.columns(3)
        fa.metric("F&O Signals", len(fno_dedup))
        fb.metric("From Breakouts", len(_fno_bo))
        fc_col.metric("From 4H", len(_fno_4h))
        st.markdown("<div style='height:12px'></div>", unsafe_allow_html=True)

        SRC_MAP = {id(b):"Breakout" for b in _fno_bo}
        SRC_MAP.update({id(b):"4H" for b in _fno_4h})
        SRC_MAP.update({id(b):"Swing" for b in _fno_sw})

        for b in fno_dedup:
            sym   = b.get("symbol","")
            p     = float(b.get("price", b.get("entry",0)) or 0)
            sl    = float(b.get("sl", b.get("sl2",p*0.95)) or p*0.95)
            t1    = float(b.get("target1",0) or p*1.05)
            t2    = float(b.get("target2",0) or t1*1.03)
            rr    = b.get("rr", b.get("rr1","—"))
            vol   = b.get("vol_ratio","—")
            tf    = b.get("timeframe","Daily")
            src   = "Breakout" if b in _fno_bo else ("4H" if b in _fno_4h else "Swing")

            if _fsrc != "All" and src != _fsrc: continue
            if _ftf  != "All" and tf  != _ftf:  continue

            src_col = {"Breakout":"#00d09c","4H":"#f59e0b","Swing":"#60a5fa"}.get(src,"#9ca3af")
            nse_link = f"https://www.nseindia.com/get-quotes/derivatives?symbol={sym}"
            tv_link  = b.get("tv_link") or f"https://in.tradingview.com/chart/?symbol=NSE:{sym}"
            pct_up   = round((t2-p)/p*100,1) if p>0 else 0

            st.markdown(f"""
<div class="card">
  <div style="display:flex;justify-content:space-between;align-items:flex-start">
    <div>
      <span class="sym">{sym}</span>
      <span style="font-size:10px;font-weight:700;padding:2px 8px;border-radius:4px;color:{src_col};
        background:rgba(0,0,0,.3);border:1px solid {src_col}44;margin-left:8px">{src}</span>
      <span class="badge fno" style="margin-left:4px">F&O</span>
      <div style="font-size:11px;color:#6b7280;margin-top:4px">{tf} · Vol {vol}× surge</div>
    </div>
    <div style="text-align:right">
      <div class="price-big" style="color:#e8eaed">₹{p:,.1f}</div>
      <div style="font-size:12px;font-weight:700;color:#00d09c;margin-top:2px">T2 +{pct_up}%</div>
    </div>
  </div>
  <div class="kv-grid">
    {_kv("Entry", f"₹{p:,.1f}")}
    {_kv("Stop",  f"₹{sl:,.1f}", "red")}
    {_kv("T1",    f"₹{t1:,.1f}", "green")}
    {_kv("T2",    f"₹{t2:,.1f}", "green")}
    {_kv("RR",    f"1:{rr}", "blue")}
  </div>
  <div style="margin-top:12px;display:flex;gap:10px">
    {_tv_btn(tv_link)}
    <a href="{nse_link}" target="_blank" style="font-size:11px;font-weight:600;color:#9ca3af;text-decoration:none;padding:4px 10px;border:1px solid #1e2025;border-radius:6px">NSE Chain ↗</a>
  </div>
</div>""", unsafe_allow_html=True)

    # Global markets
    st.markdown('<div class="section-title">🌐 Global Markets</div>', unsafe_allow_html=True)
    fc_data = _forex()
    if fc_data:
        gcols = st.columns(len(fc_data))
        for i, r in enumerate(fc_data):
            c = "#00d09c" if r["Chg%"] >= 0 else "#eb5757"
            s = "+" if r["Chg%"] >= 0 else ""
            gcols[i].markdown(f"""
<div style="background:#161618;border:1px solid #1e2025;border-radius:8px;padding:10px 12px;text-align:center">
  <div style="font-size:10px;color:#6b7280;font-weight:600;margin-bottom:4px">{r['Asset']}</div>
  <div style="font-size:14px;font-weight:700;color:#e8eaed">{r['Last']}</div>
  <div style="font-size:11px;font-weight:700;color:{c}">{s}{r['Chg%']}%</div>
</div>""", unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════════════════════
# TAB: MUTUAL FUNDS
# ══════════════════════════════════════════════════════════════════════════════
with tab_mf:
    st.markdown('<div class="section-title">💰 Mutual Funds · Portfolio + Top Funds by Category</div>', unsafe_allow_html=True)

    # ── Top funds ─────────────────────────────────────────────────────────────
    with st.expander("📊 Top Funds by Category", expanded=True):
        with st.spinner("Loading…"):
            top_data = _top_funds()
        if top_data:
            cat_tabs = st.tabs(list(top_data.keys()))
            for ct, (cat, funds) in zip(cat_tabs, top_data.items()):
                with ct:
                    if not funds:
                        st.info("No data."); continue
                    st.markdown("""
<div style="display:grid;grid-template-columns:1fr 80px 80px 80px 80px;padding:8px 14px;
  border-bottom:1px solid #1e2025;font-size:10px;font-weight:700;color:#6b7280;text-transform:uppercase;
  letter-spacing:.06em">
  <div>Fund</div><div style="text-align:right">NAV</div>
  <div style="text-align:right">1Y</div><div style="text-align:right">3Y</div><div style="text-align:right">5Y</div>
</div>""", unsafe_allow_html=True)
                    rows_html = ""
                    for f in funds:
                        def _rc(v):
                            if v is None: return "#6b7280","—"
                            return ("#00d09c" if v>=0 else "#eb5757"), f"{v:+.2f}%"
                        c1,v1 = _rc(f.get("1Y")); c3,v3 = _rc(f.get("3Y")); c5,v5 = _rc(f.get("5Y"))
                        rows_html += f"""
<div style="display:grid;grid-template-columns:1fr 80px 80px 80px 80px;padding:10px 14px;
  border-bottom:1px solid #1e2025">
  <div>
    <div style="font-size:13px;font-weight:600;color:#e8eaed">{f['short']}</div>
    <div style="font-size:10px;color:#6b7280;margin-top:1px">{f.get('fund_house','')}</div>
  </div>
  <div style="text-align:right;font-size:13px;font-weight:700;color:#e8eaed;align-self:center">₹{f['nav']:.2f}</div>
  <div style="text-align:right;font-size:13px;font-weight:700;color:{c1};align-self:center">{v1}</div>
  <div style="text-align:right;font-size:13px;font-weight:700;color:{c3};align-self:center">{v3}</div>
  <div style="text-align:right;font-size:13px;font-weight:700;color:{c5};align-self:center">{v5}</div>
</div>"""
                    st.markdown(f'<div style="background:#161618;border:1px solid #1e2025;border-radius:10px;overflow:hidden;margin-bottom:14px">{rows_html}</div>', unsafe_allow_html=True)

                    _fn  = [f["short"] for f in funds]
                    _si  = st.selectbox("Holdings breakdown", range(len(_fn)), format_func=lambda i: _fn[i], key=f"sel_{cat}")
                    _sf  = funds[_si]
                    hd   = get_fund_holdings(_sf['scheme_code'])
                    if hd:
                        _pc = ["#00d09c","#60a5fa","#a78bfa","#f59e0b","#f87171","#34d399","#fb923c","#e879f9","#94a3b8","#64748b"]
                        sec = hd["sectors"]; sec_k = list(sec.keys())[:9]; sec_v = [sec[k] for k in sec_k]
                        hdc1,hdc2 = st.columns([3,2])
                        with hdc1:
                            fig_s = go.Figure()
                            for i,(k,v) in enumerate(zip(sec_k,sec_v)):
                                fig_s.add_trace(go.Bar(x=[v],y=[""],orientation="h",name=k,marker_color=_pc[i%len(_pc)],
                                    hovertemplate=f"{k}: {v:.1f}%<extra></extra>",
                                    text=f"{k[:10]} {v:.1f}%" if v>5 else "",textposition="inside",textfont=dict(size=9,color="#000")))
                            fig_s.update_layout(barmode="stack",height=50,paper_bgcolor="rgba(0,0,0,0)",plot_bgcolor="rgba(0,0,0,0)",
                                margin=dict(l=0,r=0,t=0,b=0),showlegend=False,
                                xaxis=dict(showticklabels=False,showgrid=False,zeroline=False,range=[0,100]),
                                yaxis=dict(showticklabels=False,showgrid=False,zeroline=False))
                            st.caption("Sector Allocation")
                            st.plotly_chart(fig_s,use_container_width=True,key=f"sec_{cat}_{_si}")
                        with hdc2:
                            scripts=hd["top_scripts"]; h_l=[s[0] for s in scripts[:8]]; h_v=[s[1] for s in scripts[:8]]
                            others=max(0,100-sum(h_v))
                            if others>1: h_l.append("Others"); h_v.append(round(others,1))
                            fig_h=go.Figure(); fig_h.add_trace(go.Bar(x=h_v,y=h_l,orientation="h",
                                marker=dict(color="#00d09c",opacity=0.75),
                                text=[f"{v:.1f}%" for v in h_v],textposition="outside",
                                textfont=dict(size=9,color="#6b7280"),
                                hovertemplate="%{y}: %{x:.1f}%<extra></extra>"))
                            fig_h.update_layout(height=max(180,len(h_l)*24),paper_bgcolor="rgba(0,0,0,0)",plot_bgcolor="rgba(0,0,0,0)",
                                margin=dict(l=4,r=50,t=4,b=4),showlegend=False,
                                xaxis=dict(showgrid=False,showticklabels=False,zeroline=False),
                                yaxis=dict(showgrid=False,zeroline=False,tickfont=dict(size=10,color="#6b7280")))
                            st.caption("Top Holdings")
                            st.plotly_chart(fig_h,use_container_width=True,key=f"hld_{cat}_{_si}")

    # ── Portfolio ─────────────────────────────────────────────────────────────
    st.markdown('<div class="section-title">My Portfolio</div>', unsafe_allow_html=True)
    portfolio = load_portfolio()

    with st.expander("+ Add Fund", expanded=len(portfolio)==0):
        sq = st.text_input("Search fund name", placeholder="e.g. Parag Parikh Flexi Cap")
        if sq and len(sq)>=3:
            results = search_funds(sq)
            if results:
                opts = {f"{r['schemeName']} ({r['schemeCode']})": r for r in results[:20]}
                chosen = st.selectbox("Select fund", list(opts.keys()))
                if chosen:
                    sel = opts[chosen]
                    ac1,ac2,ac3 = st.columns(3)
                    units_in = ac1.number_input("Units",0.0,step=0.001,format="%.3f")
                    nav_in   = ac2.number_input("Purchase NAV",0.0,step=0.01)
                    if ac3.button("Add"):
                        portfolio = [p for p in portfolio if p["scheme_code"]!=sel["schemeCode"]]
                        portfolio.append({"scheme_code":sel["schemeCode"],"name":sel["schemeName"],"units":units_in,"purchase_nav":nav_in})
                        save_portfolio(portfolio); st.success(f"Added {sel['schemeName']}"); st.rerun()
            else:
                st.warning("No funds found.")

    if portfolio:
        rm_names = {p["name"]: p for p in portfolio}
        rc1,rc2 = st.columns([3,1])
        to_rm = rc1.selectbox("Remove fund", ["—"]+list(rm_names.keys()))
        with rc2:
            st.markdown("<div style='margin-top:28px'></div>",unsafe_allow_html=True)
            if st.button("Remove") and to_rm!="—":
                portfolio = [p for p in portfolio if p["name"]!=to_rm]
                save_portfolio(portfolio); st.rerun()

        with st.spinner("Loading portfolio…"):
            summary = _mf_summary(json.dumps(portfolio))

        if summary:
            total_inv = sum(s["invested"] for s in summary)
            total_cur = sum(s["current"]  for s in summary)
            total_pnl = total_cur - total_inv
            total_pct = (total_pnl/total_inv*100) if total_inv>0 else 0
            pnl_col   = "#00d09c" if total_pnl>=0 else "#eb5757"

            pm1,pm2,pm3,pm4 = st.columns(4)
            pm1.metric("Current Value",  f"₹{total_cur:,.0f}")
            pm2.metric("Invested",       f"₹{total_inv:,.0f}")
            pm3.metric("Total P&L",      f"₹{total_pnl:+,.0f}")
            pm4.metric("Returns",        f"{total_pct:+.2f}%")
            st.markdown("<div style='height:12px'></div>",unsafe_allow_html=True)

            # SIP Calculator
            with st.expander("🔢 SIP Calculator"):
                sc1,sc2,sc3 = st.columns(3)
                m_sip  = sc1.number_input("Monthly SIP (₹)",500,500000,5000,500)
                s_yrs  = sc2.slider("Years",1,30,10)
                s_rate = sc3.slider("Return %/yr",5.0,25.0,12.0,0.5)
                n=s_yrs*12; r=s_rate/100/12
                corpus   = m_sip*(((1+r)**n-1)/r)*(1+r)
                invested = m_sip*n; gained = corpus-invested
                sm1,sm2,sm3 = st.columns(3)
                sm1.metric("Invested",  f"₹{invested:,.0f}")
                sm2.metric("Returns",   f"₹{gained:,.0f}")
                sm3.metric("Corpus",    f"₹{corpus:,.0f}")

            # Fund cards
            import math as _math

            _RISK_COL = {"Low":"#00d09c","Moderate":"#60a5fa","Moderately High":"#f59e0b","High":"#fb7340","Very High":"#eb5757"}
            _PERIOD_DAYS = {"1M":30,"3M":91,"6M":182,"1Y":365,"3Y":1095,"5Y":1825}

            for s in summary:
                pnl_col2 = "#00d09c" if s["pnl_pct"]>=0 else "#eb5757"
                day_col  = "#00d09c" if s["day_chg"]>=0 else "#eb5757"
                ret      = s["returns"]
                risk     = s.get("risk","Very High")
                rc       = _RISK_COL.get(risk,"#eb5757")
                fhouse   = s.get("fund_house","") or ""
                cat      = s.get("category","") or ""

                ret_cells=""
                for p in ["1M","3M","6M","1Y","3Y","5Y"]:
                    v=ret.get(p)
                    if v is None:
                        ret_cells+=f'<div class="gw-ret-cell"><div class="gw-ret-period">{p}</div><div class="gw-ret-val" style="color:#6b7280">—</div></div>'
                    else:
                        vc="#00d09c" if v>=0 else "#eb5757"
                        ret_cells+=f'<div class="gw-ret-cell"><div class="gw-ret-period">{p}</div><div class="gw-ret-val" style="color:{vc}">{v:+.1f}%</div></div>'

                st.markdown(f"""
<div class="gw-fund-card">
  <div class="gw-card-head">
    <div style="flex:1;min-width:0">
      <div class="gw-fund-name">{s['name']}</div>
      <div class="gw-fund-amc">{fhouse}</div>
      <div class="gw-chips">
        {f'<span class="gw-chip cat">{cat}</span>' if cat else ''}
        <span class="gw-chip" style="background:rgba(0,0,0,.3);color:{rc};border:1px solid {rc}44">{risk} Risk</span>
      </div>
    </div>
    <div class="gw-nav-block">
      <div class="gw-nav-price">₹{s['nav']:.4f}</div>
      <div class="gw-nav-chg" style="color:{day_col}">{'+' if s['day_chg']>=0 else ''}{s['day_chg']:.2f}% today</div>
    </div>
  </div>
  <div class="gw-returns-bar">{ret_cells}</div>
  <div class="gw-inv-band">
    <div class="gw-inv-cell"><div class="gw-inv-label">Invested</div><div class="gw-inv-val">₹{s['invested']:,.0f}</div></div>
    <div class="gw-inv-cell"><div class="gw-inv-label">Current</div><div class="gw-inv-val">₹{s['current']:,.0f}</div></div>
    <div class="gw-inv-cell"><div class="gw-inv-label">P&L</div><div class="gw-inv-val" style="color:{pnl_col2}">₹{s['pnl']:+,.0f}</div></div>
    <div class="gw-inv-cell"><div class="gw-inv-label">Returns</div><div class="gw-inv-val" style="color:{pnl_col2}">{s['pnl_pct']:+.2f}%</div></div>
  </div>
</div>""", unsafe_allow_html=True)

                nav_df = s.get("nav_df")
                if nav_df is not None and not nav_df.empty:
                    _psel = st.radio("Period", ["1M","3M","6M","1Y","3Y","5Y"], index=3, horizontal=True,
                                     key=f"prd_{s['scheme_code']}", label_visibility="collapsed")
                    _d2   = _PERIOD_DAYS.get(_psel,365)
                    cutoff= nav_df["date"].max() - pd.Timedelta(days=_d2)
                    nav_p = nav_df[nav_df["date"]>=cutoff].copy()
                    if not nav_p.empty:
                        _nc = "#00d09c" if nav_p["nav"].iloc[-1]>=nav_p["nav"].iloc[0] else "#eb5757"
                        nav_chg=(nav_p["nav"].iloc[-1]-nav_p["nav"].iloc[0])/nav_p["nav"].iloc[0]*100
                        fig_nav=go.Figure()
                        fig_nav.add_trace(go.Scatter(x=nav_p["date"],y=nav_p["nav"],mode="lines",
                            line=dict(color=_nc,width=2),fill="tozeroy",
                            fillcolor=f"{'rgba(0,208,156,0.06)' if _nc=='#00d09c' else 'rgba(235,87,87,0.06)'}",
                            hovertemplate="₹%{y:.4f}<br>%{x|%d %b %Y}<extra></extra>"))
                        fig_nav.update_layout(height=160,margin=dict(l=0,r=0,t=4,b=0),
                            paper_bgcolor="rgba(0,0,0,0)",plot_bgcolor="rgba(0,0,0,0)",
                            xaxis=dict(showgrid=False,showticklabels=True,zeroline=False,
                                      tickfont=dict(size=9,color="#6b7280"),tickformat="%b '%y"),
                            yaxis=dict(showgrid=True,gridcolor="#e8eaed",showticklabels=True,
                                      zeroline=False,tickfont=dict(size=9,color="#6b7280"),tickformat=",.2f"),
                            showlegend=False)
                        st.markdown(f'<div style="font-size:11px;color:#6b7280;margin-bottom:4px">NAV ({_psel}): <span style="color:{_nc};font-weight:700">{nav_chg:+.2f}%</span></div>',unsafe_allow_html=True)
                        st.plotly_chart(fig_nav,use_container_width=True,key=f"nav_{s['scheme_code']}_{_psel}")

                # Fund details
                exp=s.get("exp_ratio"); min_sip=s.get("min_sip"); manager=s.get("manager","—"); bench=s.get("benchmark","—") or "—"
                st.markdown(f"""
<div class="gw-fund-card" style="margin-top:-12px;border-top:none;border-radius:0 0 12px 12px">
  <div class="gw-fund-details">
    <div class="gw-detail-cell"><div class="gw-detail-label">Expense Ratio</div><div class="gw-detail-val">{f'{exp:.2f}%' if exp else '—'}</div></div>
    <div class="gw-detail-cell"><div class="gw-detail-label">Min SIP</div><div class="gw-detail-val">{f'₹{min_sip:,}' if min_sip else '—'}</div></div>
    <div class="gw-detail-cell"><div class="gw-detail-label">Manager</div><div class="gw-detail-val" style="font-size:12px">{manager}</div></div>
    <div class="gw-detail-cell"><div class="gw-detail-label">Benchmark</div><div class="gw-detail-val" style="font-size:11px">{bench[:22]}</div></div>
    <div class="gw-detail-cell"><div class="gw-detail-label">Units · Buy NAV</div><div class="gw-detail-val" style="font-size:12px">{s['units']:.3f} · ₹{s['purchase_nav']:.2f}</div></div>
  </div>
</div>""", unsafe_allow_html=True)

            # Smart alerts
            _alerts_all = []
            for s in summary:
                if abs(s["day_chg"])>2: _alerts_all.append(f"<b>{s['name'][:40]}</b> — NAV moved {s['day_chg']:+.2f}% today")
                if s["returns"].get("1Y",0)<0: _alerts_all.append(f"<b>{s['name'][:40]}</b> — 1Y return negative ({s['returns'].get('1Y',0):.1f}%)")
                if s["pnl_pct"]<-10: _alerts_all.append(f"<b>{s['name'][:40]}</b> — Portfolio loss {s['pnl_pct']:.1f}%")
            if _alerts_all:
                st.markdown('<div class="section-title" style="color:#f59e0b">⚠ Smart Alerts</div>', unsafe_allow_html=True)
                for a in _alerts_all:
                    st.markdown(f'<div style="background:rgba(245,158,11,.06);border:1px solid rgba(245,158,11,.2);border-radius:8px;padding:10px 14px;margin-bottom:6px;font-size:12px;color:#f59e0b">{a}</div>',unsafe_allow_html=True)
    else:
        st.info("No funds tracked yet. Use the search above to add mutual funds.")


# ══════════════════════════════════════════════════════════════════════════════
# TAB: WATCHLIST  (Multibaggers + OHL/OLL)
# ══════════════════════════════════════════════════════════════════════════════
with tab_watch:
    wt1, wt2 = st.tabs(["💎 Multibaggers", "🕯️ OHL / OLL"])

    with wt1:
        st.markdown('<div class="section-title">💎 Potential Multibaggers · Weekly scan · Sat 9:30 AM IST</div>', unsafe_allow_html=True)
        mb_df = (get_multibaggers(days=7) if IS_LOCAL else _gh_multibaggers(days=7)) if True else pd.DataFrame()
        try:
            if IS_LOCAL:
                from tracker import get_multibaggers
                mb_df = get_multibaggers(days=7)
            else:
                mb_df = _gh_multibaggers(days=7)
        except Exception:
            mb_df = pd.DataFrame()

        if mb_df.empty:
            st.info("No multibagger data yet. Next scan: Saturday 9:30 AM IST.")
        else:
            mbs = mb_df.to_dict("records")
            # Filters
            mf1,mf2,mf3 = st.columns(3)
            _mfno = mf1.selectbox("F&O", ["All","F&O Only"], key="mb_fno")
            _msc  = mf2.slider("Min Score", 0, 100, 50, 5, key="mb_sc")
            _msrt = mf3.selectbox("Sort by", ["Score","RR","Vol"], key="mb_srt")

            mk1,mk2,mk3,mk4 = st.columns(4)
            mk1.metric("Candidates", len(mbs))
            mk2.metric("F&O Eligible", sum(1 for m in mbs if m.get("fno")))
            mk3.metric("Avg RR", round(sum(m.get("rr",0) for m in mbs)/len(mbs),1) if mbs else 0)
            mk4.metric("Top Score", round(max(m.get("score",0) for m in mbs),1) if mbs else 0)
            st.markdown("<div style='height:12px'></div>",unsafe_allow_html=True)

            mbs_f = [m for m in mbs if (_mfno=="All" or m.get("fno")) and m.get("score",0)>=_msc]
            _sk   = {"Score":"score","RR":"rr","Vol":"vol_ratio"}.get(_msrt,"score")
            mbs_f.sort(key=lambda m: m.get(_sk,0), reverse=True)

            for i,m in enumerate(mbs_f,1):
                fno_tag = '<span class="badge fno" style="margin-left:6px">F&O</span>' if m.get("fno") else ""
                score   = m.get("score",0)
                sc_col  = "#00d09c" if score>=70 else "#f59e0b" if score>=55 else "#9ca3af"
                tv_link = m.get("tv_link",f"https://in.tradingview.com/chart/?symbol=NSE:{m['symbol']}")
                st.markdown(f"""
<div class="card neutral">
  <div style="display:flex;justify-content:space-between;align-items:flex-start">
    <div>
      <span style="font-size:13px;font-weight:700;color:#6b7280;margin-right:4px">{i}.</span>
      <span class="sym">{m['symbol']}</span>
      {fno_tag}
      <div style="font-size:11px;color:#6b7280;margin-top:4px">{m.get('reason','')[:80]}</div>
    </div>
    <div style="text-align:right">
      <div style="font-size:14px;font-weight:800;color:{sc_col}">Score {score:.0f}</div>
      <div style="font-size:12px;color:#e8eaed;margin-top:2px">₹{m['price']}</div>
    </div>
  </div>
  <div class="kv-grid">
    {_kv("T1",  f"₹{m['target1']}", "green")}
    {_kv("T2",  f"₹{m['target2']}", "green")}
    {_kv("T3",  f"₹{m.get('target3',m['target2'])}", "green")}
    {_kv("SL",  f"₹{m['sl']}", "red")}
    {_kv("RR",  str(m['rr']), "blue")}
    {_kv("Vol", f"{m.get('vol_ratio','')}x", "amber")}
  </div>
  <div style="margin-top:10px;display:flex;gap:8px;align-items:center">
    {_tv_btn(tv_link)}
    <span style="font-size:11px;color:#6b7280">Wk RSI {m.get('wk_rsi','')} · ADX {m.get('wk_adx','')} · 52W pos {m.get('range_pos','')}%</span>
  </div>
</div>""", unsafe_allow_html=True)

            st.download_button("Export CSV", mb_df.to_csv(index=False), "multibaggers.csv","text/csv")

    with wt2:
        st.markdown('<div class="section-title">🕯️ OHL / OLL Scanner · Nifty 500 · First 15-min candle</div>', unsafe_allow_html=True)
        _now_ohl = datetime.now(IST)
        if _now_ohl.hour<9 or (_now_ohl.hour==9 and _now_ohl.minute<30):
            st.info("Market opens 9:15 AM IST. Screener activates after 9:30 AM.")
        else:
            oc1,oc2 = st.columns([5,1])
            oc1.caption(f"Nifty 500 · 0.02% tolerance · Cached 15 min · {_now_ohl.strftime('%I:%M %p IST')}")
            with oc2:
                if st.button("↺ Refresh", key="ohl_ref"):
                    st.cache_data.clear(); st.rerun()

            with st.spinner("Scanning Nifty 500…"):
                _ohl_res = _ohl_oll_scan()

            if not _ohl_res:
                st.info("No OHL/OLL setups found today.")
            else:
                _oll      = [r for r in _ohl_res if r["type"]=="OLL"]
                _ohllist  = [r for r in _ohl_res if r["type"]=="OHL"]
                _oll_a    = [r for r in _oll     if not r["broken"]]
                _ohl_a    = [r for r in _ohllist  if not r["broken"]]
                _oll_b    = [r for r in _oll     if r["broken"]]
                _ohl_b    = [r for r in _ohllist  if r["broken"]]
                _rng_all  = [r for r in _ohl_res  if r.get("range_alert")]

                om1,om2,om3,om4 = st.columns(4)
                om1.metric("OLL Active (Bullish)", len(_oll_a))
                om2.metric("OHL Active (Bearish)", len(_ohl_a))
                om3.metric("Range Coil",           len(_rng_all))
                om4.metric("Broken",               len(_oll_b)+len(_ohl_b))
                st.markdown("<div style='height:12px'></div>",unsafe_allow_html=True)

                # OHL filter
                of1 = st.selectbox("Show", ["All Active","OLL Only","OHL Only","Range Coil","Broken"], key="ohl_flt")

                def _ohl_cards(items, sig_type, broken=False):
                    if not items: return
                    bc = "#00d09c" if sig_type=="OLL" else "#eb5757"
                    if broken: bc = "#f59e0b"
                    for r in items:
                        sym=r["symbol"]; o=r["open"]; h=r["high"]; l=r["low"]
                        price=r["price"]; rsi_v=r["rsi_1h"]; drng=r.get("day_range_pct",99)
                        chg=((price-o)/o*100) if o>0 else 0
                        chg_col="#00d09c" if chg>=0 else "#eb5757"
                        rsi_col="#00d09c" if rsi_v>=55 else "#f59e0b" if rsi_v>=46 else "#eb5757"
                        tv=f"https://in.tradingview.com/chart/?symbol=NSE:{sym}"
                        ra=r.get("range_alert",False)
                        st.markdown(f"""
<div class="card" style="border-left-color:{bc}">
  <div style="display:flex;justify-content:space-between;align-items:flex-start">
    <div>
      <span class="sym">{sym}</span>
      <span class="badge {'buy' if sig_type=='OLL' else 'sell'}" style="margin-left:8px">{sig_type}</span>
      {f'<span style="font-size:10px;font-weight:700;padding:2px 8px;border-radius:4px;background:rgba(99,102,241,.1);color:#a5b4fc;border:1px solid rgba(99,102,241,.3);margin-left:4px">⊡ COIL</span>' if ra else ''}
      {f'<span style="font-size:10px;font-weight:700;padding:2px 8px;border-radius:4px;background:rgba(245,158,11,.1);color:#f59e0b;border:1px solid rgba(245,158,11,.3);margin-left:4px">⚡ BROKEN</span>' if broken else ''}
    </div>
    <div style="text-align:right">
      <div style="font-size:16px;font-weight:800;color:#e8eaed">₹{price}</div>
      <div style="font-size:11px;font-weight:700;color:{chg_col}">{chg:+.2f}% from open</div>
    </div>
  </div>
  <div class="kv-grid">
    {_kv("Open",  f"₹{o}")}
    {_kv("1st H", f"₹{h}", "green")}
    {_kv("1st L", f"₹{l}", "red")}
    {_kv("Range", f"{drng:.2f}%")}
    {_kv("1H RSI",str(rsi_v))}
  </div>
  <div style="margin-top:10px;display:flex;align-items:center;gap:10px">
    <div style="flex:1;height:4px;background:#1e2025;border-radius:2px;overflow:hidden">
      <div style="width:{min(100,rsi_v):.0f}%;height:100%;background:{rsi_col};border-radius:2px"></div>
    </div>
    {_tv_btn(tv)}
  </div>
</div>""", unsafe_allow_html=True)

                if of1=="All Active":
                    if _rng_all:
                        st.markdown('<div style="font-size:11px;font-weight:700;color:#a5b4fc;margin-bottom:6px">⊡ Range Coil — Breakout Watch</div>',unsafe_allow_html=True)
                        _ohl_cards(_rng_all, "OLL")
                        st.markdown("---")
                    st.markdown('<div style="font-size:11px;font-weight:700;color:#00d09c;margin-bottom:6px">OLL — Bullish Long Bias</div>',unsafe_allow_html=True)
                    _ohl_cards(_oll_a, "OLL")
                    st.markdown('<div style="font-size:11px;font-weight:700;color:#eb5757;margin-top:12px;margin-bottom:6px">OHL — Bearish Short Bias</div>',unsafe_allow_html=True)
                    _ohl_cards(_ohl_a, "OHL")
                elif of1=="OLL Only":
                    _ohl_cards(_oll_a, "OLL")
                elif of1=="OHL Only":
                    _ohl_cards(_ohl_a, "OHL")
                elif of1=="Range Coil":
                    _ohl_cards(_rng_all, "OLL")
                elif of1=="Broken":
                    _ohl_cards(_oll_b, "OLL", broken=True)
                    _ohl_cards(_ohl_b, "OHL", broken=True)

                st.caption("Tolerance 0.02% · Not SEBI advice · Data Yahoo Finance")


# ══════════════════════════════════════════════════════════════════════════════
# TAB: HISTORY
# ══════════════════════════════════════════════════════════════════════════════
with tab_hist:
    st.markdown('<div class="section-title">📋 Signal History · Full Audit Trail</div>', unsafe_allow_html=True)

    if IS_LOCAL:
        try:
            from tracker import _conn
            with _conn() as _hc:
                hist_all = pd.read_sql("SELECT * FROM all_signals ORDER BY date DESC LIMIT 500", _hc)
        except Exception:
            hist_all = pd.DataFrame()
        display_hist = hist_all
    else:
        display_hist = _gh_all_signals(days=9999)

    if display_hist.empty:
        st.markdown('<div style="text-align:center;padding:60px 20px"><div style="font-size:36px;margin-bottom:12px">📋</div><div style="font-size:14px;font-weight:600;color:#e8eaed;margin-bottom:6px">No signal history yet</div><div style="font-size:12px;color:#6b7280">Every scan auto-logs here. Run a scan to start building history.</div></div>',unsafe_allow_html=True)
    else:
        # Performance bar
        def _is_win(row):
            s=str(row.get("status",""))+str(row.get("lifecycle_status",""))
            return any(x in s for x in ["T1_HIT","T2_HIT","T1_Hit","T2_Hit","Partial_T1"])
        def _is_sl(row):
            return "SL" in str(row.get("status",""))+str(row.get("lifecycle_status",""))
        def _is_open(row):
            s=str(row.get("status",""))+str(row.get("lifecycle_status",""))
            return any(x in s for x in ["OPEN","Active","Generated","Entry_Triggered"])

        _recs   = display_hist.to_dict("records")
        _total  = len(_recs)
        _wins   = sum(1 for r in _recs if _is_win(r))
        _sls    = sum(1 for r in _recs if _is_sl(r))
        _opens  = sum(1 for r in _recs if _is_open(r))
        _wr     = round(_wins/(_wins+_sls)*100,1) if (_wins+_sls)>0 else 0
        _rmults = [float(r["r_multiple"]) for r in _recs if r.get("r_multiple") not in (None,"") and str(r.get("r_multiple",""))!="nan"]
        _avg_r  = round(sum(_rmults)/len(_rmults),2) if _rmults else 0

        hc1,hc2,hc3,hc4,hc5,hc6 = st.columns(6)
        hc1.metric("Total",      _total)
        hc2.metric("Open",       _opens)
        hc3.metric("Wins",       _wins)
        hc4.metric("SL Hit",     _sls)
        hc5.metric("Win Rate",   f"{_wr}%")
        hc6.metric("Avg R-Mult", _avg_r if _avg_r else "—")
        st.markdown("<div style='height:12px'></div>",unsafe_allow_html=True)

        # Filters
        fc1,fc2,fc3,fc4 = st.columns(4)
        fc5,fc6,fc7 = st.columns(3)
        _uniq = lambda col: (["All"]+sorted(display_hist[col].dropna().astype(str).unique().tolist())) if col in display_hist.columns else ["All"]

        _f_st  = fc1.selectbox("Status",    ["All","OPEN","Active","T1_HIT","T2_HIT","SL_HIT","Closed"], key="hf_st")
        _f_ty  = fc2.selectbox("Strategy",  _uniq("signal_type"), key="hf_ty")
        _f_ac  = fc3.selectbox("Direction", ["All","BUY","SELL"], key="hf_ac")
        _f_at  = fc4.selectbox("Asset",     _uniq("asset_type"),  key="hf_at")
        _f_mk  = fc5.selectbox("Market",    _uniq("market"),      key="hf_mk")
        _f_dr  = fc6.selectbox("Range",     ["All time","Today","Last 7 days","Last 30 days","Last 90 days"], key="hf_dr")
        _f_ms  = fc7.slider("Min Score",    0, 100, 0, 5, key="hf_sc")

        _fh = display_hist.copy()
        if _f_st!="All" and "status" in _fh.columns:
            _fh = _fh[(_fh["status"]==_f_st)|(_fh.get("lifecycle_status",pd.Series(dtype=str))==_f_st)]
        if _f_ty!="All" and "signal_type" in _fh.columns:
            _fh = _fh[_fh["signal_type"]==_f_ty]
        if _f_ac!="All" and "action" in _fh.columns:
            _fh = _fh[_fh["action"]==_f_ac]
        if _f_at!="All" and "asset_type" in _fh.columns:
            _fh = _fh[_fh["asset_type"]==_f_at]
        if _f_mk!="All" and "market" in _fh.columns:
            _fh = _fh[_fh["market"]==_f_mk]
        if _f_ms>0 and "score" in _fh.columns:
            _fh = _fh[_fh["score"].fillna(0).astype(float)>=_f_ms]
        if _f_dr!="All time" and "date" in _fh.columns:
            from datetime import timedelta
            _dm = {"Today":1,"Last 7 days":7,"Last 30 days":30,"Last 90 days":90}
            _fh = _fh[_fh["date"]>=str(date.today()-timedelta(days=_dm.get(_f_dr,9999)))]

        if _fh.empty:
            st.info("No records match current filters.")
        else:
            _tcols=["date","symbol","action","signal_type","score","status","entry","sl","target1","target2","rr","pnl_pct","r_multiple"]
            _tc=[c for c in _tcols if c in _fh.columns]
            _td=_fh[_tc].copy()
            for _pc2 in ["entry","sl","target1","target2"]:
                if _pc2 in _td.columns:
                    _td[_pc2]=_td[_pc2].apply(lambda v: f"₹{float(v):,.2f}" if pd.notna(v) else "—")
            st.dataframe(_td, use_container_width=True, hide_index=True, height=min(600,44+len(_td)*36))
            st.download_button("⬇ Export CSV", _fh.to_csv(index=False), "signal_history.csv","text/csv")


# ─── Footer ──────────────────────────────────────────────────────────────────
_footer_ts = datetime.now(IST).strftime("%d %b %Y · %I:%M %p IST")
st.markdown(f"""
<div style="margin-top:40px;padding-top:20px;border-top:1px solid #1e2025;
  display:flex;flex-wrap:wrap;gap:24px;justify-content:space-between">
  <div>
    <div style="font-size:13px;font-weight:800;color:#e8eaed;margin-bottom:6px">TradeFlow</div>
    <div style="font-size:11px;color:#6b7280;line-height:1.7">NSE Nifty 500 Swing Scanner<br>Mon–Fri · 9:20 AM · 11:45 AM · 4:30 PM IST</div>
  </div>
  <div>
    <div style="font-size:11px;color:#6b7280;line-height:1.7">
      Data: Yahoo Finance (15-min delay)<br>
      Last load: <span style="color:#00d09c">{_footer_ts}</span>
    </div>
  </div>
  <div>
    <div style="font-size:11px;color:#6b7280;line-height:1.7">
      ⚠ Not SEBI-registered · Not financial advice<br>
      Built by <a href="https://www.instagram.com/askakshayfinance" target="_blank" style="color:#00d09c;text-decoration:none">@askakshayfinance</a>
    </div>
  </div>
</div>
""", unsafe_allow_html=True)
