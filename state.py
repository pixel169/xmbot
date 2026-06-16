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

The <source>_minute.csv files hold ONE row per symbol (a live snapshot): the
latest candle is written in place, not appended, so the file never grows with
duplicate rows.

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
_HOT = "hot_symbols.csv"
_BREAKS = "break_counts.csv"

# Broker (XM server) date string, set by the MT5 monitor each cycle.
_broker_date = None
# Broker UTC offset in hours, measured live from a tick (None until known).
_broker_offset_hours = None

# In-memory caches so per-day work happens once per day, not every minute.
#   _levels_cache[(symbol, source)] = (prev_high, prev_low)
#   _levels_loaded_day[source]      = day string the levels were loaded for
#   _minute_rows[source]            = {symbol: [ts, symbol, o, h, l, c]}
_levels_cache = {}
_levels_loaded_day = {}
_minute_rows = {}
# Day string the in-memory caches currently belong to. When today_str()
# advances, the caches are dropped so prev-day levels are re-seeded fresh.
_cache_day = None

_IST = timezone(timedelta(hours=config.IST_OFFSET_HOURS))


def _maybe_roll_day() -> None:
    """Drop in-memory caches when the broker day advances.

    Prev-day levels are only valid for the day they were computed for.
    Without this, after midnight the monitors keep comparing today's price
    against yesterday's high/low, corrupting proximity, break counts and
    halting.
    """
    global _cache_day
    today = today_str()
    if _cache_day != today:
        _cache_day = today
        _levels_cache.clear()
        _levels_loaded_day.clear()
        _minute_rows.clear()


def to_ist(dt: datetime) -> datetime:
    """Convert a timezone-aware datetime to IST (GMT+5:30)."""
    return dt.astimezone(_IST)


def ist_str(dt: datetime) -> str:
    """Format a timezone-aware datetime as an IST timestamp string."""
    return to_ist(dt).strftime("%Y-%m-%d %H:%M:%S")


def set_broker_time(broker_dt: datetime, true_utc: datetime = None) -> None:
    """Record the current broker time so the daily reset follows XM server time.

    If `true_utc` (the real UTC instant the tick was observed) is provided,
    the broker's UTC offset is measured directly from the difference, so the
    bar->IST conversion no longer relies on a hardcoded seasonal constant.
    """
    global _broker_date, _broker_offset_hours
    _broker_date = broker_dt.strftime("%Y-%m-%d")
    if true_utc is not None:
        diff = (broker_dt - true_utc).total_seconds() / 3600.0
        # round to the nearest quarter hour to absorb sub-second jitter
        _broker_offset_hours = round(diff * 4) / 4.0


def broker_offset_hours() -> float:
    """Broker UTC offset in hours: measured live if known, else the config."""
    if _broker_offset_hours is not None:
        return _broker_offset_hours
    return float(config.MT5_SERVER_OFFSET_HOURS)


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
    """Return cached (prev_high, prev_low) for a symbol, or None.

    Rolls the caches first so a new broker day forces a re-seed of prev-day
    levels instead of returning stale ones.
    """
    _maybe_roll_day()
    return _levels_cache.get((symbol, source))


def read_all_levels(source: str) -> dict:
    """Return {symbol: (prev_high, prev_low)} for one source from the CSV."""
    out = {}
    for (symbol, src), row in _read_levels().items():
        if src != source:
            continue
        try:
            out[symbol] = (float(row["prev_high"]), float(row["prev_low"]))
        except (TypeError, ValueError, KeyError):
            continue
    return out


def save_prev_level(symbol: str, prev_high: float, prev_low: float,
                    source: str) -> None:
    """Upsert today's prev-day high/low for a symbol (also caches in memory)."""
    _maybe_roll_day()
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
# Proximity (0.3% band) helpers
# ---------------------------------------------------------------------------
def within_proximity(high: float, low: float, prev_high: float,
                     prev_low: float, pct: float = None) -> bool:
    """True if the candle high/low sits within `pct` of a prev-day level.

    The band is measured as a fraction of the prev-day level itself, so a
    0.3% setting means |price - level| <= 0.003 * level for either the high
    side (vs prev_high) or the low side (vs prev_low).
    """
    if pct is None:
        pct = config.PROXIMITY_PCT
    near_high = prev_high and abs(high - prev_high) <= abs(prev_high) * pct
    near_low = prev_low and abs(low - prev_low) <= abs(prev_low) * pct
    return bool(near_high or near_low)


# ---------------------------------------------------------------------------
# Minute OHLC snapshot (update-in-place, one row per symbol)
# ---------------------------------------------------------------------------
def _minute_path(source: str) -> str:
    return _path(f"{source}_minute.csv")


def _load_minute_rows(source: str) -> dict:
    """Load the snapshot rows for a source into the in-memory map (once)."""
    if source in _minute_rows:
        return _minute_rows[source]
    rows = {}
    path = _minute_path(source)
    if os.path.exists(path) and os.path.getsize(path) > 0:
        with open(path, newline="") as f:
            for r in csv.DictReader(f):
                sym = r.get("symbol")
                if sym:
                    rows[sym] = [r.get("timestamp"), sym, r.get("open"),
                                 r.get("high"), r.get("low"), r.get("close")]
    _minute_rows[source] = rows
    return rows


