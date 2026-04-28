import streamlit as st
import plotly.graph_objects as go
import plotly.express as px
import pandas as pd
import yfinance as yf
import ta as ta_lib
from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler
import pytz

from scanner import scan_all, load_nifty500
from telegram_bot import send_alert, send_summary, send_top_picks, test_connection, start_command_polling
from tracker import log_signals, update_outcomes, get_performance, get_history, init_db
from config import MIN_SIGNAL_SCORE, CAPITAL
from upstox_provider import is_authenticated, get_auth_url, exchange_code_for_token

st.set_page_config(page_title="SwingDesk Pro", layout="wide", page_icon="⚡")
IST = pytz.timezone("Asia/Kolkata")
init_db()

# ── Global CSS — glint.trade terminal aesthetic ───────────────────────────────
st.markdown("""
<style>
  @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap');

  html, body, [class*="css"] {
    font-family: 'Inter', sans-serif !important;
    background-color: #060b14 !important;
    color: #e2e8f0 !important;
  }
  .stApp { background: #060b14; }

  /* Header bar */
  header[data-testid="stHeader"] { background: #060b14; border-bottom: 1px solid #1a2332; }

  /* Sidebar */
  section[data-testid="stSidebar"] {
    background: #0a1120 !important;
    border-right: 1px solid #1a2332;
  }
  section[data-testid="stSidebar"] * { color: #94a3b8 !important; }
  section[data-testid="stSidebar"] .stButton button {
    background: linear-gradient(135deg, #0f4c75, #1b6ca8) !important;
    border: none !important; color: #fff !important; border-radius: 6px !important;
    font-weight: 600 !important; letter-spacing: 0.02em;
  }

  /* Tabs */
  .stTabs [data-baseweb="tab-list"] {
    background: #0a1120; border-bottom: 1px solid #1a2332; gap: 0;
  }
  .stTabs [data-baseweb="tab"] {
    background: transparent; color: #64748b !important;
    font-size: 13px; font-weight: 500; padding: 10px 20px;
    border-bottom: 2px solid transparent;
  }
  .stTabs [aria-selected="true"] {
    color: #38bdf8 !important; border-bottom: 2px solid #38bdf8 !important;
    background: transparent;
  }

  /* Metric cards */
  [data-testid="metric-container"] {
    background: #0d1829; border: 1px solid #1a2d47;
    border-radius: 8px; padding: 14px 18px;
  }
  [data-testid="metric-container"] label { color: #64748b !important; font-size: 11px !important; text-transform: uppercase; letter-spacing: 0.08em; }
  [data-testid="metric-container"] [data-testid="stMetricValue"] { color: #f1f5f9 !important; font-size: 22px !important; font-weight: 700 !important; font-family: 'JetBrains Mono', monospace !important; }

  /* Dataframe */
  .stDataFrame { border: 1px solid #1a2332 !important; border-radius: 8px; overflow: hidden; }
  .stDataFrame thead th {
    background: #0d1829 !important; color: #38bdf8 !important;
    font-size: 11px !important; text-transform: uppercase; letter-spacing: 0.06em; font-weight: 600;
  }
  .stDataFrame tbody tr { background: #060b14 !important; }
  .stDataFrame tbody tr:hover { background: #0d1829 !important; }
  .stDataFrame tbody td { color: #cbd5e1 !important; font-family: 'JetBrains Mono', monospace; font-size: 12px !important; border-color: #1a2332 !important; }

  /* Buttons */
  .stButton button {
    background: linear-gradient(135deg, #0ea5e9, #0284c7) !important;
    color: white !important; border: none !important; border-radius: 6px !important;
    font-weight: 600 !important; font-size: 13px !important; letter-spacing: 0.02em;
    transition: all 0.2s;
  }
  .stButton button:hover { opacity: 0.9; transform: translateY(-1px); }

  /* Inputs/sliders */
  .stSlider [data-baseweb="slider"] div[data-testid="stThumbValue"] { color: #38bdf8 !important; }
  .stTextInput input { background: #0d1829 !important; border: 1px solid #1a2332 !important; color: #e2e8f0 !important; border-radius: 6px !important; }
  .stSelectbox div[data-baseweb="select"] { background: #0d1829 !important; border: 1px solid #1a2332 !important; }

  /* Signal cards */
  .signal-card {
    background: #0d1829; border: 1px solid #1a2d47; border-left: 3px solid #22c55e;
    border-radius: 8px; padding: 16px; margin-bottom: 12px;
  }
  .signal-card.bearish { border-left-color: #ef4444; }
  .signal-card .sym { font-size: 18px; font-weight: 700; color: #f1f5f9; letter-spacing: -0.01em; }
  .signal-card .score-badge {
    display: inline-block; background: #1a3a1a; color: #22c55e;
    font-size: 11px; font-weight: 700; padding: 2px 8px; border-radius: 4px;
    font-family: 'JetBrains Mono', monospace;
  }
  .signal-card .score-badge.high { background: #1e3a1e; color: #4ade80; }
  .signal-card .score-badge.med  { background: #2d2a10; color: #facc15; }
  .signal-card .fno-badge {
    display: inline-block; background: #1a2d47; color: #38bdf8;
    font-size: 10px; font-weight: 700; padding: 2px 7px; border-radius: 4px;
    margin-left: 6px; font-family: 'JetBrains Mono', monospace;
  }
  .pill { display: inline-block; padding: 1px 8px; border-radius: 99px; font-size: 10px; font-weight: 600; margin: 1px; }
  .pill-green  { background: #14532d; color: #86efac; }
  .pill-yellow { background: #422006; color: #fbbf24; }
  .pill-blue   { background: #0c2d4a; color: #7dd3fc; }
  .stat-row { display: flex; gap: 24px; margin: 8px 0; flex-wrap: wrap; }
  .stat-item { display: flex; flex-direction: column; }
  .stat-label { font-size: 10px; color: #475569; text-transform: uppercase; letter-spacing: 0.06em; }
  .stat-value { font-size: 14px; font-weight: 600; color: #e2e8f0; font-family: 'JetBrains Mono', monospace; }
  .stat-value.green { color: #4ade80; }
  .stat-value.red   { color: #f87171; }

  /* FnO card */
  .fno-card {
    background: #0a1929; border: 1px solid #164e63; border-left: 3px solid #38bdf8;
    border-radius: 8px; padding: 14px 16px; margin-bottom: 10px;
  }
  .fno-card .direction-call { color: #4ade80; font-weight: 700; font-size: 15px; }
  .fno-card .direction-put  { color: #f87171; font-weight: 700; font-size: 15px; }

  /* Divider */
  hr { border-color: #1a2332 !important; margin: 16px 0 !important; }

  /* Status dot */
  .dot-green { display: inline-block; width: 8px; height: 8px; background: #22c55e; border-radius: 50%; margin-right: 6px; animation: pulse 2s infinite; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.4} }
</style>
""", unsafe_allow_html=True)


