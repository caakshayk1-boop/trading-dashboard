# Copy this to config.py and fill in your values
# config.py is in .gitignore — never committed to GitHub

import os

TELEGRAM_TOKEN      = os.environ.get("TELEGRAM_TOKEN", "paste_your_token_here")
TELEGRAM_CHAT_ID    = os.environ.get("TELEGRAM_CHAT_ID", "paste_your_chat_id_here")

CAPITAL             = 500000
RISK_PER_TRADE      = 0.01
MIN_SIGNAL_SCORE    = 70
MIN_PRICE           = 50
MIN_AVG_VOLUME      = 50000
SCAN_TIMES          = ["09:30", "14:00", "17:30"]
SEND_TOP_PICKS_ONLY = False
ENABLE_WEEKLY_CONFIRM = True
MAX_PE              = 60
MAX_WORKERS         = 10
