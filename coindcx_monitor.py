"""CoinDCX crypto-futures monitor (REST polling, public market data).

Two-tier monitoring, optimized for the fewest possible API calls:
  * Every M5 candle close, ONE bulk futures-ticker request returns the last
    price and intraday high/low for every instrument. Pairs within
    config.PROXIMITY_PCT of their prev-day high/low are promoted to the hot
    set. (Falls back to a per-pair 5m candle only when the ticker is missing
    usable high/low for a specific pair.)
  * On the other minutes only the hot set is polled (latest 1m candle).

The minute CSV holds one snapshot row per pair (update, not append).

CoinDCX public endpoints used:
  - GET https://api.coindcx.com/exchange/v1/derivatives/futures/data/active_instruments
  - GET https://public.coindcx.com/market_data/v3/current_prices/futures/rt  (bulk ticker)
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

SOURCE = "coindcx"
_BASE = "https://api.coindcx.com"
_PUBLIC = "https://public.coindcx.com"
_SESSION = requests.Session()

_CRYPTO_TOKENS_CACHE = "crypto_tokens.csv"
# Quote/settlement tokens that are NOT the crypto base asset we care about.
_QUOTE_TOKENS = {"USDT", "USDC", "USD", "INR", "BUSD", "DAI", "FDUSD"}

# Seconds to wait between per-pair calls. Kept small so neither the M5 scan
# nor the 1m hot-set poll overruns its window and starves the minute loop.
_HOT_CALL_SPACING_SEC = 0.05

# Pre-filter guard band: only pairs whose 24h ticker range comes within this
# multiple of PROXIMITY_PCT of a prev-day level are worth a per-pair 5m/1h
# fetch. Wide enough not to miss anything that could be near on the M5 close.
_PREFILTER_BAND_MULT = 20.0

# Pairs currently promoted to 1-minute monitoring (recomputed each M5 scan).
_hot_pairs = set()


# Transient network errors (DNS resolution, dropped connections) are retried
# with a short backoff instead of dropping the whole call.
_GET_RETRIES = 3
_GET_BACKOFF_SEC = 1.0


def _get(url: str, params: dict = None):
    last_exc = None
    for attempt in range(1, _GET_RETRIES + 1):
        try:
            r = _SESSION.get(url, params=params, timeout=20)
            r.raise_for_status()
            return r.json()
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < _GET_RETRIES:
                time.sleep(_GET_BACKOFF_SEC * attempt)
    print(f"[coindcx] GET failed {url}: {last_exc}")
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
    """Extract the crypto base token from a futures instrument string."""
    s = pair.upper()
    if "-" in s:
        s = s.split("-", 1)[1]
    base = s.split("_", 1)[0]
    base = re.sub(r"^[0-9]+", "", base)
    return base


def crypto_tokens(refresh: bool = False) -> set:
    """Return the set of crypto base tokens listed on CoinDCX futures."""
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


# ---------------------------------------------------------------------------
# Bulk ticker (one call for every instrument)
# ---------------------------------------------------------------------------
def _to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _ticker_rows(payload):
    """Yield individual ticker entries from various CoinDCX payload shapes."""
    if isinstance(payload, dict):
        # Sometimes wrapped, e.g. {"prices": {...}} or {"data": [...]}
        for key in ("prices", "data", "result"):
            inner = payload.get(key)
            if isinstance(inner, dict):
                for pair, row in inner.items():
                    if isinstance(row, dict):
                        row.setdefault("pair", pair)
                        yield row
                return
            if isinstance(inner, list):
                for row in inner:
                    if isinstance(row, dict):
                        yield row
                return
        # Flat mapping of pair -> row
        for pair, row in payload.items():
            if isinstance(row, dict):
                row.setdefault("pair", pair)
                yield row
    elif isinstance(payload, list):
        for row in payload:
            if isinstance(row, dict):
                yield row


def _row_pair(row: dict) -> str:
    for key in ("pair", "instrument", "market", "s", "symbol"):
        val = row.get(key)
        if val:
            return str(val)
    return ""


def _row_hl(row: dict):
    """Return (high, low, last) from a ticker row, tolerating field variants."""
    high = None
    for key in ("high", "h", "high_24h", "highest_price_24h"):
        high = _to_float(row.get(key))
        if high is not None:
            break
    low = None
    for key in ("low", "l", "low_24h", "lowest_price_24h"):
        low = _to_float(row.get(key))
        if low is not None:
            break
    last = None
    for key in ("last_price", "ls", "lp", "last", "close", "mark_price", "c"):
        last = _to_float(row.get(key))
        if last is not None:
            break
    return high, low, last


def bulk_ticker() -> dict:
    """One request -> {pair: {'high','low','last'}} for all instruments."""
    payload = _get(f"{_PUBLIC}/market_data/v3/current_prices/futures/rt")
    if payload is None:
        return {}
    out = {}
    for row in _ticker_rows(payload):
        pair = _row_pair(row)
        if not pair:
            continue
        high, low, last = _row_hl(row)
        out[pair] = {"high": high, "low": low, "last": last}
    return out


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


def _now_ist() -> str:
    return state.ist_str(datetime.now(timezone.utc))


def _latest_candle(pair: str, interval: str):
    """Return the last completed candle of `interval` as a dict, or None."""
    candles = _candles(pair, interval, 2)
    if not candles or len(candles) < 2:
        return None
    candles = sorted(candles, key=lambda c: c["time"])
    return candles[-2]  # -1 is the still-forming candle; -2 is closed


def latest_minute(pair: str):
    """Return the last completed 1-minute candle as a dict, or None."""
    return _latest_candle(pair, "1m")


def latest_m5(pair: str):
    """Return the last completed 5-minute candle as a dict, or None."""
    return _latest_candle(pair, "5m")


def _today_start_epoch_ms() -> int:
    """Epoch (ms, UTC) for 00:00 of the current broker day."""
    day = state.today_str()
    dt = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def count_breaks_1h(pair: str, prev_high: float, prev_low: float):
    """Count today's prev-day-level breaks using closed 1h candles.

    Returns (high_breaks, low_breaks). One 1h candle is counted as a HIGH
    break if its high >= prev_high, and a LOW break if its low <= prev_low.
    """
    candles = _candles(pair, "1h", 26)  # >24h of hourly bars
    if not candles:
        return 0, 0
    start_ms = _today_start_epoch_ms()
    high_breaks = low_breaks = 0
    for c in candles:
        try:
            if int(c["time"]) < start_ms:
                continue
            if prev_high and float(c["high"]) >= prev_high:
                high_breaks += 1
            if prev_low and float(c["low"]) <= prev_low:
                low_breaks += 1
        except (KeyError, TypeError, ValueError):
            continue
    return high_breaks, low_breaks


def _level_for(pair: str):
    """Return (prev_high, prev_low) for a pair, loading lazily if needed.

    Returns (levels, fetched) where `fetched` is True if a daily-candle HTTP
    call was made (so the caller can throttle only the network path).
    """
    cached = state.get_cached_level(pair, SOURCE)
    if cached:
        return cached, False
    levels = prev_day_levels(pair)
    if not levels:
        return None, True
    state.save_prev_level(pair, levels[0], levels[1], SOURCE)
    return levels, True


def _record_breaks(pair: str, prev_high: float, prev_low: float):
    """Count today's 1h breaks for a pair and persist/halt as needed."""
    high_breaks, low_breaks = count_breaks_1h(pair, prev_high, prev_low)
    state.set_break_count(SOURCE, pair, "HIGH", high_breaks)
    state.set_break_count(SOURCE, pair, "LOW", low_breaks)