# ── Upstox OAuth ──────────────────────────────────────────────────────────────
query_params = st.query_params
if "code" in query_params and not is_authenticated():
    try:
        exchange_code_for_token(query_params["code"])
        st.success("Upstox connected! Refresh the page.")
        st.stop()
    except Exception as e:
        st.error(f"Upstox login failed: {e}")

if "polling_started" not in st.session_state:
    start_command_polling()
    st.session_state["polling_started"] = True


# ── Auto-scheduler ─────────────────────────────────────────────────────────────
def _auto_scan():
    sigs = scan_all(min_score=st.session_state.get("min_score", MIN_SIGNAL_SCORE))
    st.session_state["signals"] = sigs
    st.session_state["last_scan"] = datetime.now(IST).strftime("%d %b %Y %I:%M %p IST")
    log_signals(sigs)
    update_outcomes()
    for s in sigs:
        send_alert(s)
    send_summary(sigs)

if "scheduler_started" not in st.session_state:
    _sch = BackgroundScheduler(timezone=IST)
    _sch.add_job(_auto_scan, "cron", hour=9,  minute=20)
    _sch.add_job(_auto_scan, "cron", hour=14, minute=45)
    _sch.add_job(_auto_scan, "cron", hour=15, minute=20)
    _sch.start()
    st.session_state["scheduler_started"] = True


