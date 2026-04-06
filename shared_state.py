"""
Thread-safe shared state between the asyncio bot and the Flask web dashboard.
All writes come from the asyncio event loop; all reads from the Flask thread.
threading.Lock is safe here because asyncio is single-threaded and the Flask
thread only holds the lock briefly for reads/writes of small dicts.
"""

import threading
import time
from typing import List, Dict, Any, Optional

MAX_PRICE_HISTORY = 900  # one point per second × 15 minutes


class SharedState:
    def __init__(self, initial_bankroll: float = 100.0):
        self._lock = threading.Lock()

        # Price data
        self.current_price: float = 0.0
        self.window_open_price: float = 0.0
        self.window_high: float = 0.0
        self.window_low: float = 0.0
        self.delta_pct: float = 0.0
        self.delta_1min: float = 0.0
        self.volume: float = 0.0
        self.rsi: Optional[float] = None
        self.vwap: Optional[float] = None

        # price_history: list of {"t": unix_ts_ms, "v": price}
        self.price_history: List[Dict] = []

        # Signal state
        self.signal_direction: Optional[str] = None
        self.signal_confidence: float = 0.0
        self.strategy_momentum: Optional[str] = None
        self.strategy_mean_rev: Optional[str] = None
        self.strategy_macd: Optional[str] = None
        self.skip_reason: Optional[str] = None

        # Window state
        self.window_ts: int = 0
        self.current_slug: str = "—"
        self.time_remaining: int = 900
        self.window_progress: float = 0.0

        # Risk / bankroll
        self.bankroll: float = initial_bankroll
        self.initial_bankroll: float = initial_bankroll
        self.circuit_breaker_active: bool = False
        self.circuit_breaker_remaining: int = 0

        # Portfolio stats
        self.total_trades: int = 0
        self.wins: int = 0
        self.losses: int = 0
        self.win_rate: float = 0.0
        self.total_pnl: float = 0.0
        self.best_trade: float = 0.0
        self.worst_trade: float = 0.0

        # Recent trades list (last 10)
        self.trades_list: List[Dict] = []

        # Active trade
        self.active_trade: Optional[Dict] = None

        # Data source label ("websocket" | "binance_rest" | "coinbase_rest")
        self.data_source: str = "websocket"

        self._last_updated: float = time.time()

    # ── Writers (called from asyncio thread) ────────────────────────────────

    def update_price(
        self,
        price: float,
        window_open_price: float,
        delta_pct: float,
        delta_1min: float,
        window_high: float,
        window_low: float,
        volume: float,
        rsi: Optional[float],
        vwap: Optional[float],
    ):
        with self._lock:
            self.current_price = price
            self.window_open_price = window_open_price
            self.delta_pct = delta_pct
            self.delta_1min = delta_1min
            self.window_high = window_high
            self.window_low = window_low
            self.volume = volume
            self.rsi = rsi
            self.vwap = vwap

            # Append to price history (millisecond timestamps for Chart.js)
            ts_ms = int(time.time() * 1000)
            self.price_history.append({"t": ts_ms, "v": price})
            # Keep only last MAX_PRICE_HISTORY points
            if len(self.price_history) > MAX_PRICE_HISTORY:
                self.price_history = self.price_history[-MAX_PRICE_HISTORY:]

            self._last_updated = time.time()

    def update_signal(self, signal: Dict[str, Any]):
        with self._lock:
            details = signal.get("strategy_details", {})
            self.signal_direction = signal.get("direction")
            self.signal_confidence = signal.get("confidence", 0.0)
            self.strategy_momentum = details.get("momentum")
            self.strategy_mean_rev = details.get("mean_reversion")
            self.strategy_macd = details.get("macd_cross")
            self.skip_reason = signal.get("skip_reason")

    def update_window(self, window_ts: int, slug: str, time_remaining: int, progress: float):
        with self._lock:
            self.window_ts = window_ts
            self.current_slug = slug
            self.time_remaining = time_remaining
            self.window_progress = progress

    def update_risk(self, bankroll: float, cb_active: bool, cb_remaining: int):
        with self._lock:
            self.bankroll = bankroll
            self.circuit_breaker_active = cb_active
            self.circuit_breaker_remaining = cb_remaining

    def update_active_trade(self, trade: Optional[Dict]):
        with self._lock:
            self.active_trade = trade

    def update_stats(self, stats: Dict[str, Any], trades: List[Dict]):
        with self._lock:
            self.total_trades = stats.get("total", 0)
            self.wins = stats.get("wins", 0)
            self.losses = stats.get("losses", 0)
            self.win_rate = stats.get("win_rate", 0.0)
            self.total_pnl = stats.get("total_pnl", 0.0)
            self.best_trade = stats.get("best_trade", 0.0)
            self.worst_trade = stats.get("worst_trade", 0.0)
            self.trades_list = trades

    def new_window(self, window_ts: int, open_price: float):
        """Called at the start of each new 15-min window to reset price history."""
        with self._lock:
            self.window_ts = window_ts
            self.window_open_price = open_price
            self.signal_direction = None
            self.signal_confidence = 0.0
            self.strategy_momentum = None
            self.strategy_mean_rev = None
            self.strategy_macd = None
            self.skip_reason = None
            self.active_trade = None
            # Reset chart history for the new window
            ts_ms = int(time.time() * 1000)
            self.price_history = [{"t": ts_ms, "v": open_price}] if open_price > 0 else []

    # ── Reader (called from Flask thread) ────────────────────────────────────

    def get_snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                # Price
                "price": self.current_price,
                "window_open_price": self.window_open_price,
                "delta_pct": self.delta_pct,
                "delta_1min": self.delta_1min,
                "window_high": self.window_high,
                "window_low": self.window_low,
                "volume": self.volume,
                "rsi": self.rsi,
                "vwap": self.vwap,
                # Chart data (copy so Flask thread can't mutate)
                "price_history": list(self.price_history),
                # Signal
                "signal_direction": self.signal_direction,
                "signal_confidence": self.signal_confidence,
                "strategy_momentum": self.strategy_momentum,
                "strategy_mean_rev": self.strategy_mean_rev,
                "strategy_macd": self.strategy_macd,
                "skip_reason": self.skip_reason,
                # Window
                "window_ts": self.window_ts,
                "current_slug": self.current_slug,
                "time_remaining": self.time_remaining,
                "window_progress": self.window_progress,
                # Risk
                "bankroll": self.bankroll,
                "initial_bankroll": self.initial_bankroll,
                "circuit_breaker_active": self.circuit_breaker_active,
                "circuit_breaker_remaining": self.circuit_breaker_remaining,
                # Stats
                "total_trades": self.total_trades,
                "wins": self.wins,
                "losses": self.losses,
                "win_rate": self.win_rate,
                "total_pnl": self.total_pnl,
                "best_trade": self.best_trade,
                "worst_trade": self.worst_trade,
                "trades_list": list(self.trades_list),
                "active_trade": self.active_trade,
                # Meta
                "last_updated": self._last_updated,
                "data_source": self.data_source,
            }
