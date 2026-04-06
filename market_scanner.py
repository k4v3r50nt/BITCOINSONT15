import asyncio
import time
import aiohttp
import logging
from typing import Optional, Dict, Any

from config import GAMMA_API_BASE, WINDOW_SECONDS

logger = logging.getLogger(__name__)


def current_window_ts() -> int:
    now = int(time.time())
    return now - (now % WINDOW_SECONDS)


def build_slug(window_ts: int) -> str:
    return f"btc-updown-15m-{window_ts}"


async def fetch_market_for_slug(session: aiohttp.ClientSession, slug: str) -> Optional[Dict[str, Any]]:
    url = f"{GAMMA_API_BASE}/events"
    params = {"slug": slug}
    try:
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
            if not data:
                return None
            event = data[0] if isinstance(data, list) else data
            return event
    except Exception as e:
        logger.warning(f"fetch_market_for_slug error for {slug}: {e}")
        return None


def extract_tokens(event: Dict[str, Any]) -> Optional[Dict[str, str]]:
    markets = event.get("markets", [])
    if not markets:
        return None
    market = markets[0]
    outcomes = market.get("outcomes", "")
    token_ids = market.get("clobTokenIds", "")

    # outcomes and clobTokenIds may be JSON strings or lists
    import json
    if isinstance(outcomes, str):
        try:
            outcomes = json.loads(outcomes)
        except Exception:
            outcomes = []
    if isinstance(token_ids, str):
        try:
            token_ids = json.loads(token_ids)
        except Exception:
            token_ids = []

    if not outcomes or not token_ids or len(outcomes) < 2 or len(token_ids) < 2:
        return None

    result = {}
    for i, outcome in enumerate(outcomes):
        if str(outcome).upper() in ("YES", "UP"):
            result["YES"] = str(token_ids[i])
        elif str(outcome).upper() in ("NO", "DOWN"):
            result["NO"] = str(token_ids[i])

    if "YES" not in result and len(token_ids) >= 2:
        result["YES"] = str(token_ids[0])
        result["NO"] = str(token_ids[1])

    return result if len(result) == 2 else None


async def fetch_orderbook_price(session: aiohttp.ClientSession, token_id: str) -> Optional[float]:
    url = f"{GAMMA_API_BASE}/orderbook/{token_id}"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
            asks = data.get("asks", [])
            if asks:
                best_ask = min(asks, key=lambda x: float(x.get("price", 999)))
                return float(best_ask.get("price", 0.5))
            return 0.5
    except Exception as e:
        logger.warning(f"fetch_orderbook_price error: {e}")
        return 0.5


class MarketScanner:
    def __init__(self):
        self.current_window_ts: int = 0
        self.current_slug: str = ""
        self.tokens: Optional[Dict[str, str]] = None
        self.event_data: Optional[Dict[str, Any]] = None
        self._session: Optional[aiohttp.ClientSession] = None

    async def start(self):
        self._session = aiohttp.ClientSession()
        await self.refresh()

    async def stop(self):
        if self._session:
            await self._session.close()

    async def refresh(self):
        window_ts = current_window_ts()
        if window_ts == self.current_window_ts and self.tokens:
            return

        logger.info(f"Scanning for window {window_ts}")
        self.current_window_ts = window_ts
        self.current_slug = build_slug(window_ts)
        self.tokens = None
        self.event_data = None

        # Try current window, then ±900s offsets
        offsets = [0, WINDOW_SECONDS, -WINDOW_SECONDS, 2 * WINDOW_SECONDS, -2 * WINDOW_SECONDS]
        for offset in offsets:
            ts = window_ts + offset
            slug = build_slug(ts)
            event = await fetch_market_for_slug(self._session, slug)
            if event:
                tokens = extract_tokens(event)
                if tokens:
                    self.tokens = tokens
                    self.event_data = event
                    self.current_slug = slug
                    self.current_window_ts = ts
                    logger.info(f"Found market: {slug} | YES={tokens['YES'][:12]}... NO={tokens['NO'][:12]}...")
                    return

        logger.warning(f"No market found for window {window_ts} and offsets {offsets}")

    async def get_yes_price(self) -> float:
        if not self.tokens or not self._session:
            return 0.5
        price = await fetch_orderbook_price(self._session, self.tokens["YES"])
        return price if price is not None else 0.5

    async def get_no_price(self) -> float:
        if not self.tokens or not self._session:
            return 0.5
        price = await fetch_orderbook_price(self._session, self.tokens["NO"])
        return price if price is not None else 0.5

    def window_changed(self) -> bool:
        return current_window_ts() != self.current_window_ts

    def time_remaining(self) -> int:
        window_ts = self.current_window_ts
        elapsed = int(time.time()) - window_ts
        return max(0, WINDOW_SECONDS - elapsed)

    def window_progress(self) -> float:
        window_ts = self.current_window_ts
        elapsed = int(time.time()) - window_ts
        return min(1.0, elapsed / WINDOW_SECONDS)
