import asyncio
import json
import time
import aiohttp
import logging
from typing import Optional, Dict, Any

from config import GAMMA_API_BASE, WINDOW_SECONDS

CLOB_BASE = "https://clob.polymarket.com"

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

    async def get_token_prices(self) -> Dict[str, Optional[float]]:
        """
        Fetch live best-ask prices for both YES and NO tokens.

        Priority:
          1. Polymarket CLOB orderbook (best_ask per side)
          2. Polymarket CLOB midpoints endpoint (mid price, less precise)
          3. Gamma API outcomePrices (fallback, may lag)

        Returns dict with keys: yes_ask, no_ask (None if unavailable).
        """
        empty = {"yes_ask": None, "no_ask": None}

        if not self.tokens or not self._session:
            logger.warning("get_token_prices: no tokens or session")
            return empty

        yes_id = self.tokens["YES"]
        no_id  = self.tokens["NO"]

        # ── 1. CLOB orderbook (best ask per side) ────────────────────────────
        yes_ask = await self._clob_best_ask(yes_id)
        no_ask  = await self._clob_best_ask(no_id)

        if yes_ask is not None and no_ask is not None:
            logger.debug(f"get_token_prices (CLOB orderbook): YES={yes_ask:.4f} NO={no_ask:.4f}")
            return {"yes_ask": yes_ask, "no_ask": no_ask}

        # ── 2. CLOB midpoints ─────────────────────────────────────────────────
        mids = await self._clob_midpoints(yes_id, no_id)
        if mids["yes_ask"] is not None and mids["no_ask"] is not None:
            logger.debug(
                f"get_token_prices (CLOB midpoints): "
                f"YES={mids['yes_ask']:.4f} NO={mids['no_ask']:.4f}"
            )
            return mids

        # ── 3. Gamma API outcomePrices ────────────────────────────────────────
        gamma = await self._gamma_outcome_prices(yes_id)
        if gamma["yes_ask"] is not None and gamma["no_ask"] is not None:
            logger.debug(
                f"get_token_prices (Gamma fallback): "
                f"YES={gamma['yes_ask']:.4f} NO={gamma['no_ask']:.4f}"
            )
            return gamma

        logger.warning("get_token_prices: all sources failed")
        return empty

    async def _clob_best_ask(self, token_id: str) -> Optional[float]:
        """Fetch the best (lowest) ask from the CLOB orderbook for one token."""
        url = f"{CLOB_BASE}/orderbook/{token_id}"
        try:
            async with self._session.get(
                url, timeout=aiohttp.ClientTimeout(total=8)
            ) as resp:
                if resp.status != 200:
                    logger.debug(f"CLOB orderbook HTTP {resp.status} for {token_id[:12]}...")
                    return None
                data = await resp.json()
                asks = data.get("asks", [])
                if not asks:
                    return None
                best = min(asks, key=lambda x: float(x.get("price", 999)))
                return float(best["price"])
        except Exception as e:
            logger.debug(f"CLOB orderbook error for {token_id[:12]}...: {e}")
            return None

    async def _clob_midpoints(self, yes_id: str, no_id: str) -> Dict[str, Optional[float]]:
        """Fetch midpoint prices from the CLOB midpoints endpoint."""
        url = f"{CLOB_BASE}/midpoints"
        try:
            async with self._session.get(
                url,
                params={"token_ids": f"{yes_id},{no_id}"},
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                if resp.status != 200:
                    return {"yes_ask": None, "no_ask": None}
                data = await resp.json()
                mids = data.get("midpoints", {})
                yes_mid = mids.get(yes_id)
                no_mid  = mids.get(no_id)
                return {
                    "yes_ask": float(yes_mid) if yes_mid is not None else None,
                    "no_ask":  float(no_mid)  if no_mid  is not None else None,
                }
        except Exception as e:
            logger.debug(f"CLOB midpoints error: {e}")
            return {"yes_ask": None, "no_ask": None}

    async def _gamma_outcome_prices(self, yes_id: str) -> Dict[str, Optional[float]]:
        """
        Fallback: Gamma API market lookup via clobTokenIds.
        outcomePrices is typically ["yes_price", "no_price"].
        """
        url = f"{GAMMA_API_BASE}/markets"
        try:
            async with self._session.get(
                url,
                params={"clob_token_ids": yes_id},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    return {"yes_ask": None, "no_ask": None}
                data = await resp.json()
                markets = data if isinstance(data, list) else data.get("markets", [])
                if not markets:
                    return {"yes_ask": None, "no_ask": None}
                market = markets[0]
                prices = market.get("outcomePrices", "[]")
                if isinstance(prices, str):
                    prices = json.loads(prices)
                if isinstance(prices, list) and len(prices) >= 2:
                    return {
                        "yes_ask": float(prices[0]),
                        "no_ask":  float(prices[1]),
                    }
        except Exception as e:
            logger.debug(f"Gamma outcomePrices error: {e}")
        return {"yes_ask": None, "no_ask": None}

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
