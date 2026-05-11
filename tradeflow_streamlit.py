"""
TradeFlow AI — Streamlit Backend
Handles: Upstox OAuth, portfolio sync → Supabase, AI Review (Claude), analytics
Run: streamlit run tradeflow_streamlit.py --server.port 8501
"""
import os, json, time, math
from datetime import datetime, date, timedelta
from typing import Optional
import streamlit as st
import pandas as pd
import requests
from anthropic import Anthropic

# ── Supabase REST client (no extra SDK needed) ─────────────────────────────────
SUPABASE_URL  = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY  = os.environ.get("SUPABASE_SERVICE_KEY", "")  # service role for server writes

def sb_headers():
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }

def sb_get(table: str, params: dict = {}) -> list:
    r = requests.get(f"{SUPABASE_URL}/rest/v1/{table}",
                     headers=sb_headers(), params=params)
    return r.json() if r.ok else []

def sb_insert(table: str, data: dict | list) -> dict:
    r = requests.post(f"{SUPABASE_URL}/rest/v1/{table}",
                      headers=sb_headers(), json=data)
    return r.json()

def sb_upsert(table: str, data: dict | list, on_conflict: str = "id") -> dict:
    h = {**sb_headers(), "Prefer": f"resolution=merge-duplicates,return=representation"}
    r = requests.post(f"{SUPABASE_URL}/rest/v1/{table}?on_conflict={on_conflict}",
                      headers=h, json=data)
    return r.json()

def sb_update(table: str, match: dict, data: dict) -> dict:
    params = {k: f"eq.{v}" for k, v in match.items()}
    r = requests.patch(f"{SUPABASE_URL}/rest/v1/{table}",
                       headers=sb_headers(), params=params, json=data)
    return r.json()

# ── Upstox client (reuses existing token flow) ────────────────────────────────
from upstox_provider import _load_token, _save_token, get_auth_url, exchange_code_for_token

UPSTOX_BASE = "https://api.upstox.com/v2"

