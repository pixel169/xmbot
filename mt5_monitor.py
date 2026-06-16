"""MT5 (XM) monitor.

Requires the MetaTrader 5 terminal installed and logged into your XM account.
The MetaTrader5 Python package only runs on Windows (or Wine).

Symbols are selected by their Market Watch top-level path group
(config.MT5_ALLOWED_GROUPS), e.g. only 'Forex' and 'Derivatives'. Crypto and
stocks are intentionally excluded (crypto is covered by CoinDCX).

Two-tier monitoring:
  * Every M5 candle close, every allowed symbol's M5 high/low is scanned. A
    symbol whose high or low is within config.PROXIMITY_PCT of its prev-day
    high or low is promoted to the hot set and monitored every minute.
  * On the other minutes only the hot set is polled (latest M1 candle).

The minute CSV holds one snapshot row per symbol (update, not append).
"""

import re
from datetime import datetime, timezone

try:
    import MetaTrader5 as mt5
except ImportError:  # allows the repo to be inspected on non-Windows machines
    mt5 = None

import config
import state

SOURCE = "mt5"
_initialized = False

# Allowed top-level path groups, upper-cased once for case-insensitive match.
_ALLOWED_GROUPS = {g.strip().upper() for g in config.MT5_ALLOWED_GROUPS}

# Symbols currently promoted to 1-minute monitoring (recomputed each M5 scan).
_hot_symbols = set()


def _ensure_init() -> bool:
    global _initialized
    if mt5 is None:
        print("[mt5] MetaTrader5 package not installed; skipping MT5 monitor.")
        return False
    if _initialized:
        return True

    kwargs = dict(login=int(config.MT5_LOGIN),
                  password=config.MT5_PASSWORD,
                  server=config.MT5_SERVER)
    if config.MT5_TERMINAL_PATH:
        kwargs["path"] = config.MT5_TERMINAL_PATH

    if not mt5.initialize(**kwargs):
        print(f"[mt5] initialize failed: {mt5.last_error()}")
        return False
    _initialized = True
    print("[mt5] connected")
    return True


def _update_broker_time():
    """Read the broker server time from any tick and share it with state."""
    tick = None
    for probe in ("EURUSD", "XAUUSD"):
        tick = mt5.symbol_info_tick(probe)
        if tick and tick.time:
            break
    if not tick or not tick.time:
        syms = mt5.symbols_get()
        if syms:
            mt5.symbol_select(syms[0].name, True)
            tick = mt5.symbol_info_tick(syms[0].name)
    if tick and tick.time:
        # tick.time is the broker server epoch; treating it as a UTC wall
        # clock yields the broker-local datetime. The real UTC instant is
        # 'now', so their difference is the broker's UTC offset.
        broker_dt = datetime.utcfromtimestamp(tick.time)
        true_utc = datetime.now(timezone.utc).replace(tzinfo=None)
        state.set_broker_time(broker_dt, true_utc)


def _top_group(symbol_info) -> str:
    """Return the top-level Market Watch group from the symbol path."""
    path = getattr(symbol_info, "path", "") or ""
    return path.split("\\", 1)[0].strip().upper()


def _base_name(name: str) -> str:
    """Normalized symbol key: alphanumerics only, upper-cased.

    'GBPUSD', 'GBPUSD#' and 'GBPUSD.' all map to 'GBPUSD'.
    """
    return re.sub(r"[^A-Za-z0-9]", "", name).upper()


def list_symbols():
    """Allowed symbols, de-duplicated by their suffix-free base name.

    XM lists the same instrument under both a plain and a suffixed name
    (e.g. 'GBPUSD' and 'GBPUSD#'). Keep only one per base, preferring the
    plain name so the hot list and dashboard never show duplicates.
    """
    symbols = mt5.symbols_get()
    if symbols is None:
        return []
    chosen = {}
    for s in symbols:
        if _top_group(s) not in _ALLOWED_GROUPS:
            continue
        base = _base_name(s.name)
        current = chosen.get(base)
        # Prefer the shorter (plain, suffix-free) name when a clash occurs.
        if current is None or len(s.name) < len(current):
            chosen[base] = s.name
    return list(chosen.values())


def prev_day_levels(symbol: str):
    """(prev_high, prev_low) from the last closed daily bar."""
    rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_D1, 0, 2)
    if rates is None or len(rates) < 2:
        return None
    prev = rates[-2]  # last fully closed day
    return float(prev["high"]), float(prev["low"])


def _bar_time_ist(bar) -> str:
    """Convert an MT5 bar time (broker server time) to an IST timestamp string."""
    broker_epoch = int(bar["time"])
    offset = state.broker_offset_hours()
    true_utc = broker_epoch - int(offset * 3600)
    dt_utc = datetime.fromtimestamp(true_utc, tz=timezone.utc)
    return state.ist_str(dt_utc)


