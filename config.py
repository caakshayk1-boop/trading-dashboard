import os
TELEGRAM_TOKEN      = os.environ.get("TELEGRAM_TOKEN", "8716634372:AAFuRGXIZORqyG-EUZOeVhJ2a4i85IqDmbM")
TELEGRAM_CHAT_ID    = os.environ.get("TELEGRAM_CHAT_ID", "1101865515")

UPSTOX_API_KEY      = os.environ.get("UPSTOX_API_KEY", "33270875-6e63-47dc-afa5-0e61e2962e05")
UPSTOX_API_SECRET   = os.environ.get("UPSTOX_API_SECRET", "za7r9e06g4")
UPSTOX_REDIRECT_URL = "http://localhost:8501"
UPSTOX_TOKEN_FILE   = "cache/upstox_token.json"

CAPITAL             = 500000   # Your trading capital in INR
RISK_PER_TRADE      = 0.01     # 1% risk per trade
MIN_SIGNAL_SCORE    = 70       # Minimum score to fire a signal (0-100)
MIN_PRICE           = 50       # Skip penny stocks below this price
MIN_AVG_VOLUME      = 50000    # Minimum 20-day avg volume (liquidity filter)
SCAN_TIMES          = ["09:20", "14:45", "15:20"]  # IST auto-scan times
SEND_TOP_PICKS_ONLY = False    # True = Telegram gets only top 5 signals
ENABLE_WEEKLY_CONFIRM = True   # Require weekly EMA alignment
MAX_PE              = 60       # Max trailing P/E (0 = disabled)
MAX_WORKERS         = 10       # Parallel threads for scanning