def upstox_get(endpoint: str, token: str) -> dict:
    r = requests.get(f"{UPSTOX_BASE}{endpoint}",
                     headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
    return r.json() if r.ok else {}

def get_holdings(token: str) -> list:
    data = upstox_get("/portfolio/long-term-holdings", token)
    return data.get("data", [])

def get_positions(token: str) -> list:
    data = upstox_get("/portfolio/short-term-positions", token)
    return data.get("data", [])

def get_funds(token: str) -> dict:
    data = upstox_get("/user/get-funds-and-margin?segment=SEC", token)
    return data.get("data", {}).get("equity", {})

# ── Analytics ─────────────────────────────────────────────────────────────────
def compute_risk_score(trades: list, starting_capital: float = 500000) -> int:
    if not trades:
        return 0
    score = 0
    n = len(trades)
    if n > 10: score += 30
    elif n > 5: score += 15

    losses = [t for t in trades if (t.get("pnl") or 0) < 0]
    if losses:
        max_loss = max(abs(t.get("pnl", 0)) for t in losses)
        dd_pct = max_loss / starting_capital * 100
        if dd_pct > 5: score += 30
        elif dd_pct > 2: score += 15

    revenge = sum(1 for t in trades if "revenge_trade" in (t.get("mistake_tags") or []))
    score += revenge * 10

    plan_breaks = sum(1 for t in trades if not t.get("followed_plan", True))
    score += plan_breaks * 8

    neg_emotions = sum(1 for t in trades
                       if t.get("emotion_before") in ("fearful", "anxious", "angry"))
    score += neg_emotions * 5

    return min(score, 100)

def detect_revenge_trading(trades: list) -> Optional[str]:
    sorted_t = sorted(trades, key=lambda x: x.get("entry_time", ""))
    count = 0
    for i in range(2, len(sorted_t)):
        p2, p1, cur = sorted_t[i-2], sorted_t[i-1], sorted_t[i]
        both_loss = (p2.get("pnl") or 0) < 0 and (p1.get("pnl") or 0) < 0
        if both_loss and p1.get("exit_time") and cur.get("entry_time"):
            gap_mins = (datetime.fromisoformat(cur["entry_time"]) -
                        datetime.fromisoformat(p1["exit_time"])).seconds / 60
            if gap_mins < 30:
                count += 1
    if count:
        return f"Revenge trading detected {count}x — re-entered within 30 min of back-to-back losses."
    return None

def detect_time_pattern(trades: list) -> Optional[str]:
    am = [t for t in trades if datetime.fromisoformat(t["entry_time"]).hour < 12]
    pm = [t for t in trades if datetime.fromisoformat(t["entry_time"]).hour >= 14]
    if len(am) < 3 or len(pm) < 3:
        return None
    am_wr = sum(1 for t in am if (t.get("pnl") or 0) > 0) / len(am)
    pm_wr = sum(1 for t in pm if (t.get("pnl") or 0) > 0) / len(pm)
    if am_wr - pm_wr >= 0.20:
        return f"AM win rate {am_wr*100:.0f}% vs PM {pm_wr*100:.0f}%. Stop trading after 1pm."
    return None

def detect_friday_trap(trades: list) -> Optional[str]:
    fri_pm = [t for t in trades
              if datetime.fromisoformat(t["entry_time"]).weekday() == 4
              and datetime.fromisoformat(t["entry_time"]).hour >= 14]
    if len(fri_pm) < 3:
        return None
    loss_rate = sum(1 for t in fri_pm if (t.get("pnl") or 0) < 0) / len(fri_pm)
    if loss_rate >= 0.6:
        return f"Friday PM expiry trap: {loss_rate*100:.0f}% loss rate on {len(fri_pm)} trades."
    return None

# ── Claude AI Review ──────────────────────────────────────────────────────────
COACHING_PROMPT = """You are a trading psychology coach for Indian retail traders.
Analyze the trade data and give 3-5 specific, actionable insights.
Focus: behavioral patterns, emotional mistakes, risk management, timing.
Be direct. Use specific numbers from the data. No generic advice.
Format: bullet points. Each insight max 2 sentences."""

def generate_ai_review(trades: list, metrics: dict) -> str:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return "Set ANTHROPIC_API_KEY to enable AI review."
    client = Anthropic(api_key=api_key)
    trade_summary = json.dumps([{
        "symbol": t.get("symbol"), "pnl": t.get("net_pnl"),
        "emotion_before": t.get("emotion_before"),
        "followed_plan": t.get("followed_plan"),
        "mistake_tags": t.get("mistake_tags"),
        "setup_type": t.get("setup_type"),
        "entry_time": t.get("entry_time"),
    } for t in trades[-30:]], indent=2)

    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=800,
        system=[{"type": "text", "text": COACHING_PROMPT,
                 "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content":
                   f"Last 30 trades:\n{trade_summary}\n\nMetrics: win_rate={metrics.get('win_rate'):.1%}, "
                   f"total_pnl=₹{metrics.get('total_pnl'):,.0f}, risk_score={metrics.get('risk_score')}"}]
    )
    return msg.content[0].text

# ── Streamlit UI ───────────────────────────────────────────────────────────────
st.set_page_config(page_title="TradeFlow AI — Admin", page_icon="📊",
                   layout="wide", initial_sidebar_state="expanded")

st.markdown("""
<style>
.stApp { background-color: #0A0B0D; color: #F1F5F9; }
.metric-card { background: #111318; border: 1px solid #1E2229;
               border-radius: 12px; padding: 20px; margin: 8px 0; }
.stMetric { background: #111318 !important; }
div[data-testid="stMetricValue"] { color: #00D4AA; font-size: 28px; font-weight: 700; }
</style>
""", unsafe_allow_html=True)

# ── Sidebar: Upstox Auth ───────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("## TradeFlow AI")
    st.markdown("---")

    token = _load_token()

    # Handle OAuth callback code from URL
    query_params = st.query_params
    if "code" in query_params and not token:
        code = query_params["code"]
        with st.spinner("Connecting Upstox..."):
            try:
                token = exchange_code_for_token(code)
                _save_token(token)
                st.success("Upstox connected!")
                st.query_params.clear()
                st.rerun()
            except Exception as e:
                st.error(f"Auth failed: {e}")

    if token:
        st.success("Upstox Connected")
        funds = get_funds(token)
        available = funds.get("available_margin", 0)
        st.metric("Available Funds", f"₹{available:,.0f}")
        if st.button("Disconnect"):
            if os.path.exists("cache/upstox_token.json"):
                os.remove("cache/upstox_token.json")
            st.rerun()
    else:
        auth_url = get_auth_url()
        st.markdown(f"[Connect Upstox]({auth_url})", unsafe_allow_html=True)
        st.info("Click above → login → redirected back here")

    st.markdown("---")
    user_id = st.text_input("Supabase User ID", value=st.session_state.get("user_id", ""),
                             help="From Supabase Auth → Users")
    if user_id:
        st.session_state["user_id"] = user_id
    capital = st.number_input("Starting Capital (₹)", value=500000, step=10000)

    page = st.radio("Navigate", ["Dashboard", "Sync Portfolio",
                                  "Log Trade", "AI Review", "Signal Advisor", "Insights"])

