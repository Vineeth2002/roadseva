"""
alerts.py — Telegram error-alert helper.

Extracted verbatim from main.py's _send_error_alert(). It was defined
locally in main.py and used in two places: ErrorAlertMiddleware (for 500s
and unhandled exceptions) and /health/watchdog (for scheduler-down alerts).
Both need it, and api/health.py can't import it from main.py without a
circular import (main.py imports the health router), so it moves here.

INTEGRATION STEPS:
  1. Save as C:\\RoadSeva\\alerts.py
  2. In main.py, DELETE the local def _send_error_alert(...) block
  3. In main.py, add: from alerts import send_error_alert
  4. In main.py, replace both call sites (_send_error_alert(...)) with
     send_error_alert(...) — same two spots: ErrorAlertMiddleware.dispatch()
     and wherever else references it.
  5. Confirm TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID env vars still fire an
     alert — trigger a fake 500 locally and check your Telegram chat.
"""

import os
import json
import urllib.request


def send_error_alert(message: str) -> None:
    token   = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return
    try:
        data = json.dumps({"chat_id": chat_id, "text": f"RoadSeva\n{message}"[:4000]}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=data, headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass