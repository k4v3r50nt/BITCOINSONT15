import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")
PAPER_MODE         = os.getenv("PAPER_MODE", "true").lower() == "true"

# INITIAL_BANKROLL: the "true" starting capital for ROI display purposes.
# CURRENT_BANKROLL env var (optional): override bankroll on startup — set this
#   manually in Railway Variables after important sessions to survive redeploys.
#   With RAILWAY_TOKEN + RAILWAY_SERVICE_ID configured, this is updated
#   automatically after every trade resolve.
INITIAL_BANKROLL   = float(os.getenv("INITIAL_BANKROLL", "100.0"))

MIN_CONFIDENCE     = float(os.getenv("MIN_CONFIDENCE", "0.0"))
MAX_POSITION_PCT   = float(os.getenv("MAX_POSITION_PCT", "0.05"))

GAMMA_API_BASE  = "https://gamma-api.polymarket.com"
WINDOW_SECONDS  = 900  # 15 minutes

# ── Railway API — bankroll persistence across redeploys ───────────────────────
# Set these in Railway Variables to enable automatic CURRENT_BANKROLL updates.
#
#   RAILWAY_TOKEN          — API token from railway.app/account/tokens
#   RAILWAY_SERVICE_ID     — found in service Settings → General
#   RAILWAY_ENVIRONMENT_ID — found in environment URL or service Settings
#
# If left empty the bot falls back to state.json only (survives restarts,
# but resets on redeploy unless CURRENT_BANKROLL is set manually).
RAILWAY_TOKEN          = os.getenv("RAILWAY_TOKEN", "")
RAILWAY_SERVICE_ID     = os.getenv("RAILWAY_SERVICE_ID", "")
RAILWAY_ENVIRONMENT_ID = os.getenv("RAILWAY_ENVIRONMENT_ID", "")
