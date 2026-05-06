import streamlit as st
import streamlit.components.v1 as _stc   # for TradingView widget embed
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
        return pd.DataFrame(data)  # return everything, no filter
    cutoff = str(date.today() - timedelta(days=days))
    rows = [r for r in data if r.get("date","") >= cutoff]
    return pd.DataFrame(rows)

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
                         get_corporate_actions, get_fund_holdings,
                         )

st.set_page_config(
    page_title="TradeFlow AI Pro — NSE Nifty 500 Swing Scanner",
    layout="wide", page_icon="⚡",
    initial_sidebar_state="expanded",
)
IST = pytz.timezone("Asia/Kolkata")
# True only when running on developer's own machine — NOT on Streamlit Cloud or CI.
# Streamlit Cloud always mounts repo at /mount/src/; GitHub Actions sets GITHUB_ACTIONS=true.
IS_LOCAL = (
    not os.path.exists("/mount/src") and
    os.getenv("GITHUB_ACTIONS") != "true" and
    os.getenv("STREAMLIT_SHARING_MODE") != "true"
)
try:
    init_db()
except Exception as _db_err:
    # Non-fatal on cloud — dashboard reads from GitHub JSON, not local DB
    import logging as _log
    _log.getLogger(__name__).warning(f"init_db skipped: {_db_err}")

# ── Semantic color palette (audit fix — consistent across all charts) ─────────
CLR_UP      = "#26A69A"   # profit / bullish
CLR_DOWN    = "#EF5350"   # loss / bearish
CLR_NEUTRAL = "#607D8B"   # neutral
CLR_ACCENT  = "#22c55e"   # emerald brand accent
CLR_BG      = "#0d1117"
CLR_BG2     = "#111827"

# ── Mobile warning (audit fix §5) ────────────────────────────────────────────
st.markdown("""
<div id="mobile-warn" style="display:none;background:#1e1208;border:1px solid #92400e;
  border-radius:8px;padding:10px 14px;margin-bottom:10px;font-size:12px;color:#fbbf24">
  📱 <b>Best on desktop.</b> Charts and signal cards are optimised for wide screens.
</div>
<script>
if(window.innerWidth < 768){document.getElementById('mobile-warn').style.display='block';}
</script>
""", unsafe_allow_html=True)

# ── Disclaimer banner (audit fix §5 — legal credibility) ─────────────────────
st.markdown("""
<div style="background:rgba(17,24,39,.7);border:1px solid rgba(245,158,11,.2);border-radius:8px;
  padding:8px 16px;margin-bottom:8px;display:flex;align-items:center;gap:12px;flex-wrap:wrap">
  <span style="font-size:9px;font-weight:800;color:#f59e0b;letter-spacing:.1em;
    text-transform:uppercase;white-space:nowrap">⚠ Disclaimer</span>
  <span style="font-size:10px;color:#64748b;line-height:1.5">
    Signals are generated algorithmically for <b style="color:#94a3b8">educational &amp; research purposes only</b>.
    Not SEBI-registered. Not financial advice. Data via Yahoo Finance (15-min delay during market hours).
    Past performance does not guarantee future results. Trade at your own risk.
  </span>
</div>
""", unsafe_allow_html=True)

# ── MiroFish terminal theme vars ──────────────────────────────────────────────
# Daily accent rotation (7 day cycle — Mon→Sun)
_THEME_VARS = """
:root {
  --bg:        #000000;
  --bg2:       #080808;
  --bg3:       #0e0e0e;
  --border:    #181818;
  --border2:   #111111;
  --txt:       #e8e8e8;
  --txt2:      #999999;
  --txt3:      #555555;
  --txt4:      #333333;
  --accent:    #00ff88;
  --green:     #00ff88;
  --red:       #ff3b3b;
  --amber:     #ffaa00;
  --purple:    #b48aff;
  --blue:      #4da6ff;
  --card-bg:       #0a0a0a;
  --card-border:   rgba(0,255,136,0.18);
  --card-shadow:   rgba(0,255,136,0.06);
  --header-bg:     rgba(5,5,5,0.97);
  --font-mono: 'JetBrains Mono', 'Fira Code', 'Courier New', monospace;
  --font-sans: 'Inter', system-ui, sans-serif;
  /* Daily accent rotates via JS */
  --daily-accent: #00ff88;
}
/* LIGHT mode — toggled via .light-app class */
[data-testid="stApp"].light-app {
  --bg:        #f8fafc;
  --bg2:       #ffffff;
  --bg3:       #f1f5f9;
  --border:    #e2e8f0;
  --border2:   #cbd5e1;
  --txt:       #0f172a;
  --txt2:      #334155;
  --txt3:      #64748b;
  --txt4:      #94a3b8;
  --accent:    #16a34a;
  --green:     #16a34a;
  --red:       #dc2626;
  --amber:     #d97706;
  --card-bg:       #ffffff;
  --card-border:   rgba(22,163,74,0.22);
  --card-shadow:   rgba(22,163,74,0.07);
  --header-bg:     rgba(248,250,252,0.97);
}
"""

st.markdown(f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@300;400;500;600;700;800&family=Inter:wght@300;400;500;600;700;800;900&display=swap');
{_THEME_VARS}

/* === KEYFRAMES === */
@keyframes fadeUp   {{ from {{ opacity:0; transform:translateY(20px); }} to {{ opacity:1; transform:translateY(0); }} }}
@keyframes fadeIn   {{ from {{ opacity:0; }} to {{ opacity:1; }} }}
@keyframes cardEnter {{ 0% {{ opacity:0; transform:translateY(24px) scale(.97); }} 100% {{ opacity:1; transform:translateY(0) scale(1); }} }}
@keyframes rowSlide {{ from {{ opacity:0; transform:translateX(-12px); }} to {{ opacity:1; transform:translateX(0); }} }}
@keyframes marquee  {{ 0% {{ transform:translateX(0); }} 100% {{ transform:translateX(-50%); }} }}
@keyframes pulseGlow {{ 0%,100% {{ opacity:1; }} 50% {{ opacity:.5; }} }}
@keyframes statusBlink {{ 0%,100% {{ opacity:1; }} 50% {{ opacity:.2; }} }}
@keyframes numberFlip {{ 0% {{ opacity:0; transform:translateY(-10px); }} 100% {{ opacity:1; transform:translateY(0); }} }}
@keyframes scanLine  {{ 0% {{ transform:translateX(-100%); }} 100% {{ transform:translateX(300%); }} }}
@keyframes scanDiag  {{ 0% {{ transform:translateX(-100%) translateY(-100%); }} 100% {{ transform:translateX(200%) translateY(200%); }} }}
@keyframes neuralPulse {{ 0%,100% {{ opacity:.3; transform:scale(1); }} 50% {{ opacity:.8; transform:scale(1.4); }} }}
@keyframes confFill  {{ from {{ width:0%; }} to {{ width:100%; }} }}
@keyframes tickerScroll {{ 0% {{ transform:translateX(0); }} 100% {{ transform:translateX(-50%); }} }}
@keyframes shimmer   {{ 0% {{ left:-120%; }} 100% {{ left:140%; }} }}
@keyframes glowPulse {{ 0%,100% {{ box-shadow:0 0 8px var(--accent); }} 50% {{ box-shadow:0 0 24px var(--accent), 0 0 48px rgba(0,255,136,.1); }} }}
@keyframes scanH     {{ 0% {{ top:-2px; }} 100% {{ top:102%; }} }}
@property --rot      {{ syntax:'<angle>'; inherits:false; initial-value:0deg; }}
@keyframes rotateBorder {{ to {{ --rot:360deg; }} }}

/* === BASE === */
html, body, [data-testid="stApp"] {{
  background: var(--bg) !important;
  color: var(--txt) !important;
  font-family: var(--font-mono) !important;
  -webkit-font-smoothing: antialiased;
}}
.stApp, [data-testid="stAppViewContainer"] {{ background: var(--bg) !important; }}

/* Subtle scanline texture */
[data-testid="stApp"]::after {{
  content:''; position:fixed; inset:0; pointer-events:none; z-index:0;
  background: repeating-linear-gradient(0deg, transparent, transparent 2px, rgba(0,0,0,.08) 2px, rgba(0,0,0,.08) 4px);
}}

/* Corner glow */
[data-testid="stApp"]::before {{
  content:''; position:fixed; inset:0; pointer-events:none; z-index:0;
  background:
    radial-gradient(ellipse 40% 30% at 0% 0%, rgba(0,255,136,.04) 0%, transparent 60%),
    radial-gradient(ellipse 30% 25% at 100% 100%, rgba(77,166,255,.03) 0%, transparent 60%);
}}

/* Block container */
.main .block-container {{ padding-top: 1rem !important; max-width: 1400px !important; }}

/* Theme toggle */
#theme-toggle {{
  position: fixed; top: 12px; right: 72px; z-index: 9999;
  background: var(--bg2); border: 1px solid var(--border);
  border-radius: 4px; padding: 4px 12px; cursor: pointer;
  font-size: 11px; font-weight: 700; color: var(--txt3);
  font-family: var(--font-mono); letter-spacing:.05em; text-transform:uppercase;
  transition: all .15s ease; user-select: none;
}}
#theme-toggle:hover {{ border-color: var(--accent); color: var(--accent); }}

/* Header */
header[data-testid="stHeader"] {{
  background: var(--header-bg) !important;
  border-bottom: 1px solid var(--border) !important;
}}

/* Sidebar — terminal column */
section[data-testid="stSidebar"] {{
  background: var(--bg2) !important;
  border-right: 1px solid var(--border) !important;
}}
section[data-testid="stSidebar"] > div {{ padding-top: 1rem; }}
section[data-testid="stSidebar"] label,
section[data-testid="stSidebar"] p,
section[data-testid="stSidebar"] span {{ color: var(--txt3) !important; font-size: 11px !important; }}
section[data-testid="stSidebar"] h1,h2,h3 {{ color: var(--txt2) !important; }}

/* === BUTTONS === */
.stButton > button {{
  background: transparent !important;
  color: var(--accent) !important;
  border: 1px solid var(--accent) !important;
  border-radius: 2px !important;
  font-weight: 700 !important;
  font-size: 11px !important;
  font-family: var(--font-mono) !important;
  letter-spacing: .08em !important;
  text-transform: uppercase !important;
  transition: all .15s ease !important;
  padding: 6px 16px !important;
}}
.stButton > button:hover {{
  background: rgba(0,255,136,.08) !important;
  box-shadow: 0 0 16px rgba(0,255,136,.2) !important;
}}

/* === TABS — terminal row === */
.stTabs [data-baseweb="tab-list"] {{
  background: var(--bg2);
  border-bottom: 1px solid var(--border);
  padding: 0 4px;
  gap: 0;
}}
.stTabs [data-baseweb="tab"] {{
  background: transparent;
  color: var(--txt3) !important;
  font-size: 10px; font-weight: 700;
  font-family: var(--font-mono) !important;
  padding: 10px 14px;
  border-bottom: 2px solid transparent;
  border-radius: 0;
  letter-spacing: .1em; text-transform: uppercase;
  transition: color .15s;
}}
.stTabs [data-baseweb="tab"]:hover {{ color: var(--txt2) !important; }}
.stTabs [aria-selected="true"] {{
  color: var(--accent) !important;
  border-bottom: 2px solid var(--accent) !important;
  font-weight: 900 !important;
}}

/* === METRICS — terminal stat blocks === */
[data-testid="metric-container"] {{
  background: var(--bg2);
  border: 1px solid var(--border);
  border-top: 2px solid var(--accent);
  border-radius: 2px;
  padding: 14px 18px;
  animation: cardEnter .4s ease both;
  position: relative; overflow: hidden;
}}
[data-testid="metric-container"]::after {{
  content:''; position:absolute; top:0; left:0; right:0; height:1px;
  background: linear-gradient(90deg, var(--accent), transparent);
}}
[data-testid="metric-container"]:hover {{ border-color: var(--accent); }}
[data-testid="metric-container"] label {{
  color: var(--txt3) !important; font-size: 9px !important;
  text-transform: uppercase; letter-spacing: .14em; font-weight: 600;
  font-family: var(--font-mono) !important;
}}
[data-testid="metric-container"] [data-testid="stMetricValue"] {{
  color: var(--txt) !important; font-size: 28px !important;
  font-weight: 800 !important; font-family: var(--font-mono) !important;
  letter-spacing: -.04em; animation: numberFlip .4s ease both;
}}
[data-testid="stMetricDelta"] {{ font-size: 11px !important; font-weight: 700 !important; font-family: var(--font-mono) !important; }}

/* === DATAFRAMES — terminal table === */
.stDataFrame {{ border: 1px solid var(--border) !important; border-radius: 2px; overflow: hidden; }}
.stDataFrame thead th {{
  background: var(--bg2) !important; color: var(--accent) !important;
  font-size: 9px !important; text-transform: uppercase; letter-spacing: .12em;
  font-weight: 800; font-family: var(--font-mono) !important;
  border-color: var(--border2) !important; padding: 10px 14px !important;
}}
.stDataFrame tbody tr {{ background: var(--bg) !important; border-color: var(--border2) !important; animation: rowSlide .3s ease both; }}
.stDataFrame tbody tr:hover {{ background: var(--bg2) !important; }}
.stDataFrame tbody td {{
  color: var(--txt2) !important; font-family: var(--font-mono) !important;
  font-size: 12px !important; border-color: var(--border2) !important; padding: 8px 14px !important;
}}

/* === INPUTS === */
.stTextInput input, .stSelectbox [data-baseweb="select"] {{
  background: var(--bg2) !important; border: 1px solid var(--border) !important;
  color: var(--txt) !important; border-radius: 2px !important;
  font-family: var(--font-mono) !important; font-size: 12px !important;
}}
.stTextInput input:focus {{ border-color: var(--accent) !important; box-shadow: 0 0 0 2px rgba(0,255,136,.1) !important; }}

/* === EXPANDERS === */
.streamlit-expanderHeader {{
  background: var(--bg2) !important; border: 1px solid var(--border) !important;
  border-radius: 2px !important; color: var(--txt3) !important;
  font-size: 11px !important; font-weight: 700 !important; font-family: var(--font-mono) !important;
}}
.streamlit-expanderHeader:hover {{ border-color: var(--accent) !important; color: var(--txt2) !important; }}
.streamlit-expanderContent {{ background: var(--bg2) !important; border: 1px solid var(--border) !important; border-top: none !important; }}

