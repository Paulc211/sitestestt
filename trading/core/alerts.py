"""Telegram alerts. Best-effort: an alert failure must never crash an agent.
Dedupes identical alert keys within a cooldown window so a flapping condition
doesn't flood your phone.
"""
from __future__ import annotations

import logging
import time

from core import db
from core.http import retry_request
from core.settings import telegram_configured, telegram_creds

log = logging.getLogger("alerts")

DEDUP_WINDOW_SEC = 900


def send_alert(message: str, dedup_key: str | None = None) -> bool:
    """Send a Telegram message. Returns True if delivered (or suppressed as a
    duplicate), False on failure. Never raises."""
    try:
        if dedup_key:
            last = db.kv_get(f"alert_last:{dedup_key}")
            if last and time.time() - float(last) < DEDUP_WINDOW_SEC:
                log.info("Alert suppressed (dedup %s)", dedup_key)
                return True
            db.kv_set(f"alert_last:{dedup_key}", str(time.time()))

        if not telegram_configured():
            log.warning("Telegram not configured; alert only logged: %s", message)
            return False

        token, chat_id = telegram_creds()
        retry_request(
            "POST",
            f"https://api.telegram.org/bot{token}/sendMessage",
            json_body={"chat_id": chat_id, "text": message[:4000]},
            timeout=10,
            max_retries=3,
        )
        log.info("Alert sent: %s", message.splitlines()[0][:120])
        return True
    except Exception:
        log.exception("Failed to send alert (message was logged above)")
        return False