# ── Chart ─────────────────────────────────────────────────────────────────────
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
        increasing_line_color="#22c55e", decreasing_line_color="#ef4444"))
    fig.add_trace(go.Scatter(x=df.index, y=ema20,  name="EMA20",  line=dict(color="#facc15", width=1.5)))
    fig.add_trace(go.Scatter(x=df.index, y=ema50,  name="EMA50",  line=dict(color="#38bdf8", width=1.5)))
    fig.add_trace(go.Scatter(x=df.index, y=ema200, name="EMA200", line=dict(color="#f87171", width=1.5)))

    if signal:
        fig.add_hline(y=signal["sl2"],     line_color="#ef4444", line_dash="dash", annotation_text="SL")
        fig.add_hline(y=signal["target1"], line_color="#86efac", line_dash="dot",  annotation_text="T1")
        fig.add_hline(y=signal["target2"], line_color="#4ade80", line_dash="dot",  annotation_text="T2")
        fig.add_hline(y=signal["target3"], line_color="#16a34a", line_dash="dot",  annotation_text="T3")

    fig.update_layout(
        title=dict(text=f"{symbol} — 6M Daily", font=dict(color="#94a3b8", size=13)),
        xaxis_rangeslider_visible=False, height=460,
        paper_bgcolor="#0a1120", plot_bgcolor="#060b14",
        font=dict(color="#94a3b8", size=11),
        xaxis=dict(gridcolor="#1a2332", showgrid=True),
        yaxis=dict(gridcolor="#1a2332", showgrid=True),
        legend=dict(bgcolor="#0a1120", bordercolor="#1a2332"),
        margin=dict(l=10, r=10, t=40, b=10),
    )
    st.plotly_chart(fig, use_container_width=True)


# ── Score color ───────────────────────────────────────────────────────────────
def _score_class(s):
    if s >= 85: return "high"
    if s >= 70: return "med"
    return ""

def _stars(s):
    if s >= 90: return "★★★★★"
    if s >= 80: return "★★★★"
    if s >= 70: return "★★★"
    return "★★"


# ── Sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("### ⚡ SwingDesk Pro")
    st.markdown("---")

    run_scan  = st.button("Run Scan — Nifty 500", use_container_width=True)
    send_tg   = st.checkbox("Telegram alerts", value=True)
    top_only  = st.checkbox("Top 10 picks only", value=False)

    st.markdown("**Filters**")
    min_score = st.slider("Min score", 50, 100, MIN_SIGNAL_SCORE)
    st.session_state["min_score"] = min_score

    st.markdown("**Chart**")
    chart_sym  = st.text_input("Symbol", placeholder="RELIANCE")
    show_chart = st.button("Show Chart", use_container_width=True)

    st.markdown("---")
    if is_authenticated():
        st.markdown('<span class="dot-green"></span>Upstox live', unsafe_allow_html=True)
    else:
        auth_url = get_auth_url()
        st.markdown(f"[Connect Upstox]({auth_url})")
        st.caption("Using yfinance fallback")

    st.markdown("---")
    if st.button("Test Telegram", use_container_width=True):
        test_connection()
        st.success("Sent!")
    if st.button("Update Outcomes", use_container_width=True):
        update_outcomes()
        st.success("Done!")

    st.markdown("---")
    st.caption("Auto: 9:20 · 2:45 · 3:20 IST")
    if "last_scan" in st.session_state:
        st.caption(f"Last: {st.session_state['last_scan']}")
    st.caption(f"Capital ₹{CAPITAL:,}")


# ── Header ────────────────────────────────────────────────────────────────────
st.markdown("""
<div style="display:flex;align-items:center;gap:12px;padding:8px 0 16px">
  <div style="font-size:26px;font-weight:800;color:#f1f5f9;letter-spacing:-0.02em">SwingDesk <span style="color:#38bdf8">Pro</span></div>
  <div style="font-size:11px;color:#475569;padding-top:6px">Nifty 500 · F&O · Swing · ATR Risk · Parallel Engine</div>
</div>
""", unsafe_allow_html=True)

tab1, tab2, tab3, tab4 = st.tabs(["Signals", "F&O Plays", "Performance", "History"])