# ── Main content ───────────────────────────────────────────────────────────────
uid = st.session_state.get("user_id", "")

if not uid:
    st.warning("Enter your Supabase User ID in the sidebar to begin.")
    st.stop()

# Load trades
@st.cache_data(ttl=60)
def load_trades(user_id: str) -> pd.DataFrame:
    rows = sb_get("trades", {"user_id": f"eq.{user_id}",
                              "select": "*", "order": "entry_time.desc"})
    return pd.DataFrame(rows) if rows else pd.DataFrame()

df = load_trades(uid)

# ─────────────────────────────────────────────────────────────────────────────
if page == "Dashboard":
    st.title("Dashboard")

    if df.empty:
        st.info("No trades yet. Log your first trade or sync from Upstox.")
        st.stop()

    closed = df[df["status"] == "closed"].copy()
    if not closed.empty:
        closed["net_pnl"] = pd.to_numeric(closed["net_pnl"], errors="coerce").fillna(0)
        total_pnl = closed["net_pnl"].sum()
        wins = (closed["net_pnl"] > 0).sum()
        win_rate = wins / len(closed)
        avg_win = closed[closed["net_pnl"] > 0]["net_pnl"].mean() if wins else 0
        avg_loss = closed[closed["net_pnl"] < 0]["net_pnl"].mean() if (len(closed)-wins) else 0
        risk_score = compute_risk_score(closed.to_dict("records"), capital)

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Total PnL", f"₹{total_pnl:,.0f}",
                  delta=f"₹{total_pnl:,.0f}" if total_pnl >= 0 else None)
        c2.metric("Win Rate", f"{win_rate:.1%}", f"{wins}/{len(closed)} trades")
        c3.metric("Risk Score", f"{risk_score}/100",
                  delta=None, delta_color="inverse")
        c4.metric("Avg Win/Loss", f"₹{avg_win:,.0f} / ₹{avg_loss:,.0f}")

        st.markdown("---")

        # PnL chart
        closed["entry_date"] = pd.to_datetime(closed["entry_time"]).dt.date
        daily = closed.groupby("entry_date")["net_pnl"].sum().reset_index()
        daily["cumulative"] = daily["net_pnl"].cumsum()
        st.subheader("Cumulative PnL")
        st.line_chart(daily.set_index("entry_date")["cumulative"])

        # Mistake breakdown
        all_tags = [tag for tags in closed["mistake_tags"].dropna() for tag in tags]
        if all_tags:
            tag_counts = pd.Series(all_tags).value_counts()
            st.subheader("Mistake Frequency")
            st.bar_chart(tag_counts)

        # Rule violations
        st.subheader("Behavioral Alerts")
        alerts = []
        trades_list = closed.to_dict("records")
        if r := detect_revenge_trading(trades_list):
            alerts.append(("🔴 CRITICAL", r))
        if t := detect_time_pattern(trades_list):
            alerts.append(("🟡 WARNING", t))
        if f := detect_friday_trap(trades_list):
            alerts.append(("🟡 WARNING", f))

        if alerts:
            for severity, msg in alerts:
                st.warning(f"**{severity}**: {msg}")
        else:
            st.success("No major behavioral patterns detected this period.")

    # Open trades
    open_t = df[df["status"] == "open"]
    if not open_t.empty:
        st.subheader(f"Open Positions ({len(open_t)})")
        st.dataframe(open_t[["symbol", "segment", "trade_type",
                               "entry_price", "stop_loss", "target_price",
                               "quantity", "entry_time"]].reset_index(drop=True),
                     use_container_width=True)

