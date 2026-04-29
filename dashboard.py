import streamlit as st
import plotly.graph_objects as go
import plotly.express as px
import pandas as pd
import yfinance as yf
import ta as ta_lib
from datetime import datetime
import pytz, os

from scanner import scan_all, scan_breakouts, fetch_forex_comm, obfuscate_reasons
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

# ── CSS ───────────────────────────────────────────────────────────────────────
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500&display=swap');
html,body,[class*="css"]{font-family:'Inter',sans-serif!important;background:#050c18!important;color:#e2e8f0!important}
.stApp{background:#050c18}
header[data-testid="stHeader"]{background:#050c18;border-bottom:1px solid #0f2035}
section[data-testid="stSidebar"]{background:#070f1e!important;border-right:1px solid #0f2035}
section[data-testid="stSidebar"] *{color:#94a3b8!important}
section[data-testid="stSidebar"] .stButton>button{background:linear-gradient(135deg,#0284c7,#0ea5e9)!important;color:#fff!important;border:none!important;border-radius:8px!important;font-weight:600!important;font-size:12px!important}
.stButton>button{background:linear-gradient(135deg,#0ea5e9,#38bdf8)!important;color:#fff!important;border:none!important;border-radius:8px!important;font-weight:700!important;font-size:13px!important;transition:all .2s;box-shadow:0 0 16px rgba(56,189,248,.2)}
.stTabs [data-baseweb="tab-list"]{background:#070f1e;border-bottom:1px solid #0f2035;padding:0 8px}
.stTabs [data-baseweb="tab"]{background:transparent;color:#475569!important;font-size:11px;font-weight:700;padding:12px 18px;border-bottom:2px solid transparent;text-transform:uppercase;letter-spacing:.05em}
.stTabs [aria-selected="true"]{color:#38bdf8!important;border-bottom:2px solid #38bdf8!important}
[data-testid="metric-container"]{background:#0a1929;border:1px solid #0f2d4a;border-radius:10px;padding:14px 18px}
[data-testid="metric-container"] label{color:#475569!important;font-size:10px!important;text-transform:uppercase;letter-spacing:.1em}
[data-testid="metric-container"] [data-testid="stMetricValue"]{color:#f1f5f9!important;font-size:22px!important;font-weight:800!important;font-family:'JetBrains Mono',monospace!important}
.stDataFrame{border:1px solid #0f2035!important;border-radius:8px;overflow:hidden}
.stDataFrame thead th{background:#070f1e!important;color:#38bdf8!important;font-size:10px!important;text-transform:uppercase;letter-spacing:.07em;font-weight:700}
.stDataFrame tbody tr{background:#050c18!important}
.stDataFrame tbody tr:hover{background:#0a1929!important}
.stDataFrame tbody td{color:#94a3b8!important;font-family:'JetBrains Mono',monospace;font-size:12px!important;border-color:#0f2035!important}
.stTextInput input,.stSelectbox [data-baseweb="select"]{background:#0a1929!important;border:1px solid #0f2d4a!important;color:#e2e8f0!important;border-radius:8px!important}
@keyframes fadeUp{from{opacity:0;transform:translateY(12px)}to{opacity:1;transform:translateY(0)}}
@keyframes glow{0%,100%{box-shadow:0 0 8px rgba(56,189,248,.15)}50%{box-shadow:0 0 24px rgba(56,189,248,.4)}}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
.card{background:#0a1929;border:1px solid #0f2d4a;border-left:3px solid #22c55e;border-radius:10px;padding:16px 18px;margin-bottom:12px;animation:fadeUp .35s ease}
.card:hover{border-color:#1e4a6e;transform:translateY(-1px);transition:all .2s}
.card.sell{border-left-color:#ef4444}
.card.top{animation:glow 2.5s ease-in-out infinite}
.badge{display:inline-block;padding:3px 10px;border-radius:99px;font-size:10px;font-weight:800;letter-spacing:.06em}
.badge.sb{background:rgba(34,197,94,.15);color:#4ade80;border:1px solid rgba(34,197,94,.3)}
.badge.b{background:rgba(56,189,248,.12);color:#38bdf8;border:1px solid rgba(56,189,248,.25)}
.badge.w{background:rgba(251,191,36,.1);color:#fbbf24;border:1px solid rgba(251,191,36,.25)}
.badge.fno{background:rgba(56,189,248,.1);color:#7dd3fc;border:1px solid rgba(56,189,248,.2);font-size:9px}
.conf{height:3px;background:#0f2035;border-radius:2px;margin:8px 0 10px;overflow:hidden}
.conf-fill{height:100%;border-radius:2px}
.row{display:flex;gap:16px;flex-wrap:wrap;margin:8px 0}
.kv{display:flex;flex-direction:column;min-width:55px}
.kv span:first-child{font-size:9px;color:#334155;text-transform:uppercase;letter-spacing:.07em}
.kv span:last-child{font-size:13px;font-weight:600;font-family:'JetBrains Mono',monospace;color:#e2e8f0}
.green{color:#4ade80!important}.red{color:#f87171!important}.blue{color:#38bdf8!important}
.tag{display:inline-block;padding:2px 8px;border-radius:99px;font-size:9px;font-weight:700;margin:2px;background:#052e16;color:#86efac;border:1px solid #166534}
.bo-card{background:#0a1929;border:1px solid #1a3a1a;border-left:3px solid #22c55e;border-radius:10px;padding:14px 16px;margin-bottom:10px;animation:fadeUp .35s ease}
.bo-card.weekly{border-left-color:#f59e0b;border-color:#3a2a0a}
.bo-card.monthly{border-left-color:#a78bfa;border-color:#1e1040;background:#0e0a1f}
.fno-card{background:#071e35;border:1px solid #0e3a5e;border-left:3px solid #38bdf8;border-radius:10px;padding:14px 16px;margin-bottom:10px;animation:fadeUp .35s ease}
.mf-card{background:#0a1929;border:1px solid #0f2d4a;border-radius:10px;padding:16px 18px;margin-bottom:12px;animation:fadeUp .35s ease}
.news-item{padding:10px 0;border-bottom:1px solid #0f2035}
.news-item:last-child{border-bottom:none}
.live{display:inline-block;width:7px;height:7px;background:#22c55e;border-radius:50%;margin-right:5px;animation:pulse 1.5s ease-in-out infinite;vertical-align:middle}
hr{border-color:#0f2035!important;margin:14px 0!important}
::-webkit-scrollbar{width:3px;height:3px}
::-webkit-scrollbar-track{background:#050c18}
::-webkit-scrollbar-thumb{background:#0f2d4a;border-radius:2px}
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
    st.markdown('<div style="font-size:18px;font-weight:800;color:#f1f5f9;padding:8px 0 14px">SwingDesk <span style="color:#38bdf8">Pro</span></div>', unsafe_allow_html=True)
    run_scan = st.button("Run Swing Scan", use_container_width=True)
    run_bo   = st.button("Run Breakout Scan", use_container_width=True)
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
<div style="background:#070f1e;border:1px solid #0f2035;border-radius:12px;padding:18px 22px;margin-bottom:16px;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:12px">
  <div>
    <div style="font-size:24px;font-weight:800;color:#f1f5f9;letter-spacing:-.02em">SwingDesk <span style="color:#38bdf8">Pro</span></div>
    <div style="font-size:11px;color:#334155;margin-top:2px">Nifty 500 · Breakouts · F&O · Mutual Funds · Forex</div>
  </div>
  <div style="display:flex;gap:20px;flex-wrap:wrap;align-items:center">
    <div style="text-align:center"><div style="font-size:20px;font-weight:800;color:#38bdf8;font-family:'JetBrains Mono',monospace">{sig_count}</div><div style="font-size:9px;color:#334155;text-transform:uppercase;letter-spacing:.08em">Signals</div></div>
    <div style="text-align:center"><div style="font-size:20px;font-weight:800;color:#22c55e;font-family:'JetBrains Mono',monospace">{bo_count}</div><div style="font-size:9px;color:#334155;text-transform:uppercase;letter-spacing:.08em">Breakouts</div></div>
    <div style="font-size:11px;color:#334155;font-family:'JetBrains Mono',monospace">{now_str}</div>
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
<div style="background:#070f1e;border:1px solid #0f2035;border-radius:8px;padding:8px 0;margin-bottom:14px;overflow:hidden">
  <div style="display:flex;align-items:center">
    <div style="background:#070f1e;padding:0 12px;border-right:1px solid #0f2035;margin-right:8px;flex-shrink:0">
      <span class="live"></span><span style="font-size:10px;font-weight:700;color:#22c55e;letter-spacing:.08em">LIVE</span>
    </div>
    <div style="overflow:hidden;flex:1">
      <marquee behavior="scroll" direction="left" scrollamount="4" style="display:block">{ticker_html}</marquee>
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
            rl, rc = _rating(s["score"])
            cc     = _conf_col(s["score"])
            fno_b  = '<span class="badge fno">F&amp;O</span>' if s.get("fno_eligible") else ""
            tags   = "".join(f'<span class="tag">{t.strip()}</span>'
                             for t in obfuscate_reasons(s["reasons"]).split(",") if t.strip())
            cls    = f"card {'top' if i==0 else ''} {'sell' if s['action']=='SELL' else ''}"

            st.markdown(f"""
<div class="{cls}">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px">
    <div style="display:flex;align-items:center;gap:8px">
      <span style="font-size:18px;font-weight:800;color:#f1f5f9">{s['symbol']}</span>
      {fno_b}
      <span style="font-size:10px;color:#334155">{s.get('setup_type','').replace('_',' ').title()}</span>
    </div>
    <div style="display:flex;align-items:center;gap:8px">
      <span style="font-size:12px;color:#475569">{_stars(s['score'])}</span>
      <span class="badge {rc}">{rl}</span>
    </div>
  </div>
  <div class="conf"><div class="conf-fill" style="width:{s['score']}%;background:{cc}"></div></div>
  <div class="row">
    <div class="kv"><span>Entry</span><span>₹{s['price']:,.1f}</span></div>
    <div class="kv"><span>Stop</span><span class="red">₹{s['sl2']:,.1f}</span></div>
    <div class="kv"><span>T1</span><span class="green">₹{s['target1']:,.1f}</span></div>
    <div class="kv"><span>T2</span><span class="green">₹{s['target2']:,.1f}</span></div>
    <div class="kv"><span>T3</span><span class="green">₹{s['target3']:,.1f}</span></div>
    <div class="kv"><span>RR</span><span class="blue">1:{s['rr1']}</span></div>
    <div class="kv"><span>Qty</span><span>{s['qty']}</span></div>
    <div class="kv"><span>Vol</span><span>{s['vol_ratio']:.1f}x</span></div>
  </div>
  <div style="margin-top:8px">{tags}</div>
  <div style="margin-top:8px"><a href="{s['tv_link']}" target="_blank" style="color:#38bdf8;font-size:11px;font-weight:600;text-decoration:none">Chart →</a></div>
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
        for s in sorted(fno_sigs, key=lambda x: x["score"], reverse=True):
            f     = s["fno_suggestion"]
            is_c  = f["direction"] == "CALL"
            dc    = "#4ade80" if is_c else "#f87171"
            di    = "▲ CALL" if is_c else "▼ PUT"
            rl,rc = _rating(s["score"])
            st.markdown(f"""
<div class="fno-card">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
    <span style="font-size:17px;font-weight:800;color:#f1f5f9">{s['symbol']}</span>
    <div style="display:flex;gap:10px;align-items:center">
      <span style="font-size:14px;font-weight:800;color:{dc}">{di}</span>
      <span class="badge {rc}">{rl}</span>
    </div>
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
                    r = f["returns"]
                    rows.append({
                        "Fund": f["short"],
                        "NAV": f"₹{f['nav']:.2f}",
                        "1Y %": f['1Y'] if f['1Y'] is not None else "—",
                        "3Y %": f['3Y'] if f['3Y'] is not None else "—",
                        "5Y %": f['5Y'] if f['5Y'] is not None else "—",
                    })
                df_top = pd.DataFrame(rows)

                def _style_ret(val):
                    if isinstance(val, float):
                        return f"color: {'#4ade80' if val >= 0 else '#f87171'}"
                    return "color: #94a3b8"

                st.dataframe(
                    df_top.style.map(_style_ret, subset=["1Y %", "3Y %", "5Y %"]),
                    use_container_width=True, hide_index=True
                )
                st.markdown("")
                for f in funds:
                    ret = f["returns"]
                    ret_label = " · ".join(
                        f'{k}: {v:+.1f}%' for k, v in ret.items() if v is not None
                    ) or "Insufficient history"
                    exp_label = f"{f['short']}  |  NAV ₹{f['nav']:.2f}  |  1Y: {f['1Y']:+.1f}%" if f['1Y'] else f"{f['short']}  |  NAV ₹{f['nav']:.2f}"
                    with st.expander(exp_label):
                        hd = get_fund_holdings(f['scheme_code'])
                        if hd:
                            pc1, pc2 = st.columns(2)
                            _pie_colors = ["#38bdf8","#22c55e","#a78bfa","#f59e0b","#f87171","#34d399","#fb923c","#e879f9","#94a3b8"]
                            with pc1:
                                st.markdown('<div style="font-size:10px;font-weight:700;color:#94a3b8;text-transform:uppercase;letter-spacing:.06em;margin-bottom:4px">Sector Allocation</div>', unsafe_allow_html=True)
                                sec = hd["sectors"]
                                fig_s = go.Figure(go.Pie(
                                    labels=list(sec.keys()), values=list(sec.values()),
                                    hole=0.45, textinfo="label+percent",
                                    textfont=dict(size=10, color="#e2e8f0"),
                                    marker=dict(colors=_pie_colors[:len(sec)], line=dict(color="#050c18", width=2)),
                                    hovertemplate="%{label}: %{value:.1f}%<extra></extra>"
                                ))
                                fig_s.update_layout(
                                    height=240, paper_bgcolor="#0a1929", plot_bgcolor="#0a1929",
                                    font=dict(color="#94a3b8", size=10),
                                    showlegend=False, margin=dict(l=4,r=4,t=4,b=4)
                                )
                                st.plotly_chart(fig_s, use_container_width=True)
                            with pc2:
                                st.markdown('<div style="font-size:10px;font-weight:700;color:#94a3b8;text-transform:uppercase;letter-spacing:.06em;margin-bottom:4px">Top Holdings</div>', unsafe_allow_html=True)
                                scripts = hd["top_scripts"]
                                s_labels = [s[0] for s in scripts]
                                s_vals   = [s[1] for s in scripts]
                                others   = max(0, 100 - sum(s_vals))
                                if others > 0.5:
                                    s_labels.append("Others")
                                    s_vals.append(round(others, 1))
                                fig_h = go.Figure(go.Pie(
                                    labels=s_labels, values=s_vals,
                                    hole=0.45, textinfo="label+percent",
                                    textfont=dict(size=10, color="#e2e8f0"),
                                    marker=dict(colors=_pie_colors[:len(s_labels)], line=dict(color="#050c18", width=2)),
                                    hovertemplate="%{label}: %{value:.1f}%<extra></extra>"
                                ))
                                fig_h.update_layout(
                                    height=240, paper_bgcolor="#0a1929", plot_bgcolor="#0a1929",
                                    font=dict(color="#94a3b8", size=10),
                                    showlegend=False, margin=dict(l=4,r=4,t=4,b=4)
                                )
                                st.plotly_chart(fig_h, use_container_width=True)
                        st.markdown(f'<div style="font-size:11px;color:#475569;margin-top:4px">{f["fund_house"]} &nbsp;·&nbsp; {ret_label}</div>', unsafe_allow_html=True)
                        st.markdown(f'<div style="font-size:10px;color:#334155;margin-top:4px">Holdings as of last monthly disclosure (approximate)</div>', unsafe_allow_html=True)

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
