import os

# ── Telegram API ───────────────────────────────────────────────────────────────
API_ID        = int(os.environ.get("API_ID", 0))
API_HASH      = os.environ.get("API_HASH", "")
BOT_TOKEN     = os.environ.get("BOT_TOKEN", "")

# ── Auth mode ─────────────────────────────────────────────────────────────────
# True  → every user logs in with their own Telegram account (recommended)
# False → single shared STRING_SESSION you provide
LOGIN_SYSTEM  = os.environ.get("LOGIN_SYSTEM", "True").lower() != "false"
STRING_SESSION = os.environ.get("STRING_SESSION", None)

# ── MongoDB ───────────────────────────────────────────────────────────────────
DB_URI        = os.environ.get("DB_URI", "")
DB_NAME       = os.environ.get("DB_NAME", "airsaverpro")

# ── Bot settings ──────────────────────────────────────────────────────────────
ADMINS        = [int(x) for x in os.environ.get("ADMINS", "0").split(",") if x.strip().isdigit()]
WAITING_TIME  = int(os.environ.get("WAITING_TIME", "3"))   # seconds between posts
MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT", "3")) # simultaneous downloads per user task
ERROR_MESSAGE = os.environ.get("ERROR_MESSAGE", "True").lower() != "false"
