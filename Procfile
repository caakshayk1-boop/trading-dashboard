worker: python claude_bot.py
polymon: python polymarket_monitor.py
web: gunicorn newspaper:app --bind 0.0.0.0:$PORT --workers 2 --timeout 120