# ── TAB 1: Signals ────────────────────────────────────────────────────────────
with tab1:
    if show_chart and chart_sym:
        sig_map = {s["symbol"]: s for s in st.session_state.get("signals", [])}
        plot_chart(chart_sym.upper().strip(), sig_map.get(chart_sym.upper().strip()))

    if run_scan:
        with st.spinner("Scanning Nifty 500 in parallel… (2-4 min)"):
            signals = scan_all(min_score=min_score)
            st.session_state["signals"] = signals
            st.session_state["last_scan"] = datetime.now(IST).strftime("%d %b %Y %I:%M %p IST")
            log_signals(signals)
        if send_tg:
            if top_only:
                send_top_picks(signals, top_n=10)
            else:
                for s in signals:
                    send_alert(s)
            send_summary(signals)
        st.success(f"{len(signals)} signals found!")

    signals = st.session_state.get("signals", [])

    if signals:
        # Metric strip
        c1, c2, c3, c4, c5, c6 = st.columns(6)
        c1.metric("Signals", len(signals))
        c2.metric("Top Score", f"{signals[0]['score']}/100")
        c3.metric("Avg Score", round(sum(s["score"] for s in signals)/len(signals), 1))
        c4.metric("Avg RSI", round(sum(s["rsi"] for s in signals)/len(signals), 1))
        c5.metric("Avg Vol", f"{round(sum(s['vol_ratio'] for s in signals)/len(signals),1)}x")
        fno_count = sum(1 for s in signals if s.get("fno_eligible"))
        c6.metric("F&O Eligible", fno_count)

        st.markdown("---")

        # Sort control
        col_sort, col_export = st.columns([3, 1])
        with col_sort:
            sort_col = st.selectbox("Sort by", ["score", "rsi", "vol_ratio", "rr1"], index=0)
        signals_sorted = sorted(signals, key=lambda x: x.get(sort_col, 0), reverse=True)

        # Signal cards
        for sig in signals_sorted:
            bias_class = "" if sig["action"] == "BUY" else "bearish"
            fno_badge  = '<span class="fno-badge">F&O</span>' if sig.get("fno_eligible") else ""
            sc         = _score_class(sig["score"])
            reasons_html = "".join(
                f'<span class="pill pill-green">{r.strip()}</span>'
                for r in sig["reasons"].split(",") if r.strip()
            )
            st.markdown(f"""
<div class="signal-card {bias_class}">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
    <div>
      <span class="sym">{sig['symbol']}</span>
      {fno_badge}
      <span style="margin-left:8px;font-size:11px;color:#64748b">{sig['setup_type']}</span>
    </div>
    <div style="display:flex;align-items:center;gap:8px">
      <span style="color:#64748b;font-size:13px">{_stars(sig['score'])}</span>
      <span class="score-badge {sc}">{sig['score']}/100</span>
    </div>
  </div>
  <div class="stat-row">
    <div class="stat-item"><span class="stat-label">Entry</span><span class="stat-value">₹{sig['price']:,.1f}</span></div>
    <div class="stat-item"><span class="stat-label">SL</span><span class="stat-value red">₹{sig['sl2']:,.1f}</span></div>
    <div class="stat-item"><span class="stat-label">T1</span><span class="stat-value green">₹{sig['target1']:,.1f}</span></div>
    <div class="stat-item"><span class="stat-label">T2</span><span class="stat-value green">₹{sig['target2']:,.1f}</span></div>
    <div class="stat-item"><span class="stat-label">T3</span><span class="stat-value green">₹{sig['target3']:,.1f}</span></div>
    <div class="stat-item"><span class="stat-label">RR</span><span class="stat-value">1:{sig['rr1']}</span></div>
    <div class="stat-item"><span class="stat-label">Qty</span><span class="stat-value">{sig['qty']}</span></div>
    <div class="stat-item"><span class="stat-label">RSI</span><span class="stat-value">{sig['rsi']:.0f}</span></div>
    <div class="stat-item"><span class="stat-label">ADX</span><span class="stat-value">{sig['adx']:.0f}</span></div>
    <div class="stat-item"><span class="stat-label">Vol</span><span class="stat-value">{sig['vol_ratio']:.1f}x</span></div>
  </div>
  <div style="margin-top:8px">{reasons_html}</div>
  <div style="margin-top:8px;font-size:11px">
    <a href="{sig['tv_link']}" target="_blank" style="color:#38bdf8;text-decoration:none">TradingView →</a>
  </div>
</div>
""", unsafe_allow_html=True)

        # Score distribution chart
        st.markdown("---")
        df_sig = pd.DataFrame(signals_sorted)
        col_a, col_b = st.columns([2, 1])
        with col_a:
            fig_hist = px.histogram(df_sig, x="score", nbins=10,
                                    color_discrete_sequence=["#0ea5e9"])
            fig_hist.update_layout(height=200, paper_bgcolor="#0a1120",
                                   plot_bgcolor="#060b14", font=dict(color="#94a3b8"),
                                   margin=dict(t=10, b=10, l=10, r=10),
                                   xaxis=dict(gridcolor="#1a2332"),
                                   yaxis=dict(gridcolor="#1a2332"),
                                   showlegend=False)
            st.plotly_chart(fig_hist, use_container_width=True)
        with col_b:
            picked = st.selectbox("Chart stock", [s["symbol"] for s in signals_sorted])
            sig_map = {s["symbol"]: s for s in signals_sorted}
            if st.button("Show Chart", key="chart_from_signal"):
                plot_chart(picked, sig_map.get(picked))
            tv = sig_map.get(picked, {})
            if tv:
                st.markdown(f"[Open on TradingView]({tv.get('tv_link','')})")

        # Table export
        display_cols = ["symbol","score","price","rsi","adx","vol_ratio","pe",
                        "sl1","sl2","target1","target2","target3","rr1","rr2","qty","setup_type","reasons"]
        export_cols  = [c for c in display_cols if c in df_sig.columns]
        csv = df_sig[export_cols].to_csv(index=False)
        with col_export:
            st.download_button("Export CSV", csv, "signals.csv", "text/csv")

    elif "signals" in st.session_state:
        st.info("No signals this session. Try next auto-scan at 9:20 AM / 2:45 PM / 3:20 PM IST.")
    else:
        st.markdown("""
<div style="text-align:center;padding:60px 0;color:#475569">
  <div style="font-size:40px;margin-bottom:12px">⚡</div>
  <div style="font-size:16px;font-weight:600;color:#64748b">Ready to scan</div>
  <div style="font-size:13px;margin-top:4px">Click <b>Run Scan</b> or wait for auto-scan at 9:20 AM IST</div>
</div>
""", unsafe_allow_html=True)