def _scan_m5(pairs):
    """M5 pass: one bulk ticker call decides proximity + the hot set."""
    global _hot_pairs
    ticker = bulk_ticker()
    if not ticker:
        print("[coindcx] bulk ticker unavailable; keeping previous hot set")
        return

    hot = set()
    fallback_calls = 0
    level_calls = 0
    for idx, pair in enumerate(pairs, 1):
        level, fetched = _level_for(pair)
        if fetched:
            level_calls += 1
            # Only the network path is throttled; cached lookups stay fast.
            time.sleep(_HOT_CALL_SPACING_SEC)
            if level_calls % 50 == 0:
                print(f"[coindcx] seeding prev-day levels "
                      f"{idx}/{len(pairs)}...")
        if not level:
            continue
        prev_high, prev_low = level

        # Cheap pre-filter: skip pairs whose 24h range is nowhere near a
        # prev-day level, so we only spend per-pair 5m/1h calls on candidates.
        row = ticker.get(pair)
        if row and row["high"] is not None and row["low"] is not None:
            band = config.PROXIMITY_PCT * _PREFILTER_BAND_MULT
            if not state.within_proximity(row["high"], row["low"],
                                          prev_high, prev_low, pct=band):
                # Keep a light snapshot from the ticker last price and move on.
                if row["last"] is not None:
                    state.update_minute(SOURCE, pair, _now_ist(),
                                        row["last"], row["last"],
                                        row["last"], row["last"])
                continue

        # Candidate: use the M5 candle's own high/low/close for the proximity
        # decision and breach check, exactly like the MT5 monitor.
        candle = latest_m5(pair)
        if not candle:
            continue
        fallback_calls += 1
        time.sleep(_HOT_CALL_SPACING_SEC)
        high = float(candle["high"])
        low = float(candle["low"])
        close = float(candle["close"])
        open_ = float(candle.get("open", close))

        state.update_minute(SOURCE, pair, _candle_time_ist(candle),
                            open_, high, low, close)
        # Count today's breaks from 1h candles; halts the side if it broke
        # the prev-day level more than the per-day cap.
        _record_breaks(pair, prev_high, prev_low)
        # Promote to 1m only when within proximity and not fully halted.
        if not state.within_proximity(high, low, prev_high, prev_low):
            continue
        if state.is_halted(pair, "HIGH") and state.is_halted(pair, "LOW"):
            continue
        hot.add(pair)

    _hot_pairs = hot
    state.save_hot_symbols(SOURCE, hot)
    print(f"[coindcx] M5 scan: {len(pairs)} instruments via 1 bulk call "
          f"(+{level_calls} level seeds, +{fallback_calls} candle "
          f"fallbacks), {len(hot)} promoted to 1m")