# ─────────────────────────────────────────────────────────────────────────────
elif page == "Sync Portfolio":
    st.title("Sync from Upstox")

    if not token:
        st.error("Connect Upstox first (sidebar).")
        st.stop()

    col1, col2 = st.columns(2)

    with col1:
        st.subheader("Long-term Holdings")
        if st.button("Fetch Holdings"):
            with st.spinner("Fetching..."):
                holdings = get_holdings(token)
            if holdings:
                hdf = pd.DataFrame(holdings)
                display_cols = [c for c in ["trading_symbol", "quantity", "average_price",
                                            "last_price", "pnl", "day_change_percentage"]
                                if c in hdf.columns]
                st.dataframe(hdf[display_cols], use_container_width=True)
                total_pnl = hdf["pnl"].sum() if "pnl" in hdf.columns else 0
                st.metric("Total Unrealized PnL", f"₹{total_pnl:,.0f}")
            else:
                st.info("No holdings or API error.")

    with col2:
        st.subheader("Today's Positions")
        if st.button("Fetch Positions"):
            with st.spinner("Fetching..."):
                positions = get_positions(token)
            if positions:
                pdf = pd.DataFrame(positions)
                display_cols = [c for c in ["trading_symbol", "quantity", "average_price",
                                            "last_price", "pnl", "day_change"]
                                if c in pdf.columns]
                st.dataframe(pdf[display_cols], use_container_width=True)
            else:
                st.info("No open positions today.")

    st.markdown("---")
    st.subheader("Sync Today's Session to Supabase")
    if st.button("Sync Session", type="primary"):
        with st.spinner("Syncing..."):
            today_trades = df[pd.to_datetime(df["entry_time"]).dt.date == date.today()]
            closed_today = today_trades[today_trades["status"] == "closed"]
            total = pd.to_numeric(closed_today["net_pnl"], errors="coerce").fillna(0).sum()
            wins = (pd.to_numeric(closed_today["net_pnl"], errors="coerce") > 0).sum()
            risk = compute_risk_score(closed_today.to_dict("records"), capital)
            session = {
                "user_id": uid, "date": str(date.today()),
                "total_pnl": float(total), "trade_count": len(closed_today),
                "win_count": int(wins), "loss_count": int(len(closed_today) - wins),
                "risk_score": float(risk), "starting_capital": float(capital)
            }
            result = sb_upsert("daily_sessions", session, on_conflict="user_id,date")
            st.success(f"Session synced. Risk score: {risk}/100")

# ─────────────────────────────────────────────────────────────────────────────
elif page == "Log Trade":
    st.title("Log Trade")

    with st.form("trade_form"):
        c1, c2, c3 = st.columns(3)
        symbol    = c1.text_input("Symbol *", placeholder="RELIANCE").upper()
        segment   = c2.selectbox("Segment *", ["EQ", "FO", "MF", "CRYPTO"])
        trade_type = c3.selectbox("Type *", ["BUY", "SELL", "SHORT", "COVER"])

        c4, c5, c6 = st.columns(3)
        setup_type = c4.selectbox("Setup", ["", "breakout", "reversal",
                                            "momentum", "scalp", "btst", "swing"])
        timeframe  = c5.selectbox("Timeframe", ["", "1m", "5m", "15m", "1h", "4h", "1d"])
        quantity   = c6.number_input("Quantity *", min_value=1, value=1)

        c7, c8, c9, c10 = st.columns(4)
        entry_price  = c7.number_input("Entry Price *", min_value=0.0, format="%.2f")
        exit_price   = c8.number_input("Exit Price (0=open)", min_value=0.0, format="%.2f")
        stop_loss    = c9.number_input("Stop Loss", min_value=0.0, format="%.2f")
        target_price = c10.number_input("Target", min_value=0.0, format="%.2f")

        charges = st.number_input("Charges (₹)", min_value=0.0, value=20.0, format="%.2f")

        st.markdown("**Psychology**")
        p1, p2, p3 = st.columns(3)
        emotion_before  = p1.selectbox("Emotion Before", ["", "calm", "confident",
                                                           "anxious", "fearful", "greedy", "excited"])
        emotion_after   = p2.selectbox("Emotion After", ["", "satisfied", "frustrated",
                                                          "relieved", "regretful", "neutral"])
        confidence      = p3.slider("Confidence 1-10", 1, 10, 7)

        followed_plan = st.checkbox("Followed my trading plan", value=True)
        rating = st.slider("Trade Rating 1-5", 1, 5, 3)

        mistake_options = ["revenge_trade", "overconfident", "panic_exit", "fomo",
                           "sized_too_big", "no_stop_loss", "moved_stop", "overtraded",
                           "early_exit", "late_entry", "broke_rules", "impulsive"]
        mistake_tags = st.multiselect("Mistake Tags", mistake_options)
        notes = st.text_area("Notes / Reflection")
        entry_time = st.datetime_input("Entry Time", value=datetime.now())

        submitted = st.form_submit_button("Log Trade", type="primary")

    if submitted and symbol and entry_price > 0 and quantity > 0:
        ep = exit_price if exit_price > 0 else None
        pnl = None
        net_pnl = None
        status = "open"
        if ep:
            mult = -1 if trade_type in ("SHORT", "SELL") else 1
            pnl = (ep - entry_price) * quantity * mult
            net_pnl = pnl - charges
            status = "closed"

        trade = {
            "user_id": uid, "symbol": symbol, "segment": segment,
            "trade_type": trade_type, "setup_type": setup_type or None,
            "timeframe": timeframe or None, "entry_price": float(entry_price),
            "exit_price": ep, "stop_loss": float(stop_loss) if stop_loss else None,
            "target_price": float(target_price) if target_price else None,
            "quantity": int(quantity), "entry_time": entry_time.isoformat(),
            "exit_time": datetime.now().isoformat() if ep else None,
            "pnl": pnl, "net_pnl": net_pnl, "charges": float(charges),
            "status": status, "emotion_before": emotion_before or None,
            "emotion_after": emotion_after or None, "confidence_score": int(confidence),
            "followed_plan": followed_plan, "mistake_tags": mistake_tags or None,
            "notes": notes or None, "rating": int(rating),
        }
        result = sb_insert("trades", trade)
        if isinstance(result, list) and result:
            st.success(f"Trade logged! PnL: ₹{net_pnl:,.0f}" if net_pnl else "Trade logged (open).")
            st.cache_data.clear()
        else:
            st.error(f"Error: {result}")