/* ==========================================
   SIGNAL CARDS — terminal style
   ========================================== */
.card {{
  background: var(--card-bg);
  border: 1px solid var(--border);
  border-left: 3px solid var(--accent);
  border-radius: 2px;
  padding: 14px 16px;
  margin-bottom: 10px;
  position: relative; overflow: hidden;
  animation: cardEnter .35s cubic-bezier(.22,1,.36,1) both;
  transition: border-color .2s, box-shadow .2s;
}}
.card::before {{
  content:''; position:absolute; top:0; left:0; right:0; height:1px;
  background: linear-gradient(90deg, var(--accent), transparent 40%);
  opacity: .5;
}}
/* scan line sweep on hover */
.card::after {{
  content:''; position:absolute; top:0; left:0; right:0; height:2px;
  background: linear-gradient(90deg, transparent, var(--accent), transparent);
  animation: scanLine 2.5s ease-in-out infinite; opacity:.4; pointer-events:none;
}}
.card:hover {{ border-color: var(--accent); box-shadow: 0 0 20px rgba(0,255,136,.07); }}
.card.sell {{ border-left-color: var(--red); }}
.card.sell::before {{ background: linear-gradient(90deg, var(--red), transparent 40%); }}
.card-inner {{ padding-left: 4px; }}
.card.top {{
  border-color: transparent;
  background: linear-gradient(var(--card-bg), var(--card-bg)) padding-box,
    conic-gradient(from var(--rot), var(--accent) 0%, #4ade80 33%, var(--purple) 66%, var(--accent) 100%) border-box;
  animation: rotateBorder 4s linear infinite;
}}
/* stagger */
.card:nth-child(1) {{ animation-delay:.02s; }}
.card:nth-child(2) {{ animation-delay:.07s; }}
.card:nth-child(3) {{ animation-delay:.12s; }}
.card:nth-child(4) {{ animation-delay:.17s; }}
.card:nth-child(5) {{ animation-delay:.22s; }}

/* === BREAKOUT CARDS === */
.bo-card {{
  background: var(--card-bg);
  border: 1px solid var(--border);
  border-left: 3px solid var(--accent);
  border-radius: 2px;
  padding: 12px 16px;
  margin-bottom: 8px;
  animation: cardEnter .35s ease both;
  transition: border-color .15s, box-shadow .15s;
  position: relative;
}}
.bo-card:hover {{ border-color: var(--accent); box-shadow: 0 0 16px rgba(0,255,136,.07); }}
.bo-card.weekly {{ border-left-color: var(--amber); }}
.bo-card.monthly {{ border-left-color: var(--purple); }}

/* Grade badge colors */
.grade-s {{ color:#00ff88; border-color:rgba(0,255,136,.4); background:rgba(0,255,136,.08); }}
.grade-a {{ color:#4da6ff; border-color:rgba(77,166,255,.4); background:rgba(77,166,255,.06); }}
.grade-b {{ color:#ffaa00; border-color:rgba(255,170,0,.4); background:rgba(255,170,0,.06); }}
.grade-c {{ color:#888; border-color:#333; background:rgba(255,255,255,.03); }}

/* === AI SIGNAL CARDS === */
.ai-card {{
  background: var(--card-bg);
  border: 1px solid rgba(180,138,255,.2);
  border-left: 4px solid #a78bfa;
  border-radius: 10px;
  padding: 16px 18px;
  margin-bottom: 12px;
  animation: cardEnter .45s ease both, aiGlow 4s ease-in-out infinite;
  transition: transform .3s ease, box-shadow .3s ease;
  position: relative;
  overflow: hidden;
}}
.ai-card::before {{
  content:''; position:absolute; top:0; left:0; right:0; height:1px;
  background:linear-gradient(90deg,transparent,rgba(167,139,250,.6),transparent);
}}
.ai-card:hover {{ transform: translateY(-3px); box-shadow: 0 16px 48px rgba(167,139,250,.12); }}
/* AI scan line */
.ai-card .ai-scan {{
  position:absolute; top:0; left:0; bottom:0; width:2px;
  background:linear-gradient(180deg,transparent,rgba(167,139,250,.8),rgba(236,72,153,.6),transparent);
  animation:scanDiag 3s ease-in-out infinite; pointer-events:none; }}
/* AI badge */
.ai-badge {{
  display: inline-flex; align-items: center; gap: 5px; padding: 3px 10px;
  border-radius: 99px; font-size: 9px; font-weight: 800; letter-spacing: .07em;
  text-transform: uppercase; background: rgba(167,139,250,.12); color: #c4b5fd;
  border: 1px solid rgba(167,139,250,.35);
}}
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

/* === F&O CARDS === */
.fno-card {{
  background: var(--card-bg);
  border: 1px solid var(--border); border-left: 3px solid var(--accent);
  border-radius: 2px; padding: 12px 16px; margin-bottom: 8px;
  animation: cardEnter .35s ease both; transition: border-color .15s;
}}
.fno-card:hover {{ border-color: var(--accent); }}

/* === MF CARDS (grow.in style) === */
.mf-card {{
  background: var(--card-bg);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 16px 20px; margin-bottom: 10px;
  animation: cardEnter .35s ease both;
  transition: transform .18s, box-shadow .18s, border-color .18s;
  position:relative; overflow:hidden;
}}
.mf-card::before {{
  content:''; position:absolute; left:0; top:0; bottom:0; width:4px;
  background:var(--mf-accent,var(--accent));
  border-radius:12px 0 0 12px;
}}
.mf-card:hover {{ transform:translateY(-2px); box-shadow:0 10px 36px rgba(0,0,0,.3); border-color:rgba(0,255,136,.25); }}
.mf-fund-header {{ display:flex; justify-content:space-between; align-items:flex-start; margin-bottom:12px; }}
.mf-fund-name {{ font-size:14px; font-weight:700; color:var(--txt); line-height:1.35; max-width:380px; font-family:var(--font-sans); }}
.mf-fund-house {{ font-size:10px; color:var(--txt3); margin-top:3px; font-family:var(--font-mono); }}
.mf-nav-block {{ text-align:right; flex-shrink:0; }}
.mf-nav-val {{ font-size:17px; font-weight:800; font-family:var(--font-mono); color:var(--txt); }}
.mf-day-chg {{ font-size:11px; font-weight:700; margin-top:2px; }}
.mf-cat-chip {{
  display:inline-block; padding:2px 9px; border-radius:99px;
  font-size:9px; font-weight:800; letter-spacing:.06em;
  text-transform:uppercase; font-family:var(--font-mono);
  background:rgba(0,255,136,.07); color:var(--accent);
  border:1px solid rgba(0,255,136,.2); margin-top:4px;
}}
.mf-stats-row {{ display:grid; grid-template-columns:repeat(3,1fr); gap:10px; margin:12px 0; }}
.mf-stat {{
  background:var(--bg3); border:1px solid var(--border); border-radius:8px;
  padding:8px 10px; text-align:center;
}}
.mf-stat-label {{ font-size:8px; font-weight:700; color:var(--txt3); letter-spacing:.1em; text-transform:uppercase; margin-bottom:4px; font-family:var(--font-mono); }}
.mf-stat-val {{ font-size:14px; font-weight:800; font-family:var(--font-mono); color:var(--txt); }}
.mf-returns-section {{ margin-top:12px; padding-top:12px; border-top:1px solid var(--border); }}
.mf-returns-label {{ font-size:8px; font-weight:800; color:var(--txt3); text-transform:uppercase; letter-spacing:.1em; margin-bottom:8px; font-family:var(--font-mono); }}
.mf-return-row {{ display:flex; align-items:center; gap:10px; margin-bottom:6px; }}
.mf-return-period {{ font-size:9px; font-weight:700; color:var(--txt3); min-width:24px; font-family:var(--font-mono); }}
.mf-return-bar-track {{ flex:1; height:5px; background:var(--border); border-radius:3px; overflow:hidden; }}
.mf-return-bar-fill {{ height:100%; border-radius:3px; }}
.mf-return-val {{ font-size:11px; font-weight:800; min-width:52px; text-align:right; font-family:var(--font-mono); }}

/* MF portfolio summary row */
.mf-summary-row {{
  display:grid; grid-template-columns:repeat(4,1fr); gap:12px; margin-bottom:18px;
}}
.mf-summary-card {{
  background:var(--bg2); border:1px solid var(--border); border-radius:10px;
  padding:14px 16px; position:relative; overflow:hidden;
}}
.mf-summary-card::after {{
  content:''; position:absolute; top:0; left:0; right:0; height:2px;
  background:linear-gradient(90deg, var(--mf-top-color,var(--accent)), transparent);
}}
.mf-summary-card .s-label {{ font-size:8px; font-weight:700; color:var(--txt3); letter-spacing:.12em; text-transform:uppercase; margin-bottom:6px; font-family:var(--font-mono); }}
.mf-summary-card .s-val {{ font-size:22px; font-weight:800; font-family:var(--font-mono); color:var(--txt); letter-spacing:-.03em; }}

/* === ACTION BADGES (zip style) === */
.action-badge {{
  display: inline-flex; align-items: center; padding: 2px 12px; border-radius: 99px;
  font-size: 10px; font-weight: 800; letter-spacing: .05em; text-transform: uppercase;
  font-family: 'JetBrains Mono', monospace;
}}
.action-badge.buy {{
  background: rgba(34,197,94,.15); color: #22c55e;
  border: 1px solid rgba(34,197,94,.4); box-shadow: 0 0 10px rgba(34,197,94,.2);
}}
.action-badge.sell {{
  background: rgba(239,68,68,.12); color: #ef4444;
  border: 1px solid rgba(239,68,68,.35); box-shadow: 0 0 10px rgba(239,68,68,.15);
}}

/* === TRADE GRID (entry/SL/targets - matches zip's grid) === */
.tgrid {{ display:grid; grid-template-columns:1fr 1fr; gap:8px; margin:10px 0 10px 12px; }}
.tgcell {{
  background: var(--bg3);
  border: 1px solid var(--border);
  border-radius: 8px; padding: 10px 12px;
  position: relative; overflow: hidden;
}}
.tc-label {{
  font-size: 8px; color: var(--txt4); text-transform: uppercase;
  letter-spacing: .12em; font-weight: 700; margin-bottom: 4px;
  font-family: 'JetBrains Mono', monospace;
}}
.tc-val {{
  font-size: 17px; font-weight: 800; font-family: 'JetBrains Mono', monospace;
  color: var(--txt); line-height: 1; letter-spacing: -.02em;
}}
.tgcell.sl .tc-val {{ color: #f87171; }}
.tgcell.entry .tc-val {{ color: var(--txt); }}

/* Target cells */
.tgt-row {{ display:flex; gap:8px; margin: 10px 0 10px 12px; flex-wrap:wrap; }}
.tgt-cell {{
  flex:1; min-width:60px;
  background: rgba(6,78,59,.2);
  border: 1px solid rgba(34,197,94,.2);
  border-radius: 8px; padding: 8px 10px; text-align:center;
}}
.tgt-label {{ font-size: 9px; color: rgba(34,197,94,.6); margin-bottom: 3px; font-family:'JetBrains Mono',monospace; }}
.tgt-val {{ font-size: 14px; font-weight: 800; color: #22c55e; font-family:'JetBrains Mono',monospace; }}

/* === KV ROWS === */
.row {{ display:flex; gap:18px; flex-wrap:wrap; margin:10px 0; }}
.kv {{ display:flex; flex-direction:column; min-width:55px; }}
.kv span:first-child {{
  font-size: 8px; color: var(--txt4); text-transform: uppercase;
  letter-spacing: .1em; font-weight: 700; margin-bottom: 3px;
}}
.kv span:last-child {{
  font-size: 13px; font-weight: 700; font-family: 'JetBrains Mono', monospace;
  color: var(--txt); line-height: 1;
}}

/* === BADGES === */
.badge {{
  display: inline-flex; align-items: center; padding: 3px 10px; border-radius: 99px;
  font-size: 9px; font-weight: 800; letter-spacing: .08em; text-transform: uppercase;
  font-family: 'JetBrains Mono', monospace;
}}
.badge.sb {{ background:rgba(34,197,94,.1); color:#22c55e; border:1px solid rgba(34,197,94,.3); }}
.badge.b  {{ background:rgba(34,197,94,.07); color:#4ade80; border:1px solid rgba(34,197,94,.2); }}
.badge.w  {{ background:rgba(245,158,11,.08); color:#d97706; border:1px solid rgba(245,158,11,.25); }}
.badge.fno {{ background:rgba(34,197,94,.06); color:#22c55e; border:1px solid rgba(34,197,94,.15); font-size:8px; }}

/* === TAGS === */
.tag {{
  display: inline-block; padding: 2px 8px; border-radius: 99px;
  font-size: 9px; font-weight: 700; margin: 2px 3px;
  background: rgba(34,197,94,.07); color: #22c55e;
  border: 1px solid rgba(34,197,94,.2);
  transition: all .18s;
  font-family: 'JetBrains Mono', monospace;
}}
.tag:hover {{ background:rgba(34,197,94,.14); border-color:rgba(34,197,94,.35); }}
.tag.hi-vol {{ background:rgba(239,68,68,.1); color:#f87171; border-color:rgba(239,68,68,.3); }}
.tag.md-vol {{ background:rgba(245,158,11,.08); color:#f59e0b; border-color:rgba(245,158,11,.25); }}

/* === STRENGTH BARS === */
.sbar-row {{ display:flex; align-items:center; gap:8px; margin:5px 0; }}
.sbar-lbl {{ font-size:10px; font-weight:700; min-width:100px; }}
.sbar-lbl.bull {{ color:#22c55e; }} .sbar-lbl.bear {{ color:#ef4444; }}
.sbar-track {{ flex:1; height:4px; border-radius:3px; overflow:hidden; }}
.sbar-track.bull {{ background:rgba(34,197,94,.12); }}
.sbar-track.bear {{ background:rgba(239,68,68,.08); }}
.sbar-fill.bull {{ height:100%; border-radius:3px; background:linear-gradient(90deg,#22c55e,#4ade80); }}
.sbar-fill.bear {{ height:100%; border-radius:3px; background:linear-gradient(90deg,#ef4444,#f87171); }}
.sbar-pct {{ font-size:10px; font-weight:800; min-width:28px; text-align:right; font-family:'JetBrains Mono',monospace; }}
.sbar-pct.bull {{ color:#22c55e; }} .sbar-pct.bear {{ color:#475569; }}

/* Trigger box */
.trigger-box {{
  background:rgba(239,68,68,.04); border:1px solid rgba(239,68,68,.18);
  border-radius:10px; padding:11px 14px; margin:12px 0; }}
.trig-label {{ font-size:8px; font-weight:800; color:#ef4444; letter-spacing:.14em;
  text-transform:uppercase; margin-bottom:5px; }}
.trig-text {{ font-size:12px; font-weight:600; color:#f1f5f9; line-height:1.4; }}
.trig-meta {{ font-size:10px; color:#475569; margin-top:4px; }}

/* Confidence fill */
.conf {{ height:3px; background:rgba(34,197,94,.1); border-radius:2px; margin:8px 0 10px; overflow:hidden; }}
.conf-fill {{ height:100%; border-radius:2px; background:linear-gradient(90deg,#22c55e,#4ade80); animation:confFill .7s cubic-bezier(.4,0,.2,1) forwards; }}

/* === NEWS === */
.news-item {{ padding:10px 0; border-bottom:1px solid var(--border2); transition:all .18s; }}
.news-item:hover {{ padding-left:4px; }}
.news-item:last-child {{ border-bottom:none; }}

/* === NEWS GRID (tickertape-style 2-col card layout) === */
.news-grid {{
  display:grid; grid-template-columns:1fr 1fr;
  gap:12px; margin-top:4px;
}}
@media(max-width:900px) {{ .news-grid {{ grid-template-columns:1fr; }} }}
.news-card {{
  background:var(--card-bg);
  border:1px solid var(--border);
  border-radius:10px;
  padding:14px 16px;
  display:flex; flex-direction:column; gap:8px;
  transition:transform .18s, box-shadow .18s, border-color .18s;
  cursor:pointer; text-decoration:none;
  animation: cardEnter .35s ease both;
  position:relative; overflow:hidden;
}}
.news-card:hover {{
  transform:translateY(-2px);
  box-shadow:0 8px 32px rgba(0,0,0,.35);
  border-color:rgba(var(--accent-rgb,0,255,136),.35);
}}
.news-card-top {{ display:flex; justify-content:space-between; align-items:center; gap:8px; }}
.news-cat {{
  display:inline-block; padding:2px 8px; border-radius:99px;
  font-size:9px; font-weight:800; letter-spacing:.07em; text-transform:uppercase;
  font-family:var(--font-mono);
}}
.news-sentiment {{
  font-size:9px; font-weight:800; padding:2px 7px; border-radius:99px;
  letter-spacing:.05em; text-transform:uppercase; font-family:var(--font-mono);
}}
.news-sentiment.pos {{ background:rgba(0,255,136,.08); color:var(--green); border:1px solid rgba(0,255,136,.2); }}
.news-sentiment.neg {{ background:rgba(255,59,59,.07); color:var(--red); border:1px solid rgba(255,59,59,.2); }}
.news-sentiment.neu {{ background:rgba(255,255,255,.04); color:var(--txt3); border:1px solid var(--border); }}
.news-headline {{
  font-size:13px; font-weight:600; color:var(--txt); line-height:1.5;
  font-family:var(--font-sans); text-decoration:none;
  display:block;
}}
.news-headline:hover {{ color:var(--accent); }}
.news-meta {{
  display:flex; align-items:center; gap:10px;
  font-size:10px; color:var(--txt3); font-family:var(--font-mono);
}}
.news-source {{
  font-weight:700; color:var(--txt2);
}}
.news-time {{ color:var(--txt3); }}

/* === LIVE DOT === */
.live, .live-dot {{
  display: inline-block; width: 6px; height: 6px; background: var(--accent);
  border-radius: 50%; margin-right: 6px; vertical-align: middle;
  animation: statusBlink 1.8s ease-in-out infinite;
  box-shadow: 0 0 6px var(--accent);
}}

/* === SCROLLBAR === */
::-webkit-scrollbar {{ width: 4px; height: 4px; }}
::-webkit-scrollbar-track {{ background: var(--bg); }}
::-webkit-scrollbar-thumb {{ background: var(--border); border-radius: 4px; }}
::-webkit-scrollbar-thumb:hover {{ background: #22c55e; }}

/* === UTILITY === */
.green  {{ color: var(--green)  !important; }}
.red    {{ color: var(--red)    !important; }}
.amber  {{ color: var(--amber)  !important; }}
.purple {{ color: var(--purple) !important; }}
.blue   {{ color: var(--blue)   !important; }}
.mono   {{ font-family: var(--font-mono) !important; }}
hr {{ border:none; border-top: 1px solid var(--border) !important; margin: 14px 0 !important; }}

/* === SECTION HEADER (terminal style) === */
.sec-hdr {{
  font-family: var(--font-mono); font-size: 10px; font-weight: 700;
  color: var(--txt3); letter-spacing: .18em; text-transform: uppercase;
  border-left: 2px solid var(--accent); padding-left: 8px; margin-bottom: 12px;
  display: flex; align-items: center; gap: 8px;
}}
.sec-hdr .num {{ color: var(--accent); font-size: 13px; }}

/* === NUMBER DISPLAY === */
.big-num {{
  font-family: var(--font-mono); font-size: 48px; font-weight: 800;
  color: var(--txt); letter-spacing: -.04em; line-height: 1;
  animation: numberFlip .4s ease both;
}}
.big-num.green {{ color: var(--green) !important; }}
.big-num.red   {{ color: var(--red)   !important; }}

/* scrollbar */
::-webkit-scrollbar {{ width: 3px; height: 3px; }}
::-webkit-scrollbar-track {{ background: var(--bg); }}
::-webkit-scrollbar-thumb {{ background: var(--border); border-radius: 2px; }}
::-webkit-scrollbar-thumb:hover {{ background: var(--accent); }}
</style>
""", unsafe_allow_html=True)

# ── Theme toggle + daily accent rotation ──────────────────────────────────────
st.markdown("""
<button id="theme-toggle" onclick="toggleTheme()">☀ LIGHT</button>
<script>
// Daily accent: rotates through 7 colours Mon→Sun
var DAILY_ACCENTS = ['#00ff88','#4da6ff','#ff3b3b','#ffaa00','#b48aff','#00e5ff','#ff6b35'];
var DAY_FONTS = [
  "'JetBrains Mono', monospace",
  "'Fira Code', monospace",
  "'JetBrains Mono', monospace",
  "'Courier New', monospace",
  "'JetBrains Mono', monospace",
  "'Fira Code', monospace",
  "'JetBrains Mono', monospace"
];

(function init() {
  var app = document.querySelector('[data-testid="stApp"]');
  if (!app) { setTimeout(init, 100); return; }

  // Daily accent
  var day = new Date().getDay(); // 0=Sun
  var accent = DAILY_ACCENTS[day];
  app.style.setProperty('--accent', accent);
  app.style.setProperty('--green', accent);
  app.style.setProperty('--daily-accent', accent);
  app.style.setProperty('--font-mono', DAY_FONTS[day]);

  // Light toggle
  var saved = localStorage.getItem('tradeflowTheme') || 'dark';
  var btn = document.getElementById('theme-toggle');
  if (saved === 'light') {
    app.classList.add('light-app');
    if (btn) btn.textContent = '◐ DARK';
  }
})();

function toggleTheme() {
  var app = document.querySelector('[data-testid="stApp"]');
  var btn = document.getElementById('theme-toggle');
  if (!app) return;
  if (app.classList.contains('light-app')) {
    app.classList.remove('light-app');
    if (btn) btn.textContent = '☀ LIGHT';
    localStorage.setItem('tradeflowTheme', 'dark');
  } else {
    app.classList.add('light-app');
    if (btn) btn.textContent = '◐ DARK';
    localStorage.setItem('tradeflowTheme', 'light');
  }
}
</script>
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

def _market_news_ttl():
    """Seconds until next 11am or 3pm IST window — cap at 2h."""
    now = datetime.now(IST)
    h, m = now.hour, now.minute
    slots = [(11, 0), (15, 0)]
    for sh, sm in slots:
        secs = (sh - h) * 3600 + (sm - m) * 60
        if secs > 0:
            return min(secs, 7200)
    # past 3pm — cache until next 11am next day (cap 7200)
    return 7200

@st.cache_data(ttl=900)    # 15-min cache — first candle is fixed after 9:30 AM IST
def _ohl_oll_scan():
    return scan_ohl_oll()


# ── Helpers ───────────────────────────────────────────────────────────────────
def _rating(score):
    if score >= 85: return "STRONG BUY", "sb"
    if score >= 70: return "BUY", "b"
    return "WATCH", "w"

def _conf_col(score):
    if score >= 85: return "#22c55e"
    if score >= 70: return "#22c55e"
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
        name="Price",
        increasing_line_color=CLR_UP,   decreasing_line_color=CLR_DOWN,
        increasing_fillcolor="#052e1a", decreasing_fillcolor="#450a0a"))
    fig.add_trace(go.Scatter(x=df.index, y=e20,  name="S", line=dict(color="#facc15", width=1.5)))
    fig.add_trace(go.Scatter(x=df.index, y=e50,  name="M", line=dict(color="#22c55e", width=1.5)))
    fig.add_trace(go.Scatter(x=df.index, y=e200, name="L", line=dict(color="#f87171", width=1.5)))
    if signal:
        fig.add_hline(y=signal["sl2"],     line_color="#ef4444", line_dash="dash", annotation_text="STOP")
        fig.add_hline(y=signal["target1"], line_color="#86efac", line_dash="dot",  annotation_text="T1")
        fig.add_hline(y=signal["target2"], line_color="#4ade80", line_dash="dot",  annotation_text="T2")
        fig.add_hline(y=signal["target3"], line_color="#22c55e", line_dash="dot",  annotation_text="T3")
    fig.update_layout(xaxis_rangeslider_visible=False, height=440,
        paper_bgcolor="#0d1117", plot_bgcolor="#111827",
        font=dict(color="#64748b", size=10, family="JetBrains Mono"),
        xaxis=dict(gridcolor="#1a2030"), yaxis=dict(gridcolor="#1a2030"),
        legend=dict(bgcolor="#111827", bordercolor="#1a2030", borderwidth=1, font=dict(size=10)),
        margin=dict(l=8,r=8,t=8,b=8))
    st.plotly_chart(fig, use_container_width=True)


# ── NiftyPulse-derived features ───────────────────────────────────────────────

def _tv_chart(symbol: str, interval: str = "D", height: int = 460):
    """Embed a live TradingView advanced chart (dark, IST timezone, RSI+MACD+Vol)."""
    html = (
        '<div class="tradingview-widget-container" style="height:' + str(height) + 'px;background:#0d1117">'
        + '<div id="tv_' + symbol + '" style="height:' + str(height - 5) + 'px"></div>'
        + '<script type="text/javascript" src="https://s3.tradingview.com/tv.js"></script>'
        + '<script type="text/javascript">'
        + 'new TradingView.widget({'
        + '"autosize":true,'
        + '"symbol":"NSE:' + symbol + '",'
        + '"interval":"' + interval + '",'
        + '"timezone":"Asia/Kolkata",'
        + '"theme":"dark",'
        + '"style":"1",'
        + '"locale":"in",'
        + '"toolbar_bg":"#0d1117",'
        + '"enable_publishing":false,'
        + '"hide_side_toolbar":false,'
        + '"allow_symbol_change":false,'
        + '"save_image":false,'
        + '"studies":["RSI@tv-basicstudies","MACD@tv-basicstudies","Volume@tv-basicstudies"],'
        + '"container_id":"tv_' + symbol + '"'
        + '});'
        + '</script></div>'
    )
    _stc.html(html, height=height)


@st.cache_data(ttl=1800)
def _quick_news(symbol: str, name: str = ""):
    """Fetch 3 latest news items for a stock. Cached 30 min."""
    try:
        items = get_stock_news(symbol, name)
        return items[:3] if items else []
    except Exception:
        return []


def _opportunity_digest(all_sigs: list) -> dict:
    """
    Group existing signals into opportunity categories (NiftyPulse style).
    No extra API calls — uses already-loaded signal data.
    Returns dict of category → top signal.
    """
    if not all_sigs:
        return {}
    result = {}
    by_rr   = sorted(all_sigs, key=lambda x: x.get("rr1", 0), reverse=True)
    by_vol  = sorted(all_sigs, key=lambda x: x.get("vol_ratio", 0), reverse=True)
    by_score = sorted(all_sigs, key=lambda x: x.get("score", 0), reverse=True)
    fno_sigs = [s for s in all_sigs if s.get("fno_eligible")]

    if by_rr:      result["🎯 Best R:R Setup"]       = by_rr[0]
    if by_vol:     result["⚡ Volume Surge Leader"]   = by_vol[0]
    if by_score:   result["🏆 Highest Conviction"]    = by_score[0]
    if fno_sigs:   result["📊 F&O Ready Pick"]        = sorted(fno_sigs, key=lambda x: x.get("score",0), reverse=True)[0]
    return result


# ── Sidebar (view-only — filters + info) ─────────────────────────────────────
with st.sidebar:
    st.markdown('<div style="font-size:17px;font-weight:900;padding:10px 0 16px;letter-spacing:-.02em;font-family:\'JetBrains Mono\',monospace;color:#f2f2f2">TRADEFLOW AI <span style="color:#22c55e">PRO</span></div>', unsafe_allow_html=True)

    # Last scan info — use GitHub JSON on cloud, local DB on dev
    if IS_LOCAL:
        _last_ts, _last_slot, _last_counts = get_last_scan()
    else:
        _last_ts, _last_slot, _last_counts = _gh_last_scan()
    if _last_ts:
        st.markdown(f'<div style="font-size:10px;color:#22c55e;font-weight:700;margin:6px 0 2px"><span class="live"></span>Last scan</div>', unsafe_allow_html=True)
        st.caption(f"{_last_ts}")
        st.caption(f"Slot: {_last_slot.upper() if _last_slot else '—'}")

    # Filters locked to expert-grade defaults (no UI clutter)
    min_score = MIN_SIGNAL_SCORE   # 78 — expert grade
    _days     = 365               # show full history (all signals, no day limit)

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
    # Telegram CTA (audit fix §6 — conversion hook)
    st.markdown("""
<div style="background:rgba(34,197,94,.06);border:1px solid rgba(34,197,94,.2);border-radius:8px;
  padding:10px 12px;margin-bottom:10px">
  <div style="font-size:10px;font-weight:800;color:#22c55e;margin-bottom:4px;letter-spacing:.06em">
    📲 GET FREE SIGNALS
  </div>
  <div style="font-size:10px;color:#64748b;margin-bottom:8px;line-height:1.5">
    Join Telegram — receive alerts the moment a signal fires.
  </div>
  <a href="https://t.me/your_channel" target="_blank"
    style="display:block;text-align:center;padding:6px;background:rgba(34,197,94,.15);
    border:1px solid rgba(34,197,94,.3);border-radius:6px;color:#22c55e;
    font-size:10px;font-weight:800;text-decoration:none;letter-spacing:.06em">
    JOIN TELEGRAM →
  </a>
</div>
""", unsafe_allow_html=True)

    # About + data source (audit fix §5)
    with st.expander("ℹ About · Data Sources"):
        st.markdown("""
**TradeFlow AI Pro** — Nifty 500 Swing Scanner

Built by **Akshay K** (CA, FP&A)
[@askakshayfinance](https://www.instagram.com/askakshayfinance)

**Data Sources:**
- Price data: Yahoo Finance (yfinance)
- Universe: NSE Nifty 500 CSV (official NSE archives)
- Delay: ~15 min during market hours

**Signal logic:**
- Expert-grade: EMA stack + RSI zones + ADX + Volume
- Minimum Score 78/100 · RR ≥ 2:1
- No SEBI registration. Educational use only.

**Version:** 2.1 · Scans: Mon–Fri auto
        """)
    st.caption("Data: Yahoo Finance · NSE Nifty 500 · Not SEBI Advice")


# ── Session Header (MiroFish-style terminal) ──────────────────────────────────
_now_hdr  = datetime.now(IST)
now_str   = _now_hdr.strftime("%d %b · %I:%M %p IST")
_clock_hh = _now_hdr.strftime("%H:%M:%S")
_active   = get_active_signals() if IS_LOCAL else pd.DataFrame()
_bos_df   = get_breakouts(days=_days) if IS_LOCAL else _gh_breakouts(days=_days)
sig_count = len(_active)
bo_count  = len(_bos_df)
_scan_ts_hdr, _scan_slot_hdr, _scan_counts_hdr = (
    get_last_scan() if IS_LOCAL else _gh_last_scan()
)

# Win rate from performance
try:
    _perf_hdr = get_performance() if IS_LOCAL else {}
    _wr_hdr   = _perf_hdr.get("win_rate", 0) if isinstance(_perf_hdr, dict) else 0
    _trades_hdr = _perf_hdr.get("total_trades", 0) if isinstance(_perf_hdr, dict) else 0
except Exception:
    _wr_hdr = 0; _trades_hdr = 0

# Pipeline step — derive from current time + scan slot
_pipe_step = 1
if _scan_slot_hdr:
    _pipe_step = 5 if "EOD" in str(_scan_slot_hdr) else 4 if "PM" in str(_scan_slot_hdr) else 3

st.markdown(f"""
<style>
/* ── MiroFish terminal header components ── */
@keyframes clockTick  {{ 0%,49% {{ opacity:1; }} 50%,100% {{ opacity:.4; }} }}
@keyframes sessionGlow {{ 0%,100% {{ box-shadow:0 0 20px rgba(0,255,136,.04); }} 50% {{ box-shadow:0 0 40px rgba(0,255,136,.10); }} }}
@keyframes pipeActive  {{ 0%,100% {{ background:rgba(0,255,136,.18); }} 50% {{ background:rgba(0,255,136,.30); }} }}
@keyframes bigNumIn    {{ from {{ opacity:0; transform:translateY(-16px) scale(.9); }} to {{ opacity:1; transform:translateY(0) scale(1); }} }}
@keyframes scanH2      {{ 0% {{ left:-10%; }} 100% {{ left:110%; }} }}

.session-header {{
  background:#000;
  border:1px solid rgba(0,255,136,.18);
  border-radius:0;
  position:relative; overflow:hidden;
  margin-bottom:0;
  animation: sessionGlow 4s ease-in-out infinite;
}}
/* Top scan line sweep */
.session-header::after {{
  content:''; position:absolute; top:0; left:-10%; width:30%; height:1px;
  background:linear-gradient(90deg,transparent,rgba(0,255,136,.6),transparent);
  animation:scanH2 3s ease-in-out infinite;
}}
/* Status bar row */
.sh-statusbar {{
  display:flex; justify-content:space-between; align-items:center;
  padding:6px 16px; background:rgba(0,255,136,.04);
  border-bottom:1px solid rgba(0,255,136,.1);
  flex-wrap:wrap; gap:6px;
}}
.sh-logo {{
  font-size:11px; font-weight:900; color:var(--txt);
  font-family:var(--font-mono); letter-spacing:.08em;
}}
.sh-logo span {{ color:var(--accent); }}
.sh-stat {{
  display:flex; align-items:center; gap:6px;
  font-size:9px; font-weight:700; color:var(--txt3);
  font-family:var(--font-mono); letter-spacing:.06em;
  text-transform:uppercase;
}}
.sh-stat b {{ color:var(--txt); font-size:11px; }}
.sh-clock {{
  font-size:14px; font-weight:800; font-family:var(--font-mono);
  color:var(--accent); letter-spacing:.1em;
  animation:clockTick 1s step-end infinite;
}}
/* Big metric blocks */
.sh-metrics {{
  display:grid; grid-template-columns:repeat(4,1fr);
  gap:0; border-bottom:1px solid rgba(0,255,136,.08);
}}
.sh-metric {{
  padding:16px 20px; border-right:1px solid rgba(0,255,136,.07);
  position:relative; overflow:hidden;
}}
.sh-metric:last-child {{ border-right:none; }}
.sh-metric-label {{
  font-size:8px; font-weight:700; color:var(--txt3);
  text-transform:uppercase; letter-spacing:.14em;
  font-family:var(--font-mono); margin-bottom:6px;
}}
.sh-metric-val {{
  font-size:40px; font-weight:900; font-family:var(--font-mono);
  color:var(--accent); line-height:1; letter-spacing:-.04em;
  animation:bigNumIn .5s ease both;
}}
.sh-metric-val.dim {{ color:var(--txt2); font-size:32px; }}
.sh-metric-sub {{
  font-size:9px; color:var(--txt3); font-family:var(--font-mono);
  margin-top:4px; letter-spacing:.04em;
}}
/* Pipeline row */
.sh-pipeline {{
  display:flex; align-items:center;
  padding:10px 16px; gap:0;
  background:rgba(0,0,0,.4);
}}
.sh-pipe-label {{
  font-size:8px; font-weight:800; color:var(--txt3);
  text-transform:uppercase; letter-spacing:.14em;
  font-family:var(--font-mono); margin-right:14px;
  white-space:nowrap;
}}
.sh-step {{
  display:flex; align-items:center; gap:0; flex:1;
}}
.sh-step-block {{
  flex:1; padding:6px 10px; text-align:center;
  border:1px solid rgba(0,255,136,.1);
  font-family:var(--font-mono); cursor:default;
  transition:all .3s;
  position:relative;
}}
.sh-step-block.done {{
  background:rgba(0,255,136,.06);
  border-color:rgba(0,255,136,.2);
}}
.sh-step-block.active {{
  background:rgba(0,255,136,.15);
  border-color:rgba(0,255,136,.5);
  animation:pipeActive 2s ease-in-out infinite;
}}
.sh-step-block.pending {{
  background:transparent;
  border-color:rgba(255,255,255,.05);
}}
.sh-step-num {{
  font-size:7px; font-weight:800; color:var(--txt3);
  letter-spacing:.1em; display:block;
}}
.sh-step-name {{
  font-size:9px; font-weight:700;
  letter-spacing:.04em; display:block; margin-top:1px;
}}
.sh-step-block.done  .sh-step-name {{ color:var(--accent); }}
.sh-step-block.active .sh-step-name {{ color:var(--txt); }}
.sh-step-block.pending .sh-step-name {{ color:var(--txt3); }}
.sh-arrow {{
  font-size:8px; color:rgba(0,255,136,.3); padding:0 2px;
  flex-shrink:0;
}}
</style>

<div class="session-header">
  <!-- Status bar -->
  <div class="sh-statusbar">
    <div style="display:flex;align-items:center;gap:16px">
      <div class="sh-logo">TRADEFLOW AI&nbsp;<span>PRO</span></div>
      <div class="sh-stat"><span class="live"></span> LIVE ENGINE</div>
      <div class="sh-stat">UNIVERSE&nbsp;<b>NSE 500</b></div>
      <div class="sh-stat">OHL SCAN&nbsp;<b>NIFTY 200</b></div>
      <div class="sh-stat">SLOT&nbsp;<b>{_scan_slot_hdr or "—"}</b></div>
    </div>
    <div style="display:flex;align-items:center;gap:16px">
      <div class="sh-stat">LAST SCAN&nbsp;<b>{str(_scan_ts_hdr or "—")[:16]}</b></div>
      <div class="sh-clock" id="sh-clock">{_clock_hh}</div>
      <span style="font-size:8px;font-weight:800;padding:2px 8px;background:rgba(0,255,136,.1);
        color:var(--accent);border:1px solid rgba(0,255,136,.3);font-family:var(--font-mono);
        letter-spacing:.1em">LIVE</span>
    </div>
  </div>

  <!-- Big metrics -->
  <div class="sh-metrics">
    <div class="sh-metric">
      <div class="sh-metric-label">Active Signals</div>
      <div class="sh-metric-val">{sig_count}</div>
      <div class="sh-metric-sub">Swing · Nifty 500</div>
    </div>
    <div class="sh-metric">
      <div class="sh-metric-label">Breakouts</div>
      <div class="sh-metric-val">{bo_count}</div>
      <div class="sh-metric-sub">Today · Multi-TF</div>
    </div>
    <div class="sh-metric">
      <div class="sh-metric-label">Win Rate</div>
      <div class="sh-metric-val {'dim' if _wr_hdr == 0 else ''}">{_wr_hdr:.0f}<span style="font-size:18px;color:var(--txt3)">%</span></div>
      <div class="sh-metric-sub">{_trades_hdr} closed trades</div>
    </div>
    <div class="sh-metric">
      <div class="sh-metric-label">Scan Counts</div>
      <div class="sh-metric-val dim">{_scan_counts_hdr.get('swing',0) if _scan_counts_hdr else 0}</div>
      <div class="sh-metric-sub">Swing · {_scan_counts_hdr.get('breakout',0) if _scan_counts_hdr else 0} breakouts</div>
    </div>
  </div>

  <!-- Execution pipeline -->
  <div class="sh-pipeline">
    <div class="sh-pipe-label">■ SCAN PIPELINE</div>
    <div class="sh-step">
      <div class="sh-step-block {'done' if _pipe_step > 1 else 'active' if _pipe_step == 1 else 'pending'}">
        <span class="sh-step-num">01</span>
        <span class="sh-step-name">UNIVERSE</span>
      </div>
      <div class="sh-arrow">›</div>
      <div class="sh-step-block {'done' if _pipe_step > 2 else 'active' if _pipe_step == 2 else 'pending'}">
        <span class="sh-step-num">02</span>
        <span class="sh-step-name">REGIME</span>
      </div>
      <div class="sh-arrow">›</div>
      <div class="sh-step-block {'done' if _pipe_step > 3 else 'active' if _pipe_step == 3 else 'pending'}">
        <span class="sh-step-num">03</span>
        <span class="sh-step-name">SETUP</span>
      </div>
      <div class="sh-arrow">›</div>
      <div class="sh-step-block {'done' if _pipe_step > 4 else 'active' if _pipe_step == 4 else 'pending'}">
        <span class="sh-step-num">04</span>
        <span class="sh-step-name">SCORE</span>
      </div>
      <div class="sh-arrow">›</div>
      <div class="sh-step-block {'done' if _pipe_step > 5 else 'active' if _pipe_step == 5 else 'pending'}">
        <span class="sh-step-num">05</span>
        <span class="sh-step-name">ALERT</span>
      </div>
      <div class="sh-arrow">›</div>
      <div class="sh-step-block {'done' if _pipe_step > 5 else 'pending'}">
        <span class="sh-step-num">06</span>
        <span class="sh-step-name">TRACK</span>
      </div>
    </div>
  </div>
</div>

<script>
(function liveClock() {{
  var el = document.getElementById('sh-clock');
  if (!el) {{ setTimeout(liveClock, 500); return; }}
  setInterval(function() {{
    var now = new Date();
    var ist = new Date(now.getTime() + (5.5*3600000));
    var hh = String(ist.getUTCHours()).padStart(2,'0');
    var mm = String(ist.getUTCMinutes()).padStart(2,'0');
    var ss = String(ist.getUTCSeconds()).padStart(2,'0');
    el.textContent = hh+':'+mm+':'+ss;
  }}, 1000);
}})();
</script>
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
    ticker_parts.append('<span style="margin:0 16px;color:#1a2030">│</span>')
if _fxc:
    ticker_parts += [_ti_forex(r) for r in _fxc]
if _ai_ticker:
    ticker_parts.append('<span style="margin:0 16px;color:#1a0a3a">│</span>')
    ticker_parts += [_ti_ai(s) for s in _ai_ticker]
if _sigs_ticker:
    ticker_parts.append('<span style="margin:0 16px;color:#1a2030">│</span>')
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
<div style="width:100%;background:rgba(13,17,23,.9);border-bottom:1px solid rgba(34,197,94,.2);
  overflow:hidden;backdrop-filter:blur(8px);margin-bottom:12px;border-radius:8px">
  <div style="display:flex;align-items:stretch">
    <div style="padding:0 14px;border-right:1px solid rgba(34,197,94,.15);
      display:flex;align-items:center;gap:6px;flex-shrink:0;
      background:rgba(34,197,94,.05)">
      <span class="live"></span>
      <span style="font-size:9px;font-weight:800;color:#22c55e;letter-spacing:.12em;text-transform:uppercase;line-height:1;font-family:'JetBrains Mono',monospace">Live</span>
    </div>
    <div class="ticker-wrap" style="padding:9px 0">
      <div class="ticker-track">{ticker_html}</div>
    </div>
    <div style="padding:0 12px;border-left:1px solid rgba(34,197,94,.1);
      display:flex;align-items:center;flex-shrink:0;background:rgba(0,0,0,.2)">
      <span style="font-size:8px;color:#3d4a5c;font-weight:600;letter-spacing:.06em;font-family:'JetBrains Mono',monospace">AUTO-REFRESH 60s</span>
    </div>
  </div>
</div>
""", unsafe_allow_html=True)

tab1, tab_ai, tab2, tab3, tab4, tab_mb, tab_ohl, tab7 = st.tabs(["📈 Signals", "🤖 AI Signals", "🚀 Breakouts", "📊 F&O", "💰 Mutual Funds", "💎 Multibaggers", "🕯️ OHL/OLL", "📋 History"])


# ══════════════════════════════════════════════════════════════════════════════
# TAB 1 — SIGNALS (read-only from DB)
# ══════════════════════════════════════════════════════════════════════════════
with tab1:
    signals = get_signals_display(days=_days, min_score=min_score) if IS_LOCAL else _gh_signals_display(days=_days, min_score=min_score)

    if not signals:
        st.markdown('<div style="text-align:center;padding:50px 0"><div style="font-size:36px">📡</div><div style="margin-top:8px;font-size:13px;color:#334155">No qualifying signals yet.<br>Auto-scans: 9:20 AM · 11:45 AM · 4:30 PM IST</div></div>', unsafe_allow_html=True)
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
        sigs_s = sorted(signals, key=lambda x: (x.get("date",""), x.get("score", 0)), reverse=True)

        # ── OPPORTUNITY DIGEST (NiftyPulse feature) ───────────────────────────
        opps = _opportunity_digest(sigs_s)
        if opps:
            st.markdown('<div style="font-size:11px;font-weight:800;color:#334155;text-transform:uppercase;letter-spacing:.12em;margin-bottom:10px">⚡ Today\'s Best Setups</div>', unsafe_allow_html=True)
            opp_cols = st.columns(len(opps))
            for col, (label, sig) in zip(opp_cols, opps.items()):
                pct_color = "#22c55e" if sig["action"] == "BUY" else "#ef4444"
                rr_val    = sig.get("rr1", 0)
                col.markdown(
                    '<div style="background:#111827;border:1px solid #1a2030;border-radius:8px;'
                    'padding:10px 12px;height:100%">'
                    + '<div style="font-size:9px;color:#334155;font-weight:700;text-transform:uppercase;'
                    'letter-spacing:.1em;margin-bottom:6px">' + label + '</div>'
                    + '<div style="font-size:18px;font-weight:900;color:#f1f5f9;line-height:1">'
                    + sig["symbol"] + '</div>'
                    + '<div style="font-size:10px;color:' + pct_color + ';margin-top:3px;font-weight:700">'
                    + sig["action"] + ' · Score ' + str(sig["score"]) + '</div>'
                    + '<div style="font-size:10px;color:#475569;margin-top:4px">'
                    + 'RR 1:' + str(rr_val) + ' · Vol ' + str(sig.get("vol_ratio",1.0)) + 'x</div>'
                    + '</div>',
                    unsafe_allow_html=True
                )
            st.markdown("<div style='height:16px'></div>", unsafe_allow_html=True)

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

            # ── Per-signal: TradingView chart + News (NiftyPulse features) ───
            _exp_c, _exp_n = st.columns(2)
            with _exp_c:
                with st.expander(f"📊 Live Chart — {s['symbol']}", expanded=False):
                    tf_choice = st.radio(
                        "Timeframe", ["D", "W", "240"],
                        format_func=lambda x: {"D":"Daily","W":"Weekly","240":"4H"}[x],
                        horizontal=True,
                        key=f"tf_{s['symbol']}_{i}"
                    )
                    _tv_chart(s["symbol"], interval=tf_choice, height=460)
            with _exp_n:
                with st.expander(f"📰 Latest News — {s['symbol']}", expanded=False):
                    _news = _quick_news(s["symbol"])
                    if _news:
                        for _n in _news:
                            _sent_clr = {"positive":"#22c55e","negative":"#ef4444"}.get(
                                str(_n.get("sentiment","")).lower(), "#64748b")
                            st.markdown(
                                '<div style="border-left:3px solid ' + _sent_clr + ';'
                                'padding:6px 10px;margin-bottom:8px;background:#111827;border-radius:0 4px 4px 0">'
                                + '<div style="font-size:11px;color:#cbd5e1;line-height:1.4">'
                                + str(_n.get("title",""))[:140] + '</div>'
                                + '<div style="font-size:9px;color:#334155;margin-top:4px">'
                                + str(_n.get("source","")) + ' · ' + str(_n.get("published","")) + '</div>'
                                + '</div>',
                                unsafe_allow_html=True
                            )
                    else:
                        st.caption("No recent news found.")

        st.markdown("---")
        df_s = pd.DataFrame(sigs_s)
        fig  = px.bar(df_s, x="symbol", y="score", color="score",
                      color_continuous_scale=["#22c55e","#22c55e"], range_color=[60,100])
        fig.update_layout(height=180, paper_bgcolor="#0d1117", plot_bgcolor="#111827",
            font=dict(color="#64748b",size=10), xaxis=dict(gridcolor="#1a2030"),
            yaxis=dict(gridcolor="#1a2030",range=[50,100]),
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
    Channel breakout signals · Volume confirmation required
    <br>Auto-scans: 9:20 AM (4H) · 11:45 AM (4H) · 4:30 PM (Daily EOD)
  </div>
</div>
""", unsafe_allow_html=True)

    if not ai_sigs:
        st.markdown("""
<div style="text-align:center;padding:60px 0">
  <div style="font-size:40px;margin-bottom:12px">🤖</div>
  <div style="font-size:14px;color:#4b3a7a;font-weight:600">No AI channel breakouts detected</div>
  <div style="font-size:11px;color:#2d1a55;margin-top:6px">Next auto-scan: 9:20 AM IST (4H) · 4:30 PM IST (Daily EOD)</div>
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
    <span style="font-size:9px;color:#2d1a55">AI channel breakout signals · Not SEBI advice</span>
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
        # ── Grade + sort — kill the 68-dump problem ───────────────────────────
        def _grade(b):
            vol = float(b.get("vol_ratio",1)); rr = float(b.get("rr",1))
            tf  = b.get("timeframe","Daily")
            pts = 0
            if vol >= 5:  pts += 3
            elif vol >= 3: pts += 2
            elif vol >= 2: pts += 1
            if rr >= 2.5: pts += 2
            elif rr >= 1.8: pts += 1
            if tf == "Monthly": pts += 3
            elif tf == "Weekly": pts += 2
            return {5:"S",4:"S",3:"A",2:"A",1:"B",0:"B"}.get(pts, "C")
        for b in bos_list: b["_grade"] = _grade(b)
        # Sort: Monthly first, then Weekly, then Daily; within each by vol_ratio
        tf_order = {"Monthly":0,"Weekly":1,"Daily":2}
        bos_list.sort(key=lambda b: (tf_order.get(b.get("timeframe","Daily"),3), -float(b.get("vol_ratio",1))))
        # Hard cap: top 5 Monthly + top 5 Weekly + top 10 Daily = max 20
        _by_tf = {"Monthly":[],"Weekly":[],"Daily":[]}
        for b in bos_list:
            tf = b.get("timeframe","Daily")
            if tf in _by_tf: _by_tf[tf].append(b)
        bos_list = _by_tf["Monthly"][:5] + _by_tf["Weekly"][:5] + _by_tf["Daily"][:10]

        tfc = {}
        for b in bos_list: tfc[b.get("timeframe","Daily")] = tfc.get(b.get("timeframe","Daily"),0)+1
        c1,c2,c3,c4 = st.columns(4)
        c1.metric("Curated",  len(bos_list))
        c2.metric("Monthly",  tfc.get("Monthly",0))
        c3.metric("Weekly",   tfc.get("Weekly",0))
        c4.metric("Daily",    tfc.get("Daily",0))
        st.markdown('<div class="sec-hdr">🏆 GRADE S/A — highest conviction only · max 20 shown · sorted by timeframe + vol surge</div>', unsafe_allow_html=True)
        tf_f = st.selectbox("Filter timeframe", ["All","Monthly","Weekly","Daily"])
        grade_f = st.selectbox("Filter grade", ["All","S","A","B"], index=0)
        fil = [b for b in bos_list if
               (tf_f=="All" or b.get("timeframe")==tf_f) and
               (grade_f=="All" or b.get("_grade")==grade_f)]
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
            grade   = b.get("_grade","B")
            grade_cls = {"S":"grade-s","A":"grade-a","B":"grade-b","C":"grade-c"}.get(grade,"grade-c")
            st.markdown(f"""
<div class="bo-card {cls}">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
    <div style="display:flex;align-items:center;gap:10px">
      <span style="font-family:var(--font-mono);font-size:18px;font-weight:800;color:var(--txt)">{b['symbol']}</span>
      {fno_b}
    </div>
    <div style="display:flex;gap:6px;align-items:center">
      <span class="badge {grade_cls}" style="font-size:10px;font-weight:900;padding:2px 8px;border-radius:2px;border:1px solid">GRADE {grade}</span>
      <span style="font-size:9px;font-weight:700;color:{tfc2};padding:2px 8px;border-radius:2px;border:1px solid {tfc2}40;font-family:var(--font-mono)">{tf.upper()}</span>
    </div>
  </div>
  <div style="font-size:10px;color:var(--txt3);margin-bottom:8px;font-family:var(--font-mono)">{pats}</div>
  <div class="row">
    <div class="kv"><span>ENTRY</span><span>₹{b['price']:,.1f}</span></div>
    <div class="kv"><span>STOP</span><span class="red">₹{b['sl']:,.1f}</span></div>
    <div class="kv"><span>T1</span><span class="green">₹{b['target1']:,.1f}</span></div>
    <div class="kv"><span>T2</span><span class="green">₹{b['target2']:,.1f}</span></div>
    <div class="kv"><span>T3</span><span class="green">₹{b.get('target3',b['target2']):,.1f}</span></div>
    <div class="kv"><span>R:R</span><span class="blue">1:{b['rr']}</span></div>
    <div class="kv"><span>VOL</span><span class="amber">{b['vol_ratio']}×</span></div>
  </div>
  <div style="margin-top:8px"><a href="{tv_link}" target="_blank" style="color:var(--accent);font-size:11px;font-weight:700;text-decoration:none;font-family:var(--font-mono)">CHART →</a></div>
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
  <div style="margin-top:8px"><a href="{tv4}" target="_blank" style="color:#22c55e;font-size:11px;font-weight:600;text-decoration:none">Chart →</a></div>
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
      <span style="font-size:9px;color:#475569;padding:2px 7px;border:1px solid #1a2030;border-radius:4px">{b.get('timeframe','Daily')}</span>
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
    st.markdown('<div class="sec-hdr">📊 F&amp;O WATCHLIST — breakout + 4H signals on F&amp;O-eligible stocks</div>', unsafe_allow_html=True)

    # ── Pull F&O-eligible signals from ALL sources ─────────────────────────────
    # Source 1: breakout signals with fno=True
    _fno_from_bo = [b for b in (_bos_df.to_dict("records") if not _bos_df.empty else [])
                    if b.get("fno")]
    # Source 2: 4H signals with fno=True
    _df_4h_fno = get_4h_signals(days=_days) if IS_LOCAL else _gh_4h_signals(days=_days)
    _fno_from_4h = [b for b in (_df_4h_fno.to_dict("records") if not _df_4h_fno.empty else [])
                    if b.get("fno")]
    # Source 3: swing signals with fno_eligible flag (legacy)
    _fno_from_sw = [s for s in signals if s.get("fno_eligible")]

    # Merge — breakouts first (higher quality for F&O), then 4H, then swing
    all_fno = _fno_from_bo + _fno_from_4h + _fno_from_sw

    # Deduplicate by symbol (keep first occurrence = highest priority)
    _seen_fno = set()
    fno_dedup = []
    for b in all_fno:
        sym = b.get("symbol","")
        if sym not in _seen_fno:
            _seen_fno.add(sym)
            fno_dedup.append(b)

    if not fno_dedup:
        st.info("No F&O-eligible signals today. Auto-scan runs 9:20 AM · 11:45 AM · 4:30 PM IST.\nF&O stocks appear here when they show up in Breakout, 4H or Swing scans.")
    else:
        fa, fb, fc_col = st.columns(3)
        fa.metric("F&O Signals", len(fno_dedup))
        fb.metric("From Breakouts", len(_fno_from_bo))
        fc_col.metric("From 4H", len(_fno_from_4h))
        st.markdown("---")
        for b in fno_dedup:
            sym   = b.get("symbol","")
            price = float(b.get("price", b.get("entry", 0)) or 0)
            sl    = float(b.get("sl", b.get("sl2", price*0.95)) or price*0.95)
            t1    = float(b.get("target1", 0) or price*1.05)
            t2    = float(b.get("target2", 0) or t1*1.03)
            rr    = b.get("rr", b.get("rr1", "—"))
            vol   = b.get("vol_ratio", "—")
            tf    = b.get("timeframe","Daily")
            src   = "BO" if b in _fno_from_bo else ("4H" if b in _fno_from_4h else "SW")
            src_c = {"BO":"#00ff88","4H":"#ffaa00","SW":"#4da6ff"}.get(src,"#888")
            tf_c  = {"Monthly":"#b48aff","Weekly":"#ffaa00","Daily":"#00ff88","4H":"#4da6ff"}.get(tf,"#888")
            nse_link = f"https://www.nseindia.com/get-quotes/derivatives?symbol={sym}"
            tv_link  = b.get("tv_link") or f"https://in.tradingview.com/chart/?symbol=NSE:{sym}"
            pct_move = round((t2 - price) / price * 100, 1) if price > 0 else 0
            st.markdown(f"""
<div class="fno-card">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
    <div style="display:flex;align-items:center;gap:10px">
      <span style="font-family:var(--font-mono);font-size:20px;font-weight:900;color:var(--txt)">{sym}</span>
      <span style="font-size:9px;font-weight:800;padding:2px 8px;border-radius:2px;border:1px solid {src_c};color:{src_c}">{src}</span>
      <span style="font-size:9px;font-weight:700;padding:2px 7px;border-radius:2px;border:1px solid {tf_c}40;color:{tf_c}">{tf}</span>
    </div>
    <div style="text-align:right">
      <div style="font-size:10px;color:var(--txt3);font-family:var(--font-mono)">T2 upside</div>
      <div style="font-size:16px;font-weight:800;color:var(--green);font-family:var(--font-mono)">+{pct_move}%</div>
    </div>
  </div>
  <div style="display:grid;grid-template-columns:repeat(5,1fr);gap:8px;margin-bottom:10px">
    <div style="background:var(--bg2);border:1px solid var(--border);border-radius:2px;padding:8px;text-align:center">
      <div style="font-size:8px;color:var(--txt3);text-transform:uppercase;letter-spacing:.1em;margin-bottom:3px">ENTRY</div>
      <div style="font-size:15px;font-weight:800;color:var(--txt);font-family:var(--font-mono)">₹{price:,.1f}</div>
    </div>
    <div style="background:var(--bg2);border:1px solid rgba(255,59,59,.3);border-radius:2px;padding:8px;text-align:center">
      <div style="font-size:8px;color:var(--txt3);text-transform:uppercase;letter-spacing:.1em;margin-bottom:3px">STOP</div>
      <div style="font-size:15px;font-weight:800;color:var(--red);font-family:var(--font-mono)">₹{sl:,.1f}</div>
    </div>
    <div style="background:var(--bg2);border:1px solid rgba(0,255,136,.2);border-radius:2px;padding:8px;text-align:center">
      <div style="font-size:8px;color:var(--txt3);text-transform:uppercase;letter-spacing:.1em;margin-bottom:3px">T1</div>
      <div style="font-size:15px;font-weight:800;color:var(--green);font-family:var(--font-mono)">₹{t1:,.1f}</div>
    </div>
    <div style="background:var(--bg2);border:1px solid rgba(0,255,136,.3);border-radius:2px;padding:8px;text-align:center">
      <div style="font-size:8px;color:var(--txt3);text-transform:uppercase;letter-spacing:.1em;margin-bottom:3px">T2</div>
      <div style="font-size:15px;font-weight:800;color:var(--green);font-family:var(--font-mono)">₹{t2:,.1f}</div>
    </div>
    <div style="background:var(--bg2);border:1px solid var(--border);border-radius:2px;padding:8px;text-align:center">
      <div style="font-size:8px;color:var(--txt3);text-transform:uppercase;letter-spacing:.1em;margin-bottom:3px">R:R</div>
      <div style="font-size:15px;font-weight:800;color:var(--blue);font-family:var(--font-mono)">1:{rr}</div>
    </div>
  </div>
  <div style="display:flex;justify-content:space-between;align-items:center">
    <div style="font-size:10px;color:var(--txt3);font-family:var(--font-mono)">VOL {vol}× surge</div>
    <div style="display:flex;gap:12px">
      <a href="{tv_link}" target="_blank" style="color:var(--accent);font-size:11px;font-weight:700;text-decoration:none;font-family:var(--font-mono)">CHART →</a>
      <a href="{nse_link}" target="_blank" style="color:var(--txt3);font-size:11px;font-weight:600;text-decoration:none;font-family:var(--font-mono)">NSE CHAIN →</a>
    </div>
  </div>
</div>
""", unsafe_allow_html=True)
    st.markdown('<div style="font-size:10px;color:var(--txt3);padding:8px;border:1px solid var(--border);border-radius:2px;font-family:var(--font-mono)">⚠ Verify premium, IV, OI on NSE independently. Not SEBI advice.</div>', unsafe_allow_html=True)

    # Forex watchlist
    st.markdown("---")
    st.markdown('<div style="font-size:12px;font-weight:700;color:#22c55e;margin-bottom:10px">Global Markets</div>', unsafe_allow_html=True)
    fc = _forex()
    if fc:
        cols = st.columns(len(fc))
        for i, r in enumerate(fc):
            c = "#4ade80" if r["Chg%"] >= 0 else "#f87171"
            s = "+" if r["Chg%"] >= 0 else ""
            cols[i].markdown(f"""
<div style="background:#0a1929;border:1px solid #052e16;border-radius:8px;padding:10px;text-align:center">
  <div style="font-size:9px;color:#334155;text-transform:uppercase;letter-spacing:.07em;margin-bottom:3px">{r['Asset']}</div>
  <div style="font-size:15px;font-weight:700;color:#f1f5f9;font-family:'JetBrains Mono',monospace">{r['Last']}</div>
  <div style="font-size:11px;font-weight:600;color:{c}">{s}{r['Chg%']}%</div>
</div>
""", unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════════════════════
# TAB 4 — MUTUAL FUNDS
# ══════════════════════════════════════════════════════════════════════════════
with tab4:
    st.markdown("""
<div style="display:flex;align-items:flex-end;justify-content:space-between;margin-bottom:20px;flex-wrap:wrap;gap:8px">
  <div>
    <div style="font-size:22px;font-weight:900;color:var(--txt);letter-spacing:-.03em;font-family:var(--font-sans)">Mutual Funds</div>
    <div style="font-size:11px;color:var(--txt3);margin-top:4px;font-family:var(--font-mono)">
      <span class="live"></span> Top Funds · Portfolio Tracker · NAV · CAGR Returns &nbsp;·&nbsp; AMFI data
    </div>
  </div>
</div>
""", unsafe_allow_html=True)

    # ── Top Funds per Category ─────────────────────────────────────────────
    st.markdown('<div style="font-size:10px;font-weight:800;color:var(--txt3);text-transform:uppercase;letter-spacing:.14em;margin-bottom:12px;font-family:var(--font-mono);border-left:2px solid var(--accent);padding-left:8px">Top Funds by Category</div>', unsafe_allow_html=True)
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
                    _pc = ["#22c55e","#22c55e","#a78bfa","#f59e0b","#f87171","#34d399","#fb923c","#e879f9","#94a3b8","#64748b"]
                    _pbg  = "#0d1117"
                    _pfg  = "#94a3b8"
                    _pgrd = "#1a2030"

                    # ── Sector allocation — horizontal bar ─────────────────────
                    sec      = hd["sectors"]
                    sec_keys = list(sec.keys())[:9]
                    sec_vals = [sec[k] for k in sec_keys]
                    fig_s = go.Figure()
                    for i, (k, v) in enumerate(zip(sec_keys, sec_vals)):
                        fig_s.add_trace(go.Bar(
                            x=[v], y=["Sector"], orientation="h",
                            name=k, marker_color=_pc[i % len(_pc)],
                            hovertemplate=f"{k}: {v:.1f}%<extra></extra>",
                            text=f"{k[:12]} {v:.1f}%" if v > 4 else "",
                            textposition="inside",
                            textfont=dict(size=9, color="#111827"),
                        ))
                    fig_s.update_layout(
                        barmode="stack", height=100,
                        paper_bgcolor=_pbg, plot_bgcolor=_pbg,
                        font=dict(color=_pfg, size=10),
                        margin=dict(l=0,r=0,t=0,b=0),
                        showlegend=False,
                        xaxis=dict(showticklabels=False, showgrid=False, zeroline=False, range=[0,100]),
                        yaxis=dict(showticklabels=False, showgrid=False, zeroline=False),
                    )
                    # Sector legend as HTML pills
                    sec_pills = "".join(
                        f'<span style="display:inline-flex;align-items:center;gap:4px;margin:3px 6px 3px 0;'
                        f'font-size:10px;color:{_pc[i%len(_pc)]}">'
                        f'<span style="width:8px;height:8px;border-radius:50%;background:{_pc[i%len(_pc)]};display:inline-block"></span>'
                        f'{k} <span style="color:#475569">{v:.1f}%</span></span>'
                        for i, (k, v) in enumerate(zip(sec_keys, sec_vals))
                    )
                    st.markdown('<div style="font-size:10px;font-weight:700;color:#94a3b8;margin-bottom:6px;text-transform:uppercase;letter-spacing:.08em">Sector Allocation</div>', unsafe_allow_html=True)
                    st.plotly_chart(fig_s, use_container_width=True, key=f"pie_s_{cat}_{_sel_idx}")
                    st.markdown(f'<div style="line-height:1.8">{sec_pills}</div>', unsafe_allow_html=True)

                    st.markdown("<div style='height:14px'></div>", unsafe_allow_html=True)

                    # ── Top holdings — horizontal bars sorted by weight ─────────
                    scripts  = hd["top_scripts"]
                    h_labels = [s[0] for s in scripts[:10]]
                    h_vals   = [s[1] for s in scripts[:10]]
                    others   = max(0, 100 - sum(h_vals))
                    if others > 0.5:
                        h_labels.append("Others"); h_vals.append(round(others,1))
                    # Sort descending
                    paired = sorted(zip(h_vals, h_labels), reverse=True)
                    h_vals, h_labels = [p[0] for p in paired], [p[1] for p in paired]
                    fig_h = go.Figure()
                    fig_h.add_trace(go.Bar(
                        x=h_vals, y=h_labels, orientation="h",
                        marker=dict(
                            color=h_vals,
                            colorscale=[[0,"#052e16"],[0.5,"#22c55e"],[1.0,"#22c55e"]],
                            line=dict(color=_pgrd, width=0),
                        ),
                        text=[f"{v:.1f}%" for v in h_vals],
                        textposition="outside",
                        textfont=dict(size=10, color=_pfg),
                        hovertemplate="%{y}: %{x:.1f}%<extra></extra>",
                    ))
                    fig_h.update_layout(
                        height=max(200, len(h_labels) * 26),
                        paper_bgcolor=_pbg, plot_bgcolor=_pbg,
                        font=dict(color=_pfg, size=10),
                        margin=dict(l=4, r=60, t=4, b=4),
                        showlegend=False,
                        xaxis=dict(showgrid=True, gridcolor=_pgrd, showticklabels=False, zeroline=False),
                        yaxis=dict(showgrid=False, zeroline=False, tickfont=dict(size=10, color=_pfg)),
                    )
                    st.markdown('<div style="font-size:10px;font-weight:700;color:#94a3b8;margin-bottom:6px;text-transform:uppercase;letter-spacing:.08em">Top Holdings (% weight)</div>', unsafe_allow_html=True)
                    st.plotly_chart(fig_h, use_container_width=True, key=f"pie_h_{cat}_{_sel_idx}")
                    st.caption(f"{_sf['fund_house']}  ·  Holdings approx — last AMC monthly disclosure")
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

            pnl_color = "#00ff88" if total_pnl >= 0 else "#ff3b3b"
            pct_color = "#00ff88" if total_pct >= 0 else "#ff3b3b"
            st.markdown(f"""
<div class="mf-summary-row">
  <div class="mf-summary-card" style="--mf-top-color:#4da6ff">
    <div class="s-label">Total Invested</div>
    <div class="s-val">₹{total_inv:,.0f}</div>
  </div>
  <div class="mf-summary-card" style="--mf-top-color:#00ff88">
    <div class="s-label">Current Value</div>
    <div class="s-val">₹{total_cur:,.0f}</div>
  </div>
  <div class="mf-summary-card" style="--mf-top-color:{pnl_color}">
    <div class="s-label">Total P&amp;L</div>
    <div class="s-val" style="color:{pnl_color}">₹{total_pnl:+,.0f}</div>
  </div>
  <div class="mf-summary-card" style="--mf-top-color:{pct_color}">
    <div class="s-label">Overall Return</div>
    <div class="s-val" style="color:{pct_color}">{total_pct:+.2f}%</div>
  </div>
</div>
""", unsafe_allow_html=True)

            # Fund cards
            for s in summary:
                pnl_col   = "#00ff88" if s["pnl_pct"] >= 0 else "#ff3b3b"
                day_col   = "#00ff88" if s["day_chg"] >= 0 else "#ff3b3b"
                mf_accent = "#00ff88" if s["pnl_pct"] >= 0 else "#ff3b3b"
                ret       = s["returns"]
                # Build return bars: scale to max abs return for bar width
                _ret_vals  = [v for v in ret.values() if v is not None]
                _max_abs   = max(abs(v) for v in _ret_vals) if _ret_vals else 20
                _max_abs   = max(_max_abs, 1)

                def _ret_bar_row(period, val):
                    if val is None: return ""
                    bar_w   = min(100, abs(val) / _max_abs * 100)
                    bar_col = "#00ff88" if val >= 0 else "#ff3b3b"
                    return (
                        f'<div class="mf-return-row">'
                        f'<span class="mf-return-period">{period}</span>'
                        f'<div class="mf-return-bar-track">'
                        f'<div class="mf-return-bar-fill" style="width:{bar_w:.0f}%;background:{bar_col}"></div>'
                        f'</div>'
                        f'<span class="mf-return-val" style="color:{bar_col}">{val:+.1f}%</span>'
                        f'</div>'
                    )

                ret_bars_html = "".join(_ret_bar_row(k, v) for k, v in ret.items())
                cat_label = s.get('category','') or ''
                fhouse    = s.get('fund_house','') or ''

                st.markdown(f"""
<div class="mf-card" style="--mf-accent:{mf_accent}">
  <div class="mf-fund-header">
    <div>
      <div class="mf-fund-name">{s['name']}</div>
      <div class="mf-fund-house">{fhouse}</div>
      {f'<span class="mf-cat-chip">{cat_label}</span>' if cat_label else ''}
    </div>
    <div class="mf-nav-block">
      <div class="mf-nav-val">₹{s['nav']:.4f}</div>
      <div class="mf-day-chg" style="color:{day_col}">{s['day_chg']:+.2f}% today</div>
    </div>
  </div>
  <div class="mf-stats-row">
    <div class="mf-stat">
      <div class="mf-stat-label">Invested</div>
      <div class="mf-stat-val">₹{s['invested']:,.0f}</div>
    </div>
    <div class="mf-stat">
      <div class="mf-stat-label">Current</div>
      <div class="mf-stat-val">₹{s['current']:,.0f}</div>
    </div>
    <div class="mf-stat">
      <div class="mf-stat-label">P&amp;L</div>
      <div class="mf-stat-val" style="color:{pnl_col}">₹{s['pnl']:+,.0f} <span style="font-size:11px">({s['pnl_pct']:+.1f}%)</span></div>
    </div>
  </div>
  <div style="display:flex;gap:18px;font-size:10px;color:var(--txt3);font-family:var(--font-mono);margin-bottom:12px">
    <span>Units <b style="color:var(--txt)">{s['units']:.3f}</b></span>
    <span>Buy NAV <b style="color:var(--txt)">₹{s['purchase_nav']:.2f}</b></span>
  </div>
  <div class="mf-returns-section">
    <div class="mf-returns-label">CAGR Returns</div>
    {ret_bars_html}
  </div>
</div>
""", unsafe_allow_html=True)

            # News section
            st.markdown('<div style="margin-top:18px"></div>', unsafe_allow_html=True)
            st.markdown('<div style="font-size:12px;font-weight:700;color:var(--accent);margin-bottom:12px;letter-spacing:.06em;text-transform:uppercase;font-family:var(--font-mono)">📰 Fund News</div>', unsafe_allow_html=True)
            selected_fund = st.selectbox("News for", [s["name"] for s in summary])
            sel_s = next((s for s in summary if s["name"] == selected_fund), None)
            if sel_s:
                with st.spinner("Loading news…"):
                    news = get_fund_news(selected_fund[:40])
                if news:
                    fn_html = '<div class="news-grid">'
                    for n in news:
                        fn_html += f"""
<a href="{n['link']}" target="_blank" class="news-card" style="text-decoration:none">
  <span class="news-headline">{n['title']}</span>
  <div class="news-meta"><span class="news-time">{n['published']}</span></div>
</a>"""
                    fn_html += '</div>'
                    st.markdown(fn_html, unsafe_allow_html=True)
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
# TAB MB — POTENTIAL MULTIBAGGERS (weekly, Saturday scan)
# ══════════════════════════════════════════════════════════════════════════════
with tab_mb:
    st.markdown("""
    <div style="margin-bottom:18px">
      <span style="font-size:20px;font-weight:900;color:#f59e0b;letter-spacing:-.02em">🚀 Potential Multibaggers</span>
      <div style="font-size:11px;color:#64748b;margin-top:4px">Weekly scan · Nifty 500 · Updated every Saturday 9:30 AM IST · Horizon 6–12 months</div>
    </div>
    """, unsafe_allow_html=True)

    if IS_LOCAL:
        from tracker import get_multibaggers
        mb_df = get_multibaggers(days=7)
    else:
        mb_df = _gh_multibaggers(days=7)

    if mb_df.empty:
        st.info("No multibagger data yet. Next scan: Saturday 9:30 AM IST.")
    else:
        mbs = mb_df.to_dict("records")
        mk1, mk2, mk3, mk4 = st.columns(4)
        mk1.metric("Candidates", len(mbs))
        fno_cnt = sum(1 for m in mbs if m.get("fno"))
        mk2.metric("F&O Eligible", fno_cnt)
        avg_rr = round(sum(m.get("rr",0) for m in mbs) / len(mbs), 1) if mbs else 0
        mk3.metric("Avg RR", avg_rr)
        top_score = round(max(m.get("score",0) for m in mbs), 1) if mbs else 0
        mk4.metric("Top Score", top_score)

        st.markdown("<div style='height:12px'></div>", unsafe_allow_html=True)

        for i, m in enumerate(mbs, 1):
            fno_tag = ' <span style="background:#1e40af;color:#93c5fd;font-size:9px;font-weight:700;padding:2px 6px;border-radius:99px;margin-left:6px">F&O</span>' if m.get("fno") else ""
            pe_str  = f'<span style="color:#94a3b8;font-size:11px"> · PE {m["pe"]:.0f}x</span>' if m.get("pe") else ""
            score   = m.get("score", 0)
            score_color = "#22c55e" if score >= 70 else "#f59e0b" if score >= 55 else "#64748b"
            tv_link = m.get("tv_link", f"https://in.tradingview.com/chart/?symbol=NSE:{m['symbol']}")
            st.markdown(f"""
            <div style="background:#0f172a;border:1px solid #1e293b;border-radius:12px;padding:14px 18px;margin-bottom:10px">
              <div style="display:flex;justify-content:space-between;align-items:flex-start">
                <div>
                  <span style="font-size:16px;font-weight:900;color:#f1f5f9">{i}. {m['symbol']}</span>{fno_tag}
                  <span style="font-size:13px;color:#94a3b8;margin-left:10px">₹{m['price']}</span>{pe_str}
                </div>
                <span style="font-size:13px;font-weight:700;color:{score_color}">Score {score:.0f}</span>
              </div>
              <div style="display:flex;gap:24px;margin-top:8px;flex-wrap:wrap">
                <span style="font-size:11px;color:#64748b">T1 <span style="color:#22c55e;font-weight:700">₹{m['target1']}</span></span>
                <span style="font-size:11px;color:#64748b">T2 <span style="color:#22c55e;font-weight:700">₹{m['target2']}</span></span>
                <span style="font-size:11px;color:#64748b">T3 <span style="color:#22c55e;font-weight:700">₹{m.get('target3', m['target2'])}</span></span>
                <span style="font-size:11px;color:#64748b">SL <span style="color:#f87171;font-weight:700">₹{m['sl']}</span></span>
                <span style="font-size:11px;color:#64748b">RR <span style="color:#22c55e;font-weight:700">{m['rr']}</span></span>
                <span style="font-size:11px;color:#64748b">Wk RSI <span style="color:#c4b5fd;font-weight:700">{m.get('wk_rsi','')}</span></span>
                <span style="font-size:11px;color:#64748b">ADX <span style="color:#fbbf24;font-weight:700">{m.get('wk_adx','')}</span></span>
                <span style="font-size:11px;color:#64748b">Vol <span style="color:#fb923c;font-weight:700">{m.get('vol_ratio','')}x</span></span>
                <span style="font-size:11px;color:#64748b">52W pos <span style="color:#94a3b8;font-weight:700">{m.get('range_pos','')}%</span></span>
              </div>
              <div style="margin-top:6px;font-size:10px;color:#475569">{m.get('reason','')} · <a href="{tv_link}" target="_blank" style="color:#22c55e;text-decoration:none">TradingView ↗</a></div>
            </div>
            """, unsafe_allow_html=True)

        st.download_button("Export CSV", mb_df.to_csv(index=False), "multibaggers.csv", "text/csv")
        st.markdown('<div style="font-size:10px;color:#334155;margin-top:8px">Weekly breakout + momentum + volume expansion · Not SEBI advice · Horizon 6–12 months</div>', unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════════════════════
# TAB OHL — OPEN=HIGH / OPEN=LOW INTRADAY SCREENER (Nifty 200 · 15m first candle)
# ══════════════════════════════════════════════════════════════════════════════
with tab_ohl:
    _now_ist_ohl = datetime.now(IST)
    _before_open = _now_ist_ohl.hour < 9 or (_now_ist_ohl.hour == 9 and _now_ist_ohl.minute < 30)

    st.markdown("""
<div style="display:flex;align-items:flex-end;justify-content:space-between;margin-bottom:20px;flex-wrap:wrap;gap:8px">
  <div>
    <div style="font-size:22px;font-weight:900;color:var(--txt);letter-spacing:-.03em;font-family:var(--font-sans)">OHL / OLL Screener</div>
    <div style="font-size:11px;color:var(--txt3);margin-top:4px;font-family:var(--font-mono)">
      Nifty 200 &nbsp;·&nbsp; First 15-min candle (9:15 AM IST) &nbsp;·&nbsp; 1H RSI filter
    </div>
  </div>
  <div style="display:flex;gap:10px;flex-wrap:wrap">
    <div style="background:rgba(0,255,136,.07);border:1px solid rgba(0,255,136,.2);border-radius:8px;padding:8px 14px;font-size:11px;font-family:var(--font-mono)">
      <span style="color:var(--txt3)">OLL (Bullish)</span> &nbsp;
      <span style="color:var(--green);font-weight:800">Open = Low &nbsp;·&nbsp; 1H RSI ≥ 46</span>
    </div>
    <div style="background:rgba(255,59,59,.06);border:1px solid rgba(255,59,59,.2);border-radius:8px;padding:8px 14px;font-size:11px;font-family:var(--font-mono)">
      <span style="color:var(--txt3)">OHL (Bearish)</span> &nbsp;
      <span style="color:var(--red);font-weight:800">Open = High &nbsp;·&nbsp; 1H RSI ≤ 54</span>
    </div>
  </div>
</div>
""", unsafe_allow_html=True)

    if _before_open:
        st.markdown("""
<div style="text-align:center;padding:48px 0">
  <div style="font-size:32px">🕐</div>
  <div style="font-size:14px;color:var(--txt2);margin-top:10px;font-family:var(--font-mono)">
    Market opens at 9:15 AM IST
  </div>
  <div style="font-size:11px;color:var(--txt3);margin-top:6px">
    First 15-min candle closes at 9:30 AM — screener activates then
  </div>
</div>
""", unsafe_allow_html=True)
    else:
        _scan_ts = _now_ist_ohl.strftime("%I:%M %p IST")
        col_ref, col_btn = st.columns([5, 1])
        with col_ref:
            st.markdown(f'<div style="font-size:10px;color:var(--txt3);font-family:var(--font-mono);padding-top:8px"><span class="live"></span> Scanning Nifty 200 · First candle 9:15 AM · Cached 15 min · As of {_scan_ts}</div>', unsafe_allow_html=True)
        with col_btn:
            if st.button("↺ Rescan", use_container_width=True):
                st.cache_data.clear()
                st.rerun()

        with st.spinner("Scanning Nifty 200 for OHL / OLL setups… (first run ~30s)"):
            _ohl_results = _ohl_oll_scan()

        if not _ohl_results:
            st.markdown("""
<div style="text-align:center;padding:48px 0">
  <div style="font-size:28px">🔍</div>
  <div style="font-size:13px;color:var(--txt2);margin-top:10px;font-family:var(--font-mono)">No OHL / OLL setups found today</div>
  <div style="font-size:10px;color:var(--txt3);margin-top:6px">All 200 stocks checked · RSI filter applied</div>
</div>
""", unsafe_allow_html=True)
        else:
            _oll = [r for r in _ohl_results if r["type"] == "OLL"]
            _ohl_list = [r for r in _ohl_results if r["type"] == "OHL"]

            _oll_active    = [r for r in _oll      if not r["broken"]]
            _oll_broken    = [r for r in _oll      if r["broken"]]
            _ohl_active    = [r for r in _ohl_list if not r["broken"]]
            _ohl_broken    = [r for r in _ohl_list if r["broken"]]
            _total_active  = len(_oll_active) + len(_ohl_active)
            _total_broken  = len(_oll_broken) + len(_ohl_broken)

            # Summary metrics
            st.markdown(f"""
<div style="display:flex;gap:12px;margin-bottom:20px;flex-wrap:wrap">
  <div style="background:var(--bg2);border:1px solid var(--border);border-top:2px solid var(--green);border-radius:8px;padding:12px 18px;min-width:130px">
    <div style="font-size:8px;font-weight:700;color:var(--txt3);letter-spacing:.12em;text-transform:uppercase;font-family:var(--font-mono);margin-bottom:6px">OLL Active</div>
    <div style="font-size:28px;font-weight:800;font-family:var(--font-mono);color:var(--green)">{len(_oll_active)}</div>
  </div>
  <div style="background:var(--bg2);border:1px solid var(--border);border-top:2px solid var(--red);border-radius:8px;padding:12px 18px;min-width:130px">
    <div style="font-size:8px;font-weight:700;color:var(--txt3);letter-spacing:.12em;text-transform:uppercase;font-family:var(--font-mono);margin-bottom:6px">OHL Active</div>
    <div style="font-size:28px;font-weight:800;font-family:var(--font-mono);color:var(--red)">{len(_ohl_active)}</div>
  </div>
  <div style="background:var(--bg2);border:1px solid var(--border);border-top:2px solid var(--accent);border-radius:8px;padding:12px 18px;min-width:130px">
    <div style="font-size:8px;font-weight:700;color:var(--txt3);letter-spacing:.12em;text-transform:uppercase;font-family:var(--font-mono);margin-bottom:6px">Active Total</div>
    <div style="font-size:28px;font-weight:800;font-family:var(--font-mono);color:var(--accent)">{_total_active}</div>
  </div>
  <div style="background:var(--bg2);border:1px solid rgba(255,170,0,.3);border-top:2px solid var(--amber);border-radius:8px;padding:12px 18px;min-width:130px">
    <div style="font-size:8px;font-weight:700;color:var(--txt3);letter-spacing:.12em;text-transform:uppercase;font-family:var(--font-mono);margin-bottom:6px">⚡ Broken</div>
    <div style="font-size:28px;font-weight:800;font-family:var(--font-mono);color:var(--amber)">{_total_broken}</div>
  </div>
</div>
""", unsafe_allow_html=True)

            def _render_ohl_cards(items, sig_type, broken=False):
                if broken:
                    border_col = "var(--amber)"
                    badge_bg   = "rgba(255,170,0,.08)"
                    badge_col  = "var(--amber)"
                    section_label = f"{sig_type} — ⚡ BROKEN ({len(items)} stocks)"
                    section_desc  = "Pattern violated intraday — open level breached"
                elif sig_type == "OLL":
                    border_col = "var(--green)"
                    badge_bg   = "rgba(0,255,136,.08)"
                    badge_col  = "var(--green)"
                    section_label = f"OLL — BULLISH · LONG BIAS ({len(items)} stocks)"
                    section_desc  = "Open = Low · 1H RSI ≥ 46 · price held above open"
                else:
                    border_col = "var(--red)"
                    badge_bg   = "rgba(255,59,59,.07)"
                    badge_col  = "var(--red)"
                    section_label = f"OHL — BEARISH · SHORT BIAS ({len(items)} stocks)"
                    section_desc  = "Open = High · 1H RSI ≤ 54 · price only fell from open"

                st.markdown(
                    f'<div style="font-size:10px;font-weight:800;color:var(--txt3);text-transform:uppercase;'
                    f'letter-spacing:.14em;margin:20px 0 10px;font-family:var(--font-mono);'
                    f'border-left:3px solid {border_col};padding-left:8px">'
                    f'{section_label}</div>'
                    f'<div style="font-size:9px;color:var(--txt3);font-family:var(--font-mono);margin-bottom:10px">{section_desc}</div>',
                    unsafe_allow_html=True
                )
                if not items:
                    st.markdown(f'<div style="font-size:11px;color:var(--txt3);padding:4px 0 12px;font-family:var(--font-mono)">None</div>', unsafe_allow_html=True)
                    return

                for r in items:
                    sym      = r["symbol"]
                    rsi      = r["rsi_1h"]
                    o        = r["open"]; h = r["high"]; l = r["low"]
                    c1       = r["close_1c"]; price = r["price"]
                    day_low  = r.get("day_low", l)
                    day_high = r.get("day_high", h)
                    chg_pct  = ((price - o) / o * 100) if o > 0 else 0
                    chg_col  = "var(--green)" if chg_pct >= 0 else "var(--red)"
                    rsi_bar  = min(100, rsi)
                    rsi_col  = "#00ff88" if rsi >= 55 else "#ffaa00" if rsi >= 46 else "#ff3b3b"
                    tv_link  = f"https://in.tradingview.com/chart/?symbol=NSE:{sym}"

                    # Broken: show how far open was violated
                    broken_detail = ""
                    if broken:
                        if sig_type == "OLL":
                            breach = round(o - day_low, 2)
                            breach_pct = round(breach / o * 100, 2)
                            broken_detail = f'<div style="background:rgba(255,170,0,.07);border:1px solid rgba(255,170,0,.2);border-radius:6px;padding:6px 12px;margin-bottom:10px;font-size:11px;font-family:var(--font-mono);color:var(--amber)">⚡ BROKEN — Day low ₹{day_low} went <b>{breach_pct}% below open ₹{o}</b></div>'
                        else:
                            breach = round(day_high - o, 2)
                            breach_pct = round(breach / o * 100, 2)
                            broken_detail = f'<div style="background:rgba(255,170,0,.07);border:1px solid rgba(255,170,0,.2);border-radius:6px;padding:6px 12px;margin-bottom:10px;font-size:11px;font-family:var(--font-mono);color:var(--amber)">⚡ BROKEN — Day high ₹{day_high} went <b>{breach_pct}% above open ₹{o}</b></div>'

                    card_opacity = "opacity:.65;" if broken else ""
                    status_badge = (
                        f'<span style="display:inline-block;margin-left:8px;padding:2px 9px;border-radius:99px;'
                        f'font-size:9px;font-weight:800;background:rgba(255,170,0,.1);color:var(--amber);'
                        f'border:1px solid rgba(255,170,0,.3);letter-spacing:.06em">⚡ BROKEN</span>'
                        if broken else
                        f'<span style="display:inline-block;margin-left:8px;padding:2px 9px;border-radius:99px;'
                        f'font-size:9px;font-weight:800;background:{badge_bg};color:{badge_col};'
                        f'border:1px solid {badge_col}44;letter-spacing:.06em">{sig_type} ✓ ACTIVE</span>'
                    )

                    st.markdown(f"""
<div class="card" style="border-left-color:{border_col};margin-bottom:10px;{card_opacity}">
  <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:10px">
    <div>
      <span style="font-size:18px;font-weight:900;color:var(--txt);font-family:var(--font-mono)">{sym}</span>
      {status_badge}
    </div>
    <div style="text-align:right">
      <div style="font-size:16px;font-weight:800;font-family:var(--font-mono);color:var(--txt)">₹{price}</div>
      <div style="font-size:11px;font-weight:700;color:{chg_col}">{chg_pct:+.2f}% from open</div>
    </div>
  </div>

  {broken_detail}

  <div style="display:grid;grid-template-columns:repeat(5,1fr);gap:7px;margin-bottom:12px">
    <div style="background:var(--bg3);border:1px solid var(--border);border-radius:6px;padding:7px 9px">
      <div style="font-size:7px;color:var(--txt3);text-transform:uppercase;letter-spacing:.1em;font-family:var(--font-mono);margin-bottom:3px">Open</div>
      <div style="font-size:13px;font-weight:800;font-family:var(--font-mono);color:var(--txt)">₹{o}</div>
    </div>
    <div style="background:var(--bg3);border:1px solid var(--border);border-radius:6px;padding:7px 9px">
      <div style="font-size:7px;color:var(--txt3);text-transform:uppercase;letter-spacing:.1em;font-family:var(--font-mono);margin-bottom:3px">1st High</div>
      <div style="font-size:13px;font-weight:800;font-family:var(--font-mono);color:var(--green)">₹{h}</div>
    </div>
    <div style="background:var(--bg3);border:1px solid var(--border);border-radius:6px;padding:7px 9px">
      <div style="font-size:7px;color:var(--txt3);text-transform:uppercase;letter-spacing:.1em;font-family:var(--font-mono);margin-bottom:3px">1st Low</div>
      <div style="font-size:13px;font-weight:800;font-family:var(--font-mono);color:var(--red)">₹{l}</div>
    </div>
    <div style="background:var(--bg3);border:1px solid var(--border);border-radius:6px;padding:7px 9px">
      <div style="font-size:7px;color:var(--txt3);text-transform:uppercase;letter-spacing:.1em;font-family:var(--font-mono);margin-bottom:3px">Day Low</div>
      <div style="font-size:13px;font-weight:800;font-family:var(--font-mono);color:{'var(--amber)' if broken and sig_type=='OLL' else 'var(--txt)'}">₹{day_low}</div>
    </div>
    <div style="background:var(--bg3);border:1px solid var(--border);border-radius:6px;padding:7px 9px">
      <div style="font-size:7px;color:var(--txt3);text-transform:uppercase;letter-spacing:.1em;font-family:var(--font-mono);margin-bottom:3px">Day High</div>
      <div style="font-size:13px;font-weight:800;font-family:var(--font-mono);color:{'var(--amber)' if broken and sig_type=='OHL' else 'var(--txt)'}">₹{day_high}</div>
    </div>
  </div>

  <div style="display:flex;align-items:center;gap:12px">
    <div style="font-size:9px;font-weight:700;color:var(--txt3);font-family:var(--font-mono);min-width:50px">1H RSI</div>
    <div style="flex:1;height:6px;background:var(--border);border-radius:3px;overflow:hidden">
      <div style="width:{rsi_bar:.0f}%;height:100%;background:{rsi_col};border-radius:3px"></div>
    </div>
    <div style="font-size:13px;font-weight:800;font-family:var(--font-mono);color:{rsi_col};min-width:36px;text-align:right">{rsi}</div>
    <a href="{tv_link}" target="_blank"
       style="font-size:10px;font-weight:700;color:var(--accent);text-decoration:none;
              padding:3px 10px;border:1px solid rgba(0,255,136,.25);border-radius:4px;white-space:nowrap">
      TV ↗
    </a>
  </div>
</div>
""", unsafe_allow_html=True)

            _render_ohl_cards(_oll_active,  "OLL", broken=False)
            _render_ohl_cards(_ohl_active,  "OHL", broken=False)
            if _oll_broken or _ohl_broken:
                st.markdown('<hr style="border-color:rgba(255,170,0,.2);margin:8px 0">', unsafe_allow_html=True)
                _render_ohl_cards(_oll_broken, "OLL", broken=True)
                _render_ohl_cards(_ohl_broken, "OHL", broken=True)

            st.markdown('<div style="font-size:9px;color:var(--txt3);margin-top:16px;font-family:var(--font-mono)">Tolerance: 0.02% (~1-2 ticks) · Universe: Nifty 200 · Broken = day low/high violated open · Data: Yahoo Finance · Not SEBI advice</div>', unsafe_allow_html=True)


# TAB 7 — HISTORY (all signals ever sent — permanent record)
# ══════════════════════════════════════════════════════════════════════════════
with tab7:
    if IS_LOCAL:
        try:
            hist = get_history()   # legacy signals table (local dev)
        except Exception:
            hist = pd.DataFrame()
        # Also try unified all_signals table (richer data)
        try:
            from tracker import _conn
            with _conn() as _hc:
                hist_all = pd.read_sql(
                    "SELECT * FROM all_signals ORDER BY date DESC", _hc)
        except Exception:
            hist_all = pd.DataFrame()
        # Prefer unified all_signals if available, fall back to legacy
        display_hist = hist_all if not hist_all.empty else hist
    else:
        # Cloud: read full all_signals JSON from GitHub (no day limit)
        display_hist = _gh_all_signals(days=9999)

    st.markdown('<div style="font-size:13px;font-weight:700;color:#22c55e;margin-bottom:12px">📋 Complete Signal History — All Trades Logged</div>', unsafe_allow_html=True)

    if not display_hist.empty:
        # ── Column order ──────────────────────────────────────────────────────
        priority_cols = ["date","signal_type","symbol","action","timeframe",
                         "entry","sl","target1","target2","rr","score","status","pnl_pct","r_multiple"]
        avail = [c for c in priority_cols if c in display_hist.columns]
        rest  = [c for c in display_hist.columns if c not in priority_cols
                 and c not in ("id","sent_at","metadata","exit_price","target3")]
        display_hist = display_hist[avail + rest].copy()

        # ── Format price/ratio columns as strings (kills .000000 problem) ──────
        def _fmt_price(v):
            try: return f"₹{float(v):,.2f}" if v not in (None,"") else "—"
            except: return str(v)
        def _fmt_ratio(v):
            try: return f"{float(v):.2f}" if v not in (None,"") else "—"
            except: return str(v)

        for col in ["entry","sl","target1","target2","exit_price"]:
            if col in display_hist.columns:
                display_hist[col] = display_hist[col].apply(_fmt_price)
        for col in ["rr","pnl_pct","r_multiple"]:
            if col in display_hist.columns:
                display_hist[col] = display_hist[col].apply(_fmt_ratio)

        # ── Rename for readability ────────────────────────────────────────────
        display_hist = display_hist.rename(columns={
            "signal_type":"type","pnl_pct":"P&L%","r_multiple":"R×",
            "target1":"T1","target2":"T2","timeframe":"TF"
        })

        # ── Status badge colour ────────────────────────────────────────────────
        def _style_status(val):
            colors = {"SL_HIT":"color:#ff3b3b;font-weight:700",
                      "T1_HIT":"color:#00ff88;font-weight:700",
                      "T2_HIT":"color:#4da6ff;font-weight:700",
                      "OPEN":  "color:#ffaa00;font-weight:700"}
            return colors.get(str(val), "color:#555")

        try:
            styled = (display_hist.style.map(_style_status, subset=["status"])
                      if "status" in display_hist.columns else display_hist)
        except Exception:
            styled = display_hist

        # Filter controls
        hf1, hf2 = st.columns([2,1])
        with hf1:
            status_filter = st.selectbox("Status", ["All","OPEN","SL_HIT","T1_HIT","T2_HIT"], key="hist_sf")
        with hf2:
            type_filter = st.selectbox("Type", ["All"] + sorted(display_hist["type"].dropna().unique().tolist()) if "type" in display_hist.columns else ["All"], key="hist_tf")
        filt_hist = display_hist.copy()
        if status_filter != "All" and "status" in filt_hist.columns:
            filt_hist = filt_hist[filt_hist["status"] == status_filter]
        if type_filter != "All" and "type" in filt_hist.columns:
            filt_hist = filt_hist[filt_hist["type"] == type_filter]

        st.dataframe(
            filt_hist.style.map(_style_status, subset=["status"]) if "status" in filt_hist.columns else filt_hist,
            use_container_width=True, hide_index=True,
            height=min(650, 44 + len(filt_hist) * 36)
        )
        open_c = len(display_hist[display_hist["status"]=="OPEN"]) if "status" in display_hist.columns else 0
        sl_c   = len(display_hist[display_hist["status"]=="SL_HIT"]) if "status" in display_hist.columns else 0
        win_c  = len(display_hist[display_hist["status"].isin(["T1_HIT","T2_HIT"])]) if "status" in display_hist.columns else 0
        st.caption(f"Total: {len(display_hist)} · Open: {open_c} · Win: {win_c} · SL: {sl_c}")
        st.download_button("⬇ Export CSV", display_hist.to_csv(index=False),
                           "signal_history.csv", "text/csv")
    else:
        st.info("No signal history yet. All Telegram alerts are auto-logged here after the next scan.")


# ── Footer (audit fixes §5 + §6 — trust + attribution) ───────────────────────
_footer_ts = datetime.now(IST).strftime("%d %b %Y · %I:%M %p IST")
st.markdown(f"""
<div style="margin-top:40px;border-top:1px solid #1a2030;padding:20px 0 10px;
  display:flex;flex-wrap:wrap;gap:20px;justify-content:space-between;align-items:flex-start">

  <div style="flex:1;min-width:220px">
    <div style="font-size:12px;font-weight:900;color:#f2f2f2;font-family:'JetBrains Mono',monospace;
      letter-spacing:.05em;margin-bottom:6px">TRADEFLOW AI <span style="color:#22c55e">PRO</span></div>
    <div style="font-size:10px;color:#334155;line-height:1.7">
      NSE Nifty 500 Swing Scanner<br>
      Signals updated: Mon–Fri · 9:20 AM · 11:45 AM · 4:30 PM IST<br>
      Weekly multibaggers: Every Saturday 9:30 AM IST
    </div>
  </div>

  <div style="flex:1;min-width:200px">
    <div style="font-size:9px;font-weight:700;color:#475569;text-transform:uppercase;
      letter-spacing:.1em;margin-bottom:6px">Data Sources</div>
    <div style="font-size:10px;color:#334155;line-height:1.7">
      Price data: <span style="color:#64748b">Yahoo Finance (yfinance)</span><br>
      Universe: <span style="color:#64748b">NSE India — Nifty 500 official list</span><br>
      Delay: <span style="color:#64748b">~15 min during market hours</span><br>
      Last page load: <span style="color:#22c55e">{_footer_ts}</span>
    </div>
  </div>

  <div style="flex:1;min-width:200px">
    <div style="font-size:9px;font-weight:700;color:#475569;text-transform:uppercase;
      letter-spacing:.1em;margin-bottom:6px">Legal</div>
    <div style="font-size:10px;color:#334155;line-height:1.7">
      ⚠ <b style="color:#475569">Not SEBI-registered.</b> Not financial advice.<br>
      Signals are for <b style="color:#475569">educational &amp; research</b> purposes only.<br>
      Past performance does not guarantee future results.<br>
      Trade at your own risk. Read all disclaimers.
    </div>
  </div>

  <div style="flex:1;min-width:180px">
    <div style="font-size:9px;font-weight:700;color:#475569;text-transform:uppercase;
      letter-spacing:.1em;margin-bottom:6px">Built By</div>
    <div style="font-size:10px;color:#334155;line-height:1.7">
      <b style="color:#64748b">Akshay K</b> · CA, FP&amp;A<br>
      📸 <a href="https://www.instagram.com/askakshayfinance" target="_blank"
        style="color:#22c55e;text-decoration:none">@askakshayfinance</a><br>
      Version 2.1 · May 2026<br>
      <span style="color:#1a2030">Signal engine: Python + yfinance</span>
    </div>
  </div>

</div>
<div style="text-align:center;padding:10px 0 4px;font-size:9px;color:#1e293b;letter-spacing:.04em">
  TradeFlow AI Pro · Not affiliated with NSE, BSE, SEBI or any broker · All rights reserved
</div>
""", unsafe_allow_html=True)

st.markdown('<div style="text-align:center;padding:16px 0 4px;font-size:10px;color:#1a2030">TradeFlow AI Pro · Personal Research · Not SEBI Advice</div>', unsafe_allow_html=True)
