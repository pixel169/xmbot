"""All configuration in one place. Fill in every CHANGE_ME value."""

# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = "7416935286:AAH_S6MxQCLY8DvoTrQvaP-Q8TWfeQqpa30"          # token from @BotFather
TELEGRAM_CHAT_ID = "-1002286002947"            # private channel chat id, e.g. -1001234567890

# ---------------------------------------------------------------------------
# CoinDCX (crypto futures). Public market data needs no key, but keys are kept
# here in case you extend to private endpoints.
# ---------------------------------------------------------------------------
COINDCX_API_KEY = "dda98ef736f7e56fe0b26d8aee9582ecc93700d8be9e39d9"
COINDCX_API_SECRET = "7e4316550e3fc728bec9905c676e8d64310b6317bf911a510310574865f54143"

# ---------------------------------------------------------------------------
# MT5 (XM)
# ---------------------------------------------------------------------------
MT5_LOGIN = 309465011     # CHANGE_ME: your XM account number (int)
MT5_PASSWORD = "d9v/P86ln,gc"               # XM password
MT5_SERVER = "XMGlobal-MT5 6"                 # e.g. "XMGlobal-MT5 7"
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