def _latest_bar(symbol: str, timeframe):
    """Last closed bar of `timeframe` as (time_ist, o, h, l, c) or None."""
    rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, 2)
    if rates is None or len(rates) < 2:
        return None
    bar = rates[-2]  # index -1 is the still-forming bar; -2 is closed
    return (_bar_time_ist(bar), float(bar["open"]), float(bar["high"]),
            float(bar["low"]), float(bar["close"]))


def latest_minute(symbol: str):
    """Last closed M1 bar as (time_ist, open, high, low, close) or None."""
    return _latest_bar(symbol, mt5.TIMEFRAME_M1)


def latest_m5(symbol: str):
    """Last closed M5 bar as (time_ist, open, high, low, close) or None."""
    return _latest_bar(symbol, mt5.TIMEFRAME_M5)


def _today_start_broker_epoch() -> int:
    """Broker-server epoch for 00:00 of the current broker day."""
    day = state.today_str()
    dt = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    # Bars carry broker-server time, so anchor the day in the same frame.
    return int(dt.timestamp())


def count_breaks_h1(symbol: str, prev_high: float, prev_low: float):
    """Count today's prev-day-level breaks from closed H1 bars.

    Returns (high_breaks, low_breaks).
    """
    rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_H1, 0, 26)
    if rates is None or len(rates) == 0:
        return 0, 0
    start = _today_start_broker_epoch()
    high_breaks = low_breaks = 0
    # Skip the still-forming last bar (index -1).
    for bar in rates[:-1]:
        try:
            if int(bar["time"]) < start:
                continue
            if prev_high and float(bar["high"]) >= prev_high:
                high_breaks += 1
            if prev_low and float(bar["low"]) <= prev_low:
                low_breaks += 1
        except (KeyError, TypeError, ValueError):
            continue
    return high_breaks, low_breaks


def _level_for(symbol: str, load_levels: bool):
    """Return (prev_high, prev_low) for a symbol, loading lazily if needed."""
    cached = state.get_cached_level(symbol, SOURCE)
    if cached:
        return cached
    # Lazy load: either it's the daily reload, or this symbol was never loaded
    # (e.g. it joined Market Watch later). This reconciles missing symbols.
    levels = prev_day_levels(symbol)
    if not levels:
        return None
    state.save_prev_level(symbol, levels[0], levels[1], SOURCE)
    return levels


def _record_breaks(symbol: str, prev_high: float, prev_low: float):
    """Count today's H1 breaks for a symbol and persist/halt as needed."""
    high_breaks, low_breaks = count_breaks_h1(symbol, prev_high, prev_low)
    state.set_break_count(SOURCE, symbol, "HIGH", high_breaks)
    state.set_break_count(SOURCE, symbol, "LOW", low_breaks)


def _scan_m5(symbols):
    """M5 pass: refresh every symbol's snapshot and recompute the hot set."""
    global _hot_symbols
    hot = set()
    for symbol in symbols:
        mt5.symbol_select(symbol, True)
        level = _level_for(symbol, load_levels=True)
        if not level:
            continue
        prev_high, prev_low = level

        ohlc = latest_m5(symbol)
        if not ohlc:
            continue
        ts, o, h, l, c = ohlc
        # Update the snapshot row from the M5 candle.
        state.update_minute(SOURCE, symbol, ts, o, h, l, c)
        # Count today's breaks from H1 bars; halts the side if it broke the
        # prev-day level more than the per-day cap.
        _record_breaks(symbol, prev_high, prev_low)
        # Promote to 1m monitoring when within the proximity band and the
        # symbol is not fully halted on both sides.
        if not state.within_proximity(h, l, prev_high, prev_low):
            continue
        if state.is_halted(symbol, "HIGH") and state.is_halted(symbol, "LOW"):
            continue
        hot.add(symbol)
    _hot_symbols = hot
    state.save_hot_symbols(SOURCE, hot)
    print(f"[mt5] M5 scan: {len(symbols)} symbols, "
          f"{len(hot)} promoted to 1m")


def _scan_1m():
    """1m pass: only the hot set, using the latest closed M1 candle."""
    for symbol in _hot_symbols:
        level = state.get_cached_level(symbol, SOURCE)
        if not level:
            continue
        prev_high, prev_low = level
        ohlc = latest_minute(symbol)
        if not ohlc:
            continue
        ts, o, h, l, c = ohlc
        state.update_minute(SOURCE, symbol, ts, o, h, l, c)


def run_cycle(is_m5_boundary: bool = True):
    """One cycle. On an M5 boundary do the full scan + hot-set refresh;
    otherwise only poll the hot set at 1-minute resolution."""
    if not _ensure_init():
        return
    _update_broker_time()

    if is_m5_boundary:
        symbols = list_symbols()
        _scan_m5(symbols)
    # Always poll the hot set at 1m, including on the M5 boundary, so every
    # hot symbol's latest M1 bar is refreshed every minute.
    _scan_1m()
