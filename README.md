# Market Breakout Alerts

Monitors **CoinDCX crypto futures** and **MT5 (XM) forex + metals** for breaches of the
**previous day's high / low**, and sends alerts to a private Telegram channel.

## What it does

- Every minute, pulls the last completed 1-minute candle for each symbol.
- Stores each symbol's **previous-day high and low** in `data/prev_day_levels.csv`.
- Appends the latest minute OHLC to `data/<source>_minute.csv`.
- Fires a Telegram alert when the current minute **crosses** the previous day high or low:
  - upside: `minute_high >= prev_day_high`
  - downside: `minute_low <= prev_day_low`
- The message clearly states whether **HIGH** or **LOW** was crossed.
- **Max 3 HIGH-cross alerts and 3 LOW-cross alerts per symbol per day** (6 max total).
  After the cap is hit for a side, the `symbol|side` is written to `data/halted_pairs.csv`
  and no further alerts for that side are sent until the next day.

## Daily reset

The "day" boundary follows **MT5 broker (XM server) time**, not UTC. The MT5 monitor
reads the broker clock each cycle and shares it with the rest of the app so both
sources reset their alert counts on the same broker-time midnight.

## Sources

- **CoinDCX** -> crypto futures **only** (REST polling).
- **MT5 (XM)** -> everything **except** crypto (forex pairs + metals like XAUUSD, XAGUSD).

## Setup (Windows)

```bash
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

Then edit `config.py` and fill in every value marked `CHANGE_ME`.

MT5 requires the **MetaTrader 5 terminal** installed and logged into your XM account.
The `MetaTrader5` Python package only works on Windows (or Wine).

## Run

```bash
python main.py
```

It runs forever, executing one polling cycle at the top of every minute.

## Files

| File | Purpose |
|------|---------|
| `config.py` | All credentials & settings (fill these in) |
| `telegram_alert.py` | Sends Telegram messages |
| `state.py` | CSV storage, per-symbol/side alert counting, halt logic, broker-time clock |
| `coindcx_monitor.py` | CoinDCX crypto-futures monitor |
| `mt5_monitor.py` | MT5 (XM) forex + metals monitor |
| `main.py` | Per-minute scheduler running both monitors |

Generated CSVs live in `data/` and are git-ignored.
