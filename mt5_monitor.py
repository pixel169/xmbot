"""MT5 (XM) monitor.

Requires the MetaTrader 5 terminal installed and logged into your XM account.
The MetaTrader5 Python package only runs on Windows (or Wine).

Symbols are selected by their Market Watch top-level path group
(config.MT5_ALLOWED_GROUPS), e.g. only 'Forex' and 'Derivatives'. Crypto and
stocks are intentionally excluded (crypto is covered by CoinDCX).

Flow per cycle:
  1. Initialize / ensure MT5 connection.
  2. Read broker (server) time and share it with state for the daily reset.
  3. Enumerate symbols whose top-level path group is allowed.
  4. For each symbol: prev-day high/low from the last closed D1 bar (once/day).
  5. Latest closed M1 bar -> append CSV, check breach (subject to caps).
"""

from datetime import datetime, timezone

try:
    import MetaTrader5 as mt5
except ImportError:  # allows the repo to be inspected on non-Windows machines
    mt5 = None

import config
import state
from telegram_alert import send_message, format_alert

SOURCE = "mt5"
_initialized = False

# Allowed top-level path groups, upper-cased once for case-insensitive match.
_ALLOWED_GROUPS = {g.strip().upper() for g in config.MT5_ALLOWED_GROUPS}


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
    """Read the broker server time from any tick and share it with state.

    MT5 tick timestamps are in broker server time (as a unix epoch). We treat
    that epoch as broker-local so the date string reflects XM server time.
    """
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
        broker_dt = datetime.utcfromtimestamp(tick.time)
        state.set_broker_time(broker_dt)


def _top_group(symbol_info) -> str:
    """Return the top-level Market Watch group from the symbol path.

    e.g. 'Forex\\Majors\\EURUSD' -> 'FOREX', 'Stocks\\US\\AAPL' -> 'STOCKS'.
    """
    path = getattr(symbol_info, "path", "") or ""
    return path.split("\\", 1)[0].strip().upper()


def list_symbols():
    """Symbols whose top-level path group is in the allow-list."""
    symbols = mt5.symbols_get()
    if symbols is None:
        return []
    return [s.name for s in symbols if _top_group(s) in _ALLOWED_GROUPS]


def prev_day_levels(symbol: str):
    """(prev_high, prev_low) from the last closed daily bar."""
    rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_D1, 0, 2)
    if rates is None or len(rates) < 2:
        return None
    prev = rates[-2]  # last fully closed day
    return float(prev["high"]), float(prev["low"])


def _bar_time_ist(bar) -> str:
    """Convert an MT5 bar time (broker server time) to an IST timestamp string.

    MT5 bar `time` is a unix epoch expressed in broker server time, so we
    subtract the broker's UTC offset to recover the true UTC instant, then
    let state format it as IST.
    """
    broker_epoch = int(bar["time"])
    true_utc = broker_epoch - int(config.MT5_SERVER_OFFSET_HOURS * 3600)
    dt_utc = datetime.fromtimestamp(true_utc, tz=timezone.utc)
    return state.ist_str(dt_utc)


def latest_minute(symbol: str):
    """Last closed M1 bar as (time_ist, open, high, low, close) or None."""
    rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M1, 0, 2)
    if rates is None or len(rates) < 2:
        return None
    bar = rates[-2]  # index -1 is the still-forming bar; -2 is closed
    return (_bar_time_ist(bar), float(bar["open"]), float(bar["high"]),
            float(bar["low"]), float(bar["close"]))


def _check(symbol: str, ohlc, prev_high: float, prev_low: float):
    _ts, _o, high, low, close = ohlc
    if high >= prev_high:
        ok, count, cap = state.register_cross(symbol, "HIGH")
        if ok:
            send_message(format_alert(SOURCE, symbol, "HIGH", close,
                                      prev_high, count, cap))
    if low <= prev_low:
        ok, count, cap = state.register_cross(symbol, "LOW")
        if ok:
            send_message(format_alert(SOURCE, symbol, "LOW", close,
                                      prev_low, count, cap))


def run_cycle():
    if not _ensure_init():
        return
    _update_broker_time()
    symbols = list_symbols()
    print(f"[mt5] monitoring {len(symbols)} symbols "
          f"(groups: {', '.join(config.MT5_ALLOWED_GROUPS)})")

    # Prev-day high/low is a once-a-day job: only (re)load it on the first
    # cycle of a new day. Other minutes reuse the cached levels.
    load_levels = not state.levels_loaded_for(SOURCE)

    for symbol in symbols:
        mt5.symbol_select(symbol, True)
        if load_levels:
            levels = prev_day_levels(symbol)
            if not levels:
                continue
            prev_high, prev_low = levels
            state.save_prev_level(symbol, prev_high, prev_low, SOURCE)
        else:
            cached = state.get_cached_level(symbol, SOURCE)
            if not cached:
                continue
            prev_high, prev_low = cached

        ohlc = latest_minute(symbol)
        if not ohlc:
            continue
        ts, o, h, l, c = ohlc
        written = state.append_minute(SOURCE, symbol, ts, o, h, l, c)
        if written:
            _check(symbol, ohlc, prev_high, prev_low)

    if load_levels:
        state.mark_levels_loaded(SOURCE)