# ── TAB 2: F&O Plays ─────────────────────────────────────────────────────────
with tab2:
    st.markdown("""
<div style="margin-bottom:16px">
  <div style="font-size:15px;font-weight:700;color:#38bdf8">F&O Trade Suggestions</div>
  <div style="font-size:12px;color:#475569;margin-top:2px">Based on Nifty 200 stocks with liquid options · Verify premium on NSE/broker before trading</div>
</div>
""", unsafe_allow_html=True)

    signals = st.session_state.get("signals", [])
    fno_sigs = [s for s in signals if s.get("fno_eligible") and s.get("fno_suggestion")]

    if fno_sigs:
        for sig in sorted(fno_sigs, key=lambda x: x["score"], reverse=True):
            f = sig["fno_suggestion"]
            dir_class = "direction-call" if f["direction"] == "CALL" else "direction-put"
            dir_icon  = "▲ CALL" if f["direction"] == "CALL" else "▼ PUT"
            st.markdown(f"""
<div class="fno-card">
  <div style="display:flex;justify-content:space-between;align-items:center">
    <div>
      <span style="font-size:17px;font-weight:700;color:#f1f5f9">{sig['symbol']}</span>
      <span style="margin-left:10px;font-size:12px;color:#64748b">{sig['setup_type']}</span>
    </div>
    <div>
      <span class="{dir_class}">{dir_icon}</span>
      <span class="score-badge" style="margin-left:8px">{sig['score']}/100</span>
    </div>
  </div>
  <div class="stat-row" style="margin-top:12px">
    <div class="stat-item"><span class="stat-label">Spot</span><span class="stat-value">₹{sig['price']:,.1f}</span></div>
    <div class="stat-item"><span class="stat-label">ATM Strike</span><span class="stat-value">₹{f['atm_strike']:,}</span></div>
    <div class="stat-item"><span class="stat-label">OTM Strike</span><span class="stat-value">₹{f['otm_strike']:,}</span></div>
    <div class="stat-item"><span class="stat-label">Risk (pts)</span><span class="stat-value red">{f['risk_pts']}</span></div>
    <div class="stat-item"><span class="stat-label">Expiry</span><span class="stat-value" style="font-size:11px">{f['expiry']}</span></div>
  </div>
  <div style="margin-top:10px;font-size:11px;color:#64748b;background:#060b14;padding:8px 12px;border-radius:6px;font-family:'JetBrains Mono',monospace">
    {f['note']}
  </div>
  <div style="margin-top:8px;font-size:11px">
    <a href="{sig['tv_link']}" target="_blank" style="color:#38bdf8;text-decoration:none">TradingView →</a>
    &nbsp;&nbsp;
    <a href="https://www.nseindia.com/get-quotes/derivatives?symbol={sig['symbol']}" target="_blank" style="color:#64748b;text-decoration:none">NSE Chain →</a>
  </div>
</div>
""", unsafe_allow_html=True)

        st.markdown("""
<div style="padding:12px;background:#0a1120;border:1px solid #1e3a5f;border-radius:8px;margin-top:8px;font-size:12px;color:#64748b">
  ⚠️ <b>Disclaimer:</b> F&O suggestions show direction & strike based on swing signal. Premium, IV, and liquidity must be verified independently on NSE/broker. Not SEBI-registered advice.
</div>
""", unsafe_allow_html=True)

    elif signals:
        st.info("No F&O eligible stocks in current signals. Run a scan — signals from Nifty 200 stocks auto-qualify.")
    else:
        st.info("Run a scan first. F&O suggestions appear for Nifty 200 stocks with swing signals.")

    # Forex/Commodities watchlist
    st.markdown("---")
    st.markdown('<div style="font-size:14px;font-weight:700;color:#38bdf8;margin-bottom:12px">Forex & Commodities Watchlist</div>', unsafe_allow_html=True)

    watchlist = {
        "USD/INR":  "INR=X",
        "EUR/INR":  "EURINR=X",
        "GBP/INR":  "GBPINR=X",
        "Gold":     "GC=F",
        "Silver":   "SI=F",
        "Crude Oil":"CL=F",
    }
    wdata = []
    for name, ticker in watchlist.items():
        try:
            t  = yf.Ticker(ticker)
            h  = t.history(period="2d")
            if len(h) >= 2:
                prev  = h["Close"].iloc[-2]
                last  = h["Close"].iloc[-1]
                chg   = round((last - prev) / prev * 100, 2)
                wdata.append({"Asset": name, "Last": round(last, 2), "Chg%": chg})
        except Exception:
            pass

    if wdata:
        wdf = pd.DataFrame(wdata)
        # Color code: positive green, negative red
        def color_chg(v):
            return "color: #4ade80" if v >= 0 else "color: #f87171"
        st.dataframe(
            wdf.style.map(color_chg, subset=["Chg%"]),
            use_container_width=True, hide_index=True
        )