def _write_minute_rows(source: str) -> None:
    path = _minute_path(source)
    rows = _minute_rows.get(source, {})
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["timestamp", "symbol", "open", "high", "low", "close"])
        for row in rows.values():
            w.writerow(row)


def update_minute(source: str, symbol: str, ts: str, o: float, h: float,
                  l: float, c: float) -> bool:
    """Update (not append) one symbol's latest minute candle.

    The minute CSV keeps a single row per symbol; the row is rewritten in
    place. `ts` must be the candle's own time (IST string). Returns True if
    the row changed (new candle), False if the same candle was already stored.
    """
    rows = _load_minute_rows(source)
    existing = rows.get(symbol)
    if existing and existing[0] == ts:
        return False  # same candle already snapshotted
    rows[symbol] = [ts, symbol, o, h, l, c]
    _write_minute_rows(source)
    return True


# Backwards-compatible alias: callers that still say append_minute now upsert.
append_minute = update_minute


# ---------------------------------------------------------------------------
# Hot (promoted to 1m) symbols snapshot
# ---------------------------------------------------------------------------
def save_hot_symbols(source: str, symbols) -> None:
    """Persist the set of symbols promoted to 1m monitoring for `source`.

    Rewrites this source's rows in data/hot_symbols.csv so the file always
    shows the current promoted set across all sources. Symbols not listed
    here are the ones filtered out by the 0.3%% proximity check (stay on 5m).
    """
    path = _path(_HOT)
    rows = []
    if os.path.exists(path) and os.path.getsize(path) > 0:
        with open(path, newline="") as f:
            for r in csv.DictReader(f):
                if r.get("source") and r.get("source") != source:
                    rows.append([r.get("source"), r.get("symbol"),
                                 r.get("tier", "1m")])
    for sym in sorted(symbols):
        rows.append([source, sym, "1m"])
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["source", "symbol", "tier"])
        for row in rows:
            w.writerow(row)


# ---------------------------------------------------------------------------
# Daily break counts (how many times a side broke prev-day level today)
# ---------------------------------------------------------------------------
def _read_breaks() -> dict:
    """Return {(source, symbol, side): count} for today's break counts."""
    path = _path(_BREAKS)
    out = {}
    if not os.path.exists(path):
        return out
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            if r.get("date") != today_str():
                continue
            source = r.get("source")
            symbol = r.get("symbol")
            side = r.get("side")
            count = r.get("count")
            if not source or not symbol or not side or count is None:
                continue
            try:
                out[(source, symbol, side)] = int(count)
            except (TypeError, ValueError):
                continue
    return out


def _write_breaks(breaks: dict) -> None:
    path = _path(_BREAKS)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "source", "symbol", "side", "count"])
        for (source, symbol, side), count in breaks.items():
            w.writerow([today_str(), source, symbol, side, count])


def set_break_count(source: str, symbol: str, side: str, count: int) -> None:
    """Store today's break count for a source/symbol/side (upsert).

    Halts the side automatically once the count exceeds the per-day cap.
    """
    breaks = _read_breaks()
    breaks[(source, symbol, side)] = int(count)
    _write_breaks(breaks)
    if count > config.MAX_ALERTS_PER_SIDE_PER_DAY:
        _halt(symbol, side)


def get_break_count(source: str, symbol: str, side: str) -> int:
    """Return today's stored break count for a source/symbol/side (0 if none)."""
    return _read_breaks().get((source, symbol, side), 0)


def read_all_breaks() -> dict:
    """Public accessor: {(source, symbol, side): count} for today."""
    return _read_breaks()


# ---------------------------------------------------------------------------
# Dashboard read helpers
# ---------------------------------------------------------------------------
def read_hot_symbols() -> list:
    """Return [(source, symbol, tier)] from hot_symbols.csv."""
    path = _path(_HOT)
    out = []
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return out
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            source = r.get("source")
            symbol = r.get("symbol")
            if source and symbol:
                out.append((source, symbol, r.get("tier", "1m")))
    return out


def read_minute_rows(source: str) -> dict:
    """Return {symbol: {timestamp, open, high, low, close}} for a source."""
    out = {}
    path = _minute_path(source)
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return out
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            sym = r.get("symbol")
            if sym:
                out[sym] = r
    return out


def read_halted() -> list:
    """Public accessor: today's halted (symbol, side) rows."""
    return _read_halted()


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


def _read_halted() -> list:
    """Return today's halted (symbol, side) rows from the CSV."""
    path = _path(_HALTED)
    out = []
    if not os.path.exists(path):
        return out
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            if (r.get("date") == today_str()
                    and r.get("symbol") and r.get("side")):
                out.append((r["symbol"], r["side"]))
    return out


def _write_halted(pairs) -> None:
    """Rewrite halted_pairs.csv with only the current broker day's rows.

    Rewriting (instead of appending) prunes stale rows from previous days so
    the file always reflects the latest halt state for today.
    """
    path = _path(_HALTED)
    seen = set()
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "symbol", "side"])
        for symbol, side in pairs:
            if (symbol, side) in seen:
                continue
            seen.add((symbol, side))
            w.writerow([today_str(), symbol, side])


def _halt(symbol: str, side: str) -> None:
    if is_halted(symbol, side):
        return
    pairs = _read_halted()
    pairs.append((symbol, side))
    _write_halted(pairs)


def is_halted(symbol: str, side: str) -> bool:
    return (symbol, side) in set(_read_halted())


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
