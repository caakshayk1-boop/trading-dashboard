import streamlit as st
import plotly.graph_objects as go
import plotly.express as px
import pandas as pd
import yfinance as yf
import ta as ta_lib
from datetime import datetime, date
import pytz, os, json, requests

from scanner import fetch_forex_comm, obfuscate_reasons
from tracker import (get_performance, get_history, get_active_signals, init_db,
                     get_breakouts, get_4h_signals, get_commodity_signals,
                     get_last_scan, get_signals_display)
from config import MIN_SIGNAL_SCORE, CAPITAL

# ── GitHub raw data source (Streamlit Cloud reads scans from here) ────────────
_GH_RAW = "https://raw.githubusercontent.com/caakshayk1-boop/trading-dashboard/main/data"

@st.cache_data(ttl=60)
def _fetch_json(name: str):
    """Fetch data/name.json from GitHub raw URL."""
    try:
        r = requests.get(f"{_GH_RAW}/{name}.json", timeout=10)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None

def _gh_signals_display(days=3, min_score=0):
    """Read signals from GitHub JSON (cloud fallback)."""
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

def _get_ai_signals(days=3):
    """Read TLM channel breakout signals (branded as AI Signals) from breakouts table."""
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
    # Filter: AI signals have pattern = "AI Channel Breakout"
    ai = [r for r in rows if "AI Channel" in str(r.get("pattern","")) or "Channel Breakout" in str(r.get("pattern",""))]
    return ai

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
@keyframes tickerScroll {{ 0% {{ transform:translateX(0); }} 100% {{ transform:translateX(-50%); }} }}
@keyframes aiGlow {{ 0%,100% {{ box-shadow:0 0 18px rgba(167,139,250,.25),0 0 0 1px rgba(167,139,250,.2); }} 50% {{ box-shadow:0 0 32px rgba(167,139,250,.45),0 0 0 1px rgba(167,139,250,.4); }} }}
@keyframes neuralPulse {{ 0%,100% {{ opacity:.3; transform:scale(1); }} 50% {{ opacity:.7; transform:scale(1.3); }} }}
@keyframes scanDiag {{ 0% {{ transform:translateX(-100%) translateY(-100%); }} 100% {{ transform:translateX(200%) translateY(200%); }} }}
@keyframes numberFlip {{ 0% {{ opacity:0; transform:translateY(-8px); }} 100% {{ opacity:1; transform:translateY(0); }} }}
@keyframes borderRun {{ 0% {{ background-position:0% 50%; }} 100% {{ background-position:200% 50%; }} }}

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

/* ---- AI Signal Cards ---- */
.ai-card {{
  background:linear-gradient(135deg,rgba(10,7,24,.97),rgba(17,9,36,.97));
  border:1px solid rgba(167,139,250,.2);
  border-left:3px solid #a78bfa;
  border-radius:16px; padding:22px 24px; margin-bottom:16px;
  animation:fadeUp .45s cubic-bezier(.4,0,.2,1), aiGlow 4s ease-in-out infinite;
  transition:transform .28s ease, box-shadow .28s ease;
  position:relative; overflow:hidden; }}
.ai-card::before {{
  content:''; position:absolute; top:0; left:0; right:0; height:1px; pointer-events:none;
  background:linear-gradient(90deg, transparent, rgba(167,139,250,.7), rgba(236,72,153,.5), transparent); }}
.ai-card::after {{
  content:''; position:absolute; inset:0; pointer-events:none;
  background:radial-gradient(ellipse 55% 40% at 92% 8%, rgba(167,139,250,.07) 0%, transparent 65%); }}
.ai-card:hover {{ transform:translateY(-3px); box-shadow:0 20px 56px rgba(167,139,250,.15), 0 0 0 1px rgba(167,139,250,.3); }}
/* AI scan line */
.ai-card .ai-scan {{
  position:absolute; top:0; left:0; bottom:0; width:2px;
  background:linear-gradient(180deg,transparent,rgba(167,139,250,.8),rgba(236,72,153,.6),transparent);
  animation:scanDiag 3s ease-in-out infinite; pointer-events:none; }}
/* AI badge */
.ai-badge {{
  display:inline-flex; align-items:center; gap:5px; padding:3px 12px; border-radius:99px;
  font-size:10px; font-weight:800; letter-spacing:.07em; text-transform:uppercase;
  background:linear-gradient(135deg,rgba(167,139,250,.15),rgba(236,72,153,.1));
  color:#c4b5fd; border:1px solid rgba(167,139,250,.4);
  box-shadow:0 0 12px rgba(167,139,250,.2); }}
