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
from tracker import log_signals, update_outcomes, get_performance, get_history, init_db, mute_asset, unmute_asset
from config import MIN_SIGNAL_SCORE, CAPITAL

st.set_page_config(page_title="Nifty 500 Swing Scanner", layout="wide", page_icon="📈")
IST = pytz.timezone("Asia/Kolkata")
init_db()

# Start command polling (handles /start /active /performance /mute /stats)
if "polling_started" not in st.session_state:
    start_command_polling()
    st.session_state["polling_started"] = True

# ── Auto-scheduler ─────────────────────────────────────────────────────────────
def _auto_scan():
    cfg = st.session_state
    sigs = scan_all(min_score=cfg.get("min_score", MIN_SIGNAL_SCORE))
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


# ── Chart helper ───────────────────────────────────────────────────────────────
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
        low=df["Low"].squeeze(),  close=close, name="Price"))
    fig.add_trace(go.Scatter(x=df.index, y=ema20,  name="EMA20",  line=dict(color="#FFD700", width=1.5)))
    fig.add_trace(go.Scatter(x=df.index, y=ema50,  name="EMA50",  line=dict(color="#00CED1", width=1.5)))
    fig.add_trace(go.Scatter(x=df.index, y=ema200, name="EMA200", line=dict(color="#FF6B6B", width=1.5)))

    if signal:
        price = signal["price"]
        fig.add_hline(y=signal["sl"],      line_color="red",    line_dash="dash", annotation_text="SL")
        fig.add_hline(y=signal["target1"], line_color="#90EE90", line_dash="dot",  annotation_text="T1")
        fig.add_hline(y=signal["target2"], line_color="#32CD32", line_dash="dot",  annotation_text="T2")
        fig.add_hline(y=signal["target3"], line_color="#006400", line_dash="dot",  annotation_text="T3")

    fig.update_layout(title=f"{symbol} — 6M Daily",
                      xaxis_rangeslider_visible=False, height=480,
                      paper_bgcolor="#0d1117", plot_bgcolor="#0d1117",
                      font=dict(color="#c9d1d9"))
    st.plotly_chart(fig, use_container_width=True)


# ── Sidebar ────────────────────────────────────────────────────────────────────
with st.sidebar:
    st.title("⚙️ Controls")
    st.divider()

    st.subheader("Scan")
    run_scan  = st.button("🔍 Run Full Scan (Nifty 500)", use_container_width=True)
    send_tg   = st.checkbox("Telegram alerts", value=True)
    top_only  = st.checkbox("Top 5 picks only", value=False)

    st.subheader("Filters")
    min_score = st.slider("Min signal score", 50, 100, MIN_SIGNAL_SCORE)
    st.session_state["min_score"] = min_score

    st.subheader("Chart")
    chart_sym  = st.text_input("Symbol", placeholder="e.g. RELIANCE")
    show_chart = st.button("📊 Show Chart")

    st.divider()
    if st.button("🔔 Test Telegram"):
        test_connection()
        st.success("Sent!")
    if st.button("🔄 Update Outcomes"):
        update_outcomes()
        st.success("Outcomes updated!")

    st.divider()
    st.caption("Auto-scan: 9:20 AM | 2:45 PM | 3:20 PM IST")
    if "last_scan" in st.session_state:
        st.caption(f"Last: {st.session_state['last_scan']}")
    st.caption(f"Capital: ₹{CAPITAL:,}")


# ── Main ───────────────────────────────────────────────────────────────────────
st.title("📈 Nifty 500 Advanced Swing Scanner")
st.caption("Scoring Engine · ATR Risk · Multi-timeframe · Parallel Scan · Signal Tracker")

tab1, tab2, tab3 = st.tabs(["🎯 Signals", "📊 Performance", "📜 History"])

