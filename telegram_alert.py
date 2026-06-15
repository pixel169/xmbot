"""Minimal Telegram sender (no external Telegram SDK needed)."""

import requests

import config


def send_message(text: str) -> bool:
    """Send a plain-text message to the configured private channel.

    Returns True on success, False otherwise (never raises, so the monitor
    loop keeps running).
    """
    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": config.TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        resp = requests.post(url, json=payload, timeout=15)
        if resp.status_code != 200:
            print(f"[telegram] failed {resp.status_code}: {resp.text}")
            return False
        return True
    except requests.RequestException as exc:
        print(f"[telegram] error: {exc}")
        return False


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
