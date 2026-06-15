"""CoinDCX crypto-futures monitor (REST polling, public market data).

Flow per cycle:
  1. Discover all active futures instruments.
  2. For each, ensure today's previous-day high/low is stored (from daily candles).
  3. Pull the latest 1-minute candle, append to CSV.
  4. If that minute crosses prev-day high/low, alert (subject to caps).

CoinDCX public endpoints used:
  - GET https://api.coindcx.com/exchange/v1/derivatives/futures/data/active_instruments
  - GET https://public.coindcx.com/market_data/candles?pair=<pair>&interval=<i>&limit=<n>
"""

import csv
import os
import re
import time
from datetime import datetime, timezone

import requests

import config
import state
from telegram_alert import send_message, format_alert

SOURCE = "coindcx"
_BASE = "https://api.coindcx.com"
_PUBLIC = "https://public.coindcx.com"
_SESSION = requests.Session()

_CRYPTO_TOKENS_CACHE = "crypto_tokens.csv"
# Quote/settlement tokens that are NOT the crypto base asset we care about.
_QUOTE_TOKENS = {"USDT", "USDC", "USD", "INR", "BUSD", "DAI", "FDUSD"}


def _get(url: str, params: dict = None):
    try:
        r = _SESSION.get(url, params=params, timeout=20)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as exc:
        print(f"[coindcx] GET failed {url}: {exc}")
        return None


def list_futures_instruments():
    """Return list of active futures instrument pair strings."""
    url = f"{_BASE}/exchange/v1/derivatives/futures/data/active_instruments"
    data = _get(url)
    if not data:
        return []
    if isinstance(data, dict):
        for key in ("instruments", "data", "pairs"):
            if key in data and isinstance(data[key], list):
                return data[key]
        return []
    return data


def _extract_base_token(pair: str) -> str:
    """Extract the crypto base token from a futures instrument string.

    Examples:
      'B-BTC_USDT' -> 'BTC'
      'B-1000PEPE_USDT' -> 'PEPE'
      'ETH_USDT' -> 'ETH'
    """
    s = pair.upper()
    # drop a leading exchange prefix like 'B-'
    if "-" in s:
        s = s.split("-", 1)[1]
    # base is the part before the quote separator
    base = s.split("_", 1)[0]
    # strip leading multipliers such as '1000' in '1000PEPE'
    base = re.sub(r"^[0-9]+", "", base)
    return base


def crypto_tokens(refresh: bool = False) -> set:
    """Return the set of crypto base tokens listed on CoinDCX futures.

    Cached once to data/crypto_tokens.csv. Pass refresh=True to rebuild
    from the live instrument list.
    """
    path = os.path.join(config.DATA_DIR, _CRYPTO_TOKENS_CACHE)

    if not refresh and os.path.exists(path) and os.path.getsize(path) > 0:
        tokens = set()
        with open(path, newline="") as f:
            for r in csv.DictReader(f):
                tok = (r.get("token") or "").strip().upper()
                if tok:
                    tokens.add(tok)
        if tokens:
            return tokens

    # build from the live futures instrument list
    tokens = set()
    for pair in list_futures_instruments():
        base = _extract_base_token(pair)
        if base and base not in _QUOTE_TOKENS:
            tokens.add(base)

    if tokens:
        os.makedirs(config.DATA_DIR, exist_ok=True)
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["token"])
            for tok in sorted(tokens):
                w.writerow([tok])
    return tokens


def _candles(pair: str, interval: str, limit: int):
    url = f"{_PUBLIC}/market_data/candles"
    return _get(url, {"pair": pair, "interval": interval, "limit": limit})


def prev_day_levels(pair: str):
    """Return (prev_high, prev_low) from the last completed daily candle."""
    candles = _candles(pair, "1d", 2)
    if not candles or len(candles) < 2:
        return None
    candles = sorted(candles, key=lambda c: c["time"])
    prev = candles[-2]  # the fully-closed previous day
    return float(prev["high"]), float(prev["low"])


def _candle_time_ist(candle: dict) -> str:
    """Convert a CoinDCX candle epoch (ms, UTC) to an IST timestamp string."""
    epoch_ms = int(candle["time"])
    dt_utc = datetime.fromtimestamp(epoch_ms / 1000.0, tz=timezone.utc)
    return state.ist_str(dt_utc)


def latest_minute(pair: str):
    """Return the last completed 1-minute candle as a dict, or None."""
    candles = _candles(pair, "1m", 2)
    if not candles or len(candles) < 2:
        return None
    candles = sorted(candles, key=lambda c: c["time"])
    return candles[-2]  # -1 is the still-forming candle; -2 is closed


def _check(pair: str, minute: dict, prev_high: float, prev_low: float):
    high = float(minute["high"])
    low = float(minute["low"])
    close = float(minute["close"])

    if high >= prev_high:
        ok, count, cap = state.register_cross(pair, "HIGH")
        if ok:
            send_message(format_alert(SOURCE, pair, "HIGH", close,
                                      prev_high, count, cap))
    if low <= prev_low:
        ok, count, cap = state.register_cross(pair, "LOW")
        if ok:
            send_message(format_alert(SOURCE, pair, "LOW", close,
                                      prev_low, count, cap))


def run_cycle():
    """One full polling cycle across all futures instruments."""
    pairs = list_futures_instruments()
    if not pairs:
        return
    print(f"[coindcx] monitoring {len(pairs)} futures instruments")

    # Prev-day high/low is a once-a-day job: only (re)load it on the first
    # cycle of a new day. Other minutes reuse the cached levels.
    load_levels = not state.levels_loaded_for(SOURCE)

    for pair in pairs:
        if load_levels:
            levels = prev_day_levels(pair)
            if not levels:
                continue
            prev_high, prev_low = levels
            state.save_prev_level(pair, prev_high, prev_low, SOURCE)
        else:
            cached = state.get_cached_level(pair, SOURCE)
            if not cached:
                continue
            prev_high, prev_low = cached

        minute = latest_minute(pair)
        if not minute:
            continue
        ts = _candle_time_ist(minute)
        written = state.append_minute(
            SOURCE, pair, ts,
            float(minute["open"]), float(minute["high"]),
            float(minute["low"]), float(minute["close"]),
        )
        if written:
            _check(pair, minute, prev_high, prev_low)
        time.sleep(0.1)  # be gentle on the public API

    if load_levels:
        state.mark_levels_loaded(SOURCE)
