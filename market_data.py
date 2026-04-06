import asyncio
import enum
import logging
import time
from collections import deque
from typing import List, Dict, Optional, Deque

import aiohttp

logger = logging.getLogger(__name__)

MAX_CANDLES = 30
TICK_HISTORY_SECONDS = 900   # 15 minutes of price history
POLL_INTERVAL = 3            # seconds between price fetches
SYNTHETIC_CANDLE_SECONDS = 60  # group ticks into 1-min synthetic candles

COINGECKO_URL = "https://api.coingecko.com/api/v3/simple/price"
MEMPOOL_URL   = "https://mempool.space/api/v1/prices"


class DataSource(enum.Enum):
    COINGECKO = "coingecko"
    MEMPOOL   = "mempool"
    NONE      = "none"


class SyntheticCandle:
    """
    1-minute candle built from price ticks.
    Replaces the Binance kline candle for indicator calculations.
    """
    def __init__(self, open_price: float, open_time: float):
        self.open_time: float = open_time
        self.open:   float = open_price
        self.high:   float = open_price
        self.low:    float = open_price
        self.close:  float = open_price
        self.volume: float = 0.0      # no volume data from these APIs
        self.is_closed: bool = False

    def update(self, price: float):
        self.high  = max(self.high, price)
        self.low   = min(self.low,  price)
        self.close = price

    def close_candle(self) -> "SyntheticCandle":
        self.is_closed = True
        return self