# ── TAB 1: Signals ─────────────────────────────────────────────────────────────
with tab1:
    if show_chart and chart_sym:
        sig_map = {s["symbol"]: s for s in st.session_state.get("signals", [])}
        plot_chart(chart_sym.upper().strip(), sig_map.get(chart_sym.upper().strip()))

    if run_scan:
        with st.spinner("Scanning 500 stocks in parallel… (2-4 min)"):
            signals = scan_all(min_score=min_score)
            st.session_state["signals"] = signals
            st.session_state["last_scan"] = datetime.now(IST).strftime("%d %b %Y %I:%M %p IST")
            log_signals(signals)

        if send_tg:
            if top_only:
                send_top_picks(signals, top_n=5)
            else:
                for s in signals:
                    send_alert(s)
            send_summary(signals)
        st.success(f"✅ {len(signals)} signals found!")

    if "signals" in st.session_state and st.session_state["signals"]:
        signals = st.session_state["signals"]

        # Metric row
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Signals", len(signals))
        c2.metric("Avg Score", round(sum(s["score"] for s in signals)/len(signals), 1))
        c3.metric("Avg RSI", round(sum(s["rsi"] for s in signals)/len(signals), 1))
        c4.metric("Avg Vol", f"{round(sum(s['vol_ratio'] for s in signals)/len(signals),1)}x")
        top_score = signals[0]["score"] if signals else 0
        c5.metric("Top Score", f"{top_score}/100")

        st.divider()

        # Heatmap by score bucket
        df_sig = pd.DataFrame(signals)

        col_a, col_b = st.columns([2, 1])
        with col_a:
            st.subheader("Score Distribution")
            fig_hist = px.histogram(df_sig, x="score", nbins=10,
                                    color_discrete_sequence=["#FFD700"])
            fig_hist.update_layout(height=220, paper_bgcolor="#0d1117",
                                   plot_bgcolor="#0d1117", font=dict(color="#c9d1d9"),
                                   margin=dict(t=10,b=10))
            st.plotly_chart(fig_hist, use_container_width=True)

        with col_b:
            st.subheader("Sort by")
            sort_col = st.selectbox("", ["score","rsi","vol_ratio","rr1"], label_visibility="collapsed")
            df_sig = df_sig.sort_values(sort_col, ascending=False)

        # Signal table
        st.subheader("All Signals")
        display = ["symbol","score","price","rsi","adx","vol_ratio","pe",
                   "sl","target1","target2","target3","rr1","rr2","qty","reasons"]
        st.dataframe(
            df_sig[display].rename(columns={
                "symbol":"Stock","score":"Score","price":"Price","rsi":"RSI",
                "adx":"ADX","vol_ratio":"Vol","pe":"P/E","sl":"Stop Loss",
                "target1":"T1","target2":"T2","target3":"T3",
                "rr1":"RR1","rr2":"RR2","qty":"Qty","reasons":"Signals Fired"
            }),
            use_container_width=True, hide_index=True)

        # TradingView links
        st.subheader("Charts")
        picked = st.selectbox("Chart a signal", [s["symbol"] for s in signals])
        sig_map = {s["symbol"]: s for s in signals}
        if st.button("Show Signal Chart"):
            plot_chart(picked, sig_map.get(picked))

        tv_sig = sig_map.get(picked, {})
        if tv_sig:
            st.markdown(f"[Open {picked} on TradingView]({tv_sig.get('tv_link','')})")

        # CSV export
        csv = df_sig[display].to_csv(index=False)
        st.download_button("⬇️ Export CSV", csv, "signals.csv", "text/csv")

    elif "signals" in st.session_state:
        st.info("No signals today. Try again at 9:20 AM or 3:10 PM IST.")
    else:
        st.info("Click **Run Full Scan** to start.")


# ── TAB 2: Performance ─────────────────────────────────────────────────────────
with tab2:
    st.subheader("Strategy Performance")
    perf = get_performance()
    if perf:
        p1, p2, p3, p4, p5 = st.columns(5)
        p1.metric("Total Signals", perf["total"])
        p2.metric("Win Rate", f"{perf['win_rate']}%")
        p3.metric("Avg P&L", f"{perf['avg_pnl']}%")
        p4.metric("Best Trade", f"+{perf['best']}%")
        p5.metric("Worst Trade", f"{perf['worst']}%")

        hist = get_history()
        closed = hist[hist["status"] != "OPEN"]
        if not closed.empty:
            fig_pnl = px.bar(closed, x="symbol", y="pnl_pct",
                             color="pnl_pct", color_continuous_scale=["#FF4444","#00CC44"],
                             title="P&L by Trade (%)")
            fig_pnl.update_layout(paper_bgcolor="#0d1117", plot_bgcolor="#0d1117",
                                   font=dict(color="#c9d1d9"), height=350)
            st.plotly_chart(fig_pnl, use_container_width=True)
    else:
        st.info("No closed trades yet. Signals are tracked after each scan.")


# ── TAB 3: History ─────────────────────────────────────────────────────────────
with tab3:
    st.subheader("Signal History")
    hist = get_history()
    if not hist.empty:
        st.dataframe(hist, use_container_width=True, hide_index=True)
        csv_h = hist.to_csv(index=False)
        st.download_button("⬇️ Export History", csv_h, "signal_history.csv", "text/csv")
    else:
        st.info("No history yet. Run a scan first.")

st.divider()
st.caption("Personal research tool · Not SEBI-registered advice · Data via Yahoo Finance")
