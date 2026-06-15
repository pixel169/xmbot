"""Minimal Telegram sender (no external Telegram SDK needed)."""

import requests

import config

_WARNED_MISSING = False


def _creds_ok() -> bool:
    """True only if both the bot token and chat id are configured."""
    global _WARNED_MISSING
    token = str(getattr(config, "TELEGRAM_BOT_TOKEN", "") or "").strip()
    chat = str(getattr(config, "TELEGRAM_CHAT_ID", "") or "").strip()
    if not token or not chat:
        if not _WARNED_MISSING:
            print("[telegram] DISABLED: TELEGRAM_BOT_TOKEN and/or "
                  "TELEGRAM_CHAT_ID are empty in config.py. No messages "
                  "will be sent until both are set.")
            _WARNED_MISSING = True
        return False
    return True


def send_message(text: str) -> bool:
    """Send a plain-text message to the configured private channel.

    Returns True on success, False otherwise (never raises, so the monitor
    loop keeps running).
    """
    if not _creds_ok():
        return False

    token = str(config.TELEGRAM_BOT_TOKEN).strip()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": str(config.TELEGRAM_CHAT_ID).strip(),
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        resp = requests.post(url, json=payload, timeout=15)
        # Telegram always returns JSON with an "ok" boolean and, on error,
        # a "description" explaining exactly what went wrong.
        try:
            data = resp.json()
        except ValueError:
            data = {}
        if resp.status_code != 200 or not data.get("ok", False):
            desc = data.get("description") or resp.text
            print(f"[telegram] send failed (HTTP {resp.status_code}): {desc}")
            return False
        return True
    except requests.RequestException as exc:
        print(f"[telegram] network error: {exc}")
        return False


def send_startup_ping() -> bool:
    """Send a one-off message at startup to confirm Telegram is wired up.

    Helps distinguish 'no breaches yet' from 'Telegram is misconfigured'.
    """
    if not _creds_ok():
        return False
    ok = send_message("\u2705 Market breakout alerter started "
                      "\u2014 Telegram connected.")
    if ok:
        print("[telegram] startup ping sent OK")
    else:
        print("[telegram] startup ping FAILED \u2014 check token, chat id, "
              "and that the bot is a member/admin of the chat/channel.")
    return ok


def format_alert(source: str, symbol: str, side: str, price: float,
                 level: float, count: int, cap: int) -> str:
    """Build a human-readable breach message.

    `side` is either 'HIGH' or 'LOW'.
    """
    arrow = "\U0001F4C8" if side == "HIGH" else "\U0001F4C9"  # chart up / down
    return (
        f"{arrow} <b>{side} CROSSED</b>\n"
        f"Source: <b>{source}</b>\n"
        f"Symbol: <b>{symbol}</b>\n"
        f"Prev-day {side.lower()}: <code>{level}</code>\n"
        f"Current price: <code>{price}</code>\n"
        f"Alert {count}/{cap} today for this side"
    )
