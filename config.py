import os

# ── Telegram API ───────────────────────────────────────────────────────────────
API_ID        = int(os.environ.get("API_ID", 0))
API_HASH      = os.environ.get("API_HASH", "")
BOT_TOKEN     = os.environ.get("BOT_TOKEN", "")

# ── Auth mode ─────────────────────────────────────────────────────────────────
LOGIN_SYSTEM   = os.environ.get("LOGIN_SYSTEM", "True").lower() != "false"
STRING_SESSION = os.environ.get("STRING_SESSION", None)

# ── MongoDB ───────────────────────────────────────────────────────────────────
DB_URI  = os.environ.get("DB_URI", "")
DB_NAME = os.environ.get("DB_NAME", "airsaverpro")

# ── Bot settings ──────────────────────────────────────────────────────────────
ADMINS            = [int(x) for x in os.environ.get("ADMINS", "0").split(",") if x.strip().isdigit()]
WAITING_TIME      = int(os.environ.get("WAITING_TIME", "1"))    # Phase 1: 3→1
MAX_CONCURRENT    = int(os.environ.get("MAX_CONCURRENT", "6"))  # Phase 1: 4→6
MAX_TRANSMISSIONS = int(os.environ.get("MAX_TRANSMISSIONS", "16"))  # Phase 1: 8→16
ERROR_MESSAGE     = os.environ.get("ERROR_MESSAGE", "True").lower() != "false"

# ── Phase 2 & 3 ───────────────────────────────────────────────────────────────
PARALLEL_FILES    = int(os.environ.get("PARALLEL_FILES", "2"))      # Phase 2: 2 files at once
INMEM_THRESHOLD   = int(os.environ.get("INMEM_THRESHOLD", "209715200"))  # 200 MB
PIPELINE_DEPTH    = int(os.environ.get("PIPELINE_DEPTH", "2"))      # Phase 3: lookahead size