# ── TAB 3: Performance ────────────────────────────────────────────────────────
with tab3:
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
            fig_pnl = px.bar(closed, x="symbol", y="pnl_pct",
                             color="pnl_pct",
                             color_continuous_scale=["#ef4444", "#1a2332", "#22c55e"],
                             title="P&L per Trade (%)")
            fig_pnl.update_layout(
                paper_bgcolor="#0a1120", plot_bgcolor="#060b14",
                font=dict(color="#94a3b8"), height=380,
                xaxis=dict(gridcolor="#1a2332"),
                yaxis=dict(gridcolor="#1a2332"),
                margin=dict(l=10, r=10, t=40, b=10),
            )
            st.plotly_chart(fig_pnl, use_container_width=True)
    else:
        st.info("No closed trades yet. Signals are tracked automatically after each scan.")


# ── TAB 4: History ────────────────────────────────────────────────────────────
with tab4:
    hist = get_history()
    if not hist.empty:
        st.dataframe(hist, use_container_width=True, hide_index=True)
        st.download_button("Export History", hist.to_csv(index=False), "signal_history.csv", "text/csv")
    else:
        st.info("No history yet. Run a scan first.")

st.markdown("""
<div style="text-align:center;padding:20px 0 8px;font-size:11px;color:#1e293b">
  Personal research tool · Not SEBI-registered advice · Data via Upstox / Yahoo Finance
</div>
""", unsafe_allow_html=True)
