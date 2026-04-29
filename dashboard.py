import streamlit as st
import plotly.graph_objects as go
import plotly.express as px
import pandas as pd
import yfinance as yf
import ta as ta_lib
from datetime import datetime
import pytz, os

from scanner import scan_all, scan_breakouts, scan_4h, fetch_forex_comm, obfuscate_reasons
from telegram_bot import send_alert, send_summary, send_top_picks, test_connection, start_command_polling
from tracker import log_signals, update_outcomes, get_performance, get_history, init_db
from config import MIN_SIGNAL_SCORE, CAPITAL
from upstox_provider import is_authenticated, get_auth_url, exchange_code_for_token
from mf_tracker import (search_funds, get_nav_history, calc_returns, get_fund_news,
                         load_portfolio, save_portfolio, get_portfolio_summary,
                         get_index_quotes, get_top_funds_data, get_stock_news,
                         get_corporate_actions, get_fund_holdings)

st.set_page_config(page_title="SwingDesk Pro", layout="wide", page_icon="⚡",
                   initial_sidebar_state="expanded")
IST      = pytz.timezone("Asia/Kolkata")
IS_LOCAL = os.path.exists("config.py")
init_db()

# ── Theme ─────────────────────────────────────────────────────────────────────
if "theme" not in st.session_state:
    st.session_state["theme"] = "dark"
_DARK = st.session_state["theme"] == "dark"

# ── CSS ───────────────────────────────────────────────────────────────────────
_THEME_VARS = """
:root {
  --bg:       #030912;
  --bg2:      #070f1e;
  --bg3:      #0a1929;
  --border:   rgba(56,189,248,.1);
  --border2:  rgba(56,189,248,.06);
  --txt:      #e2e8f0;
  --txt2:     #94a3b8;
  --txt3:     #475569;
  --txt4:     #334155;
  --accent:   #38bdf8;
  --green:    #22c55e;
  --red:      #ef4444;
  --card-bg:  linear-gradient(135deg,rgba(10,25,41,.9),rgba(7,15,30,.9));
}
""" if _DARK else """
:root {
  --bg:       #f0f4f8;
  --bg2:      #e2eaf3;
  --bg3:      #ffffff;
  --border:   rgba(2,132,199,.15);
  --border2:  rgba(2,132,199,.08);
  --txt:      #0f172a;
  --txt2:     #334155;
  --txt3:     #64748b;
  --txt4:     #94a3b8;
  --accent:   #0284c7;
  --green:    #16a34a;
  --red:      #dc2626;
  --card-bg:  linear-gradient(135deg,#ffffff,#f8fafc);
}
"""

