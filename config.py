import os
from dotenv import load_dotenv

load_dotenv()

def _require(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise EnvironmentError(f"Required environment variable '{name}' is not set. Check your .env file.")
    return val

TELEGRAM_TOKEN      = _require("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID    = _require("TELEGRAM_CHAT_ID")

UPSTOX_API_KEY      = os.environ.get("UPSTOX_API_KEY", "")   # optional — not used in scan flow
UPSTOX_API_SECRET   = os.environ.get("UPSTOX_API_SECRET", "")
UPSTOX_REDIRECT_URL = "http://localhost:8501"
UPSTOX_TOKEN_FILE   = "cache/upstox_token.json"

CAPITAL             = 500000   # Your trading capital in INR
RISK_PER_TRADE      = 0.01     # 1% risk per trade
MIN_SIGNAL_SCORE    = 78       # Expert grade: 78+ only (was 75)
MIN_PRICE           = 50       # Skip penny stocks below this price
MIN_AVG_VOLUME      = 150000   # Expert grade: min 20-day avg volume (was 100k)
MIN_RR              = 2.0      # Expert grade: minimum 2:1 risk:reward (was 1.5)
SCAN_TIMES          = ["09:20", "11:45", "16:30"]  # IST auto-scan times
SEND_TOP_PICKS_ONLY = True     # Telegram gets only top 5 signals (quality)
ENABLE_WEEKLY_CONFIRM = True   # Require weekly EMA alignment
MAX_PE              = 80       # Max trailing P/E (0 = disabled)
MAX_WORKERS         = 12       # Parallel threads for scanning

GROQ_API_KEY        = _require("GROQ_API_KEY")

VERCEL_URL          = "https://tradeflow-pro-kappa.vercel.app"
