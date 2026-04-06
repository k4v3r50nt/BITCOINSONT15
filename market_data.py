import asyncio
import enum
import json
import logging
import time
from collections import deque
from typing import List, Dict, Optional, Deque

import aiohttp
import websockets

from config import BINANCE_WS_URL

logger = logging.getLogger(__name__)

MAX_CANDLES = 30
TICK_HISTORY_SECONDS = 900  # 15 minutes
REST_POLL_INTERVAL = 2      # seconds between REST price polls
KLINE_REFRESH_INTERVAL = 60 # seconds between kline fetches in REST mode
WS_RETRY_INTERVAL = 300     # seconds before re-attempting WebSocket after failure

BINANCE_PRICE_URL = "https://api.binance.com/api/v3/ticker/price"
BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"
COINBASE_PRICE_URL = "https://api.coinbase.com/v2/prices/BTC-USD/spot"


class DataSource(enum.Enum):
    WEBSOCKET = "websocket"
    BINANCE_REST = "binance_rest"
    COINBASE_REST = "coinbase_rest"


class Candle:
    """Unified candle — built from either a WS kline message or a REST kline row."""

    @classmethod
    def from_ws(cls, data: dict) -> "Candle":
        k = data.get("k", data)
        c = cls.__new__(cls)
        c.open_time = k["t"]
        c.open = float(k["o"])
        c.high = float(k["h"])
        c.low = float(k["l"])
        c.close = float(k["c"])
        c.volume = float(k["v"])
        c.is_closed = k.get("x", False)
        return c

    @classmethod
    def from_rest_row(cls, row: list) -> "Candle":
        """
        Binance klines REST row:
        [open_time, open, high, low, close, volume, close_time, ...]
        """
        c = cls.__new__(cls)
        c.open_time = int(row[0])
        c.open = float(row[1])
        c.high = float(row[2])
        c.low = float(row[3])
        c.close = float(row[4])
        c.volume = float(row[5])
        # All rows from /klines are closed candles except possibly the last
        close_time = int(row[6])
        c.is_closed = close_time < int(time.time() * 1000)
        return c