st.markdown(f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800;900&family=JetBrains+Mono:wght@400;500;700&display=swap');
{_THEME_VARS}

/* ---- Rotating border property (Chrome 85+ / Safari 15.4+) ---- */
@property --rot {{
  syntax: '<angle>';
  inherits: false;
  initial-value: 0deg;
}}
@keyframes rotateBorder {{ to {{ --rot: 360deg; }} }}
@keyframes fadeUp {{ from {{ opacity:0; transform:translateY(18px); }} to {{ opacity:1; transform:translateY(0); }} }}
@keyframes fadeIn  {{ from {{ opacity:0; }} to {{ opacity:1; }} }}
@keyframes slideRight {{ from {{ transform:translateX(-100%); }} to {{ transform:translateX(0); }} }}
@keyframes shimmer  {{ 0% {{ left:-120%; }} 100% {{ left:120%; }} }}
@keyframes pulse    {{ 0%,100% {{ opacity:1; box-shadow:0 0 6px var(--green); }} 50% {{ opacity:.5; box-shadow:0 0 14px var(--green); }} }}
@keyframes confFill {{ from {{ width:0%; }} to {{ width:100%; }} }}
@keyframes scanLine {{ 0% {{ transform:translateX(-100%); }} 100% {{ transform:translateX(200%); }} }}
@keyframes breathe  {{ 0%,100% {{ opacity:.6; }} 50% {{ opacity:1; }} }}

/* ---- Base ---- */
html,body,[class*="css"] {{ font-family:'Inter',sans-serif!important; background:var(--bg)!important; color:var(--txt)!important; -webkit-font-smoothing:antialiased; }}
.stApp {{ background:var(--bg); }}
.stApp::before {{
  content:''; position:fixed; inset:0; pointer-events:none; z-index:0;
  background:
    radial-gradient(ellipse 70% 50% at 15% 35%, rgba(56,189,248,.05) 0%, transparent 60%),
    radial-gradient(ellipse 50% 40% at 85% 10%, rgba(34,197,94,.04) 0%, transparent 50%),
    radial-gradient(ellipse 40% 35% at 55% 85%, rgba(167,139,250,.03) 0%, transparent 45%);
}}
header[data-testid="stHeader"] {{ background:var(--bg2); backdrop-filter:blur(24px); -webkit-backdrop-filter:blur(24px); border-bottom:1px solid var(--border); box-shadow:0 1px 20px rgba(0,0,0,.15); }}
section[data-testid="stSidebar"] {{ background:var(--bg2)!important; border-right:1px solid var(--border); }}
section[data-testid="stSidebar"] *{{ color:var(--txt3)!important; }}
section[data-testid="stSidebar"] h1,section[data-testid="stSidebar"] h2,section[data-testid="stSidebar"] h3 {{ color:var(--txt2)!important; }}

/* ---- Buttons ---- */
.stButton>button {{
  background:linear-gradient(135deg,#0369a1 0%,#0ea5e9 60%,#38bdf8 100%)!important;
  color:#fff!important; border:none!important; border-radius:9px!important;
  font-weight:800!important; font-size:12px!important; letter-spacing:.04em!important;
  box-shadow:0 4px 20px rgba(14,165,233,.35),0 0 0 1px rgba(56,189,248,.15)!important;
  transition:all .25s cubic-bezier(.4,0,.2,1)!important; position:relative!important; overflow:hidden!important; }}
.stButton>button::after {{
  content:''; position:absolute; top:0; left:-120%; width:60%; height:100%;
  background:linear-gradient(90deg,transparent,rgba(255,255,255,.18),transparent);
  transition:left .45s ease; }}
.stButton>button:hover {{ box-shadow:0 6px 32px rgba(14,165,233,.55),0 0 0 1px rgba(56,189,248,.3)!important; transform:translateY(-2px)!important; }}
.stButton>button:hover::after {{ left:160%; }}
.stButton>button:active {{ transform:translateY(0)!important; }}

/* ---- Tabs ---- */
.stTabs [data-baseweb="tab-list"] {{
  background:var(--bg2); border-bottom:1px solid var(--border); padding:0 16px;
  backdrop-filter:blur(12px); gap:4px; }}
.stTabs [data-baseweb="tab"] {{
  background:transparent; color:var(--txt3)!important; font-size:10px; font-weight:700;
  padding:13px 18px; border-bottom:2px solid transparent; border-radius:0;
  text-transform:uppercase; letter-spacing:.09em; transition:all .22s ease; }}
.stTabs [data-baseweb="tab"]:hover {{ color:var(--txt2)!important; }}
.stTabs [aria-selected="true"] {{
  color:var(--accent)!important; border-bottom:2px solid var(--accent)!important;
  text-shadow:0 0 16px color-mix(in srgb, var(--accent) 60%, transparent); }}

/* ---- Metrics ---- */
[data-testid="metric-container"] {{
  background:var(--bg3); border:1px solid var(--border); border-radius:14px;
  padding:18px 22px; transition:all .3s ease; position:relative; overflow:hidden; }}
[data-testid="metric-container"]::before {{
  content:''; position:absolute; inset:0;
  background:linear-gradient(135deg, rgba(56,189,248,.04) 0%, transparent 60%);
  pointer-events:none; }}
[data-testid="metric-container"]::after {{
  content:''; position:absolute; top:0; left:0; right:0; height:1px;
  background:linear-gradient(90deg, transparent 0%, var(--accent) 50%, transparent 100%);
  opacity:.35; }}
[data-testid="metric-container"]:hover {{
  border-color:var(--accent); transform:translateY(-1px);
  box-shadow:0 8px 28px rgba(0,0,0,.12), 0 0 24px rgba(56,189,248,.06); }}
[data-testid="metric-container"] label {{
  color:var(--txt3)!important; font-size:9px!important;
  text-transform:uppercase; letter-spacing:.14em; font-weight:700; }}
[data-testid="metric-container"] [data-testid="stMetricValue"] {{
  color:var(--txt)!important; font-size:26px!important; font-weight:800!important;
  font-family:'JetBrains Mono',monospace!important; letter-spacing:-.03em; }}
[data-testid="stMetricDelta"] {{ font-size:11px!important; font-weight:700!important; }}

/* ---- DataFrames ---- */
.stDataFrame {{ border:1px solid var(--border)!important; border-radius:12px; overflow:hidden; }}
.stDataFrame thead th {{
  background:var(--bg2)!important; color:var(--accent)!important;
  font-size:9px!important; text-transform:uppercase; letter-spacing:.12em;
  font-weight:800; border-color:var(--border2)!important; padding:10px 14px!important; }}
.stDataFrame tbody tr {{ background:var(--bg)!important; transition:background .15s; }}
.stDataFrame tbody tr:hover {{ background:var(--bg3)!important; }}
.stDataFrame tbody td {{
  color:var(--txt2)!important; font-family:'JetBrains Mono',monospace;
  font-size:12px!important; border-color:var(--border2)!important;
  padding:9px 14px!important; }}

/* ---- Inputs ---- */
.stTextInput input, .stSelectbox [data-baseweb="select"] {{
  background:var(--bg3)!important; border:1px solid var(--border)!important;
  color:var(--txt)!important; border-radius:9px!important;
  transition:border-color .2s, box-shadow .2s; }}
.stTextInput input:focus {{
  border-color:var(--accent)!important;
  box-shadow:0 0 0 3px rgba(56,189,248,.1)!important; }}
.stSelectbox [data-baseweb="select"]:focus-within {{
  border-color:var(--accent)!important; }}

/* ---- Expanders ---- */
.streamlit-expanderHeader {{
  background:var(--bg3)!important; border:1px solid var(--border)!important;
  border-radius:10px!important; color:var(--txt2)!important;
  font-size:12px!important; font-weight:600!important; transition:all .2s; }}
.streamlit-expanderHeader:hover {{ border-color:var(--accent)!important; color:var(--txt)!important; }}
.streamlit-expanderContent {{
  background:var(--bg2)!important; border:1px solid var(--border2)!important;
  border-top:none!important; border-radius:0 0 10px 10px!important; }}

/* ---- Signal Cards (Terminal Style) ---- */
.card {{
  background:var(--card-bg); border:1px solid var(--border);
  border-radius:16px; padding:22px 24px; margin-bottom:16px;
  animation:fadeUp .45s cubic-bezier(.4,0,.2,1);
  transition:transform .28s ease, box-shadow .28s ease;
  position:relative; overflow:hidden; }}
.card::before {{
  content:''; position:absolute; top:0; left:0; right:0; height:1px; pointer-events:none;
  background:linear-gradient(90deg, transparent, rgba(34,197,94,.55), transparent); }}
.card::after {{
  content:''; position:absolute; inset:0; pointer-events:none;
  background:radial-gradient(ellipse 55% 40% at 92% 8%, rgba(34,197,94,.05) 0%, transparent 65%); }}
.card:hover {{ transform:translateY(-3px); box-shadow:0 16px 48px rgba(0,0,0,.28), 0 0 0 1px rgba(34,197,94,.18); }}
.card.sell::before {{ background:linear-gradient(90deg, transparent, rgba(239,68,68,.55), transparent); }}
.card.sell::after {{ background:radial-gradient(ellipse 55% 40% at 92% 8%, rgba(239,68,68,.05) 0%, transparent 65%); }}
.card.sell:hover {{ box-shadow:0 16px 48px rgba(0,0,0,.28), 0 0 0 1px rgba(239,68,68,.18); }}
.card.top {{
  border-color:transparent;
  background:linear-gradient(var(--card-bg), var(--card-bg)) padding-box,
    conic-gradient(from var(--rot), #22c55e 0%, #38bdf8 33%, #a78bfa 66%, #22c55e 100%) border-box;
  animation:rotateBorder 4s linear infinite; }}
/* ---- Action Badge ---- */
.action-badge {{
  display:inline-flex; align-items:center; padding:3px 13px; border-radius:99px;
  font-size:11px; font-weight:800; letter-spacing:.05em; text-transform:uppercase; }}
.action-badge.buy {{
  background:rgba(34,197,94,.15); color:#22c55e; border:1px solid rgba(34,197,94,.4);
  box-shadow:0 0 12px rgba(34,197,94,.2); }}
.action-badge.sell {{
  background:rgba(239,68,68,.12); color:#ef4444; border:1px solid rgba(239,68,68,.35);
  box-shadow:0 0 12px rgba(239,68,68,.18); }}
/* ---- Strength Bars ---- */
.sbar-row {{ display:flex; align-items:center; gap:8px; margin:5px 0; }}
.sbar-lbl {{ font-size:10px; font-weight:700; min-width:100px; }}
.sbar-lbl.bull {{ color:#22c55e; }} .sbar-lbl.bear {{ color:#ef4444; }}
.sbar-track {{ flex:1; height:5px; border-radius:3px; overflow:hidden; }}
.sbar-track.bull {{ background:rgba(34,197,94,.12); }}
.sbar-track.bear {{ background:rgba(239,68,68,.08); }}
.sbar-fill.bull {{ height:100%;border-radius:3px;background:linear-gradient(90deg,#22c55e,#4ade80); }}
.sbar-fill.bear {{ height:100%;border-radius:3px;background:linear-gradient(90deg,#ef4444,#f87171); }}
.sbar-pct {{ font-size:10px; font-weight:800; min-width:28px; text-align:right;
  font-family:'JetBrains Mono',monospace; }}
.sbar-pct.bull {{ color:#22c55e; }} .sbar-pct.bear {{ color:#475569; }}
/* ---- Trigger Box ---- */
.trigger-box {{
  background:rgba(239,68,68,.04); border:1px solid rgba(239,68,68,.18);
  border-radius:10px; padding:11px 14px; margin:12px 0; }}
.trig-label {{ font-size:8px; font-weight:800; color:#ef4444; letter-spacing:.14em;
  text-transform:uppercase; margin-bottom:5px; }}
.trig-text {{ font-size:12px; font-weight:600; color:#f1f5f9; line-height:1.4; }}
.trig-meta {{ font-size:10px; color:#475569; margin-top:4px; }}
/* ---- Trade Grid ---- */
.tgrid {{ display:grid; grid-template-columns:1fr 1fr; gap:8px; margin:12px 0; }}
.tgcell {{
  background:var(--bg2); border:1px solid var(--border2);
  border-radius:10px; padding:11px 13px; position:relative; overflow:hidden; }}
.tgcell::before {{
  content:''; position:absolute; top:0; left:0; right:0; height:1px;
  background:linear-gradient(90deg, transparent, var(--border), transparent); }}
.tc-label {{ font-size:8px; color:var(--txt4); text-transform:uppercase;
  letter-spacing:.12em; font-weight:700; margin-bottom:5px; }}
.tc-val {{ font-size:19px; font-weight:800; font-family:'JetBrains Mono',monospace;
  color:var(--txt); line-height:1; letter-spacing:-.02em; }}
.tgcell.sl {{ border-color:rgba(239,68,68,.22); }}
.tgcell.sl::before {{ background:linear-gradient(90deg,transparent,rgba(239,68,68,.35),transparent); }}
.tgcell.t1 {{ border-color:rgba(34,197,94,.22); }}
.tgcell.t1::before {{ background:linear-gradient(90deg,transparent,rgba(34,197,94,.35),transparent); }}
/* vol tag variants */
.tag.hi-vol {{ background:rgba(239,68,68,.1); color:#f87171; border-color:rgba(239,68,68,.3); }}
.tag.md-vol {{ background:rgba(245,158,11,.08); color:#f59e0b; border-color:rgba(245,158,11,.25); }}

/* ---- Breakout Cards ---- */
.bo-card {{
  background:var(--card-bg); border:1px solid var(--border); border-left:3px solid var(--green);
  border-radius:14px; padding:18px 20px; margin-bottom:12px;
  animation:fadeUp .4s ease; transition:all .25s; position:relative; overflow:hidden; }}
.bo-card::before {{
  content:''; position:absolute; top:0; left:0; right:0; height:1px;
  background:linear-gradient(90deg, transparent, var(--green), transparent); opacity:.3; }}
.bo-card:hover {{ transform:translateY(-2px); box-shadow:0 8px 28px rgba(0,0,0,.12); border-color:rgba(34,197,94,.3); }}
.bo-card.weekly {{ border-left-color:#f59e0b; }}
.bo-card.weekly::before {{ background:linear-gradient(90deg, transparent, #f59e0b, transparent); }}
.bo-card.monthly {{ border-left-color:#a78bfa; }}
.bo-card.monthly::before {{ background:linear-gradient(90deg, transparent, #a78bfa, transparent); }}

/* ---- F&O Cards ---- */
.fno-card {{
  background:var(--card-bg); border:1px solid var(--border); border-left:3px solid var(--accent);
  border-radius:14px; padding:18px 20px; margin-bottom:12px;
  animation:fadeUp .4s ease; transition:all .25s; position:relative; overflow:hidden; }}
.fno-card::before {{
  content:''; position:absolute; top:0; left:0; right:0; height:1px;
  background:linear-gradient(90deg, transparent, var(--accent), transparent); opacity:.4; }}
.fno-card:hover {{ transform:translateY(-2px); box-shadow:0 8px 28px rgba(56,189,248,.1); }}

/* ---- MF Cards ---- */
.mf-card {{
  background:var(--card-bg); border:1px solid var(--border);
  border-radius:14px; padding:20px 22px; margin-bottom:14px;
  animation:fadeUp .4s ease; transition:all .25s; }}
.mf-card:hover {{ transform:translateY(-2px); border-color:var(--accent); box-shadow:0 8px 24px rgba(0,0,0,.1); }}

/* ---- Badges ---- */
.badge {{
  display:inline-flex; align-items:center;
  padding:4px 12px; border-radius:99px; font-size:9px; font-weight:800;
  letter-spacing:.09em; text-transform:uppercase; }}
.badge.sb {{
  background:rgba(34,197,94,.1); color:#22c55e; border:1px solid rgba(34,197,94,.3);
  box-shadow:0 0 10px rgba(34,197,94,.2), inset 0 0 8px rgba(34,197,94,.05); }}
.badge.b {{
  background:rgba(56,189,248,.1); color:#0ea5e9; border:1px solid rgba(56,189,248,.3);
  box-shadow:0 0 10px rgba(56,189,248,.15); }}
.badge.w {{
  background:rgba(251,191,36,.08); color:#d97706; border:1px solid rgba(251,191,36,.25); }}
.badge.fno {{
  background:rgba(56,189,248,.07); color:#0284c7; border:1px solid rgba(56,189,248,.2);
  font-size:9px; }}

/* ---- Confidence Bar (legacy, kept for breakout cards) ---- */
.conf {{ height:4px; background:var(--border2); border-radius:3px; margin:10px 0 12px; overflow:hidden; position:relative; }}
.conf-fill {{ height:100%; border-radius:3px; animation:confFill .7s cubic-bezier(.4,0,.2,1) forwards; }}

/* ---- KV Rows ---- */
.row {{ display:flex; gap:20px; flex-wrap:wrap; margin:10px 0; }}
.kv {{ display:flex; flex-direction:column; min-width:60px; }}
.kv span:first-child {{
  font-size:8px; color:var(--txt3); text-transform:uppercase;
  letter-spacing:.1em; font-weight:700; margin-bottom:3px; }}
.kv span:last-child {{
  font-size:13px; font-weight:700; font-family:'JetBrains Mono',monospace;
  color:var(--txt); line-height:1; }}

/* ---- Tags ---- */
.tag {{
  display:inline-block; padding:3px 10px; border-radius:99px;
  font-size:9px; font-weight:700; margin:2px 3px;
  background:rgba(34,197,94,.07); color:var(--green);
  border:1px solid rgba(34,197,94,.2);
  transition:all .18s; }}
.tag:hover {{ background:rgba(34,197,94,.14); border-color:rgba(34,197,94,.35); }}

/* ---- News ---- */
.news-item {{ padding:11px 0; border-bottom:1px solid var(--border2); transition:all .18s; }}
.news-item:hover {{ padding-left:4px; }}
.news-item:last-child {{ border-bottom:none; }}

/* ---- Live dot ---- */
.live {{
  display:inline-block; width:7px; height:7px; background:var(--green); border-radius:50%;
  margin-right:6px; animation:pulse 2s ease-in-out infinite; vertical-align:middle; }}

/* ---- Utility ---- */
.green {{ color:var(--green)!important; }}
.red   {{ color:var(--red)!important; }}
.blue  {{ color:var(--accent)!important; }}
hr {{ border-color:var(--border2)!important; margin:18px 0!important; }}

/* ---- Scrollbar ---- */
::-webkit-scrollbar {{ width:4px; height:4px; }}
::-webkit-scrollbar-track {{ background:var(--bg); }}
::-webkit-scrollbar-thumb {{ background:var(--border); border-radius:4px; }}
::-webkit-scrollbar-thumb:hover {{ background:var(--accent); }}
</style>
""", unsafe_allow_html=True)


# ── Upstox OAuth ──────────────────────────────────────────────────────────────
if "code" in st.query_params and not is_authenticated():
    try:
        exchange_code_for_token(st.query_params["code"])
        st.success("Upstox connected! Refresh.")
        st.stop()
    except Exception as e:
        st.error(f"Upstox login failed: {e}")

if IS_LOCAL and "polling_started" not in st.session_state:
    start_command_polling()
    st.session_state["polling_started"] = True

if IS_LOCAL and "scheduler_started" not in st.session_state:
    from apscheduler.schedulers.background import BackgroundScheduler
    def _auto_scan():
        sigs = scan_all(min_score=st.session_state.get("min_score", MIN_SIGNAL_SCORE))
        st.session_state.update(signals=sigs, last_scan=datetime.now(IST).strftime("%d %b %Y %I:%M %p IST"))
        log_signals(sigs); update_outcomes()
        for s in sigs: send_alert(s)
        send_summary(sigs)
    _sch = BackgroundScheduler(timezone=IST)
    _sch.add_job(_auto_scan, "cron", hour=9,  minute=25)
    _sch.add_job(_auto_scan, "cron", hour=14, minute=0)
    _sch.add_job(_auto_scan, "cron", hour=17, minute=0)
    _sch.start()
    st.session_state["scheduler_started"] = True


# ── Cached fetchers ───────────────────────────────────────────────────────────
@st.cache_data(ttl=300)
def _forex():
    return fetch_forex_comm()

@st.cache_data(ttl=60)
def _index_quotes():
    return get_index_quotes()

@st.cache_data(ttl=3600)
def _top_funds():
    return get_top_funds_data()

@st.cache_data(ttl=600)
def _mf_summary(portfolio_json):
    import json
    return get_portfolio_summary(json.loads(portfolio_json))


# ── Helpers ───────────────────────────────────────────────────────────────────
def _rating(score):
    if score >= 85: return "STRONG BUY", "sb"
    if score >= 70: return "BUY", "b"
    return "WATCH", "w"

def _conf_col(score):
    if score >= 85: return "#22c55e"
    if score >= 70: return "#38bdf8"
    return "#f59e0b"

def _stars(s):
    n = 5 if s >= 90 else 4 if s >= 80 else 3 if s >= 70 else 2
    return "★"*n + "☆"*(5-n)

def _ret_col(v):
    return "green" if v >= 0 else "red"


def plot_chart(symbol, signal=None):
    df = yf.download(symbol + ".NS", period="6mo", interval="1d",
                     progress=False, auto_adjust=True)
    if df.empty:
        st.warning(f"No data for {symbol}"); return
    close = df["Close"].squeeze()
    e20  = ta_lib.trend.EMAIndicator(close, window=20).ema_indicator()
    e50  = ta_lib.trend.EMAIndicator(close, window=50).ema_indicator()
    e200 = ta_lib.trend.EMAIndicator(close, window=200).ema_indicator()
    fig  = go.Figure()
    fig.add_trace(go.Candlestick(x=df.index, open=df["Open"].squeeze(),
        high=df["High"].squeeze(), low=df["Low"].squeeze(), close=close,
        name="Price", increasing_line_color="#22c55e", decreasing_line_color="#ef4444",
        increasing_fillcolor="#052e16", decreasing_fillcolor="#450a0a"))
    fig.add_trace(go.Scatter(x=df.index, y=e20,  name="S", line=dict(color="#facc15", width=1.5)))
    fig.add_trace(go.Scatter(x=df.index, y=e50,  name="M", line=dict(color="#38bdf8", width=1.5)))
    fig.add_trace(go.Scatter(x=df.index, y=e200, name="L", line=dict(color="#f87171", width=1.5)))
    if signal:
        fig.add_hline(y=signal["sl2"],     line_color="#ef4444", line_dash="dash", annotation_text="STOP")
        fig.add_hline(y=signal["target1"], line_color="#86efac", line_dash="dot",  annotation_text="T1")
        fig.add_hline(y=signal["target2"], line_color="#4ade80", line_dash="dot",  annotation_text="T2")
        fig.add_hline(y=signal["target3"], line_color="#22c55e", line_dash="dot",  annotation_text="T3")
    fig.update_layout(xaxis_rangeslider_visible=False, height=440,
        paper_bgcolor="#070f1e", plot_bgcolor="#050c18",
        font=dict(color="#64748b", size=10, family="JetBrains Mono"),
        xaxis=dict(gridcolor="#0f2035"), yaxis=dict(gridcolor="#0f2035"),
        legend=dict(bgcolor="#070f1e", bordercolor="#0f2035", borderwidth=1, font=dict(size=10)),
        margin=dict(l=8,r=8,t=8,b=8))
    st.plotly_chart(fig, use_container_width=True)


# ── Sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    _th_icon = "☀️" if _DARK else "🌙"
    _th_lbl  = f"{_th_icon} {'Light' if _DARK else 'Dark'} Mode"
    col_logo, col_th = st.columns([3, 2])
    with col_logo:
        st.markdown('<div style="font-size:17px;font-weight:900;padding:10px 0 16px;letter-spacing:-.02em">SwingDesk <span style="color:#0ea5e9">Pro</span></div>', unsafe_allow_html=True)
    with col_th:
        st.markdown("<div style='margin-top:10px'></div>", unsafe_allow_html=True)
        if st.button(_th_lbl, use_container_width=True):
            st.session_state["theme"] = "light" if _DARK else "dark"
            st.rerun()
    run_scan = st.button("Run Swing Scan", use_container_width=True)
    run_bo   = st.button("Run Breakout Scan", use_container_width=True)
    run_4h   = st.button("Run 4H Early Scan", use_container_width=True)
    send_tg  = st.checkbox("Telegram alerts", value=True)
    st.markdown("**Min Score**")
    min_score = st.slider("", 50, 100, MIN_SIGNAL_SCORE, label_visibility="collapsed")
    st.session_state["min_score"] = min_score
    st.markdown("**Chart Symbol**")
    chart_sym  = st.text_input("", placeholder="RELIANCE", label_visibility="collapsed")
    show_chart = st.button("Show Chart", use_container_width=True)
    st.markdown("---")
    if is_authenticated():
        st.markdown('<span class="live"></span>Upstox Live', unsafe_allow_html=True)
    else:
        st.markdown(f"[Connect Upstox]({get_auth_url()})")
        st.caption("Using yfinance")
    st.markdown("---")
    if st.button("Test Telegram", use_container_width=True):
        test_connection(); st.success("Sent!")
    if st.button("Update Outcomes", use_container_width=True):
        update_outcomes(); st.success("Done!")
    st.markdown("---")
    st.caption("Auto: 9:25 · 14:00 · 17:00 IST")
    if "last_scan" in st.session_state:
        st.caption(f"Last: {st.session_state['last_scan']}")
    st.caption(f"₹{CAPITAL:,} capital")


# ── Header ────────────────────────────────────────────────────────────────────
now_str   = datetime.now(IST).strftime("%d %b · %I:%M %p IST")
sig_count = len(st.session_state.get("signals", []))
bo_count  = len(st.session_state.get("breakouts", []))

st.markdown(f"""
<div style="background:linear-gradient(135deg,rgba(7,15,30,.97),rgba(3,9,18,.97));
  border:1px solid rgba(56,189,248,.14);border-radius:16px;padding:18px 24px;margin-bottom:14px;
  display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:14px;
  backdrop-filter:blur(20px);position:relative;overflow:hidden">
  <div style="position:absolute;top:0;left:0;right:0;height:1px;background:linear-gradient(90deg,transparent,rgba(56,189,248,.5),transparent)"></div>
  <div style="position:absolute;inset:0;pointer-events:none;
    background:radial-gradient(ellipse 50% 60% at 10% 50%,rgba(56,189,248,.04),transparent 60%)"></div>
  <div>
    <div style="display:flex;align-items:center;gap:12px;margin-bottom:6px">
      <div style="font-size:24px;font-weight:900;color:#f1f5f9;letter-spacing:-.03em;line-height:1">
        SwingDesk&nbsp;<span style="color:#38bdf8;text-shadow:0 0 18px rgba(56,189,248,.55)">Pro</span>
      </div>
      <span style="font-size:8px;font-weight:800;padding:3px 9px;border-radius:4px;
        background:rgba(34,197,94,.12);color:#22c55e;border:1px solid rgba(34,197,94,.3);
        letter-spacing:.1em;text-transform:uppercase">
        <span class="live" style="margin-right:4px"></span>LIVE
      </span>
      <span style="font-size:8px;color:#334155;font-weight:600;letter-spacing:.06em">TRADER v2.0</span>
    </div>
    <div style="font-size:9px;color:#1e3a5f;letter-spacing:.09em;text-transform:uppercase;font-weight:600">
      Nifty 500 &nbsp;·&nbsp; Breakouts &nbsp;·&nbsp; 4H Early &nbsp;·&nbsp; F&amp;O &nbsp;·&nbsp; MF &nbsp;·&nbsp; Global Markets
    </div>
  </div>
  <div style="display:flex;gap:12px;flex-wrap:wrap;align-items:center">
    <div style="text-align:center;padding:10px 18px;background:rgba(56,189,248,.05);border:1px solid rgba(56,189,248,.12);border-radius:10px">
      <div style="font-size:26px;font-weight:900;color:#38bdf8;font-family:'JetBrains Mono',monospace;letter-spacing:-.02em;text-shadow:0 0 14px rgba(56,189,248,.4);line-height:1">{sig_count}</div>
      <div style="font-size:7px;color:#1e3a5f;text-transform:uppercase;letter-spacing:.12em;font-weight:800;margin-top:3px">SIGNALS</div>
    </div>
    <div style="text-align:center;padding:10px 18px;background:rgba(34,197,94,.05);border:1px solid rgba(34,197,94,.12);border-radius:10px">
      <div style="font-size:26px;font-weight:900;color:#22c55e;font-family:'JetBrains Mono',monospace;letter-spacing:-.02em;text-shadow:0 0 14px rgba(34,197,94,.4);line-height:1">{bo_count}</div>
      <div style="font-size:7px;color:#1e3a5f;text-transform:uppercase;letter-spacing:.12em;font-weight:800;margin-top:3px">BREAKOUTS</div>
    </div>
    <div style="text-align:right">
      <div style="font-size:11px;color:#475569;font-family:'JetBrains Mono',monospace">{now_str}</div>
      <div style="font-size:8px;color:#1e3a5f;margin-top:3px;letter-spacing:.05em;text-transform:uppercase">Auto-refresh 60s</div>
      <div style="font-size:8px;color:#0f2035;margin-top:2px;letter-spacing:.04em">NOT SEBI ADVICE · EDUCATIONAL</div>
    </div>
  </div>
</div>
""", unsafe_allow_html=True)

# ── Bloomberg Ticker Bar ─────────────────────────────────────────────────────
def _ti_idx(r):
    c = "#4ade80" if r["chg"] >= 0 else "#f87171"
    s = "+" if r["chg"] >= 0 else ""
    return (f'<span style="margin:0 24px;white-space:nowrap">'
            f'<span style="color:#94a3b8;font-size:11px;font-weight:700;letter-spacing:.04em">{r["name"]}</span>'
            f'&nbsp;&nbsp;<span style="color:#f1f5f9;font-size:12px;font-weight:800;'
            f'font-family:\'JetBrains Mono\',monospace">{r["last"]:,.2f}</span>'
            f'&nbsp;<span style="color:{c};font-size:11px;font-weight:700">{s}{r["chg"]}%</span>'
            f'</span>')

def _ti_forex(r):
    c = "#4ade80" if r["Chg%"] >= 0 else "#f87171"
    s = "+" if r["Chg%"] >= 0 else ""
    return (f'<span style="margin:0 24px;white-space:nowrap">'
            f'<span style="color:#a78bfa;font-size:11px;font-weight:700;letter-spacing:.04em">{r["Asset"]}</span>'
            f'&nbsp;&nbsp;<span style="color:#f1f5f9;font-size:12px;font-weight:800;'
            f'font-family:\'JetBrains Mono\',monospace">{r["Last"]}</span>'
            f'&nbsp;<span style="color:{c};font-size:11px;font-weight:700">{s}{r["Chg%"]}%</span>'
            f'</span>')

def _ti_signal(sig):
    action = sig.get("action", "BUY")
    c = "#4ade80" if action == "BUY" else "#f87171"
    arrow = "▲" if action == "BUY" else "▼"
    return (f'<span style="margin:0 24px;white-space:nowrap;background:rgba(34,197,94,.07);'
            f'border:1px solid rgba(34,197,94,.2);border-radius:4px;padding:2px 8px">'
            f'<span style="color:#fbbf24;font-size:10px;font-weight:700">SIGNAL</span>'
            f'&nbsp;<span style="color:{c};font-size:11px;font-weight:800">{arrow} {sig["symbol"]}</span>'
            f'&nbsp;<span style="color:#94a3b8;font-size:11px">₹{sig["price"]:,.1f}</span>'
            f'</span>')

_iq  = _index_quotes()
_fxc = _forex()
_sigs_ticker = st.session_state.get("signals", [])[:6]

ticker_parts = []
if _iq:
    ticker_parts += [_ti_idx(r) for r in _iq]
    ticker_parts.append('<span style="margin:0 16px;color:#0f2035">│</span>')
if _fxc:
    ticker_parts += [_ti_forex(r) for r in _fxc]
if _sigs_ticker:
    ticker_parts.append('<span style="margin:0 16px;color:#0f2035">│</span>')
    ticker_parts += [_ti_signal(s) for s in _sigs_ticker]

if ticker_parts:
    ticker_html = "".join(ticker_parts)
    st.markdown(f"""
<div style="background:linear-gradient(90deg,rgba(5,12,24,.95),rgba(7,18,36,.95));
  border:1px solid rgba(56,189,248,.1);border-radius:10px;padding:0;margin-bottom:14px;
  overflow:hidden;backdrop-filter:blur(12px);position:relative">
  <div style="position:absolute;top:0;left:0;right:0;height:1px;
    background:linear-gradient(90deg,transparent,rgba(56,189,248,.3),transparent)"></div>
  <div style="display:flex;align-items:stretch">
    <div style="padding:0 14px;border-right:1px solid rgba(56,189,248,.1);
      display:flex;align-items:center;gap:6px;flex-shrink:0;background:rgba(56,189,248,.04)">
      <span class="live"></span>
      <span style="font-size:9px;font-weight:800;color:#22c55e;letter-spacing:.12em;text-transform:uppercase">Live</span>
    </div>
    <div style="overflow:hidden;flex:1;padding:9px 0">
      <marquee behavior="scroll" direction="left" scrollamount="5" style="display:block">{ticker_html}</marquee>
    </div>
  </div>
</div>
""", unsafe_allow_html=True)

tab1, tab2, tab3, tab4, tab5, tab6, tab7 = st.tabs(["Signals", "Breakouts", "F&O", "Mutual Funds", "Market News", "Performance", "History"])


# ══════════════════════════════════════════════════════════════════════════════
# TAB 1 — SIGNALS
# ══════════════════════════════════════════════════════════════════════════════
with tab1:
    if show_chart and chart_sym:
        sig_map = {s["symbol"]: s for s in st.session_state.get("signals", [])}
        plot_chart(chart_sym.upper().strip(), sig_map.get(chart_sym.upper().strip()))

    if run_scan:
        with st.spinner("Scanning Nifty 500… 2-4 min"):
            sigs = scan_all(min_score=min_score)
            st.session_state.update(signals=sigs,
                last_scan=datetime.now(IST).strftime("%d %b %Y %I:%M %p IST"))
            log_signals(sigs)
        if send_tg:
            for s in sigs: send_alert(s)
            send_summary(sigs)
        st.success(f"{len(sigs)} signals found!")
        st.rerun()

    signals = st.session_state.get("signals", [])
    if not signals:
        st.markdown('<div style="text-align:center;padding:50px 0;color:#1e3a5f"><div style="font-size:36px">⚡</div><div style="margin-top:8px;font-size:14px;color:#334155">Click <b>Run Swing Scan</b> · Auto: 9:25 AM · 2 PM · 5 PM IST</div></div>', unsafe_allow_html=True)
    else:
        # KPI
        c = st.columns(5)
        c[0].metric("Signals",   len(signals))
        c[1].metric("Top Score", f"{signals[0]['score']}/100")
        c[2].metric("Avg Score", f"{round(sum(s['score'] for s in signals)/len(signals),1)}")
        c[3].metric("F&O Ready", sum(1 for s in signals if s.get("fno_eligible")))
        c[4].metric("Avg RR",    f"1:{round(sum(s['rr1'] for s in signals)/len(signals),1)}")

        st.markdown("---")
        sort_by = st.selectbox("Sort by", ["score","rr1","vol_ratio"], index=0)
        sigs_s  = sorted(signals, key=lambda x: x.get(sort_by,0), reverse=True)

        for i, s in enumerate(sigs_s):
            # --- derived display values ---
            score       = s['score']
            uncertainty = 100 - score
            bull_pct    = min(score, 100)
            fno_b       = '<span class="badge fno">F&amp;O</span>' if s.get("fno_eligible") else ""
            cls         = f"card {'top' if i==0 else ''} {'sell' if s['action']=='SELL' else ''}"
            act_cls     = "buy" if s['action'] == "BUY" else "sell"

            # vol tag
            vr = s.get('vol_ratio', 1.0)
            if vr >= 2.2:
                vol_tag, vol_cls = "HIGH VOLATILITY", "hi-vol"
            elif vr >= 1.6:
                vol_tag, vol_cls = "ELEVATED VOL", "md-vol"
            else:
                vol_tag, vol_cls = "NORMAL VOL", ""

            # reason tags (obfuscated)
            reason_tags = "".join(
                f'<span class="tag">{t.strip()}</span>'
                for t in obfuscate_reasons(s["reasons"]).split(",") if t.strip()
            )

            # trigger text from setup
            _trig_map = {
                "breakout":   "Price broke above key resistance zone with strong volume surge",
                "pullback":   "Pullback to EMA support zone — trend continuation setup",
                "divergence": "Bullish RSI divergence detected at structural support",
            }
            trigger_text = _trig_map.get(s.get("setup_type", ""), "Multi-factor setup triggered across trend, structure, volume")

            st.markdown(f"""
<div class="{cls}">
  <!-- Header: Symbol + Action + Confidence -->
  <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:2px">
    <div>
      <div style="display:flex;align-items:center;gap:10px;margin-bottom:5px">
        <span style="font-size:22px;font-weight:900;color:#f1f5f9;letter-spacing:-.02em;line-height:1">{s['symbol']}</span>
        <span class="action-badge {act_cls}">{s['action']}</span>
        {fno_b}
      </div>
      <div style="font-size:10px;color:var(--txt4)">{s.get('setup_type','').replace('_',' ').title()} &nbsp;·&nbsp; NSE Equity &nbsp;·&nbsp; Swing</div>
    </div>
    <div style="text-align:right;flex-shrink:0;margin-left:16px">
      <div style="font-size:36px;font-weight:900;color:#22c55e;font-family:'JetBrains Mono',monospace;line-height:1;text-shadow:0 0 22px rgba(34,197,94,.35)">{score}%</div>
      <div style="font-size:8px;color:var(--txt4);text-transform:uppercase;letter-spacing:.1em;margin-top:2px">confidence</div>
      <div style="font-size:14px;font-weight:700;color:#f59e0b;margin-top:5px">{uncertainty}%</div>
      <div style="font-size:8px;color:var(--txt4);text-transform:uppercase;letter-spacing:.1em">uncertainty</div>
    </div>
  </div>

  <!-- Strength bars -->
  <div style="margin:14px 0 10px">
    <div class="sbar-row">
      <span class="sbar-lbl bull">Bullish Strength</span>
      <div class="sbar-track bull"><div class="sbar-fill bull" style="width:{bull_pct}%"></div></div>
      <span class="sbar-pct bull">{bull_pct}%</span>
    </div>
    <div class="sbar-row">
      <span class="sbar-lbl bear">Bearish Strength</span>
      <div class="sbar-track bear"><div class="sbar-fill bear" style="width:0%"></div></div>
      <span class="sbar-pct bear">0%</span>
    </div>
  </div>

  <!-- Tags -->
  <div style="margin-bottom:10px">
    <span class="tag {vol_cls}">{vol_tag}</span>
    {reason_tags}
  </div>

  <!-- Trigger box -->
  <div class="trigger-box">
    <div class="trig-label">SIGNAL TRIGGER</div>
    <div class="trig-text">{trigger_text}</div>
    <div class="trig-meta">ADX {s.get('adx',0)} &nbsp;·&nbsp; Vol {vr:.1f}x avg &nbsp;·&nbsp; {s.get('regime','').title()} Regime &nbsp;·&nbsp; RSI {s.get('rsi',0)}</div>
  </div>

  <!-- Trade Structure -->
  <div style="font-size:8px;font-weight:800;color:var(--txt4);text-transform:uppercase;letter-spacing:.14em;margin-bottom:8px">TRADE STRUCTURE</div>
  <div class="tgrid">
    <div class="tgcell">
      <div class="tc-label">CURRENT PRICE</div>
      <div class="tc-val">₹{s['price']:,.2f}</div>
    </div>
    <div class="tgcell">
      <div class="tc-label">ENTRY ZONE</div>
      <div class="tc-val">₹{s['price']:,.2f}</div>
    </div>
    <div class="tgcell sl">
      <div class="tc-label">STOP LOSS</div>
      <div class="tc-val" style="color:#ef4444">₹{s['sl2']:,.2f}</div>
    </div>
    <div class="tgcell t1">
      <div class="tc-label">TARGET 1</div>
      <div class="tc-val" style="color:#22c55e">₹{s['target1']:,.2f}</div>
    </div>
  </div>

  <!-- Extra targets + stats -->
  <div class="row" style="margin-top:4px">
    <div class="kv"><span>Risk/Reward</span><span class="blue">1:{s['rr1']}</span></div>
    <div class="kv"><span>Target 2</span><span class="green">₹{s['target2']:,.1f}</span></div>
    <div class="kv"><span>Target 3</span><span class="green">₹{s['target3']:,.1f}</span></div>
    <div class="kv"><span>Max Pos.</span><span>{s['qty']} shares</span></div>
    <div class="kv"><span>ATR (Daily)</span><span>₹{s.get('atr',0):.2f}</span></div>
  </div>

  <!-- Footer -->
  <div style="display:flex;justify-content:space-between;align-items:center;margin-top:14px;padding-top:10px;border-top:1px solid var(--border2)">
    <span style="font-size:9px;color:var(--txt4)">Educational purposes only &nbsp;·&nbsp; Not SEBI advice &nbsp;·&nbsp; Model v2.0</span>
    <a href="{s['tv_link']}" target="_blank" style="color:var(--accent);font-size:11px;font-weight:700;text-decoration:none">Chart →</a>
  </div>
</div>
""", unsafe_allow_html=True)

        st.markdown("---")
        ca, cb = st.columns([3,1])
        with ca:
            df_s = pd.DataFrame(sigs_s)
            fig  = px.bar(df_s, x="symbol", y="score", color="score",
                          color_continuous_scale=["#0ea5e9","#22c55e"], range_color=[60,100])
            fig.update_layout(height=180, paper_bgcolor="#070f1e", plot_bgcolor="#050c18",
                font=dict(color="#64748b",size=10), xaxis=dict(gridcolor="#0f2035"),
                yaxis=dict(gridcolor="#0f2035",range=[50,100]),
                margin=dict(l=8,r=8,t=8,b=8), showlegend=False, coloraxis_showscale=False)
            st.plotly_chart(fig, use_container_width=True)
        with cb:
            pk = st.selectbox("Chart", [s["symbol"] for s in sigs_s])
            sm = {s["symbol"]:s for s in sigs_s}
            if st.button("View", key="v1"): plot_chart(pk, sm.get(pk))
            if pk in sm: st.markdown(f"[TradingView]({sm[pk]['tv_link']})")

        st.download_button("Export CSV", df_s.to_csv(index=False), "signals.csv", "text/csv")


# ══════════════════════════════════════════════════════════════════════════════
# TAB 2 — BREAKOUTS
# ══════════════════════════════════════════════════════════════════════════════
with tab2:
    st.markdown('<div style="font-size:13px;font-weight:700;color:#22c55e;margin-bottom:4px">Confirmed Breakouts</div><div style="font-size:11px;color:#334155;margin-bottom:14px">Daily · Weekly · Monthly — closed above key level with volume</div>', unsafe_allow_html=True)

    if run_bo:
        with st.spinner("Scanning F&O universe… 3-5 min"):
            bos = scan_breakouts()
            st.session_state["breakouts"] = bos
        st.success(f"{len(bos)} breakouts confirmed!")
        st.rerun()

    if run_4h:
        with st.spinner("4H RSI-55 scan… 2-3 min"):
            sigs_4h = scan_4h()
            st.session_state["signals_4h"] = sigs_4h
        if send_tg:
            from datetime import datetime
            ts = datetime.now(IST).strftime("%d %b %Y %I:%M %p IST")
            if sigs_4h:
                from telegram_bot import _post
                lines = [f"⚡ *4H Early-Entry Signals* — {ts}\n_(RSI crossing 55 + Volume surge)_\n"]
                for b in sigs_4h[:8]:
                    fno_tag = " `F&O`" if b["fno"] else ""
                    lines.append(f"• *{b['symbol']}*{fno_tag} | ₹{b['price']} | RSI {b['rsi']} | Vol {b['vol_ratio']}x | T1 ₹{b['target1']} | SL ₹{b['sl']}")
                _post("\n".join(lines))
        st.success(f"{len(sigs_4h)} 4H early signals!")
        st.rerun()

    breakouts = st.session_state.get("breakouts", [])
    if not breakouts:
        st.info("Click **Run Breakout Scan** in sidebar.")
    else:
        tfc = {}
        for b in breakouts: tfc[b["timeframe"]] = tfc.get(b["timeframe"],0)+1
        c1,c2,c3,c4 = st.columns(4)
        c1.metric("Total", len(breakouts))
        c2.metric("Monthly", tfc.get("Monthly",0))
        c3.metric("Weekly",  tfc.get("Weekly",0))
        c4.metric("Daily",   tfc.get("Daily",0))
        st.markdown("---")
        tf_f = st.selectbox("Filter", ["All","Monthly","Weekly","Daily"])
        fil  = [b for b in breakouts if tf_f=="All" or b["timeframe"]==tf_f]
        for b in fil:
            tf   = b["timeframe"]
            cls  = {"Monthly":"monthly","Weekly":"weekly","Daily":""}.get(tf,"")
            tfc2 = {"Monthly":"#a78bfa","Weekly":"#f59e0b","Daily":"#22c55e"}.get(tf,"#22c55e")
            fno_b = '<span class="badge fno">F&amp;O</span>' if b.get("fno") else ""
            pats  = " · ".join(b.get("patterns",[b["pattern"]]))
            st.markdown(f"""
<div class="bo-card {cls}">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
    <div style="display:flex;align-items:center;gap:8px">
      <span style="font-size:16px;font-weight:800;color:#f1f5f9">{b['symbol']}</span>{fno_b}
    </div>
    <span style="font-size:10px;font-weight:700;color:{tfc2};padding:2px 8px;border-radius:99px;border:1px solid {tfc2}40">{tf.upper()}</span>
  </div>
  <div style="font-size:10px;color:#475569;margin-bottom:8px">{pats}</div>
  <div class="row">
    <div class="kv"><span>Price</span><span>₹{b['price']:,.1f}</span></div>
    <div class="kv"><span>Stop</span><span class="red">₹{b['sl']:,.1f}</span></div>
    <div class="kv"><span>T1</span><span class="green">₹{b['target1']:,.1f}</span></div>
    <div class="kv"><span>T2</span><span class="green">₹{b['target2']:,.1f}</span></div>
    <div class="kv"><span>T3</span><span class="green">₹{b['target3']:,.1f}</span></div>
    <div class="kv"><span>RR</span><span class="blue">1:{b['rr']}</span></div>
    <div class="kv"><span>Vol</span><span>{b['vol_ratio']}x</span></div>
  </div>
  <div style="margin-top:8px"><a href="{b['tv_link']}" target="_blank" style="color:#38bdf8;font-size:11px;font-weight:600;text-decoration:none">Chart →</a></div>
</div>
""", unsafe_allow_html=True)

    # 4H Early-Entry Signals Section
    sigs_4h = st.session_state.get("signals_4h", [])
    st.markdown("---")
    st.markdown('<div style="font-size:13px;font-weight:700;color:#f59e0b;margin-bottom:4px">⚡ 4H Early-Entry Signals</div><div style="font-size:11px;color:#334155;margin-bottom:14px">RSI crossing 55 + Volume surge — fires before daily candle closes</div>', unsafe_allow_html=True)
    if not sigs_4h:
        st.info("Click **Run 4H Early Scan** in sidebar.")
    else:
        cc1, cc2 = st.columns(2)
        cc1.metric("4H Signals", len(sigs_4h))
        cc2.metric("Avg Vol Ratio", f"{round(sum(s['vol_ratio'] for s in sigs_4h)/len(sigs_4h),1)}x")
        for b in sigs_4h:
            fno_b = '<span class="badge fno">F&amp;O</span>' if b.get("fno") else ""
            st.markdown(f"""
<div class="bo-card" style="border-color:#f59e0b40">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
    <div style="display:flex;align-items:center;gap:8px">
      <span style="font-size:16px;font-weight:800;color:#f1f5f9">{b['symbol']}</span>{fno_b}
    </div>
    <span style="font-size:10px;font-weight:700;color:#f59e0b;padding:2px 8px;border-radius:99px;border:1px solid #f59e0b40">4H · EARLY</span>
  </div>
  <div style="font-size:10px;color:#94a3b8;margin-bottom:8px">{b['reason']}</div>
  <div class="row">
    <div class="kv"><span>Price</span><span>₹{b['price']:,.2f}</span></div>
    <div class="kv"><span>Stop</span><span class="red">₹{b['sl']:,.2f}</span></div>
    <div class="kv"><span>T1</span><span class="green">₹{b['target1']:,.2f}</span></div>
    <div class="kv"><span>T2</span><span class="green">₹{b['target2']:,.2f}</span></div>
    <div class="kv"><span>RR</span><span class="blue">1:{b['rr']}</span></div>
    <div class="kv"><span>Vol</span><span>{b['vol_ratio']}x</span></div>
  </div>
  <div style="margin-top:8px"><a href="{b['tv_link']}" target="_blank" style="color:#38bdf8;font-size:11px;font-weight:600;text-decoration:none">Chart →</a></div>
</div>
""", unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════════════════════
# TAB 3 — F&O
# ══════════════════════════════════════════════════════════════════════════════
with tab3:
    st.markdown('<div style="font-size:13px;font-weight:700;color:#38bdf8;margin-bottom:4px">F&O Trade Suggestions</div><div style="font-size:11px;color:#334155;margin-bottom:14px">Nifty 200 stocks · Verify premium &amp; IV on NSE before trading</div>', unsafe_allow_html=True)

    signals  = st.session_state.get("signals", [])
    fno_sigs = [s for s in signals if s.get("fno_eligible") and s.get("fno_suggestion")]

    if not signals:
        st.info("Run **Swing Scan** first → F&O suggestions auto-appear for Nifty 200 stocks.")
    elif not fno_sigs:
        st.warning(f"Scan found {len(signals)} signals but none are from F&O eligible stocks today. Try lowering Min Score or run again at next session.")
        # Show all signals as reference
        st.markdown("**All current signals (for reference):**")
        for s in signals[:5]:
            st.markdown(f"• **{s['symbol']}** — {s['setup_type']} — score {s['score']}")
    else:
        _tf_map = {"pullback": "Swing · 5–12 days", "breakout": "Swing · 8–15 days", "divergence": "Reversal · 3–8 days"}
        for s in sorted(fno_sigs, key=lambda x: x["score"], reverse=True):
            f     = s["fno_suggestion"]
            is_c  = f["direction"] == "CALL"
            dc    = "#4ade80" if is_c else "#f87171"
            di    = "▲ CALL" if is_c else "▼ PUT"
            rl,rc = _rating(s["score"])
            tf_label = _tf_map.get(s.get("setup_type",""), "Swing · 5–15 days")
            st.markdown(f"""
<div class="fno-card">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
    <span style="font-size:17px;font-weight:800;color:#f1f5f9">{s['symbol']}</span>
    <div style="display:flex;gap:10px;align-items:center">
      <span style="font-size:14px;font-weight:800;color:{dc}">{di}</span>
      <span class="badge {rc}">{rl}</span>
    </div>
  </div>
  <div style="margin-bottom:8px;display:flex;gap:12px;align-items:center">
    <span style="font-size:10px;font-weight:600;color:#38bdf8;background:#0a1929;border:1px solid #0f2d4a;border-radius:4px;padding:2px 7px">⏱ {tf_label}</span>
    <span style="font-size:10px;color:#475569">{f['expiry']}</span>
  </div>
  <div class="row">
    <div class="kv"><span>Spot</span><span>₹{s['price']:,.1f}</span></div>
    <div class="kv"><span>ATM Strike</span><span class="blue">₹{f['atm_strike']:,}</span></div>
    <div class="kv"><span>OTM Strike</span><span class="blue">₹{f['otm_strike']:,}</span></div>
    <div class="kv"><span>Risk pts</span><span class="red">{f['risk_pts']}</span></div>
    <div class="kv"><span>Stock SL</span><span class="red">₹{s['sl2']:,.1f}</span></div>
    <div class="kv"><span>Stock T1</span><span class="green">₹{s['target1']:,.1f}</span></div>
  </div>
  <div style="margin-top:10px;background:#050c18;border:1px solid #0f2035;border-radius:6px;padding:8px 12px;font-family:'JetBrains Mono',monospace;font-size:10px;color:#475569">{f['note']}</div>
  <div style="margin-top:8px;font-size:11px;display:flex;gap:14px">
    <a href="{s['tv_link']}" target="_blank" style="color:#38bdf8;font-weight:600;text-decoration:none">Chart →</a>
    <a href="https://www.nseindia.com/get-quotes/derivatives?symbol={s['symbol']}" target="_blank" style="color:#475569;text-decoration:none">NSE Chain →</a>
  </div>
</div>
""", unsafe_allow_html=True)
        st.markdown('<div style="font-size:10px;color:#334155;padding:8px;background:#050c18;border:1px solid #0f2035;border-radius:6px">⚠️ Strike &amp; direction from swing signal + ATR. Verify premium, IV, OI independently. Not SEBI advice.</div>', unsafe_allow_html=True)

    # Forex watchlist
    st.markdown("---")
    st.markdown('<div style="font-size:12px;font-weight:700;color:#38bdf8;margin-bottom:10px">Global Markets</div>', unsafe_allow_html=True)
    fc = _forex()
    if fc:
        cols = st.columns(len(fc))
        for i, r in enumerate(fc):
            c = "#4ade80" if r["Chg%"] >= 0 else "#f87171"
            s = "+" if r["Chg%"] >= 0 else ""
            cols[i].markdown(f"""
<div style="background:#0a1929;border:1px solid #0f2d4a;border-radius:8px;padding:10px;text-align:center">
  <div style="font-size:9px;color:#334155;text-transform:uppercase;letter-spacing:.07em;margin-bottom:3px">{r['Asset']}</div>
  <div style="font-size:15px;font-weight:700;color:#f1f5f9;font-family:'JetBrains Mono',monospace">{r['Last']}</div>
  <div style="font-size:11px;font-weight:600;color:{c}">{s}{r['Chg%']}%</div>
</div>
""", unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════════════════════
# TAB 4 — MUTUAL FUNDS
# ══════════════════════════════════════════════════════════════════════════════
with tab4:
    st.markdown('<div style="font-size:13px;font-weight:700;color:#38bdf8;margin-bottom:4px">Mutual Fund Intelligence</div><div style="font-size:11px;color:#334155;margin-bottom:16px">Top Funds Discovery · Portfolio Tracker · NAV · Returns — powered by AMFI</div>', unsafe_allow_html=True)

    # ── Top Funds per Category ─────────────────────────────────────────────
    st.markdown('<div style="font-size:12px;font-weight:700;color:#f1f5f9;margin-bottom:10px">Top Funds by Category</div>', unsafe_allow_html=True)
    with st.spinner("Loading top funds data…"):
        top_data = _top_funds()

    if top_data:
        cat_tabs = st.tabs(list(top_data.keys()))
        for ct, (cat, funds) in zip(cat_tabs, top_data.items()):
            with ct:
                if not funds:
                    st.info("No data available.")
                    continue
                rows = []
                for f in funds:
                    def _fmt(v):
                        return f"{v:+.2f}%" if v is not None else "—"
                    rows.append({
                        "Fund": f["short"],
                        "NAV": f"₹{f['nav']:.2f}",
                        "1Y": _fmt(f['1Y']),
                        "3Y": _fmt(f['3Y']),
                        "5Y": _fmt(f['5Y']),
                    })
                df_top = pd.DataFrame(rows)

                def _style_ret(val):
                    if isinstance(val, str) and val != "—":
                        return "color:#4ade80;font-weight:700" if val.startswith("+") else "color:#f87171;font-weight:700"
                    return "color:#475569"

                st.dataframe(
                    df_top.style.map(_style_ret, subset=["1Y", "3Y", "5Y"]),
                    use_container_width=True, hide_index=True
                )
                # Selectbox to pick which fund to drill into
                _fund_names = [f["short"] for f in funds]
                _sel_idx = st.selectbox("View fund breakdown", range(len(_fund_names)),
                                        format_func=lambda i: _fund_names[i],
                                        key=f"sel_{cat}", label_visibility="collapsed")
                _sf = funds[_sel_idx]
                hd  = get_fund_holdings(_sf['scheme_code'])
                if hd:
                    _pie_colors = ["#38bdf8","#22c55e","#a78bfa","#f59e0b","#f87171","#34d399","#fb923c","#e879f9","#94a3b8"]
                    _pbg = "#0a1929" if _DARK else "#ffffff"
                    _pfg = "#94a3b8" if _DARK else "#475569"
                    _ptxt = "#e2e8f0" if _DARK else "#1a2332"
                    _pline = "#050c18" if _DARK else "#f0f4f8"
                    pc1, pc2 = st.columns(2)
                    with pc1:
                        sec = hd["sectors"]
                        fig_s = go.Figure(go.Pie(
                            labels=list(sec.keys()), values=list(sec.values()),
                            hole=0.5, textinfo="label+percent",
                            textfont=dict(size=10, color=_ptxt),
                            marker=dict(colors=_pie_colors[:len(sec)], line=dict(color=_pline, width=2)),
                            hovertemplate="%{label}: %{value:.1f}%<extra></extra>"
                        ))
                        fig_s.update_layout(
                            title=dict(text="Sector Allocation", font=dict(size=11, color=_pfg), x=0.5),
                            height=280, paper_bgcolor=_pbg, plot_bgcolor=_pbg,
                            font=dict(color=_pfg, size=10),
                            showlegend=False, margin=dict(l=4,r=4,t=36,b=4)
                        )
                        st.plotly_chart(fig_s, use_container_width=True, key=f"pie_s_{cat}_{_sel_idx}")
                    with pc2:
                        scripts = hd["top_scripts"]
                        s_labels = [s[0] for s in scripts]
                        s_vals   = [s[1] for s in scripts]
                        others   = max(0, 100 - sum(s_vals))
                        if others > 0.5:
                            s_labels.append("Others"); s_vals.append(round(others, 1))
                        fig_h = go.Figure(go.Pie(
                            labels=s_labels, values=s_vals,
                            hole=0.5, textinfo="label+percent",
                            textfont=dict(size=10, color=_ptxt),
                            marker=dict(colors=_pie_colors[:len(s_labels)], line=dict(color=_pline, width=2)),
                            hovertemplate="%{label}: %{value:.1f}%<extra></extra>"
                        ))
                        fig_h.update_layout(
                            title=dict(text="Top Holdings", font=dict(size=11, color=_pfg), x=0.5),
                            height=280, paper_bgcolor=_pbg, plot_bgcolor=_pbg,
                            font=dict(color=_pfg, size=10),
                            showlegend=False, margin=dict(l=4,r=4,t=36,b=4)
                        )
                        st.plotly_chart(fig_h, use_container_width=True, key=f"pie_h_{cat}_{_sel_idx}")
                    st.caption(f"{_sf['fund_house']}  ·  Holdings approximate as of last monthly AMC disclosure")
                else:
                    st.info("Holdings data not available for this fund.")

    st.markdown("---")

    # Portfolio manager
    portfolio = load_portfolio()

    with st.expander("+ Add Fund to Portfolio", expanded=len(portfolio)==0):
        search_q = st.text_input("Search fund name", placeholder="e.g. Parag Parikh Flexi Cap")
        if search_q and len(search_q) >= 3:
            results = search_funds(search_q)
            if results:
                options = {f"{r['schemeName']} ({r['schemeCode']})": r for r in results[:20]}
                chosen  = st.selectbox("Select fund", list(options.keys()))
                if chosen:
                    sel  = options[chosen]
                    col1, col2, col3 = st.columns(3)
                    units_in  = col1.number_input("Units", min_value=0.0, step=0.001, format="%.3f")
                    nav_in    = col2.number_input("Purchase NAV", min_value=0.0, step=0.01)
                    if col3.button("Add Fund"):
                        new_entry = {
                            "scheme_code":  sel["schemeCode"],
                            "name":         sel["schemeName"],
                            "units":        units_in,
                            "purchase_nav": nav_in,
                        }
                        portfolio = [p for p in portfolio if p["scheme_code"] != sel["schemeCode"]]
                        portfolio.append(new_entry)
                        save_portfolio(portfolio)
                        st.success(f"Added {sel['schemeName']}")
                        st.rerun()
            else:
                st.warning("No funds found. Try a different name.")

    if portfolio:
        # Remove fund
        rm_names = {p["name"]: p for p in portfolio}
        col_rm1, col_rm2 = st.columns([3,1])
        with col_rm1:
            to_remove = st.selectbox("Remove fund", ["—"] + list(rm_names.keys()))
        with col_rm2:
            st.markdown("<div style='margin-top:28px'></div>", unsafe_allow_html=True)
            if st.button("Remove") and to_remove != "—":
                portfolio = [p for p in portfolio if p["name"] != to_remove]
                save_portfolio(portfolio)
                st.rerun()

        st.markdown("---")

        # Load summary
        import json
        with st.spinner("Loading portfolio data…"):
            summary = _mf_summary(json.dumps(portfolio))

        if summary:
            # Portfolio totals
            total_inv = sum(s["invested"] for s in summary)
            total_cur = sum(s["current"]  for s in summary)
            total_pnl = total_cur - total_inv
            total_pct = (total_pnl / total_inv * 100) if total_inv > 0 else 0

            kc = st.columns(4)
            kc[0].metric("Total Invested", f"₹{total_inv:,.0f}")
            kc[1].metric("Current Value",  f"₹{total_cur:,.0f}")
            kc[2].metric("P&L",            f"₹{total_pnl:+,.0f}")
            kc[3].metric("Overall Return", f"{total_pct:+.2f}%")

            st.markdown("---")

            # Fund cards
            for s in summary:
                pnl_col  = "#4ade80" if s["pnl_pct"] >= 0 else "#f87171"
                day_col  = "#4ade80" if s["day_chg"] >= 0 else "#f87171"
                ret      = s["returns"]
                ret_html = "".join(
                    f'<div class="kv"><span>{k}</span><span class="{_ret_col(v)}">{v:+.1f}%</span></div>'
                    for k, v in ret.items()
                )
                st.markdown(f"""
<div class="mf-card">
  <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:10px">
    <div>
      <div style="font-size:15px;font-weight:700;color:#f1f5f9;max-width:420px;line-height:1.3">{s['name']}</div>
      <div style="font-size:10px;color:#334155;margin-top:2px">{s.get('fund_house','')} · {s.get('category','')}</div>
    </div>
    <div style="text-align:right">
      <div style="font-size:16px;font-weight:800;font-family:'JetBrains Mono',monospace;color:#f1f5f9">₹{s['nav']:.4f}</div>
      <div style="font-size:11px;font-weight:600;color:{day_col}">{s['day_chg']:+.2f}% today</div>
    </div>
  </div>
  <div class="row">
    <div class="kv"><span>Invested</span><span>₹{s['invested']:,.0f}</span></div>
    <div class="kv"><span>Current</span><span>₹{s['current']:,.0f}</span></div>
    <div class="kv"><span>P&L</span><span style="color:{pnl_col}">₹{s['pnl']:+,.0f}</span></div>
    <div class="kv"><span>Return</span><span style="color:{pnl_col}">{s['pnl_pct']:+.2f}%</span></div>
    <div class="kv"><span>Units</span><span>{s['units']:.3f}</span></div>
    <div class="kv"><span>Buy NAV</span><span>₹{s['purchase_nav']:.2f}</span></div>
  </div>
  <div style="margin-top:10px;padding-top:10px;border-top:1px solid #0f2035">
    <div style="font-size:9px;color:#334155;text-transform:uppercase;letter-spacing:.07em;margin-bottom:6px">CAGR Returns</div>
    <div class="row">{ret_html}</div>
  </div>
</div>
""", unsafe_allow_html=True)

            # News section
            st.markdown("---")
            st.markdown('<div style="font-size:12px;font-weight:700;color:#38bdf8;margin-bottom:12px">Fund News</div>', unsafe_allow_html=True)
            selected_fund = st.selectbox("News for", [s["name"] for s in summary])
            sel_s = next((s for s in summary if s["name"] == selected_fund), None)
            if sel_s:
                with st.spinner("Loading news…"):
                    news = get_fund_news(selected_fund[:40])
                if news:
                    for n in news:
                        st.markdown(f"""
<div class="news-item">
  <a href="{n['link']}" target="_blank" style="color:#e2e8f0;font-size:13px;font-weight:500;text-decoration:none">{n['title']}</a>
  <div style="font-size:10px;color:#334155;margin-top:3px">{n['published']}</div>
</div>
""", unsafe_allow_html=True)
                else:
                    st.info("No recent news found for this fund.")

            # Alerts section
            st.markdown("---")
            st.markdown('<div style="font-size:12px;font-weight:700;color:#fbbf24;margin-bottom:8px">⚠️ Smart Alerts</div>', unsafe_allow_html=True)
            for s in summary:
                alerts = []
                if abs(s["day_chg"]) > 2:
                    alerts.append(f"NAV moved {s['day_chg']:+.2f}% today — unusual move")
                ret1y = s["returns"].get("1Y", 0)
                if ret1y < 0:
                    alerts.append(f"1Y return is negative ({ret1y:.1f}%) — review")
                if s["pnl_pct"] < -10:
                    alerts.append(f"Portfolio down {s['pnl_pct']:.1f}% from cost — consider reviewing")
                if alerts:
                    for a in alerts:
                        st.markdown(f'<div style="background:#1a1200;border:1px solid #422006;border-radius:6px;padding:8px 12px;margin-bottom:6px;font-size:12px;color:#fbbf24">⚠️ <b>{s["name"][:40]}</b> — {a}</div>', unsafe_allow_html=True)
            st.markdown('<div style="font-size:10px;color:#334155;margin-top:8px">Fund manager / category / objective changes require AMC website monitoring — coming soon</div>', unsafe_allow_html=True)

        else:
            st.error("Could not load portfolio data. Check scheme codes.")
    else:
        st.markdown('<div style="text-align:center;padding:40px 0;color:#334155"><div style="font-size:32px">📊</div><div style="margin-top:8px;font-size:13px">Add your mutual funds above to start tracking</div></div>', unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════════════════════
# TAB 5 — MARKET NEWS
# ══════════════════════════════════════════════════════════════════════════════
with tab5:
    st.markdown('<div style="font-size:13px;font-weight:700;color:#38bdf8;margin-bottom:4px">Market News & Corporate Actions</div><div style="font-size:11px;color:#334155;margin-bottom:16px">Nifty 500 stocks · Results declarations · Dividends · Earnings</div>', unsafe_allow_html=True)

    col_ns, col_nb = st.columns([3, 1])
    with col_ns:
        news_sym = st.text_input("Stock symbol", placeholder="e.g. RELIANCE, INFY, HDFC", label_visibility="visible")
    with col_nb:
        st.markdown("<div style='margin-top:28px'></div>", unsafe_allow_html=True)
        news_go = st.button("Fetch News", use_container_width=True)

    if news_sym and news_go:
        sym_clean = news_sym.upper().strip()
        col_a, col_b = st.columns([3, 2])

        with col_a:
            st.markdown(f'<div style="font-size:12px;font-weight:700;color:#f1f5f9;margin-bottom:10px">{sym_clean} — Latest News</div>', unsafe_allow_html=True)
            with st.spinner("Fetching news…"):
                news_items = get_stock_news(sym_clean, n=8)
            if news_items:
                for n in news_items:
                    st.markdown(f"""
<div class="news-item">
  <a href="{n['link']}" target="_blank" style="color:#e2e8f0;font-size:13px;font-weight:500;text-decoration:none;line-height:1.45">{n['title']}</a>
  <div style="font-size:10px;color:#334155;margin-top:3px">{n['published']}</div>
</div>
""", unsafe_allow_html=True)
            else:
                st.info("No recent news found.")

        with col_b:
            st.markdown(f'<div style="font-size:12px;font-weight:700;color:#fbbf24;margin-bottom:10px">Corporate Actions</div>', unsafe_allow_html=True)
            with st.spinner("Loading…"):
                actions = get_corporate_actions(sym_clean)
            if actions:
                for a in actions:
                    st.markdown(f'<div style="background:#1a1200;border:1px solid #422006;border-radius:6px;padding:8px 12px;margin-bottom:6px;font-size:12px;color:#fbbf24">📋 {a}</div>', unsafe_allow_html=True)
            else:
                st.info("No corporate actions found.")

            st.markdown("---")
            st.markdown(f'<div style="font-size:11px;color:#334155">Links</div>', unsafe_allow_html=True)
            st.markdown(f'<a href="https://www.nseindia.com/get-quotes/equity?symbol={sym_clean}" target="_blank" style="color:#38bdf8;font-size:12px;font-weight:600;text-decoration:none;display:block;margin:4px 0">NSE Quote →</a>', unsafe_allow_html=True)
            st.markdown(f'<a href="https://www.bseindia.com/stockinfo/AnnSubCategorywise.html" target="_blank" style="color:#38bdf8;font-size:12px;font-weight:600;text-decoration:none;display:block;margin:4px 0">BSE Announcements →</a>', unsafe_allow_html=True)
    else:
        st.markdown('<div style="text-align:center;padding:50px 0;color:#1e3a5f"><div style="font-size:36px">📰</div><div style="margin-top:8px;font-size:14px;color:#334155">Enter a NSE symbol above (e.g. RELIANCE, INFY) to fetch news &amp; corporate actions</div></div>', unsafe_allow_html=True)

        # Quick picks - popular stocks
        st.markdown('<div style="font-size:11px;font-weight:700;color:#334155;margin-bottom:8px;text-transform:uppercase;letter-spacing:.06em">Quick Search</div>', unsafe_allow_html=True)
        quick = ["RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK", "WIPRO", "BAJFINANCE", "LT", "SBIN", "MARUTI"]
        cols_q = st.columns(5)
        for i, sym in enumerate(quick):
            if cols_q[i % 5].button(sym, key=f"qs_{sym}"):
                st.session_state["_news_sym"] = sym
                st.rerun()


# ══════════════════════════════════════════════════════════════════════════════
# TAB 6 — PERFORMANCE
# ══════════════════════════════════════════════════════════════════════════════
with tab6:
    perf = get_performance()
    if perf:
        p = st.columns(5)
        p[0].metric("Total Signals", perf["total"])
        p[1].metric("Win Rate",  f"{perf['win_rate']}%")
        p[2].metric("Avg P&L",  f"{perf['avg_pnl']}%")
        p[3].metric("Best",     f"+{perf['best']}%")
        p[4].metric("Worst",    f"{perf['worst']}%")
        hist   = get_history()
        closed = hist[hist["status"] != "OPEN"]
        if not closed.empty:
            fig = px.bar(closed, x="symbol", y="pnl_pct", color="pnl_pct",
                         color_continuous_scale=["#ef4444","#0f2035","#22c55e"],
                         range_color=[-20,20])
            fig.update_layout(paper_bgcolor="#070f1e", plot_bgcolor="#050c18",
                font=dict(color="#64748b",size=10), xaxis=dict(gridcolor="#0f2035"),
                yaxis=dict(gridcolor="#0f2035"), height=360,
                margin=dict(l=8,r=8,t=8,b=8), coloraxis_showscale=False, showlegend=False)
            st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("No closed trades yet.")


# ══════════════════════════════════════════════════════════════════════════════
# TAB 7 — HISTORY
# ══════════════════════════════════════════════════════════════════════════════
with tab7:
    hist = get_history()
    if not hist.empty:
        st.dataframe(hist, use_container_width=True, hide_index=True)
        st.download_button("Export", hist.to_csv(index=False), "history.csv", "text/csv")
    else:
        st.info("No history yet.")

st.markdown('<div style="text-align:center;padding:16px 0 4px;font-size:10px;color:#0f2035">SwingDesk Pro · Personal Research · Not SEBI Advice</div>', unsafe_allow_html=True)
