"""All configuration in one place. Fill in every CHANGE_ME value."""

# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = ""          # token from @BotFather
TELEGRAM_CHAT_ID = ""            # private channel chat id, e.g. -1001234567890

# ---------------------------------------------------------------------------
# CoinDCX (crypto futures). Public market data needs no key, but keys are kept
# here in case you extend to private endpoints.
# ---------------------------------------------------------------------------
COINDCX_API_KEY = ""
COINDCX_API_SECRET = ""

# ---------------------------------------------------------------------------
# MT5 (XM)
# ---------------------------------------------------------------------------
MT5_LOGIN =      # CHANGE_ME: your XM account number (int)
MT5_PASSWORD = ""               # XM password
MT5_SERVER = ""                 # e.g. "XMGlobal-MT5 7"
MT5_TERMINAL_PATH = "C:\\Program Files\\XM Global MT5\\terminal64.exe"                    # optional: full path to terminal64.exe

# Substrings used to EXCLUDE crypto symbols from the MT5 side.
MT5_ALLOWED_GROUPS = [
    "Derivatives",
    "Forex",
]

# ---------------------------------------------------------------------------
# Alert rules
# ---------------------------------------------------------------------------
MAX_ALERTS_PER_SIDE_PER_DAY = 3          # 3 for HIGH + 3 for LOW = 6 max per symbol/day
IST_OFFSET_HOURS = 5.5
# XM broker server time offset. XM is GMT+3 (GMT+2 in winter; adjust if needed).
MT5_SERVER_OFFSET_HOURS = 3.0

# ---------------------------------------------------------------------------
# Time zones (hours offset from UTC)
# ---------------------------------------------------------------------------
# All CSV timestamps are written in IST (GMT+5:30).
IST_OFFSET_HOURS = 5.5
# XM broker server time offset. XM is GMT+3 (GMT+2 in winter; adjust if needed).
MT5_SERVER_OFFSET_HOURS = 3.0

# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------
DATA_DIR = "data"
