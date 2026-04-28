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

st.set_page_config(page_title="SwingDesk Pro", layout="wide", page_icon="⚡",
                   initial_sidebar_state="expanded")
IST = pytz.timezone("Asia/Kolkata")
init_db()

# Detect cloud vs local (APScheduler + polling only on local — saves cloud memory)
IS_LOCAL = os.path.exists("config.py")

# ── CSS — $1000 trading terminal UI ──────────────────────────────────────────
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600&display=swap');

html,body,[class*="css"]{font-family:'Inter',sans-serif!important;background:#050c18!important;color:#e2e8f0!important}
.stApp{background:#050c18}
header[data-testid="stHeader"]{background:#050c18;border-bottom:1px solid #0f2035}

/* Sidebar */
section[data-testid="stSidebar"]{background:linear-gradient(180deg,#070f1e 0%,#050c18 100%)!important;border-right:1px solid #0f2035}
section[data-testid="stSidebar"] *{color:#94a3b8!important}
section[data-testid="stSidebar"] .stButton>button{
  background:linear-gradient(135deg,#0284c7,#0ea5e9)!important;
  color:#fff!important;border:none!important;border-radius:8px!important;
  font-weight:600!important;font-size:12px!important;letter-spacing:.03em;
  padding:8px 14px!important;transition:all .2s;box-shadow:0 0 12px rgba(14,165,233,.2)
}
section[data-testid="stSidebar"] .stButton>button:hover{box-shadow:0 0 20px rgba(14,165,233,.4);transform:translateY(-1px)}

/* Main scan button */
.stButton>button{
  background:linear-gradient(135deg,#0ea5e9,#38bdf8)!important;
  color:#fff!important;border:none!important;border-radius:8px!important;
  font-weight:700!important;font-size:13px!important;letter-spacing:.03em;
  padding:10px 20px!important;transition:all .25s;
  box-shadow:0 0 16px rgba(56,189,248,.25)
}
.stButton>button:hover{box-shadow:0 0 28px rgba(56,189,248,.5);transform:translateY(-2px)}

/* Tabs */
.stTabs [data-baseweb="tab-list"]{background:#070f1e;border-bottom:1px solid #0f2035;gap:0;padding:0 8px}
.stTabs [data-baseweb="tab"]{background:transparent;color:#475569!important;font-size:12px;font-weight:600;
  padding:12px 22px;border-bottom:2px solid transparent;letter-spacing:.04em;text-transform:uppercase}
.stTabs [aria-selected="true"]{color:#38bdf8!important;border-bottom:2px solid #38bdf8!important}

/* Metrics */
[data-testid="metric-container"]{
  background:linear-gradient(135deg,#0a1929,#0d1f35);
  border:1px solid #0f2d4a;border-radius:12px;padding:16px 20px;
  box-shadow:0 4px 24px rgba(0,0,0,.3)
}
[data-testid="metric-container"] label{color:#475569!important;font-size:10px!important;text-transform:uppercase;letter-spacing:.1em}
[data-testid="metric-container"] [data-testid="stMetricValue"]{
  color:#f1f5f9!important;font-size:24px!important;font-weight:800!important;
  font-family:'JetBrains Mono',monospace!important
}

/* Dataframe */
.stDataFrame{border:1px solid #0f2035!important;border-radius:10px;overflow:hidden}
.stDataFrame thead th{background:#070f1e!important;color:#38bdf8!important;
  font-size:10px!important;text-transform:uppercase;letter-spacing:.07em;font-weight:700}
.stDataFrame tbody tr{background:#050c18!important}
.stDataFrame tbody tr:hover{background:#0a1929!important}
.stDataFrame tbody td{color:#94a3b8!important;font-family:'JetBrains Mono',monospace;
  font-size:12px!important;border-color:#0f2035!important}

/* Inputs */
.stTextInput input,.stSelectbox [data-baseweb="select"]{
  background:#0a1929!important;border:1px solid #0f2d4a!important;
  color:#e2e8f0!important;border-radius:8px!important}
.stSlider [data-baseweb="slider"] [data-testid="stThumbValue"]{color:#38bdf8!important}

/* Animations */
@keyframes fadeInUp{from{opacity:0;transform:translateY(16px)}to{opacity:1;transform:translateY(0)}}
@keyframes glow{0%,100%{box-shadow:0 0 12px rgba(56,189,248,.2)}50%{box-shadow:0 0 28px rgba(56,189,248,.5)}}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
@keyframes shimmer{0%{background-position:-200% 0}100%{background-position:200% 0}}

/* Signal cards */
.sig-card{
  background:linear-gradient(135deg,#0a1929 0%,#0d2035 100%);
  border:1px solid #0f2d4a;border-left:3px solid #22c55e;
  border-radius:12px;padding:18px 20px;margin-bottom:14px;
  animation:fadeInUp .4s ease forwards;
  transition:all .25s;cursor:pointer
}
.sig-card:hover{border-color:#38bdf8;box-shadow:0 8px 32px rgba(56,189,248,.12);transform:translateY(-2px)}
.sig-card.bearish{border-left-color:#ef4444}
.sig-card.top{animation:glow 2s ease-in-out infinite}

/* Rating badge */
.rating{display:inline-flex;align-items:center;gap:6px;padding:4px 12px;border-radius:99px;font-size:11px;font-weight:800;letter-spacing:.06em}
.rating.strong-buy{background:rgba(34,197,94,.15);color:#4ade80;border:1px solid rgba(34,197,94,.3)}
.rating.buy{background:rgba(56,189,248,.12);color:#38bdf8;border:1px solid rgba(56,189,248,.3)}
.rating.watch{background:rgba(251,191,36,.1);color:#fbbf24;border:1px solid rgba(251,191,36,.3)}

/* Confidence bar */
.conf-wrap{display:flex;align-items:center;gap:10px;margin:6px 0}
.conf-bar{flex:1;height:4px;background:#0f2035;border-radius:2px;overflow:hidden}
.conf-fill{height:100%;border-radius:2px;transition:width .6s ease}

/* Stat row */
.stat-row{display:flex;gap:20px;flex-wrap:wrap;margin:10px 0}
.stat-item{display:flex;flex-direction:column;min-width:60px}
.stat-label{font-size:9px;color:#334155;text-transform:uppercase;letter-spacing:.08em;margin-bottom:2px}
.stat-value{font-size:13px;font-weight:600;color:#e2e8f0;font-family:'JetBrains Mono',monospace}
.stat-value.green{color:#4ade80}
.stat-value.red{color:#f87171}
.stat-value.blue{color:#38bdf8}

/* Tag pills */
.tag{display:inline-block;padding:2px 9px;border-radius:99px;font-size:10px;font-weight:600;margin:2px}
.tag-green{background:#052e16;color:#86efac;border:1px solid #166534}
.tag-blue{background:#0c2d4a;color:#7dd3fc;border:1px solid #0369a1}
.tag-gold{background:#292101;color:#fcd34d;border:1px solid #92400e}

/* FnO card */
.fno-card{
  background:linear-gradient(135deg,#071e35,#0a2540);
  border:1px solid #0e3a5e;border-left:3px solid #38bdf8;
  border-radius:12px;padding:16px 20px;margin-bottom:12px;
  animation:fadeInUp .4s ease forwards
}

/* Breakout card */
.bo-card{
  background:linear-gradient(135deg,#0f1e0f,#141f14);
  border:1px solid #1a3a1a;border-left:3px solid #22c55e;
  border-radius:12px;padding:16px 20px;margin-bottom:12px;
  animation:fadeInUp .4s ease forwards
}
.bo-card.weekly{border-left-color:#f59e0b}
.bo-card.monthly{border-left-color:#a78bfa;background:linear-gradient(135deg,#1a0f2e,#1e1040)}

/* Header gradient */
.hero-header{
  background:linear-gradient(135deg,#050c18 0%,#0a1929 50%,#050c18 100%);
  border:1px solid #0f2035;border-radius:16px;padding:24px 28px;margin-bottom:20px;
  position:relative;overflow:hidden
}
.hero-header::before{
  content:'';position:absolute;top:-50%;left:-50%;width:200%;height:200%;
  background:radial-gradient(ellipse at 60% 50%,rgba(56,189,248,.06) 0%,transparent 60%);
  animation:pulse 4s ease-in-out infinite
}

/* Live dot */
.live-dot{display:inline-block;width:7px;height:7px;background:#22c55e;border-radius:50%;
  margin-right:5px;animation:pulse 1.5s ease-in-out infinite;vertical-align:middle}

/* Scrollbar */
::-webkit-scrollbar{width:4px;height:4px}
::-webkit-scrollbar-track{background:#050c18}
::-webkit-scrollbar-thumb{background:#0f2d4a;border-radius:2px}
::-webkit-scrollbar-thumb:hover{background:#1e4a6e}

hr{border-color:#0f2035!important;margin:16px 0!important}
</style>
""", unsafe_allow_html=True)


# ── Upstox OAuth ──────────────────────────────────────────────────────────────
if "code" in st.query_params and not is_authenticated():
    try:
        exchange_code_for_token(st.query_params["code"])
        st.success("Upstox connected! Refresh the page.")
        st.stop()
    except Exception as e:
        st.error(f"Upstox login failed: {e}")

# Only run background threads on local (saves cloud memory)
if IS_LOCAL and "polling_started" not in st.session_state:
    start_command_polling()
    st.session_state["polling_started"] = True

if IS_LOCAL and "scheduler_started" not in st.session_state:
    from apscheduler.schedulers.background import BackgroundScheduler
    def _auto_scan():
        sigs = scan_all(min_score=st.session_state.get("min_score", MIN_SIGNAL_SCORE))
        st.session_state["signals"] = sigs
        st.session_state["last_scan"] = datetime.now(IST).strftime("%d %b %Y %I:%M %p IST")
        log_signals(sigs)
        update_outcomes()
        for s in sigs:
            send_alert(s)
        send_summary(sigs)
    _sch = BackgroundScheduler(timezone=IST)
    _sch.add_job(_auto_scan, "cron", hour=9,  minute=25)
    _sch.add_job(_auto_scan, "cron", hour=14, minute=0)
    _sch.add_job(_auto_scan, "cron", hour=17, minute=0)
    _sch.start()
    st.session_state["scheduler_started"] = True


# ── Cached data fetchers ──────────────────────────────────────────────────────
@st.cache_data(ttl=300)
def get_forex_data():
    return fetch_forex_comm()


# ── Helpers ───────────────────────────────────────────────────────────────────
def _rating(score):
    if score >= 85:
        return "STRONG BUY", "strong-buy"
    elif score >= 70:
        return "BUY", "buy"
    return "WATCH", "watch"

def _conf_color(score):
    if score >= 85: return "#22c55e"
    if score >= 70: return "#38bdf8"
    return "#f59e0b"

def _stars(score):
    n = 5 if score >= 90 else 4 if score >= 80 else 3 if score >= 70 else 2
    return "★" * n + "☆" * (5 - n)


def plot_chart(symbol, signal=None):
    df = yf.download(symbol + ".NS", period="6mo", interval="1d",
                     progress=False, auto_adjust=True)
    if df.empty:
        st.warning(f"No data for {symbol}")
        return
    close  = df["Close"].squeeze()
    ema20  = ta_lib.trend.EMAIndicator(close, window=20).ema_indicator()
    ema50  = ta_lib.trend.EMAIndicator(close, window=50).ema_indicator()
    ema200 = ta_lib.trend.EMAIndicator(close, window=200).ema_indicator()

    fig = go.Figure()
    fig.add_trace(go.Candlestick(
        x=df.index,
        open=df["Open"].squeeze(), high=df["High"].squeeze(),
        low=df["Low"].squeeze(), close=close, name="Price",
        increasing_line_color="#22c55e", decreasing_line_color="#ef4444",
        increasing_fillcolor="#052e16", decreasing_fillcolor="#450a0a"))
    fig.add_trace(go.Scatter(x=df.index, y=ema20,  name="Short",  line=dict(color="#facc15", width=1.5)))
    fig.add_trace(go.Scatter(x=df.index, y=ema50,  name="Mid",    line=dict(color="#38bdf8", width=1.5)))
    fig.add_trace(go.Scatter(x=df.index, y=ema200, name="Long",   line=dict(color="#f87171", width=1.5)))
    if signal:
        fig.add_hline(y=signal["sl2"],     line_color="#ef4444", line_dash="dash",
                      annotation_text="STOP", annotation_font_color="#ef4444")
        fig.add_hline(y=signal["target1"], line_color="#86efac", line_dash="dot",
                      annotation_text="T1", annotation_font_color="#86efac")
        fig.add_hline(y=signal["target2"], line_color="#4ade80", line_dash="dot",
                      annotation_text="T2", annotation_font_color="#4ade80")
        fig.add_hline(y=signal["target3"], line_color="#22c55e", line_dash="dot",
                      annotation_text="T3", annotation_font_color="#22c55e")
    fig.update_layout(
        xaxis_rangeslider_visible=False, height=460,
        paper_bgcolor="#070f1e", plot_bgcolor="#050c18",
        font=dict(color="#64748b", size=11, family="JetBrains Mono"),
        xaxis=dict(gridcolor="#0f2035", showgrid=True, zeroline=False),
        yaxis=dict(gridcolor="#0f2035", showgrid=True, zeroline=False),
        legend=dict(bgcolor="#070f1e", bordercolor="#0f2035", borderwidth=1,
                    font=dict(size=10)),
        margin=dict(l=8, r=8, t=8, b=8),
    )
    st.plotly_chart(fig, use_container_width=True)


# ── Sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("""
<div style="text-align:center;padding:12px 0 16px">
  <div style="font-size:20px;font-weight:800;letter-spacing:-.01em;color:#f1f5f9">
    Swing<span style="color:#38bdf8">Desk</span> <span style="font-size:11px;font-weight:600;color:#0ea5e9;background:#0c2d4a;padding:2px 7px;border-radius:4px">PRO</span>
  </div>
</div>
""", unsafe_allow_html=True)

    run_scan   = st.button("Run Full Scan", use_container_width=True)
    run_bo     = st.button("Run Breakout Scan", use_container_width=True)
    send_tg    = st.checkbox("Telegram alerts", value=True)

    st.markdown("**Min Signal Score**")
    min_score = st.slider("", 50, 100, MIN_SIGNAL_SCORE, label_visibility="collapsed")
    st.session_state["min_score"] = min_score

    st.markdown("**Chart Symbol**")
    chart_sym  = st.text_input("", placeholder="RELIANCE", label_visibility="collapsed")
    show_chart = st.button("Show Chart", use_container_width=True)

    st.markdown("---")
    if is_authenticated():
        st.markdown('<span class="live-dot"></span>Upstox Live', unsafe_allow_html=True)
    else:
        auth_url = get_auth_url()
        st.markdown(f"[Connect Upstox]({auth_url})")
        st.caption("Fallback: Yahoo Finance")

    st.markdown("---")
    if st.button("Test Telegram", use_container_width=True):
        test_connection()
        st.success("Sent!")
    if st.button("Update Outcomes", use_container_width=True):
        update_outcomes()
        st.success("Done!")

    st.markdown("---")
    st.caption("Auto: 9:25 · 14:00 · 17:00 IST")
    if "last_scan" in st.session_state:
        st.caption(f"Last: {st.session_state['last_scan']}")
    st.caption(f"Capital ₹{CAPITAL:,}")
    if not IS_LOCAL:
        st.caption("Cloud mode (GitHub Actions handles auto-scan)")


# ── Hero Header ───────────────────────────────────────────────────────────────
now_ist  = datetime.now(IST).strftime("%d %b %Y · %I:%M %p IST")
sig_count = len(st.session_state.get("signals", []))
bo_count  = len(st.session_state.get("breakouts", []))

st.markdown(f"""
<div class="hero-header">
  <div style="display:flex;justify-content:space-between;align-items:flex-start;flex-wrap:wrap;gap:12px">
    <div>
      <div style="font-size:28px;font-weight:800;letter-spacing:-.02em;color:#f1f5f9">
        SwingDesk <span style="color:#38bdf8">Pro</span>
      </div>
      <div style="font-size:12px;color:#334155;margin-top:4px;letter-spacing:.02em">
        Nifty 500 · F&O · Breakouts · Forex · ATR Risk Engine
      </div>
    </div>
    <div style="display:flex;gap:16px;flex-wrap:wrap">
      <div style="text-align:center">
        <div style="font-size:22px;font-weight:800;color:#38bdf8;font-family:'JetBrains Mono',monospace">{sig_count}</div>
        <div style="font-size:9px;color:#334155;text-transform:uppercase;letter-spacing:.08em">Signals</div>
      </div>
      <div style="text-align:center">
        <div style="font-size:22px;font-weight:800;color:#22c55e;font-family:'JetBrains Mono',monospace">{bo_count}</div>
        <div style="font-size:9px;color:#334155;text-transform:uppercase;letter-spacing:.08em">Breakouts</div>
      </div>
      <div style="text-align:center">
        <div style="font-size:12px;font-weight:600;color:#475569;font-family:'JetBrains Mono',monospace;padding-top:4px">{now_ist}</div>
        <div style="font-size:9px;color:#334155;text-transform:uppercase;letter-spacing:.08em">Market Time</div>
      </div>
    </div>
  </div>
</div>
""", unsafe_allow_html=True)


tab1, tab2, tab3, tab4, tab5 = st.tabs(["Signals", "Breakouts", "F&O", "Performance", "History"])


# ── TAB 1: Signals ────────────────────────────────────────────────────────────
with tab1:
    if show_chart and chart_sym:
        sig_map = {s["symbol"]: s for s in st.session_state.get("signals", [])}
        plot_chart(chart_sym.upper().strip(), sig_map.get(chart_sym.upper().strip()))

    if run_scan:
        with st.spinner("Scanning Nifty 500… (2-4 min)"):
            sigs = scan_all(min_score=min_score)
            st.session_state["signals"] = sigs
            st.session_state["last_scan"] = datetime.now(IST).strftime("%d %b %Y %I:%M %p IST")
            log_signals(sigs)
        if send_tg:
            for s in sigs:
                send_alert(s)
            send_summary(sigs)
        st.success(f"{len(sigs)} signals found!")
        st.rerun()

    signals = st.session_state.get("signals", [])

    if signals:
        # KPI row
        cols = st.columns(6)
        cols[0].metric("Signals", len(signals))
        cols[1].metric("Top Score", f"{signals[0]['score']}/100")
        cols[2].metric("Avg Score", f"{round(sum(s['score'] for s in signals)/len(signals),1)}")
        cols[3].metric("F&O Ready", sum(1 for s in signals if s.get("fno_eligible")))
        cols[4].metric("Avg RR", f"1:{round(sum(s['rr1'] for s in signals)/len(signals),1)}")
        vol_avg = round(sum(s["vol_ratio"] for s in signals)/len(signals), 1)
        cols[5].metric("Vol Surge", f"{vol_avg}x")

        st.markdown("---")

        sort_col = st.selectbox("Sort by", ["score", "rr1", "vol_ratio"], index=0,
                                label_visibility="visible")
        signals_sorted = sorted(signals, key=lambda x: x.get(sort_col, 0), reverse=True)

        for i, sig in enumerate(signals_sorted):
            rating_label, rating_cls = _rating(sig["score"])
            is_top = i == 0
            card_cls = f"sig-card {'top' if is_top else ''} {'bearish' if sig['action']=='SELL' else ''}"
            conf_pct = sig["score"]
            conf_col = _conf_color(sig["score"])
            fno_tag  = '<span class="tag tag-blue">F&O</span>' if sig.get("fno_eligible") else ""
            reasons_safe = obfuscate_reasons(sig["reasons"])
            tags_html = "".join(f'<span class="tag tag-green">{r.strip()}</span>'
                                for r in reasons_safe.split(",") if r.strip())

            st.markdown(f"""
<div class="{card_cls}">
  <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:10px">
    <div>
      <span style="font-size:20px;font-weight:800;color:#f1f5f9;letter-spacing:-.01em">{sig['symbol']}</span>
      {fno_tag}
      <span style="margin-left:10px;font-size:11px;color:#334155">{sig.get('setup_type','').replace('_',' ').title()}</span>
    </div>
    <div style="display:flex;align-items:center;gap:10px">
      <span style="font-size:13px;color:#475569">{_stars(sig['score'])}</span>
      <span class="rating {rating_cls}">{rating_label}</span>
    </div>
  </div>
  <div class="conf-wrap">
    <div class="conf-bar"><div class="conf-fill" style="width:{conf_pct}%;background:{conf_col}"></div></div>
    <span style="font-size:11px;font-family:'JetBrains Mono',monospace;color:{conf_col};font-weight:700">{conf_pct}%</span>
  </div>
  <div class="stat-row">
    <div class="stat-item"><span class="stat-label">Entry</span><span class="stat-value">₹{sig['price']:,.1f}</span></div>
    <div class="stat-item"><span class="stat-label">Stop</span><span class="stat-value red">₹{sig['sl2']:,.1f}</span></div>
    <div class="stat-item"><span class="stat-label">Target 1</span><span class="stat-value green">₹{sig['target1']:,.1f}</span></div>
    <div class="stat-item"><span class="stat-label">Target 2</span><span class="stat-value green">₹{sig['target2']:,.1f}</span></div>
    <div class="stat-item"><span class="stat-label">Target 3</span><span class="stat-value green">₹{sig['target3']:,.1f}</span></div>
    <div class="stat-item"><span class="stat-label">Risk:Reward</span><span class="stat-value blue">1:{sig['rr1']}</span></div>
    <div class="stat-item"><span class="stat-label">Qty</span><span class="stat-value">{sig['qty']}</span></div>
    <div class="stat-item"><span class="stat-label">Vol Surge</span><span class="stat-value">{sig['vol_ratio']:.1f}x</span></div>
  </div>
  <div style="margin-top:10px">{tags_html}</div>
  <div style="margin-top:10px;font-size:11px">
    <a href="{sig['tv_link']}" target="_blank" style="color:#38bdf8;text-decoration:none;font-weight:600">View Chart →</a>
  </div>
</div>
""", unsafe_allow_html=True)

        # Score distribution
        st.markdown("---")
        c1, c2 = st.columns([3, 1])
        with c1:
            df_sig = pd.DataFrame(signals_sorted)
            fig = px.bar(df_sig, x="symbol", y="score",
                         color="score", color_continuous_scale=["#0ea5e9","#22c55e"],
                         range_color=[60, 100])
            fig.update_layout(height=200, paper_bgcolor="#070f1e", plot_bgcolor="#050c18",
                              font=dict(color="#64748b", size=10),
                              xaxis=dict(gridcolor="#0f2035"),
                              yaxis=dict(gridcolor="#0f2035", range=[50, 100]),
                              margin=dict(l=8, r=8, t=8, b=8), showlegend=False,
                              coloraxis_showscale=False)
            st.plotly_chart(fig, use_container_width=True)
        with c2:
            picked = st.selectbox("Chart", [s["symbol"] for s in signals_sorted])
            sig_map2 = {s["symbol"]: s for s in signals_sorted}
            if st.button("View", key="view_sig_chart"):
                plot_chart(picked, sig_map2.get(picked))
            if picked in sig_map2:
                st.markdown(f"[TradingView]({sig_map2[picked]['tv_link']})")

        csv = df_sig.to_csv(index=False)
        st.download_button("Export CSV", csv, "signals.csv", "text/csv")

    elif "signals" in st.session_state:
        st.info("No signals this scan. Auto-scans: 9:25 AM · 2 PM · 5 PM IST")
    else:
        st.markdown("""
<div style="text-align:center;padding:60px 20px">
  <div style="font-size:48px;margin-bottom:16px">⚡</div>
  <div style="font-size:18px;font-weight:700;color:#1e3a5f">Ready to Scan</div>
  <div style="font-size:13px;color:#334155;margin-top:6px">
    Click <b>Run Full Scan</b> in sidebar · Auto-runs at 9:25 AM · 2 PM · 5 PM IST
  </div>
</div>
""", unsafe_allow_html=True)


# ── TAB 2: Breakouts ──────────────────────────────────────────────────────────
with tab2:
    st.markdown("""
<div style="margin-bottom:16px">
  <div style="font-size:15px;font-weight:700;color:#22c55e">Confirmed Breakouts</div>
  <div style="font-size:12px;color:#334155;margin-top:2px">
    Daily · Weekly · Monthly — only stocks that closed above key levels with volume confirmation
  </div>
</div>
""", unsafe_allow_html=True)

    if run_bo:
        with st.spinner("Scanning F&O universe for breakouts… (3-5 min)"):
            bos = scan_breakouts()
            st.session_state["breakouts"] = bos
        st.success(f"{len(bos)} breakouts found!")
        st.rerun()

    breakouts = st.session_state.get("breakouts", [])

    if breakouts:
        tf_counts = {}
        for b in breakouts:
            tf_counts[b["timeframe"]] = tf_counts.get(b["timeframe"], 0) + 1

        mc1, mc2, mc3, mc4 = st.columns(4)
        mc1.metric("Total Breakouts", len(breakouts))
        mc2.metric("Monthly", tf_counts.get("Monthly", 0))
        mc3.metric("Weekly",  tf_counts.get("Weekly", 0))
        mc4.metric("Daily",   tf_counts.get("Daily", 0))

        st.markdown("---")

        tf_filter = st.selectbox("Filter by timeframe", ["All", "Monthly", "Weekly", "Daily"])
        filtered  = [b for b in breakouts if tf_filter == "All" or b["timeframe"] == tf_filter]

        for b in filtered:
            tf   = b["timeframe"]
            cls  = {"Monthly": "monthly", "Weekly": "weekly", "Daily": ""}.get(tf, "")
            tf_c = {"Monthly": "#a78bfa", "Weekly": "#f59e0b", "Daily": "#22c55e"}.get(tf, "#22c55e")
            fno_tag = '<span class="tag tag-blue">F&O</span>' if b.get("fno") else ""
            pats  = " · ".join(b.get("patterns", [b["pattern"]]))

            st.markdown(f"""
<div class="bo-card {cls}">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
    <div>
      <span style="font-size:18px;font-weight:800;color:#f1f5f9">{b['symbol']}</span>
      {fno_tag}
    </div>
    <span style="font-size:12px;font-weight:700;color:{tf_c};background:rgba(0,0,0,.3);
      padding:3px 10px;border-radius:99px;border:1px solid {tf_c}40">{tf.upper()}</span>
  </div>
  <div style="font-size:11px;color:#64748b;margin-bottom:10px">{pats}</div>
  <div class="stat-row">
    <div class="stat-item"><span class="stat-label">Price</span><span class="stat-value">₹{b['price']:,.1f}</span></div>
    <div class="stat-item"><span class="stat-label">Stop</span><span class="stat-value red">₹{b['sl']:,.1f}</span></div>
    <div class="stat-item"><span class="stat-label">Target 1</span><span class="stat-value green">₹{b['target1']:,.1f}</span></div>
    <div class="stat-item"><span class="stat-label">Target 2</span><span class="stat-value green">₹{b['target2']:,.1f}</span></div>
    <div class="stat-item"><span class="stat-label">Target 3</span><span class="stat-value green">₹{b['target3']:,.1f}</span></div>
    <div class="stat-item"><span class="stat-label">RR</span><span class="stat-value blue">1:{b['rr']}</span></div>
    <div class="stat-item"><span class="stat-label">Vol</span><span class="stat-value">{b['vol_ratio']}x</span></div>
  </div>
  <div style="margin-top:10px;font-size:11px">
    <a href="{b['tv_link']}" target="_blank" style="color:#38bdf8;text-decoration:none;font-weight:600">View Chart →</a>
  </div>
</div>
""", unsafe_allow_html=True)

    else:
        st.info("Click **Run Breakout Scan** in sidebar to detect daily/weekly/monthly breakouts.")


# ── TAB 3: F&O ────────────────────────────────────────────────────────────────
with tab3:
    st.markdown("""
<div style="margin-bottom:16px">
  <div style="font-size:15px;font-weight:700;color:#38bdf8">F&O Trade Suggestions</div>
  <div style="font-size:12px;color:#334155;margin-top:2px">
    Nifty 200 stocks with liquid options · Verify premium &amp; IV on NSE before trading
  </div>
</div>
""", unsafe_allow_html=True)

    signals  = st.session_state.get("signals", [])
    fno_sigs = [s for s in signals if s.get("fno_eligible") and s.get("fno_suggestion")]

    if fno_sigs:
        for sig in sorted(fno_sigs, key=lambda x: x["score"], reverse=True):
            f = sig["fno_suggestion"]
            is_call = f["direction"] == "CALL"
            dir_col = "#4ade80" if is_call else "#f87171"
            dir_icon = "▲ CALL" if is_call else "▼ PUT"
            rating_label, rating_cls = _rating(sig["score"])

            st.markdown(f"""
<div class="fno-card">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
    <div>
      <span style="font-size:18px;font-weight:800;color:#f1f5f9">{sig['symbol']}</span>
      <span style="margin-left:8px;font-size:11px;color:#334155">{sig.get('setup_type','').replace('_',' ').title()}</span>
    </div>
    <div style="display:flex;align-items:center;gap:10px">
      <span style="font-size:15px;font-weight:800;color:{dir_col}">{dir_icon}</span>
      <span class="rating {rating_cls}">{rating_label}</span>
    </div>
  </div>
  <div class="stat-row">
    <div class="stat-item"><span class="stat-label">Spot</span><span class="stat-value">₹{sig['price']:,.1f}</span></div>
    <div class="stat-item"><span class="stat-label">ATM Strike</span><span class="stat-value blue">₹{f['atm_strike']:,}</span></div>
    <div class="stat-item"><span class="stat-label">OTM Strike</span><span class="stat-value blue">₹{f['otm_strike']:,}</span></div>
    <div class="stat-item"><span class="stat-label">Risk (pts)</span><span class="stat-value red">{f['risk_pts']}</span></div>
    <div class="stat-item"><span class="stat-label">Stock SL</span><span class="stat-value red">₹{sig['sl2']:,.1f}</span></div>
    <div class="stat-item"><span class="stat-label">Stock T1</span><span class="stat-value green">₹{sig['target1']:,.1f}</span></div>
  </div>
  <div style="margin-top:10px;background:#050c18;border:1px solid #0f2035;border-radius:8px;
    padding:10px 14px;font-family:'JetBrains Mono',monospace;font-size:11px;color:#475569">
    {f['note']}
  </div>
  <div style="margin-top:10px;font-size:11px;display:flex;gap:16px">
    <a href="{sig['tv_link']}" target="_blank" style="color:#38bdf8;text-decoration:none;font-weight:600">Chart →</a>
    <a href="https://www.nseindia.com/get-quotes/derivatives?symbol={sig['symbol']}" target="_blank"
       style="color:#64748b;text-decoration:none">NSE Chain →</a>
  </div>
</div>
""", unsafe_allow_html=True)

        st.markdown("""
<div style="padding:12px 16px;background:#0a1120;border:1px solid #1e3a5f;border-radius:8px;font-size:11px;color:#475569;margin-top:8px">
  ⚠️ Strike and direction calculated from swing signal + ATR. Premium, IV, open interest must be verified independently. Not SEBI-registered advice.
</div>
""", unsafe_allow_html=True)

    elif signals:
        st.info("No F&O eligible stocks in current signals. F&O suggestions appear for Nifty 200 stocks.")
    else:
        st.info("Run a swing scan first.")

    # Forex & Commodities watchlist
    st.markdown("---")
    st.markdown('<div style="font-size:13px;font-weight:700;color:#38bdf8;margin-bottom:12px">Global Markets</div>', unsafe_allow_html=True)

    with st.spinner("Loading market data…"):
        fc_data = get_forex_data()

    if fc_data:
        cols = st.columns(len(fc_data))
        for i, row in enumerate(fc_data):
            chg = row["Chg%"]
            col_c = "#4ade80" if chg >= 0 else "#f87171"
            sign = "+" if chg >= 0 else ""
            cols[i].markdown(f"""
<div style="background:#0a1929;border:1px solid #0f2d4a;border-radius:10px;padding:12px;text-align:center">
  <div style="font-size:10px;color:#334155;text-transform:uppercase;letter-spacing:.08em;margin-bottom:4px">{row['Asset']}</div>
  <div style="font-size:16px;font-weight:700;color:#f1f5f9;font-family:'JetBrains Mono',monospace">{row['Last']}</div>
  <div style="font-size:12px;font-weight:600;color:{col_c};margin-top:2px">{sign}{chg}%</div>
</div>
""", unsafe_allow_html=True)


# ── TAB 4: Performance ────────────────────────────────────────────────────────
with tab4:
    perf = get_performance()
    if perf:
        p1, p2, p3, p4, p5 = st.columns(5)
        p1.metric("Total Signals", perf["total"])
        p2.metric("Win Rate",  f"{perf['win_rate']}%")
        p3.metric("Avg P&L",  f"{perf['avg_pnl']}%")
        p4.metric("Best",     f"+{perf['best']}%")
        p5.metric("Worst",    f"{perf['worst']}%")

        hist   = get_history()
        closed = hist[hist["status"] != "OPEN"]
        if not closed.empty:
            fig_pnl = px.bar(closed, x="symbol", y="pnl_pct", color="pnl_pct",
                             color_continuous_scale=["#ef4444","#0f2035","#22c55e"],
                             range_color=[-20, 20])
            fig_pnl.update_layout(
                paper_bgcolor="#070f1e", plot_bgcolor="#050c18",
                font=dict(color="#64748b", size=10),
                xaxis=dict(gridcolor="#0f2035"),
                yaxis=dict(gridcolor="#0f2035"),
                height=380, margin=dict(l=8,r=8,t=8,b=8),
                coloraxis_showscale=False, showlegend=False,
            )
            st.plotly_chart(fig_pnl, use_container_width=True)
    else:
        st.info("No closed trades yet. Signals tracked automatically after each scan.")


# ── TAB 5: History ────────────────────────────────────────────────────────────
with tab5:
    hist = get_history()
    if not hist.empty:
        st.dataframe(hist, use_container_width=True, hide_index=True)
        st.download_button("Export History", hist.to_csv(index=False), "history.csv", "text/csv")
    else:
        st.info("No history yet. Run a scan first.")

st.markdown("""
<div style="text-align:center;padding:20px 0 6px;font-size:10px;color:#0f2035">
  SwingDesk Pro · Personal Research Tool · Not SEBI-Registered Advice
</div>
""", unsafe_allow_html=True)
