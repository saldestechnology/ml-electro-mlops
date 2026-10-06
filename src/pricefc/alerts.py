"""Failure alerts to the Telegram monitoring group.

Alerts are best effort: a missing secret (Vault sealed, local run) or an unreachable Telegram
must never turn into a second failure, so `send_telegram` logs and returns False instead of
raising. The bot token is part of the request URL, so neither the URL nor a request exception's
text is ever logged; only status codes and exception types are.
"""

from __future__ import annotations

import os
import re

import requests
import structlog

from pricefc.secrets import get_secret

log = structlog.get_logger(__name__)

API = "https://api.telegram.org"
MAX_CHARS = 4000  # Telegram's limit is 4096 characters per message
_REDACT = (
    (re.compile(r"bot\d+:[\w-]+"), "bot<redacted>"),
    (re.compile(r"(?i)(securityToken|token|apikey|api_key)=[^&\s]+"), r"\1=<redacted>"),
)


def redact(text: str) -> str:
    """Strip anything that looks like a token from text that is about to leave the process."""
    for pattern, repl in _REDACT:
        text = pattern.sub(repl, text)
    return text


def send_telegram(text: str) -> bool:
    """Send `text` (plain, prefixed with the environment) to the alert chat. True if sent."""
    try:
        token = get_secret("telegram_bot_token")
        chat_id = get_secret("telegram_chat_id")
        if not token or not chat_id:
            missing = [n for n, v in (("bot_token", token), ("chat_id", chat_id)) if not v]
            log.warning("telegram_not_configured", missing=missing)
            return False
        env = os.environ.get("PRICEFC_ENV", "dev")
        message = redact(f"[{env}] {text}")[:MAX_CHARS]
        resp = requests.post(
            f"{API}/bot{token}/sendMessage",
            data={"chat_id": chat_id, "text": message, "disable_web_page_preview": "true"},
            timeout=10,
        )
        if resp.status_code != 200:
            log.warning("telegram_failed", status=resp.status_code)
            return False
        log.info("telegram_sent", status=resp.status_code)
        return True
    except Exception as exc:  # never raise from the alert path
        log.warning("telegram_error", error=type(exc).__name__)
        return False
