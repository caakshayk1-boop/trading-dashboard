"""Sends Upstox login reminder to Telegram every morning at 8:45 AM IST."""
import os, requests
from upstox_provider import get_auth_url

TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

if not TELEGRAM_TOKEN:
    from config import TELEGRAM_TOKEN, TELEGRAM_CHAT_ID

auth_url = get_auth_url()
msg = (
    "⏰ *Good morning!*\n\n"
    "Login to Upstox before the 9:30 AM scan:\n\n"
    f"👉 [Tap here to login]({auth_url})\n\n"
    "After login, Upstox data activates automatically.\n"
    "_(Scanner runs in 45 minutes)_"
)

requests.post(
    f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
    data={"chat_id": TELEGRAM_CHAT_ID, "text": msg,
          "parse_mode": "Markdown", "disable_web_page_preview": False},
    timeout=10
)
print("Login reminder sent")