class MarketData:
    def __init__(self):
        self.candles: Deque[Candle] = deque(maxlen=MAX_CANDLES)
        self.current_price: float = 0.0
        self.current_candle: Optional[Candle] = None
        self.window_open_price: float = 0.0
        self.window_open_time: int = 0

        # Tick history for chart: list of {timestamp, price}
        self.price_history: List[Dict] = []

        # Data source tracking
        self.source: DataSource = DataSource.WEBSOCKET
        self._ws_last_failed: float = 0.0
        self._last_kline_fetch: float = 0.0

        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._lock = asyncio.Lock()

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def start(self):
        self._running = True
        self._session = aiohttp.ClientSession()
        self._task = asyncio.create_task(self._main_loop())

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
            self.price_history.append({"timestamp": ts, "price": self.current_price})

    # ── Main dispatch loop ───────────────────────────────────────────────────

    async def _main_loop(self):
        while self._running:
            try:
                if self.source == DataSource.WEBSOCKET:
                    await self._try_websocket()
                    # _try_websocket returns only on failure; fall through to REST
                    if self._running:
                        logger.info("Falling back to Binance REST polling")
                        self.source = DataSource.BINANCE_REST
                        self._ws_last_failed = time.time()
                else:
                    # REST polling tick
                    await self._rest_poll_once()
                    await asyncio.sleep(REST_POLL_INTERVAL)

                    # Periodically retry the WebSocket
                    if (
                        self.source != DataSource.WEBSOCKET
                        and time.time() - self._ws_last_failed > WS_RETRY_INTERVAL
                    ):
                        logger.info("Retrying Binance WebSocket after REST fallback period...")
                        self.source = DataSource.WEBSOCKET

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception(f"_main_loop unexpected error: {e}")
                await asyncio.sleep(5)

    # ── WebSocket path ───────────────────────────────────────────────────────

    async def _try_websocket(self):
        """
        Attempt to connect and stream. Returns (without raising) on any error
        so the caller can switch to REST.
        """
        try:
            logger.info(f"Connecting to Binance WebSocket: {BINANCE_WS_URL}")
            async with websockets.connect(
                BINANCE_WS_URL,
                ping_interval=20,
                ping_timeout=10,
                open_timeout=15,
            ) as ws:
                logger.info("Binance WebSocket connected — source: WEBSOCKET")
                self.source = DataSource.WEBSOCKET
                async for raw in ws:
                    if not self._running:
                        return
                    try:
                        msg = json.loads(raw)
                        await self._handle_kline_ws(msg)
                    except Exception as parse_err:
                        logger.warning(f"WS parse error: {parse_err}")

        except websockets.exceptions.InvalidStatus as e:
            code = getattr(e.response, "status_code", None)
            logger.warning(
                f"Binance WS rejected with HTTP {code} "
                f"({'geo-blocked' if code == 451 else 'auth/other'}) — switching to REST"
            )
        except (OSError, asyncio.TimeoutError, websockets.exceptions.WebSocketException) as e:
            logger.warning(f"Binance WS connection error: {e} — switching to REST")
        except Exception as e:
            logger.warning(f"Binance WS unexpected error: {e} — switching to REST")
        # Return normally; caller will set source = BINANCE_REST

    async def _handle_kline_ws(self, msg: dict):
        candle = Candle.from_ws(msg)
        await self._ingest_price(candle.close)
        async with self._lock:
            self.current_candle = candle
            if candle.is_closed:
                self.candles.append(candle)

    # ── REST polling path ────────────────────────────────────────────────────

    async def _rest_poll_once(self):
        """Single REST tick: price + (periodically) klines for indicators."""
        price = await self._fetch_price_rest()
        if price is None:
            logger.warning("All REST price sources failed this tick")
            return

        await self._ingest_price(price)

        # Refresh kline candle buffer periodically for indicators
        if time.time() - self._last_kline_fetch >= KLINE_REFRESH_INTERVAL:
            await self._fetch_klines_rest()
            self._last_kline_fetch = time.time()

    async def _fetch_price_rest(self) -> Optional[float]:
        """Try Binance REST, then Coinbase. Returns price or None."""
        price = await self._binance_price()
        if price is not None:
            if self.source != DataSource.BINANCE_REST:
                logger.info("Source: BINANCE_REST")
                self.source = DataSource.BINANCE_REST
            return price

        logger.warning("Binance REST price failed — trying Coinbase")
        price = await self._coinbase_price()
        if price is not None:
            if self.source != DataSource.COINBASE_REST:
                logger.info("Source: COINBASE_REST")
                self.source = DataSource.COINBASE_REST
            return price

        return None

    async def _binance_price(self) -> Optional[float]:
        try:
            async with self._session.get(
                BINANCE_PRICE_URL,
                params={"symbol": "BTCUSDT"},
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status == 451:
                    logger.warning("Binance REST HTTP 451 — geo-blocked")
                    return None
                if resp.status != 200:
                    logger.warning(f"Binance REST price HTTP {resp.status}")
                    return None
                data = await resp.json()
                return float(data["price"])
        except Exception as e:
            logger.warning(f"Binance REST price error: {e}")
            return None

    async def _coinbase_price(self) -> Optional[float]:
        try:
            async with self._session.get(
                COINBASE_PRICE_URL,
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status != 200:
                    logger.warning(f"Coinbase REST price HTTP {resp.status}")
                    return None
                data = await resp.json()
                return float(data["data"]["amount"])
        except Exception as e:
            logger.warning(f"Coinbase REST price error: {e}")
            return None

    async def _fetch_klines_rest(self):
        """
        Fetch last 30 closed 1-minute candles from Binance REST.
        Silently skips if Binance is unavailable (indicators stay stale).
        """
        try:
            async with self._session.get(
                BINANCE_KLINES_URL,
                params={"symbol": "BTCUSDT", "interval": "1m", "limit": MAX_CANDLES + 1},
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                if resp.status != 200:
                    logger.warning(f"Binance klines HTTP {resp.status} — indicators stale")
                    return
                rows = await resp.json()
        except Exception as e:
            logger.warning(f"Binance klines fetch error: {e} — indicators stale")
            return

        candles = [Candle.from_rest_row(row) for row in rows]
        closed = [c for c in candles if c.is_closed]

        async with self._lock:
            self.candles.clear()
            for c in closed[-MAX_CANDLES:]:
                self.candles.append(c)
            # Use the last (possibly open) candle as current
            if candles:
                self.current_candle = candles[-1]

        logger.debug(f"Refreshed {len(closed)} closed candles via REST")

    # ── Shared price ingestion ───────────────────────────────────────────────

    async def _ingest_price(self, price: float):
        """Update current_price and price_history regardless of source."""
        now = time.time()
        async with self._lock:
            self.current_price = price
            cutoff = now - TICK_HISTORY_SECONDS
            self.price_history = [p for p in self.price_history if p["timestamp"] >= cutoff]
            self.price_history.append({"timestamp": now, "price": price})

    # ── Indicators ──────────────────────────────────────────────────────────

    def get_closes(self) -> List[float]:
        closes = [c.close for c in self.candles]
        if self.current_candle:
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
        candles = list(self.candles)
        if not candles:
            return None
        total_vol = sum(c.volume for c in candles)
        if total_vol == 0:
            return None
        total_tp_vol = sum(((c.high + c.low + c.close) / 3) * c.volume for c in candles)
        return total_tp_vol / total_vol

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
        if self.current_candle:
            return self.current_candle.volume
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
