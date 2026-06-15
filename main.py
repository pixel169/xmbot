"""Per-minute scheduler running both monitors.

Runs forever. At the top of every minute it executes one MT5 cycle (which also
refreshes the broker-time daily clock) and one CoinDCX cycle. Errors in one
source never stop the other.

Monitoring tiers:
  * Every 5th minute (and the very first cycle) is an "M5 boundary": both
    monitors scan every symbol's 5m high/low and recompute which symbols are
    within config.PROXIMITY_PCT of their prev-day high/low. Those symbols are
    promoted to the hot set.
  * On the other minutes only the hot set is polled at 1-minute resolution.

MT5 runs first so the broker-time daily reset is up to date before CoinDCX
evaluates its alert caps.
"""

import time
import traceback
from datetime import datetime, timezone

import coindcx_monitor
import mt5_monitor
import telegram_alert


def _run_safe(name, fn, *args):
    try:
        fn(*args)
    except Exception:  # noqa: BLE001 - keep the loop alive no matter what
        print(f"[{name}] cycle error:\n{traceback.format_exc()}")


def _sleep_to_next_minute():
    now = time.time()
    time.sleep(60 - (now % 60))


def main():
    print("Market breakout alerter started. Ctrl+C to stop.")
    telegram_alert.send_startup_ping()
    first = True
    while True:
        now = datetime.now(timezone.utc)
        stamp = now.strftime("%Y-%m-%d %H:%M:%S UTC")
        # The first cycle and every 5th wall-clock minute is an M5 boundary.
        is_m5 = first or (now.minute % 5 == 0)
        first = False
        print(f"\n=== cycle {stamp} (m5={is_m5}) ===")
        _run_safe("mt5", mt5_monitor.run_cycle, is_m5)
        _run_safe("coindcx", coindcx_monitor.run_cycle, is_m5)
        _sleep_to_next_minute()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