def _scan_1m():
    """1m pass: only the hot set, using the latest closed 1m candle."""
    for pair in _hot_pairs:
        level = state.get_cached_level(pair, SOURCE)
        if not level:
            continue
        prev_high, prev_low = level
        candle = latest_minute(pair)
        if not candle:
            continue
        ts = _candle_time_ist(candle)
        high = float(candle["high"])
        low = float(candle["low"])
        close = float(candle["close"])
        state.update_minute(
            SOURCE, pair, ts, float(candle["open"]), high, low, close,
        )
        time.sleep(_HOT_CALL_SPACING_SEC)


def run_cycle(is_m5_boundary: bool = True):
    """One cycle. On an M5 boundary do the single-bulk-call scan and refresh
    the hot set; otherwise only poll the hot set at 1-minute resolution."""
    if is_m5_boundary:
        pairs = list_futures_instruments()
        if not pairs:
            # Network blip on the instrument list: still poll the existing
            # hot set so 1m snapshots keep updating.
            _scan_1m()
            return
        print(f"[coindcx] monitoring {len(pairs)} futures instruments")
        _scan_m5(pairs)
    # Always poll the hot set at 1m, including on the M5 boundary, so every
    # hot symbol's latest 1m candle is refreshed every minute.
    _scan_1m()
