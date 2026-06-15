"""Per-minute scheduler running both monitors.

Runs forever. At the top of every minute it executes one MT5 cycle (which also
refreshes the broker-time daily clock) and one CoinDCX cycle. Errors in one
source never stop the other.

MT5 runs first so the broker-time daily reset is up to date before CoinDCX
evaluates its alert caps.
"""

import time
import traceback
from datetime import datetime, timezone

import coindcx_monitor
import mt5_monitor


def _run_safe(name, fn):
    try:
        fn()
    except Exception:  # noqa: BLE001 - keep the loop alive no matter what
        print(f"[{name}] cycle error:\n{traceback.format_exc()}")


def _sleep_to_next_minute():
    now = time.time()
    time.sleep(60 - (now % 60))


def main():
    print("Market breakout alerter started. Ctrl+C to stop.")
    while True:
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        print(f"\n=== cycle {stamp} ===")
        _run_safe("mt5", mt5_monitor.run_cycle)
        _run_safe("coindcx", coindcx_monitor.run_cycle)
        _sleep_to_next_minute()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