/* Neural dots */
.neural-dot {{
  display:inline-block; width:5px; height:5px; border-radius:50%;
  background:#a78bfa; animation:neuralPulse 2s ease-in-out infinite; }}
/* AI channel viz */
.ai-channel {{
  background:rgba(167,139,250,.04); border:1px solid rgba(167,139,250,.12);
  border-radius:10px; padding:10px 14px; margin:10px 0; }}
.ai-channel-label {{ font-size:8px; font-weight:800; color:#a78bfa; letter-spacing:.14em; text-transform:uppercase; margin-bottom:6px; }}
.ai-channel-band {{
  display:flex; justify-content:space-between; align-items:center;
  font-family:'JetBrains Mono',monospace; font-size:11px; }}
.ai-channel-upper {{ color:#c4b5fd; font-weight:700; }}
.ai-channel-lower {{ color:#7c3aed; font-weight:600; }}
.ai-channel-width {{ color:#475569; font-size:10px; }}
/* Performance AI section */
.perf-ai-card {{
  background:linear-gradient(135deg,rgba(10,7,24,.97),rgba(17,9,36,.97));
  border:1px solid rgba(167,139,250,.15); border-radius:14px; padding:18px 20px; margin-bottom:12px;
  animation:fadeUp .4s ease; }}

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


# ── Auto-refresh every 60s (view-only mode) ──────────────────────────────────
try:
    from streamlit_autorefresh import st_autorefresh
    st_autorefresh(interval=60_000, key="live_refresh")
except ImportError:
    pass  # graceful — manual refresh if package missing


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


# ── Sidebar (view-only — filters + info) ─────────────────────────────────────
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

    # Last scan info — use GitHub JSON on cloud, local DB on dev
    if IS_LOCAL:
        _last_ts, _last_slot, _last_counts = get_last_scan()
    else:
        _last_ts, _last_slot, _last_counts = _gh_last_scan()
    if _last_ts:
        st.markdown(f'<div style="font-size:10px;color:#22c55e;font-weight:700;margin:6px 0 2px"><span class="live"></span>Last scan</div>', unsafe_allow_html=True)
        st.caption(f"{_last_ts}")
        st.caption(f"Slot: {_last_slot.upper() if _last_slot else '—'}")

    st.markdown("---")
    st.markdown('<div style="font-size:10px;font-weight:700;color:var(--txt3);letter-spacing:.08em;text-transform:uppercase;margin-bottom:6px">Filters</div>', unsafe_allow_html=True)
    min_score  = st.slider("Min Score", 50, 100, MIN_SIGNAL_SCORE)
    days_back  = st.selectbox("Show last", ["1 day", "3 days", "7 days"], index=1)
    _days      = int(days_back.split()[0])

    st.markdown("---")
    # Schedule info
    st.markdown("""
<div style="font-size:10px;color:var(--txt4);line-height:1.8">
  <div style="font-weight:700;color:var(--txt3);margin-bottom:4px">Auto Schedule (IST)</div>
  <div>⚡ 9:20 AM — 4H + Commodities</div>
  <div>📊 11:45 AM — Swing + F&O</div>
  <div>📋 4:30 PM — Breakouts + EOD</div>
  <div style="margin-top:6px;color:#334155">Signals via Telegram + site updates live</div>
</div>
""", unsafe_allow_html=True)
    st.markdown("---")
    st.caption("Data: yfinance · Not SEBI advice")


# ── Header ────────────────────────────────────────────────────────────────────
now_str   = datetime.now(IST).strftime("%d %b · %I:%M %p IST")
_active   = get_active_signals() if IS_LOCAL else pd.DataFrame()
_bos_df   = get_breakouts(days=_days) if IS_LOCAL else _gh_breakouts(days=_days)
sig_count = len(_active)
bo_count  = len(_bos_df)

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
# Ticker signals from DB (active signals)
_sigs_ticker = []
if not _active.empty:
    for _, _row in _active.head(5).iterrows():
        _sigs_ticker.append({"symbol": _row["symbol"], "action": _row.get("action","BUY"), "price": _row["entry"], "is_ai": False})

# AI signals in ticker
_ai_ticker = _get_ai_signals(days=1)[:3]

def _ti_ai(sig):
    return (f'<span style="margin:0 22px;white-space:nowrap;'
            f'background:linear-gradient(135deg,rgba(167,139,250,.12),rgba(236,72,153,.07));'
            f'border:1px solid rgba(167,139,250,.3);border-radius:5px;padding:2px 10px">'
            f'<span style="font-size:9px;font-weight:800;color:#a78bfa;letter-spacing:.06em">🤖 AI</span>'
            f'&nbsp;<span style="color:#c4b5fd;font-size:11px;font-weight:800">{sig.get("symbol","")}</span>'
            f'&nbsp;<span style="color:#475569;font-size:10px">₹{float(sig.get("price",0)):,.1f}</span>'
            f'&nbsp;<span style="font-size:9px;color:#7c3aed">▲</span>'
            f'</span>')

ticker_parts = []
if _iq:
    ticker_parts += [_ti_idx(r) for r in _iq]
    ticker_parts.append('<span style="margin:0 16px;color:#0f2035">│</span>')
if _fxc:
    ticker_parts += [_ti_forex(r) for r in _fxc]
if _ai_ticker:
    ticker_parts.append('<span style="margin:0 16px;color:#1a0a3a">│</span>')
    ticker_parts += [_ti_ai(s) for s in _ai_ticker]
if _sigs_ticker:
    ticker_parts.append('<span style="margin:0 16px;color:#0f2035">│</span>')
    ticker_parts += [_ti_signal(s) for s in _sigs_ticker]

if ticker_parts:
    # Duplicate items for seamless CSS loop
    ticker_inner = "".join(ticker_parts)
    ticker_html  = ticker_inner + ticker_inner  # duplicate for seamless loop
    n_items = len(ticker_parts)
    anim_dur = max(18, n_items * 3)  # scale speed to content length
    st.markdown(f"""
<style>
.ticker-wrap {{ overflow:hidden; flex:1; }}
.ticker-track {{
  display:inline-flex; align-items:center; white-space:nowrap;
  animation:tickerScroll {anim_dur}s linear infinite; }}
.ticker-track:hover {{ animation-play-state:paused; }}
</style>
<div style="background:linear-gradient(90deg,rgba(3,6,14,.97),rgba(5,12,24,.97));
  border:1px solid rgba(56,189,248,.1);border-radius:10px;padding:0;margin-bottom:14px;
  overflow:hidden;backdrop-filter:blur(16px);position:relative">
  <div style="position:absolute;top:0;left:0;right:0;height:1px;
    background:linear-gradient(90deg,transparent,rgba(56,189,248,.4),rgba(167,139,250,.3),transparent)"></div>
  <div style="position:absolute;bottom:0;left:0;right:0;height:1px;
    background:linear-gradient(90deg,transparent,rgba(56,189,248,.15),transparent)"></div>
  <div style="display:flex;align-items:stretch">
    <div style="padding:0 14px;border-right:1px solid rgba(56,189,248,.08);
      display:flex;align-items:center;gap:6px;flex-shrink:0;
      background:linear-gradient(135deg,rgba(56,189,248,.06),rgba(167,139,250,.04))">
      <span class="live"></span>
      <span style="font-size:9px;font-weight:800;color:#22c55e;letter-spacing:.12em;text-transform:uppercase;line-height:1">Live</span>
    </div>
    <div class="ticker-wrap" style="padding:9px 0">
      <div class="ticker-track">{ticker_html}</div>
    </div>
    <div style="padding:0 12px;border-left:1px solid rgba(56,189,248,.08);
      display:flex;align-items:center;flex-shrink:0;background:rgba(0,0,0,.2)">
      <span style="font-size:8px;color:#1e3a5f;font-weight:600;letter-spacing:.06em">AUTO-REFRESH 60s</span>
    </div>
  </div>
</div>
""", unsafe_allow_html=True)

tab1, tab_ai, tab2, tab3, tab4, tab5, tab6, tab7 = st.tabs(["Signals", "🤖 AI Signals", "Breakouts", "F&O", "Mutual Funds", "Market News", "Performance", "History"])


# ══════════════════════════════════════════════════════════════════════════════
# TAB 1 — SIGNALS (read-only from DB)
# ══════════════════════════════════════════════════════════════════════════════
with tab1:
    signals = get_signals_display(days=_days, min_score=min_score) if IS_LOCAL else _gh_signals_display(days=_days, min_score=min_score)

    if not signals:
        st.markdown(f'<div style="text-align:center;padding:50px 0"><div style="font-size:36px">📡</div><div style="margin-top:8px;font-size:13px;color:#334155">No signals in last {_days} day(s) above score {min_score}.<br>Auto-scans: 9:20 AM · 11:45 AM · 4:30 PM IST</div></div>', unsafe_allow_html=True)
    else:
        # KPI
        c = st.columns(5)
        c[0].metric("Active Signals", len(signals))
        c[1].metric("Top Score",  f"{signals[0]['score']}/100")
        c[2].metric("Avg Score",  f"{round(sum(s['score'] for s in signals)/len(signals),1)}")
        c[3].metric("F&O Ready",  sum(1 for s in signals if s.get("fno_eligible")))
        rr_vals = [s['rr1'] for s in signals if s['rr1'] > 0]
        c[4].metric("Avg RR", f"1:{round(sum(rr_vals)/len(rr_vals),1)}" if rr_vals else "—")

        st.markdown("---")
        sort_by = st.selectbox("Sort by", ["score","rr1","vol_ratio"], index=0)
        sigs_s  = sorted(signals, key=lambda x: x.get(sort_by, 0) or 0, reverse=True)

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
        df_s = pd.DataFrame(sigs_s)
        fig  = px.bar(df_s, x="symbol", y="score", color="score",
                      color_continuous_scale=["#0ea5e9","#22c55e"], range_color=[60,100])
        fig.update_layout(height=180, paper_bgcolor="#070f1e", plot_bgcolor="#050c18",
            font=dict(color="#64748b",size=10), xaxis=dict(gridcolor="#0f2035"),
            yaxis=dict(gridcolor="#0f2035",range=[50,100]),
            margin=dict(l=8,r=8,t=8,b=8), showlegend=False, coloraxis_showscale=False)
        st.plotly_chart(fig, use_container_width=True)
        st.download_button("Export CSV", df_s.to_csv(index=False), "signals.csv", "text/csv")


# ══════════════════════════════════════════════════════════════════════════════
# TAB AI — AI SIGNALS (TLM Trendline Channel Breakouts)
# ══════════════════════════════════════════════════════════════════════════════
with tab_ai:
    ai_sigs = _get_ai_signals(days=_days)

    # Header
    st.markdown("""
<div style="background:linear-gradient(135deg,rgba(10,7,24,.98),rgba(20,9,42,.98));
  border:1px solid rgba(167,139,250,.2);border-radius:16px;padding:18px 22px;margin-bottom:18px;
  position:relative;overflow:hidden">
  <div style="position:absolute;top:0;left:0;right:0;height:1px;
    background:linear-gradient(90deg,transparent,rgba(167,139,250,.6),rgba(236,72,153,.4),transparent)"></div>
  <div style="position:absolute;inset:0;pointer-events:none;
    background:radial-gradient(ellipse 45% 60% at 5% 50%,rgba(167,139,250,.06),transparent 60%)"></div>
  <div style="display:flex;align-items:center;gap:12px;margin-bottom:6px">
    <span style="font-size:20px;font-weight:900;color:#c4b5fd;letter-spacing:-.02em">AI Signal Detection</span>
    <span style="font-size:8px;font-weight:800;padding:3px 9px;border-radius:4px;
      background:rgba(167,139,250,.12);color:#a78bfa;border:1px solid rgba(167,139,250,.3);
      letter-spacing:.1em;text-transform:uppercase">TRENDLINE · CHANNEL · ML PATTERN</span>
  </div>
  <div style="font-size:11px;color:#4b3a7a;line-height:1.5">
    OLS regression trendline channel · Pivot high/low detection · Breakout above upper band + volume confirmation
    <br>Auto-scans: 9:20 AM (4H) · 11:45 AM (4H) · 4:30 PM (Daily EOD)
  </div>
</div>
""", unsafe_allow_html=True)

    if not ai_sigs:
        st.markdown("""
<div style="text-align:center;padding:60px 0">
  <div style="font-size:40px;margin-bottom:12px">🤖</div>
  <div style="font-size:14px;color:#4b3a7a;font-weight:600">No AI channel breakouts detected</div>
  <div style="font-size:11px;color:#2d1a55;margin-top:6px">Next scan: 9:20 AM IST (4H) · 4:30 PM IST (Daily EOD)</div>
  <div style="font-size:10px;color:#1a0a2e;margin-top:4px">Signals fire when price breaks OLS regression upper band with vol surge</div>
</div>
""", unsafe_allow_html=True)
    else:
        # KPI row
        kc = st.columns(4)
        kc[0].metric("AI Signals", len(ai_sigs))
        rr_ai = [float(s.get("rr",0)) for s in ai_sigs if s.get("rr",0)]
        kc[1].metric("Avg RR", f"1:{round(sum(rr_ai)/len(rr_ai),1)}" if rr_ai else "—")
        fno_ai = sum(1 for s in ai_sigs if s.get("fno"))
        kc[2].metric("F&O Ready", fno_ai)
        vol_ai = [float(s.get("vol_ratio",1)) for s in ai_sigs]
        kc[3].metric("Avg Vol Surge", f"{round(sum(vol_ai)/len(vol_ai),1)}x" if vol_ai else "—")

        st.markdown("---")

        for b in ai_sigs:
            sym      = b.get("symbol","")
            price    = float(b.get("price", 0))
            sl       = float(b.get("sl", 0))
            t1       = float(b.get("target1", 0))
            t2       = float(b.get("target2", 0))
            t3       = float(b.get("target3", t2))
            rr       = b.get("rr", 0)
            vol_r    = float(b.get("vol_ratio", 1))
            tf       = b.get("timeframe", "4H")
            upper_b  = float(b.get("upper_band", price))
            lower_b  = float(b.get("lower_band", sl))
            ch_w     = float(b.get("channel_width", upper_b - lower_b))
            fno_b    = '<span class="ai-badge" style="font-size:8px;padding:2px 7px;margin-left:4px">F&amp;O</span>' if b.get("fno") else ""
            tv_link  = b.get("tv_link") or f"https://in.tradingview.com/chart/?symbol=NSE:{sym}"
            pats     = b.get("pattern","TL Channel Breakout")
            risk     = max(price - sl, 0.01)
            # Channel fill % (where is price relative to channel)
            ch_pos_pct = min(100, max(0, round((price - lower_b) / max(ch_w, 0.01) * 100, 0))) if ch_w > 0 else 80

            # Breakout % above upper band
            bo_pct = round((price - upper_b) / upper_b * 100, 2) if upper_b > 0 else 0

            st.markdown(f"""
<div class="ai-card">
  <div class="ai-scan"></div>

  <!-- Header -->
  <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:10px">
    <div>
      <div style="display:flex;align-items:center;gap:8px;margin-bottom:4px">
        <span style="font-size:22px;font-weight:900;color:#f1f5f9;letter-spacing:-.02em">{sym}</span>
        <span class="ai-badge"><span class="neural-dot"></span>AI DETECTED</span>
        {fno_b}
      </div>
      <div style="font-size:10px;color:#4b3a7a">{pats} &nbsp;·&nbsp; {tf} &nbsp;·&nbsp; NSE Equity</div>
    </div>
    <div style="text-align:right">
      <div style="font-size:28px;font-weight:900;color:#a78bfa;font-family:'JetBrains Mono',monospace;line-height:1;
        text-shadow:0 0 20px rgba(167,139,250,.4);animation:numberFlip .4s ease">{'+' if bo_pct>=0 else ''}{bo_pct}%</div>
      <div style="font-size:8px;color:#4b3a7a;text-transform:uppercase;letter-spacing:.1em;margin-top:2px">above upper band</div>
      <div style="font-size:10px;font-weight:700;color:#c4b5fd;margin-top:4px">Vol {vol_r:.1f}× avg</div>
    </div>
  </div>

  <!-- Channel visualisation -->
  <div class="ai-channel">
    <div class="ai-channel-label">🔮 OLS REGRESSION CHANNEL</div>
    <div class="ai-channel-band">
      <span class="ai-channel-upper">▲ Upper: ₹{upper_b:,.2f}</span>
      <span class="ai-channel-width">Width: {ch_w:.1f} pts</span>
      <span class="ai-channel-lower">▼ Lower: ₹{lower_b:,.2f}</span>
    </div>
    <!-- channel position bar -->
    <div style="margin-top:8px;height:6px;background:linear-gradient(90deg,rgba(124,58,237,.2),rgba(167,139,250,.15),rgba(236,72,153,.1));
      border-radius:3px;position:relative;overflow:visible">
      <div style="position:absolute;height:12px;width:3px;top:-3px;background:#a78bfa;border-radius:2px;
        left:calc({min(95,ch_pos_pct)}% - 1px);box-shadow:0 0 8px rgba(167,139,250,.8)"></div>
      <div style="position:absolute;right:-2px;top:-3px;height:12px;width:3px;background:rgba(167,139,250,.3);border-radius:2px"></div>
    </div>
    <div style="display:flex;justify-content:space-between;margin-top:4px;font-size:8px;color:#2d1a55">
      <span>Lower Band</span><span style="color:#a78bfa;font-weight:700">▲ BREAKOUT (CURRENT ₹{price:,.2f})</span><span>Upper Band</span>
    </div>
  </div>

  <!-- Trade structure -->
  <div style="font-size:8px;font-weight:800;color:#4b3a7a;text-transform:uppercase;letter-spacing:.14em;margin-bottom:8px">TRADE STRUCTURE</div>
  <div class="tgrid">
    <div class="tgcell" style="border-color:rgba(167,139,250,.2)">
      <div class="tc-label">ENTRY (CURRENT)</div>
      <div class="tc-val" style="color:#c4b5fd">₹{price:,.2f}</div>
    </div>
    <div class="tgcell" style="border-color:rgba(167,139,250,.2)">
      <div class="tc-label">TIMEFRAME</div>
      <div class="tc-val" style="color:#a78bfa">{tf}</div>
    </div>
    <div class="tgcell sl">
      <div class="tc-label">STOP LOSS</div>
      <div class="tc-val" style="color:#ef4444">₹{sl:,.2f}</div>
    </div>
    <div class="tgcell t1">
      <div class="tc-label">TARGET 1</div>
      <div class="tc-val" style="color:#22c55e">₹{t1:,.2f}</div>
    </div>
  </div>

  <div class="row" style="margin-top:4px">
    <div class="kv"><span>Risk/Reward</span><span class="blue">1:{rr}</span></div>
    <div class="kv"><span>Target 2</span><span class="green">₹{t2:,.1f}</span></div>
    <div class="kv"><span>Target 3</span><span class="green">₹{t3:,.1f}</span></div>
    <div class="kv"><span>Risk pts</span><span class="red">₹{risk:,.1f}</span></div>
  </div>

  <!-- Footer -->
  <div style="display:flex;justify-content:space-between;align-items:center;margin-top:14px;
    padding-top:10px;border-top:1px solid rgba(167,139,250,.1)">
    <span style="font-size:9px;color:#2d1a55">AI pattern detection · OLS regression · Not SEBI advice</span>
    <a href="{tv_link}" target="_blank"
      style="color:#a78bfa;font-size:11px;font-weight:700;text-decoration:none">Chart →</a>
  </div>
</div>
""", unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════════════════════
# TAB 2 — BREAKOUTS (read from DB)
# ══════════════════════════════════════════════════════════════════════════════
with tab2:
    st.markdown('<div style="font-size:13px;font-weight:700;color:#22c55e;margin-bottom:4px">Confirmed Breakouts</div><div style="font-size:11px;color:#334155;margin-bottom:14px">Daily · Weekly · Monthly — auto-updated 4:30 PM IST</div>', unsafe_allow_html=True)

    breakouts_df = _bos_df  # already fetched above
    if breakouts_df.empty:
        st.markdown('<div style="text-align:center;padding:40px 0"><div style="font-size:32px">📋</div><div style="font-size:13px;color:#334155;margin-top:8px">No breakouts in DB yet.<br>Auto-scan runs 4:30 PM IST on trading days.</div></div>', unsafe_allow_html=True)
    else:
        bos_list = breakouts_df.to_dict("records")
        tfc = {}
        for b in bos_list: tfc[b.get("timeframe","Daily")] = tfc.get(b.get("timeframe","Daily"),0)+1
        c1,c2,c3,c4 = st.columns(4)
        c1.metric("Total",   len(bos_list))
        c2.metric("Monthly", tfc.get("Monthly",0))
        c3.metric("Weekly",  tfc.get("Weekly",0))
        c4.metric("Daily",   tfc.get("Daily",0))
        st.markdown("---")
        tf_f = st.selectbox("Filter", ["All","Monthly","Weekly","Daily"])
        fil  = [b for b in bos_list if tf_f=="All" or b.get("timeframe")==tf_f]
        for b in fil:
            tf   = b.get("timeframe","Daily")
            cls  = {"Monthly":"monthly","Weekly":"weekly","Daily":""}.get(tf,"")
            tfc2 = {"Monthly":"#a78bfa","Weekly":"#f59e0b","Daily":"#22c55e"}.get(tf,"#22c55e")
            fno_b = '<span class="badge fno">F&amp;O</span>' if b.get("fno") else ""
            raw_pats = b.get("patterns", [])
            if isinstance(raw_pats, str):
                import json as _json
                try: raw_pats = _json.loads(raw_pats)
                except: raw_pats = []
            pats = " · ".join(raw_pats) if raw_pats else b.get("pattern","")
            tv_link = b.get("tv_link") or f"https://in.tradingview.com/chart/?symbol=NSE:{b['symbol']}"
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
  <div style="margin-top:8px"><a href="{tv_link}" target="_blank" style="color:#38bdf8;font-size:11px;font-weight:600;text-decoration:none">Chart →</a></div>
</div>
""", unsafe_allow_html=True)

    # 4H section — from DB
    st.markdown("---")
    st.markdown('<div style="font-size:13px;font-weight:700;color:#f59e0b;margin-bottom:4px">⚡ 4H Early-Entry Signals</div><div style="font-size:11px;color:#334155;margin-bottom:14px">RSI crossing 55 + Volume surge — auto-updated 9:20 AM & 11:45 AM IST</div>', unsafe_allow_html=True)
    df_4h = get_4h_signals(days=_days) if IS_LOCAL else _gh_4h_signals(days=_days)
    if df_4h.empty:
        st.info("No 4H signals today. Next auto-scan: 9:20 AM IST.")
    else:
        sigs_4h = df_4h.to_dict("records")
        cc1, cc2 = st.columns(2)
        cc1.metric("4H Signals", len(sigs_4h))
        cc2.metric("Avg Vol", f"{round(sum(float(s.get('vol_ratio',1)) for s in sigs_4h)/len(sigs_4h),1)}x")
        for b in sigs_4h:
            fno_b = '<span class="badge fno">F&amp;O</span>' if b.get("fno") else ""
            tv4 = b.get("tv_link") or f"https://in.tradingview.com/chart/?symbol=NSE:{b['symbol']}"
            st.markdown(f"""
<div class="bo-card" style="border-color:#f59e0b40">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
    <div style="display:flex;align-items:center;gap:8px">
      <span style="font-size:16px;font-weight:800;color:#f1f5f9">{b['symbol']}</span>{fno_b}
    </div>
    <span style="font-size:10px;font-weight:700;color:#f59e0b;padding:2px 8px;border-radius:99px;border:1px solid #f59e0b40">4H · EARLY</span>
  </div>
  <div style="font-size:10px;color:#94a3b8;margin-bottom:8px">{b.get('reason','RSI 55 cross + Volume surge')}</div>
  <div class="row">
    <div class="kv"><span>Price</span><span>₹{float(b['price']):,.2f}</span></div>
    <div class="kv"><span>Stop</span><span class="red">₹{float(b['sl']):,.2f}</span></div>
    <div class="kv"><span>T1</span><span class="green">₹{float(b['target1']):,.2f}</span></div>
    <div class="kv"><span>T2</span><span class="green">₹{float(b['target2']):,.2f}</span></div>
    <div class="kv"><span>RR</span><span class="blue">1:{b['rr']}</span></div>
    <div class="kv"><span>Vol</span><span>{b.get('vol_ratio',0)}x</span></div>
  </div>
  <div style="margin-top:8px"><a href="{tv4}" target="_blank" style="color:#38bdf8;font-size:11px;font-weight:600;text-decoration:none">Chart →</a></div>
</div>
""", unsafe_allow_html=True)

    # Commodity signals — from DB
    st.markdown("---")
    st.markdown('<div style="font-size:13px;font-weight:700;color:#fbbf24;margin-bottom:4px">🥇 Commodity Signals</div><div style="font-size:11px;color:#334155;margin-bottom:14px">Gold · Silver · Crude Oil · Nat Gas — Global futures</div>', unsafe_allow_html=True)
    df_comm = get_commodity_signals(days=_days) if IS_LOCAL else _gh_commodity_signals(days=_days)
    if df_comm.empty:
        st.info("No commodity signals today. Next auto-scan: 9:20 AM IST.")
    else:
        for b in df_comm.to_dict("records"):
            action = b.get("action","BUY")
            ac  = "#22c55e" if action == "BUY" else "#ef4444"
            arr = "▲ BUY" if action == "BUY" else "▼ SELL"
            st.markdown(f"""
<div class="bo-card" style="border-left-color:{ac}">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
    <div>
      <span style="font-size:16px;font-weight:800;color:#f1f5f9">{b['symbol']}</span>
      <span style="font-size:10px;color:#475569;margin-left:8px">{b.get('label','')}</span>
    </div>
    <div style="display:flex;gap:8px;align-items:center">
      <span style="font-size:11px;font-weight:800;color:{ac}">{arr}</span>
      <span style="font-size:9px;color:#475569;padding:2px 7px;border:1px solid #0f2035;border-radius:4px">{b.get('timeframe','Daily')}</span>
    </div>
  </div>
  <div class="row">
    <div class="kv"><span>Price</span><span>₹{float(b['price']):,.2f}</span></div>
    <div class="kv"><span>Stop</span><span class="red">{float(b['sl']):,.2f}</span></div>
    <div class="kv"><span>T1</span><span class="green">{float(b['target1']):,.2f}</span></div>
    <div class="kv"><span>T2</span><span class="green">{float(b['target2']):,.2f}</span></div>
    <div class="kv"><span>RR</span><span class="blue">1:{b['rr']}</span></div>
    <div class="kv"><span>RSI</span><span>{b.get('rsi',0)}</span></div>
  </div>
</div>
""", unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════════════════════
# TAB 3 — F&O
# ══════════════════════════════════════════════════════════════════════════════
with tab3:
    st.markdown('<div style="font-size:13px;font-weight:700;color:#38bdf8;margin-bottom:4px">F&O Trade Suggestions</div><div style="font-size:11px;color:#334155;margin-bottom:14px">Nifty 200 stocks · Verify premium &amp; IV on NSE before trading</div>', unsafe_allow_html=True)

    fno_sigs = [s for s in signals if s.get("fno_eligible") and s.get("fno_suggestion")]

    if not signals:
        st.info("No swing signals in DB yet. Auto-scan: 11:45 AM IST.")
    elif not fno_sigs:
        st.warning(f"Scan found {len(signals)} signals but none are F&O eligible today.")
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

    # ── Swing Signal Performance ──────────────────────────────────────────────
    st.markdown('<div style="font-size:13px;font-weight:700;color:#38bdf8;margin-bottom:12px">Swing Signal Performance</div>', unsafe_allow_html=True)
    if perf:
        p = st.columns(5)
        p[0].metric("Total Signals",   perf["total"])
        p[1].metric("Win Rate",        f"{perf['win_rate']}%")
        p[2].metric("Avg P&L",         f"{perf['avg_pnl']}%")
        p[3].metric("Best",            f"+{perf['best']}%")
        p[4].metric("Worst",           f"{perf['worst']}%")
        hist   = get_history()
        closed = hist[hist["status"] != "OPEN"]
        if not closed.empty:
            fig = px.bar(closed, x="symbol", y="pnl_pct", color="pnl_pct",
                         color_continuous_scale=["#ef4444","#0f2035","#22c55e"],
                         range_color=[-20,20], title="Closed Trade P&L (%)")
            fig.update_layout(paper_bgcolor="#070f1e", plot_bgcolor="#050c18",
                font=dict(color="#64748b",size=10), xaxis=dict(gridcolor="#0f2035"),
                yaxis=dict(gridcolor="#0f2035"), height=320,
                margin=dict(l=8,r=8,t=32,b=8), coloraxis_showscale=False, showlegend=False,
                title_font=dict(color="#475569",size=11))
            st.plotly_chart(fig, use_container_width=True)
        # Setup breakdown
        if perf.get("by_setup"):
            st.markdown('<div style="font-size:11px;font-weight:700;color:#334155;margin:8px 0 6px">P&L by Setup Type</div>', unsafe_allow_html=True)
            sd_cols = st.columns(len(perf["by_setup"]))
            for i, (k, v) in enumerate(perf["by_setup"].items()):
                c = "#4ade80" if v >= 0 else "#f87171"
                sd_cols[i].markdown(f'<div style="background:#0a1929;border:1px solid #0f2d4a;border-radius:8px;padding:10px;text-align:center"><div style="font-size:9px;color:#334155;text-transform:uppercase;letter-spacing:.07em;margin-bottom:3px">{k}</div><div style="font-size:15px;font-weight:800;color:{c};font-family:JetBrains Mono,monospace">{v:+.2f}%</div></div>', unsafe_allow_html=True)
    else:
        st.info("No closed swing trades yet. Outcomes tracked automatically each day.")

    st.markdown("---")

    # ── AI Signal Performance ─────────────────────────────────────────────────
    st.markdown("""
<div style="background:linear-gradient(135deg,rgba(10,7,24,.97),rgba(20,9,42,.97));
  border:1px solid rgba(167,139,250,.15);border-radius:14px;padding:16px 20px;margin-bottom:16px;position:relative;overflow:hidden">
  <div style="position:absolute;top:0;left:0;right:0;height:1px;
    background:linear-gradient(90deg,transparent,rgba(167,139,250,.5),transparent)"></div>
  <div style="font-size:13px;font-weight:700;color:#c4b5fd;margin-bottom:4px">🤖 AI Signal Performance</div>
  <div style="font-size:11px;color:#4b3a7a">Trendline channel breakout signals — tracked from breakouts table</div>
</div>
""", unsafe_allow_html=True)

    ai_all = _get_ai_signals(days=30)  # last 30 days
    if not ai_all:
        st.markdown('<div style="background:rgba(10,7,24,.9);border:1px solid rgba(167,139,250,.1);border-radius:10px;padding:20px;text-align:center;color:#4b3a7a;font-size:12px">No AI signals in last 30 days. Signals appear here once auto-scan detects trendline breakouts.</div>', unsafe_allow_html=True)
    else:
        ai_pc = st.columns(4)
        ai_rr_vals = [float(s.get("rr",0)) for s in ai_all if s.get("rr",0)]
        ai_vol_vals = [float(s.get("vol_ratio",1)) for s in ai_all]
        ai_fno_ct   = sum(1 for s in ai_all if s.get("fno"))
        ai_tf_4h    = sum(1 for s in ai_all if str(s.get("timeframe","")) == "4H")
        ai_tf_d     = sum(1 for s in ai_all if str(s.get("timeframe","")) == "Daily")
        ai_pc[0].metric("Total AI Signals", len(ai_all))
        ai_pc[1].metric("Avg RR", f"1:{round(sum(ai_rr_vals)/len(ai_rr_vals),2)}" if ai_rr_vals else "—")
        ai_pc[2].metric("F&O Eligible", ai_fno_ct)
        ai_pc[3].metric("Avg Vol Surge", f"{round(sum(ai_vol_vals)/len(ai_vol_vals),1)}x" if ai_vol_vals else "—")

        # Timeframe split
        st.markdown(f"""
<div style="display:flex;gap:12px;margin:12px 0">
  <div style="background:rgba(167,139,250,.06);border:1px solid rgba(167,139,250,.12);border-radius:8px;padding:10px 16px;flex:1;text-align:center">
    <div style="font-size:9px;color:#4b3a7a;text-transform:uppercase;letter-spacing:.08em;margin-bottom:4px">4H Signals</div>
    <div style="font-size:22px;font-weight:800;color:#a78bfa;font-family:JetBrains Mono,monospace">{ai_tf_4h}</div>
  </div>
  <div style="background:rgba(167,139,250,.06);border:1px solid rgba(167,139,250,.12);border-radius:8px;padding:10px 16px;flex:1;text-align:center">
    <div style="font-size:9px;color:#4b3a7a;text-transform:uppercase;letter-spacing:.08em;margin-bottom:4px">Daily EOD</div>
    <div style="font-size:22px;font-weight:800;color:#c4b5fd;font-family:JetBrains Mono,monospace">{ai_tf_d}</div>
  </div>
  <div style="background:rgba(167,139,250,.06);border:1px solid rgba(167,139,250,.12);border-radius:8px;padding:10px 16px;flex:1;text-align:center">
    <div style="font-size:9px;color:#4b3a7a;text-transform:uppercase;letter-spacing:.08em;margin-bottom:4px">F&O Ready</div>
    <div style="font-size:22px;font-weight:800;color:#e879f9;font-family:JetBrains Mono,monospace">{ai_fno_ct}</div>
  </div>
</div>
""", unsafe_allow_html=True)

        # Signal history table
        if ai_all:
            ai_df = pd.DataFrame([{
                "Date":      s.get("date",""),
                "Symbol":    s.get("symbol",""),
                "TF":        s.get("timeframe",""),
                "Entry":     f"₹{float(s.get('price',0)):,.2f}",
                "Upper Band":f"₹{float(s.get('upper_band', s.get('price',0))):,.2f}",
                "SL":        f"₹{float(s.get('sl',0)):,.2f}",
                "T1":        f"₹{float(s.get('target1',0)):,.2f}",
                "RR":        f"1:{s.get('rr',0)}",
                "Vol":       f"{float(s.get('vol_ratio',1)):.1f}x",
                "F&O":       "✓" if s.get("fno") else "—",
            } for s in ai_all[:20]])
            st.dataframe(ai_df, use_container_width=True, hide_index=True)
            st.download_button("Export AI Signals CSV",
                               ai_df.to_csv(index=False), "ai_signals.csv", "text/csv",
                               key="dl_ai")

        # RR distribution chart
        if len(ai_rr_vals) >= 3:
            fig_ai = go.Figure()
            fig_ai.add_trace(go.Bar(
                x=list(range(len(ai_rr_vals))), y=ai_rr_vals,
                marker_color="#a78bfa", name="RR Ratio",
                hovertemplate="RR: 1:%{y}<extra></extra>"
            ))
            fig_ai.add_hline(y=2.0, line_color="#22c55e", line_dash="dash",
                             annotation_text="Min 2:1", annotation_font_color="#22c55e")
            fig_ai.update_layout(
                paper_bgcolor="#07030e", plot_bgcolor="#0a0514",
                font=dict(color="#64748b",size=10),
                xaxis=dict(gridcolor="#1a0a2e",showticklabels=False),
                yaxis=dict(gridcolor="#1a0a2e",title="R:R Ratio"),
                height=200, margin=dict(l=8,r=8,t=8,b=8),
                showlegend=False, title_text=""
            )
            st.plotly_chart(fig_ai, use_container_width=True)


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
