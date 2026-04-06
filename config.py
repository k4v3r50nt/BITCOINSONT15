import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
PAPER_MODE = os.getenv("PAPER_MODE", "true").lower() == "true"
INITIAL_BANKROLL = float(os.getenv("INITIAL_BANKROLL", "100.0"))
MIN_CONFIDENCE = float(os.getenv("MIN_CONFIDENCE", "0.60"))
MAX_POSITION_PCT = float(os.getenv("MAX_POSITION_PCT", "0.05"))

GAMMA_API_BASE = "https://gamma-api.polymarket.com"
WINDOW_SECONDS = 900  # 15 minutes
