"""Local Flask dashboard for the market-breakout monitor.

Reads the CSV snapshots written by the monitors (no DB) and renders a single
auto-refreshing page that lists the current hot symbols for each source with:
  * source (mt5 / coindcx)
  * today's HIGH and LOW break counts (from break_counts.csv)
  * prev-day high/low levels
  * the latest price snapshot (from <source>_minute.csv)
  * halt status (from halted_pairs.csv)
  * a clickable link to trade the symbol on the matching broker

Run it alongside main.py:
    python dashboard.py
Then open http://127.0.0.1:5000/
"""

import re

from flask import Flask, render_template_string

import config
import state

app = Flask(__name__)

# Seconds between automatic browser refreshes.
_REFRESH_SEC = 30


def _xm_link(symbol: str) -> str:
    """Build the XM symbol-info URL, stripping any broker suffix.

    e.g. 'EURUSD.' / 'XAUUSD#' -> 'EURUSD' / 'XAUUSD'.
    """
    clean = re.sub(r"[^A-Za-z0-9]", "", symbol.upper())
    return f"https://my.xm.com/symbol-info/{clean}"


def _coindcx_link(pair: str) -> str:
    """Build the CoinDCX futures URL (pair string is used as-is)."""
    return f"https://coindcx.com/futures/{pair}"


def _trade_link(source: str, symbol: str) -> str:
    if source == "mt5":
        return _xm_link(symbol)
    if source == "coindcx":
        return _coindcx_link(symbol)
    return "#"


def _to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fmt(value) -> str:
    """Format a price/level without float noise, keeping small decimals."""
    f = _to_float(value)
    if f is None:
        return ""
    # Use up to 8 significant digits, then trim trailing zeros.
    s = f"{f:.8g}"
    return s


def _nearest_pct(high, low, prev_high, prev_low):
    """Smallest distance to a prev-day level, as a percentage (or None)."""
    dists = []
    if high is not None and prev_high:
        dists.append(abs(high - prev_high) / abs(prev_high) * 100.0)
    if low is not None and prev_low:
        dists.append(abs(low - prev_low) / abs(prev_low) * 100.0)
    return min(dists) if dists else None


def _rows():
    """Assemble dashboard rows from the CSV snapshots."""
    breaks = state.read_all_breaks()
    halted = set(state.read_halted())
    minute_cache = {}
    level_cache = {}
    rows = []
    for source, symbol, tier in state.read_hot_symbols():
        if source not in minute_cache:
            minute_cache[source] = state.read_minute_rows(source)
        snap = minute_cache[source].get(symbol, {})
        if source not in level_cache:
            level_cache[source] = state.read_all_levels(source)
        level = level_cache[source].get(symbol)
        prev_high, prev_low = (level if level else (None, None))
        s_high = _to_float(snap.get("high"))
        s_low = _to_float(snap.get("low"))
        high_breaks = breaks.get((source, symbol, "HIGH"), 0)
        low_breaks = breaks.get((source, symbol, "LOW"), 0)
        rows.append({
            "source": source,
            "symbol": symbol,
            "tier": tier,
            "link": _trade_link(source, symbol),
            "timestamp": snap.get("timestamp", ""),
            "close": _fmt(snap.get("close")),
            "high": _fmt(snap.get("high")),
            "low": _fmt(snap.get("low")),
            "prev_high": _fmt(prev_high),
            "prev_low": _fmt(prev_low),
            "near_pct": _nearest_pct(s_high, s_low, prev_high, prev_low),
            "high_breaks": high_breaks,
            "low_breaks": low_breaks,
            "high_halted": (symbol, "HIGH") in halted,
            "low_halted": (symbol, "LOW") in halted,
        })
    rows.sort(key=lambda r: (r["source"],
                             -(r["high_breaks"] + r["low_breaks"]),
                             r["symbol"]))
    return rows


