"""Shared state: CSV persistence, alert counting, halt logic, broker clock.

The "day" boundary follows MT5 broker (XM server) time. The MT5 monitor calls
`set_broker_time(...)` each cycle with the broker timestamp; everything else
uses `today_str()` which is derived from that broker time. If the broker time
has not been set yet (e.g. MT5 not connected), it falls back to UTC.

CSV files (all under DATA_DIR):
  prev_day_levels.csv   symbol, date, prev_high, prev_low, source
  alert_counts.csv      date, symbol, side, count
  halted_pairs.csv      date, symbol, side
  <source>_minute.csv   timestamp, symbol, open, high, low, close

Alert rule: independent caps per side ('HIGH' / 'LOW'). When a side's count
exceeds the cap, the symbol|side is halted for the rest of that broker day.
"""

import csv
import os
from datetime import datetime, timedelta, timezone

import config

_LEVELS = "prev_day_levels.csv"
_COUNTS = "alert_counts.csv"
_HALTED = "halted_pairs.csv"

# Broker (XM server) date string, set by the MT5 monitor each cycle.
_broker_date = None

# In-memory caches so per-day work happens once per day, not every minute.
#   _levels_cache[(symbol, source)] = (prev_high, prev_low)
#   _levels_loaded_day[source]      = day string the levels were loaded for
#   _last_candle_written[(source, symbol)] = last candle-time string written
_levels_cache = {}
_levels_loaded_day = {}
_last_candle_written = {}

_IST = timezone(timedelta(hours=config.IST_OFFSET_HOURS))


def to_ist(dt: datetime) -> datetime:
    """Convert a timezone-aware datetime to IST (GMT+5:30)."""
    return dt.astimezone(_IST)


def ist_str(dt: datetime) -> str:
    """Format a timezone-aware datetime as an IST timestamp string."""
    return to_ist(dt).strftime("%Y-%m-%d %H:%M:%S")


def set_broker_time(broker_dt: datetime) -> None:
    """Record the current broker time so the daily reset follows XM server time."""
    global _broker_date
    _broker_date = broker_dt.strftime("%Y-%m-%d")


def today_str() -> str:
    """Current 'day' for reset purposes: broker date if known, else UTC."""
    if _broker_date is not None:
        return _broker_date
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _path(name: str) -> str:
    os.makedirs(config.DATA_DIR, exist_ok=True)
    return os.path.join(config.DATA_DIR, name)


# ---------------------------------------------------------------------------
# Previous-day levels
# ---------------------------------------------------------------------------
def levels_loaded_for(source: str) -> bool:
    """True if prev-day levels were already loaded for `source` today."""
    return _levels_loaded_day.get(source) == today_str()


def mark_levels_loaded(source: str) -> None:
    """Record that prev-day levels for `source` are loaded for today."""
    _levels_loaded_day[source] = today_str()


def get_cached_level(symbol: str, source: str):
    """Return cached (prev_high, prev_low) for a symbol, or None."""
    return _levels_cache.get((symbol, source))


def save_prev_level(symbol: str, prev_high: float, prev_low: float,
                    source: str) -> None:
    """Upsert today's prev-day high/low for a symbol (also caches in memory)."""
    _levels_cache[(symbol, source)] = (prev_high, prev_low)
    rows = _read_levels()
    rows[(symbol, source)] = {
        "symbol": symbol,
        "date": today_str(),
        "prev_high": prev_high,
        "prev_low": prev_low,
        "source": source,
    }
    _write_levels(rows)


def _read_levels() -> dict:
    path = _path(_LEVELS)
    out = {}
    if not os.path.exists(path):
        return out
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            out[(r["symbol"], r["source"])] = r
    return out


def _write_levels(rows: dict) -> None:
    path = _path(_LEVELS)
    fields = ["symbol", "date", "prev_high", "prev_low", "source"]
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for row in rows.values():
            w.writerow(row)


# ---------------------------------------------------------------------------
# Minute OHLC append
# ---------------------------------------------------------------------------
def append_minute(source: str, symbol: str, ts: str, o: float, h: float,
                  l: float, c: float) -> bool:
    """Append one minute candle, de-duplicated by (source, symbol, ts).

    `ts` must be the candle's own time (IST string), not wall-clock now.
    Returns True if a row was written, False if it was a duplicate.
    """
    key = (source, symbol)
    if _last_candle_written.get(key) == ts:
        return False  # already wrote this exact candle
    _last_candle_written[key] = ts

    path = _path(f"{source}_minute.csv")
    new_file = (not os.path.exists(path)) or os.path.getsize(path) == 0
    with open(path, "a", newline="") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(["timestamp", "symbol", "open", "high", "low", "close"])
        w.writerow([ts, symbol, o, h, l, c])
    return True


# ---------------------------------------------------------------------------
# Alert counts + halt logic
# ---------------------------------------------------------------------------
def _read_counts() -> dict:
    path = _path(_COUNTS)
    out = {}
    if not os.path.exists(path):
        return out
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            # tolerate malformed / headerless / blank rows
            if r.get("date") != today_str():
                continue
            symbol = r.get("symbol")
            side = r.get("side")
            count = r.get("count")
            if not symbol or not side or count is None:
                continue
            try:
                out[(symbol, side)] = int(count)
            except (TypeError, ValueError):
                continue
    return out


def _write_counts(counts: dict) -> None:
    path = _path(_COUNTS)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "symbol", "side", "count"])
        for (symbol, side), count in counts.items():
            w.writerow([today_str(), symbol, side, count])


def _halt(symbol: str, side: str) -> None:
    if is_halted(symbol, side):
        return
    path = _path(_HALTED)
    # write header if the file is missing or empty (guards headerless files)
    need_header = (not os.path.exists(path)) or os.path.getsize(path) == 0
    with open(path, "a", newline="") as f:
        w = csv.writer(f)
        if need_header:
            w.writerow(["date", "symbol", "side"])
        w.writerow([today_str(), symbol, side])


def is_halted(symbol: str, side: str) -> bool:
    path = _path(_HALTED)
    if not os.path.exists(path):
        return False
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            # tolerate malformed / headerless / blank rows
            if (r.get("date") == today_str()
                    and r.get("symbol") == symbol
                    and r.get("side") == side):
                return True
    return False


def register_cross(symbol: str, side: str):
    """Record one cross for symbol/side.

    Returns (should_alert: bool, new_count: int, cap: int).
    Halts the side once the cap is exceeded.
    """
    cap = config.MAX_ALERTS_PER_SIDE_PER_DAY
    if is_halted(symbol, side):
        return False, cap + 1, cap

    counts = _read_counts()
    key = (symbol, side)
    new_count = counts.get(key, 0) + 1
    counts[key] = new_count
    _write_counts(counts)

    if new_count > cap:
        _halt(symbol, side)
        return False, new_count, cap
    return True, new_count, cap