# ─────────────────────────────────────────────────────────────────────────────
elif page == "AI Review":
    st.title("AI Review — Claude Sonnet")

    if df.empty:
        st.info("No trades to review.")
        st.stop()

    closed = df[df["status"] == "closed"]
    if closed.empty:
        st.info("No closed trades yet.")
        st.stop()

    closed["net_pnl"] = pd.to_numeric(closed["net_pnl"], errors="coerce").fillna(0)
    wins = (closed["net_pnl"] > 0).sum()
    metrics = {
        "total_pnl": float(closed["net_pnl"].sum()),
        "win_rate": wins / len(closed) if len(closed) else 0,
        "risk_score": compute_risk_score(closed.to_dict("records"), capital),
    }

    col1, col2, col3 = st.columns(3)
    col1.metric("Total PnL", f"₹{metrics['total_pnl']:,.0f}")
    col2.metric("Win Rate", f"{metrics['win_rate']:.1%}")
    col3.metric("Risk Score", f"{metrics['risk_score']}/100")

    st.markdown("---")
    st.subheader("Deterministic Patterns")
    trades_list = closed.to_dict("records")
    found = False
    for fn, label in [(detect_revenge_trading, "Revenge Trading"),
                      (detect_time_pattern, "Time Pattern"),
                      (detect_friday_trap, "Friday Trap")]:
        result = fn(trades_list)
        if result:
            st.warning(f"**{label}**: {result}")
            found = True
    if not found:
        st.success("No behavioral patterns detected.")

    st.markdown("---")
    st.subheader("Claude AI Coaching")

    if not os.environ.get("ANTHROPIC_API_KEY"):
        st.error("Set ANTHROPIC_API_KEY env var to enable Claude.")
    else:
        days = st.slider("Analyze last N days", 7, 90, 30)
        cutoff = datetime.now() - timedelta(days=days)
        recent = closed[pd.to_datetime(closed["entry_time"]) >= cutoff]

        if st.button("Generate AI Review", type="primary"):
            with st.spinner("Claude analyzing your trades..."):
                review = generate_ai_review(recent.to_dict("records"), metrics)
            st.markdown(review)

            # Save to Supabase
            insight = {
                "user_id": uid, "type": "pattern", "category": "psychology",
                "title": f"AI Review — {date.today().strftime('%d %b %Y')}",
                "body": review, "severity": "info",
                "period_from": str((date.today() - timedelta(days=days))),
                "period_to": str(date.today())
            }
            sb_insert("ai_insights", insight)