_TEMPLATE = """\
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta http-equiv="refresh" content="{{ refresh }}">
  <title>Market Breakout - Hot Symbols</title>
  <style>
    body { font-family: -apple-system, Segoe UI, Roboto, sans-serif;
           margin: 24px; background: #0f1419; color: #e6e6e6; }
    h1 { font-size: 20px; margin: 0 0 4px; }
    .meta { color: #8b98a5; font-size: 13px; margin-bottom: 16px; }
    table { border-collapse: collapse; width: 100%; font-size: 13px; }
    th, td { padding: 8px 10px; border-bottom: 1px solid #243340;
             text-align: right; white-space: nowrap; }
    th { text-align: right; color: #8b98a5; font-weight: 600;
         position: sticky; top: 0; background: #15202b; }
    th.l, td.l { text-align: left; }
    a { color: #1d9bf0; text-decoration: none; }
    a:hover { text-decoration: underline; }
    .src { text-transform: uppercase; font-size: 11px; color: #8b98a5; }
    .pill { display: inline-block; padding: 1px 7px; border-radius: 10px;
            font-size: 11px; }
    .hi { background: #14361f; color: #4ade80; }
    .lo { background: #3a1620; color: #f87171; }
    .halt { background: #4a1d1d; color: #fca5a5; }
    .ok { color: #4ade80; }
  </style>
</head>
<body>
  <h1>Hot symbols &mdash; within {{ pct }}% of prev-day high/low</h1>
  <div class="meta">
    {{ rows|length }} symbol(s) &middot; cap {{ cap }} breaks/side/day
    &middot; auto-refresh {{ refresh }}s
  </div>
  <table>
    <thead>
      <tr>
        <th class="l">Source</th>
        <th class="l">Symbol</th>
        <th>Last</th>
        <th>M-High</th>
        <th>M-Low</th>
        <th>Prev High</th>
        <th>Prev Low</th>
        <th>Near %</th>
        <th>High breaks</th>
        <th>Low breaks</th>
        <th class="l">Status</th>
        <th class="l">Updated (IST)</th>
      </tr>
    </thead>
    <tbody>
    {% for r in rows %}
      <tr>
        <td class="l src">{{ r.source }}</td>
        <td class="l"><a href="{{ r.link }}" target="_blank"
            rel="noopener">{{ r.symbol }}</a></td>
        <td>{{ r.close }}</td>
        <td>{{ r.high }}</td>
        <td>{{ r.low }}</td>
        <td>{{ r.prev_high }}</td>
        <td>{{ r.prev_low }}</td>
        <td>{% if r.near_pct is not none %}{{ "%.3f"|format(r.near_pct) }}%
            {% else %}-{% endif %}</td>
        <td>
          <span class="pill hi">{{ r.high_breaks }}</span>
          {% if r.high_halted %}<span class="pill halt">halted</span>{% endif %}
        </td>
        <td>
          <span class="pill lo">{{ r.low_breaks }}</span>
          {% if r.low_halted %}<span class="pill halt">halted</span>{% endif %}
        </td>
        <td class="l">
          {% if r.high_halted and r.low_halted %}
            <span class="pill halt">both halted</span>
          {% elif r.high_halted %}
            <span class="pill halt">HIGH halted</span>
            <span class="ok">+ 1m LOW</span>
          {% elif r.low_halted %}
            <span class="pill halt">LOW halted</span>
            <span class="ok">+ 1m HIGH</span>
          {% else %}
            <span class="ok">monitoring 1m</span>
          {% endif %}
        </td>
        <td class="l">{{ r.timestamp }}</td>
      </tr>
    {% endfor %}
    {% if not rows %}
      <tr><td class="l" colspan="12">No hot symbols yet.</td></tr>
    {% endif %}
    </tbody>
  </table>
</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(
        _TEMPLATE,
        rows=_rows(),
        refresh=_REFRESH_SEC,
        pct=config.PROXIMITY_PCT * 100,
        cap=config.MAX_ALERTS_PER_SIDE_PER_DAY,
    )


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
