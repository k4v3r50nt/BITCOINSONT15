import asyncio
import json
import logging
import time
from collections import deque
from typing import List, Dict, Optional, Deque

import websockets
from websockets.exceptions import ConnectionClosed

from config import BINANCE_WS_URL

logger = logging.getLogger(__name__)

MAX_CANDLES = 30
TICK_HISTORY_SECONDS = 900  # 15 minutes


class Candle:
    def __init__(self, data: dict):
        k = data.get("k", data)
        self.open_time: int = k["t"]
        self.open: float = float(k["o"])
        self.high: float = float(k["h"])
        self.low: float = float(k["l"])
        self.close: float = float(k["c"])
        self.volume: float = float(k["v"])
        self.is_closed: bool = k.get("x", False)


class MarketData:
    def __init__(self):
        self.candles: Deque[Candle] = deque(maxlen=MAX_CANDLES)
        self.current_price: float = 0.0
        self.current_candle: Optional[Candle] = None
        self.window_open_price: float = 0.0
        self.window_open_time: int = 0

        # Tick history for chart: list of {timestamp, price}
        self.price_history: List[Dict] = []
        self._tick_cutoff = 0

        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()

    async def start(self):
        self._running = True
        self._task = asyncio.create_task(self._ws_loop())

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def set_window_open(self, ts: int):
        self.window_open_time = ts
        self.window_open_price = self.current_price
        # Reset tick history for new window
        self.price_history = []
        if self.current_price > 0:
            self.price_history.append({
                "timestamp": ts,
                "price": self.current_price,
            })

    async def _ws_loop(self):
        while self._running:
            try:
                await self._connect()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"WS error: {e}, reconnecting in 3s...")
                await asyncio.sleep(3)

    async def _connect(self):
        logger.info(f"Connecting to Binance WS: {BINANCE_WS_URL}")
        async with websockets.connect(BINANCE_WS_URL, ping_interval=20, ping_timeout=10) as ws:
            logger.info("Binance WS connected")
            async for raw in ws:
                if not self._running:
                    break
                try:
                    msg = json.loads(raw)
                    await self._handle_kline(msg)
                except Exception as e:
                    logger.warning(f"WS parse error: {e}")

    async def _handle_kline(self, msg: dict):
        candle = Candle(msg)
        now = time.time()

        async with self._lock:
            self.current_price = candle.close
            self.current_candle = candle

            # Keep tick history (last 15 minutes)
            cutoff = now - TICK_HISTORY_SECONDS
            self.price_history = [p for p in self.price_history if p["timestamp"] >= cutoff]
            self.price_history.append({"timestamp": now, "price": candle.close})

            if candle.is_closed:
                self.candles.append(candle)

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

        # Wilder's smoothed averages
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
        }