class MarketData:
    def __init__(self):
        self.candles: Deque[SyntheticCandle] = deque(maxlen=MAX_CANDLES)
        self.current_price: float = 0.0
        self.current_candle: Optional[SyntheticCandle] = None
        self.window_open_price: float = 0.0
        self.window_open_time: int = 0

        # Tick history for chart: list of {timestamp, price}
        self.price_history: List[Dict] = []

        self.source: DataSource = DataSource.NONE
        self._candle_start: float = 0.0

        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._lock = asyncio.Lock()

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def start(self):
        self._running = True
        self._session = aiohttp.ClientSession(
            headers={"User-Agent": "BITCOINSONT15/1.0"}
        )
        self._task = asyncio.create_task(self._poll_loop())

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._session:
            await self._session.close()

    def set_window_open(self, ts: int):
        self.window_open_time = ts
        self.window_open_price = self.current_price
        self.price_history = []
        if self.current_price > 0:
            self.price_history.append({"timestamp": float(ts), "price": self.current_price})

    # ── Poll loop ────────────────────────────────────────────────────────────

    async def _poll_loop(self):
        """Fetch price every POLL_INTERVAL seconds, trying sources in order."""
        while self._running:
            try:
                price = await self._fetch_price()
                if price is not None:
                    await self._ingest_price(price)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"Poll loop error: {e}")

            await asyncio.sleep(POLL_INTERVAL)

    # ── Price fetchers ───────────────────────────────────────────────────────

    async def _fetch_price(self) -> Optional[float]:
        """Try CoinGecko first, then Mempool. Returns float or None."""
        price = await self._coingecko_price()
        if price is not None:
            if self.source != DataSource.COINGECKO:
                logger.info("Price source: CoinGecko")
                self.source = DataSource.COINGECKO
            return price

        logger.warning("CoinGecko failed — trying Mempool.space")
        price = await self._mempool_price()
        if price is not None:
            if self.source != DataSource.MEMPOOL:
                logger.info("Price source: Mempool.space")
                self.source = DataSource.MEMPOOL
            return price

        if self.source != DataSource.NONE:
            logger.error("All price sources failed this tick")
            self.source = DataSource.NONE
        return None

    async def _coingecko_price(self) -> Optional[float]:
        try:
            async with self._session.get(
                COINGECKO_URL,
                params={"ids": "bitcoin", "vs_currencies": "usd"},
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                if resp.status != 200:
                    logger.warning(f"CoinGecko HTTP {resp.status}")
                    return None
                data = await resp.json()
                return float(data["bitcoin"]["usd"])
        except Exception as e:
            logger.warning(f"CoinGecko error: {e}")
            return None

    async def _mempool_price(self) -> Optional[float]:
        try:
            async with self._session.get(
                MEMPOOL_URL,
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                if resp.status != 200:
                    logger.warning(f"Mempool HTTP {resp.status}")
                    return None
                data = await resp.json()
                return float(data["USD"])
        except Exception as e:
            logger.warning(f"Mempool error: {e}")
            return None

    # ── Price ingestion + synthetic candle builder ───────────────────────────

    async def _ingest_price(self, price: float):
        now = time.time()
        async with self._lock:
            self.current_price = price

            # Update price_history (last 15 minutes)
            cutoff = now - TICK_HISTORY_SECONDS
            self.price_history = [p for p in self.price_history if p["timestamp"] >= cutoff]
            self.price_history.append({"timestamp": now, "price": price})

            # Build / close synthetic 1-minute candles for indicators
            if self.current_candle is None or self._candle_start == 0.0:
                # Start first candle
                self.current_candle = SyntheticCandle(price, now)
                self._candle_start = now
            else:
                elapsed_in_candle = now - self._candle_start
                if elapsed_in_candle >= SYNTHETIC_CANDLE_SECONDS:
                    # Close current candle, start new one
                    self.current_candle.close_candle()
                    self.candles.append(self.current_candle)
                    self.current_candle = SyntheticCandle(price, now)
                    self._candle_start = now
                else:
                    self.current_candle.update(price)

    # ── Indicators ──────────────────────────────────────────────────────────

    def get_closes(self) -> List[float]:
        closes = [c.close for c in self.candles]
        if self.current_candle is not None:
            closes.append(self.current_candle.close)
        return closes

    def rsi(self, period: int = 14) -> Optional[float]:
        closes = self.get_closes()
        if len(closes) < period + 1:
            return None
        gains, losses = [], []
        for i in range(1, len(closes)):
            diff = closes[i] - closes[i - 1]
            gains.append(max(diff, 0))
            losses.append(max(-diff, 0))

        avg_gain = sum(gains[:period]) / period
        avg_loss = sum(losses[:period]) / period
        for i in range(period, len(gains)):
            avg_gain = (avg_gain * (period - 1) + gains[i]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i]) / period

        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))

    def ema(self, prices: List[float], period: int) -> List[float]:
        if len(prices) < period:
            return []
        k = 2 / (period + 1)
        result = [sum(prices[:period]) / period]
        for price in prices[period:]:
            result.append(price * k + result[-1] * (1 - k))
        return result

    def macd(self, fast: int = 12, slow: int = 26, signal: int = 9):
        closes = self.get_closes()
        if len(closes) < slow + signal:
            return None, None, None
        ema_fast = self.ema(closes, fast)
        ema_slow = self.ema(closes, slow)
        min_len = min(len(ema_fast), len(ema_slow))
        macd_line = [ema_fast[-(min_len - i)] - ema_slow[-(min_len - i)] for i in range(min_len)]
        if len(macd_line) < signal:
            return None, None, None
        signal_line = self.ema(macd_line, signal)
        histogram = [macd_line[-(len(signal_line) - i)] - signal_line[i] for i in range(len(signal_line))]
        return macd_line[-1], signal_line[-1], histogram

    def vwap(self) -> Optional[float]:
        # No volume data from public price APIs — return None gracefully
        return None

    def delta_from_open(self) -> float:
        if self.window_open_price == 0 or self.current_price == 0:
            return 0.0
        return (self.current_price - self.window_open_price) / self.window_open_price * 100

    def delta_1min(self) -> float:
        closes = self.get_closes()
        if len(closes) < 2:
            return 0.0
        return (closes[-1] - closes[-2]) / closes[-2] * 100

    def window_high(self) -> float:
        history = list(self.price_history)
        if not history:
            return self.current_price
        return max(p["price"] for p in history)

    def window_low(self) -> float:
        history = list(self.price_history)
        if not history:
            return self.current_price
        return min(p["price"] for p in history)

    def current_volume(self) -> float:
        # No volume available from these APIs
        return 0.0

    def snapshot(self) -> dict:
        rsi_val = self.rsi()
        macd_val, signal_val, hist = self.macd()
        return {
            "price": self.current_price,
            "rsi": rsi_val,
            "macd": macd_val,
            "macd_signal": signal_val,
            "macd_hist": hist,
            "vwap": self.vwap(),
            "delta_pct": self.delta_from_open(),
            "delta_1min": self.delta_1min(),
            "window_high": self.window_high(),
            "window_low": self.window_low(),
            "volume": self.current_volume(),
            "window_open_price": self.window_open_price,
            "candle_count": len(self.candles),
            "price_history": list(self.price_history),
            "source": self.source.value,
        }