# ─────────────────────────────────────────────────────────────────────────────
elif page == "Signal Advisor":
    st.title("Signal Advisor — Expert Setup Review")
    st.caption("Paste your setup. Get a pro-level intraday opinion in 10 seconds.")

    if not os.environ.get("GROQ_API_KEY"):
        st.error("Set GROQ_API_KEY in Streamlit secrets (Settings → Secrets) to enable Signal Advisor.")
        st.stop()

    ADVISOR_SYSTEM = """You are a senior intraday trader and technical analyst with 15+ years on NSE/MCX/global markets.
When given a trading setup, respond with a structured expert analysis. Be specific with numbers. No generic advice.

Format your response EXACTLY like this:

**SIGNAL STRENGTH:** [Strong / Moderate / Weak] — [one-line reason]

**ENTRY ZONE:** [price range or specific level]
**STOP LOSS:** [price] — [why: below support / prev low / ATR etc]
**TARGET 1:** [price] — [% gain]
**TARGET 2:** [price] — [% gain]
**RISK:REWARD:** [X:1]

**CONFIRM BEFORE ENTRY:**
• [key confirmation 1]
• [key confirmation 2]
• [key confirmation 3]

**WATCH OUT FOR:**
• [risk factor 1]
• [risk factor 2]

**VERDICT:** [2-3 direct sentences. Specific. No hedging. Tell them what a pro would do.]"""

    col1, col2, col3 = st.columns(3)
    with col1:
        symbol = st.text_input("Symbol", placeholder="GOLD / RELIANCE / BANKNIFTY", value="GOLD")
    with col2:
        timeframe = st.selectbox("Timeframe", ["5m", "15m", "1H", "Daily", "Weekly"], index=1)
    with col3:
        setup_type = st.selectbox("Setup Type", [
            "RSI Breakout", "RSI Oversold Bounce", "MACD Crossover",
            "EMA Crossover", "Support Bounce", "Resistance Breakout",
            "Momentum Continuation", "Opening Range Breakout", "Other"
        ])

    col4, col5 = st.columns(2)
    with col4:
        rsi_val = st.number_input("RSI Value", min_value=0.0, max_value=100.0, value=72.0, step=0.1)
    with col5:
        current_price = st.number_input("Current Price", min_value=0.0, value=0.0, step=0.5,
                                         help="Leave 0 if unknown")

    notes = st.text_area("Additional context", placeholder="Volume spike? Above key EMA? Previous resistance level? Any macro catalyst?", height=80)

    if st.button("Get Expert Analysis", type="primary"):
        user_msg = f"""Symbol: {symbol}
Timeframe: {timeframe}
Setup: {setup_type}
RSI: {rsi_val}
{"Current Price: " + str(current_price) if current_price > 0 else ""}
{"Additional context: " + notes if notes.strip() else ""}""".strip()

        with st.spinner("Analyzing setup..."):
            try:
                from groq import Groq as _Groq
                _client = _Groq(api_key=os.environ["GROQ_API_KEY"])
                _msg = _client.chat.completions.create(
                    model="llama-3.3-70b-versatile",
                    max_tokens=700,
                    messages=[
                        {"role": "system", "content": ADVISOR_SYSTEM},
                        {"role": "user", "content": user_msg},
                    ],
                )
                analysis = _msg.choices[0].message.content
                st.markdown("---")
                st.markdown(analysis)
                st.markdown("---")
                st.caption("⚠ Not SEBI-registered · Educational only · Always manage your own risk.")
            except Exception as e:
                st.error(f"Analysis error: {e}")

# ─────────────────────────────────────────────────────────────────────────────
elif page == "Insights":
    st.title("Past Insights")

    insights = sb_get("ai_insights", {
        "user_id": f"eq.{uid}",
        "order": "created_at.desc",
        "limit": "20"
    })

    if not insights:
        st.info("No insights yet. Run AI Review first.")
    else:
        for ins in insights:
            severity_color = {"critical": "🔴", "warning": "🟡", "info": "🔵"}.get(
                ins.get("severity", "info"), "🔵")
            with st.expander(f"{severity_color} {ins['title']} — {ins['created_at'][:10]}"):
                st.markdown(ins["body"])
                if not ins.get("is_read"):
                    if st.button("Mark read", key=ins["id"]):
                        sb_update("ai_insights", {"id": ins["id"]}, {"is_read": True})
                        st.rerun()
