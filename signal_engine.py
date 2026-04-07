"""
BITCOINSONT15 — Signal Engine v2: Mispricing Hunter

Core insight: in binary Polymarket markets YES + NO should sum to ~$1.00.
When the implied total drops below $0.94, one side is mispriced and buying
it has positive expected value regardless of which direction BTC moves.

Edge formula:
  token_price = ask price you pay (e.g. $0.44)
  fair_value  = $0.50 (50/50 binary, no information edge)
  edge        = fair_value - token_price = $0.06 per dollar risked

This is the strategy profitable Polymarket traders actually use.
"""

import logging
import time
from typing import Optional, Dict, Any, List

logger = logging.getLogger(__name__)

# ── Mispricing thresholds ─────────────────────────────────────────────────────

IMPLIED_TOTAL_FLOOR     = 0.94   # below this → mispricing exists, trade eligible
IMPLIED_TOTAL_CEILING   = 1.06   # above this → market overvalued, skip
MAX_TOKEN_PRICE         = 0.47   # don't buy a side priced above this
MIN_EDGE_PCT            = 0.03   # minimum edge (50% - token_price) to act

# Trading window within each 15-min candle
TRADE_WINDOW_MIN_START  = 3.0    # skip first 3 min (wide spreads at open)
TRADE_WINDOW_MIN_END    = 13.0   # skip last 2 min (market already pricing result)

# Circuit breaker: if implied total stays >= this for this many minutes → min bets
CB_EFFICIENT_THRESHOLD  = 0.97
CB_EFFICIENT_MINUTES    = 30

# Kelly-like confidence scaling: this implied-total gap = confidence 1.0
CONFIDENCE_SCALE_GAP    = 0.10   # 10-cent mispricing → max confidence


class SignalEngine:
    def __init__(self, scanner, min_confidence: float = 0.0):
        """
        scanner: MarketScanner — used to fetch live YES/NO token prices.
        min_confidence: minimum edge-derived confidence to fire a trade.
                        Default 0.0 means any positive mispricing qualifies.
        """
        self.scanner = scanner
        self.min_confidence = min_confidence

        # Rolling history of implied totals for efficiency circuit breaker
        self._implied_history: List[Dict] = []   # [{ts, implied_total}]
        self._consecutive_wins_with_mispricing: int = 0

    # ── Public API ────────────────────────────────────────────────────────────

    async def evaluate(self, snapshot: Dict[str, Any], minutes_elapsed: float) -> Dict[str, Any]:
        """
        Async — fetches live token prices then runs mispricing analysis.
        Returns a signal dict compatible with the rest of the bot.
        """
        price      = snapshot.get("price", 0)
        delta      = snapshot.get("delta_pct", 0.0)
        rsi        = snapshot.get("rsi")

        logger.info(
            f"[Signal] T+{minutes_elapsed:.1f}min | BTC=${price:,.2f} "
            f"delta={delta:+.3f}% rsi={rsi!r}"
        )

        # ── 1. Timing gate ────────────────────────────────────────────────────
        if minutes_elapsed < TRADE_WINDOW_MIN_START:
            reason = f"too_early_{minutes_elapsed:.1f}min<{TRADE_WINDOW_MIN_START}"
            logger.info(f"[Signal] SKIP — {reason}")
            return self._skip(reason)

        if minutes_elapsed > TRADE_WINDOW_MIN_END:
            reason = f"too_late_{minutes_elapsed:.1f}min>{TRADE_WINDOW_MIN_END}"
            logger.info(f"[Signal] SKIP — {reason}")
            return self._skip(reason)

        # ── 2. Fetch live YES / NO prices from CLOB ───────────────────────────
        prices = await self.scanner.get_token_prices()
        yes_ask = prices.get("yes_ask")
        no_ask  = prices.get("no_ask")

        if yes_ask is None or no_ask is None:
            logger.warning("[Signal] SKIP — could not fetch token prices")
            return self._skip("price_fetch_failed")

        implied_total = round(yes_ask + no_ask, 4)
        logger.info(
            f"[Signal] yes_ask={yes_ask:.4f}  no_ask={no_ask:.4f}  "
            f"implied_total={implied_total:.4f}"
        )

        # Record for circuit breaker
        self._record_implied(implied_total)

        # ── 3. Efficiency checks ──────────────────────────────────────────────
        if implied_total > IMPLIED_TOTAL_CEILING:
            reason = f"overvalued_total={implied_total:.4f}>{IMPLIED_TOTAL_CEILING}"
            logger.info(f"[Signal] SKIP — {reason}")
            return self._skip(reason)

        if implied_total >= IMPLIED_TOTAL_FLOOR:
            reason = f"efficient_total={implied_total:.4f}>={IMPLIED_TOTAL_FLOOR}"
            logger.info(f"[Signal] SKIP — {reason}")
            return self._skip(reason, implied_total=implied_total)

        # ── 4. Which side has the edge ────────────────────────────────────────
        direction, token_price = self._pick_side(yes_ask, no_ask)

        if direction is None:
            reason = f"no_edge yes={yes_ask:.4f} no={no_ask:.4f}"
            logger.info(f"[Signal] SKIP — {reason}")
            return self._skip(reason, implied_total=implied_total)

        # ── 5. Compute edge and confidence ────────────────────────────────────
        edge_pct   = round(0.50 - token_price, 4)   # positive means we have edge
        gap        = round(IMPLIED_TOTAL_FLOOR - implied_total, 4)
        confidence = round(min(1.0, gap / CONFIDENCE_SCALE_GAP), 4)

        if edge_pct < MIN_EDGE_PCT:
            reason = f"edge_too_small_{edge_pct:.4f}<{MIN_EDGE_PCT}"
            logger.info(f"[Signal] SKIP — {reason}")
            return self._skip(reason, implied_total=implied_total)

        if confidence < self.min_confidence:
            reason = f"low_confidence_{confidence:.2f}<{self.min_confidence:.2f}"
            logger.info(f"[Signal] SKIP — {reason}")
            return self._skip(reason, implied_total=implied_total)

        # ── 6. Circuit breaker: market too efficient today → min bet ──────────
        force_min_bet = self._market_too_efficient()
        if force_min_bet:
            logger.info("[Signal] Circuit breaker: market efficient today → forcing min bet")

        logger.info(
            f"[Signal] FIRE {direction} | token={token_price:.4f} "
            f"edge={edge_pct:+.4f} ({edge_pct*100:.1f}%) "
            f"conf={confidence:.2f} implied={implied_total:.4f}"
        )

        return {
            "direction":        direction,       # "YES" or "NO"
            "token_price":      token_price,
            "yes_ask":          yes_ask,
            "no_ask":           no_ask,
            "implied_total":    implied_total,
            "edge_pct":         edge_pct,
            "confidence":       confidence,
            "force_min_bet":    force_min_bet,
            "skip_reason":      None,
            # compat fields expected by dashboard / shared_state
            "strategy_details": {
                "momentum":       direction,
                "mean_reversion": None,
                "macd_cross":     None,
            },
            "agreement":        1,
            "strategies_voted": 1,
        }

    def record_win(self, had_mispricing: bool):
        """Call after a winning trade to track consecutive mispricing wins."""
        if had_mispricing:
            self._consecutive_wins_with_mispricing += 1
            logger.info(
                f"[Signal] Consecutive mispricing wins: "
                f"{self._consecutive_wins_with_mispricing}"
            )
        else:
            self._consecutive_wins_with_mispricing = 0

    def record_loss(self):
        self._consecutive_wins_with_mispricing = 0

    def mispricing_win_streak(self) -> int:
        return self._consecutive_wins_with_mispricing

    # ── Private helpers ───────────────────────────────────────────────────────

    def _pick_side(self, yes_ask: float, no_ask: float):
        """Return (direction, token_price) for the cheaper side, or (None, None)."""
        yes_cheap = yes_ask <= no_ask and yes_ask < MAX_TOKEN_PRICE
        no_cheap  = no_ask  <  yes_ask and no_ask  < MAX_TOKEN_PRICE

        if yes_cheap:
            logger.debug(f"[Signal] Cheaper side: YES @ {yes_ask:.4f}")
            return "YES", yes_ask
        if no_cheap:
            logger.debug(f"[Signal] Cheaper side: NO @ {no_ask:.4f}")
            return "NO", no_ask

        # Both above MAX_TOKEN_PRICE or equal and both high
        logger.debug(
            f"[Signal] Neither side cheap enough: YES={yes_ask:.4f} NO={no_ask:.4f} "
            f"max={MAX_TOKEN_PRICE}"
        )
        return None, None

    def _record_implied(self, implied_total: float):
        now = time.time()
        self._implied_history.append({"ts": now, "implied_total": implied_total})
        cutoff = now - CB_EFFICIENT_MINUTES * 60
        self._implied_history = [r for r in self._implied_history if r["ts"] >= cutoff]

    def _market_too_efficient(self) -> bool:
        """True if implied_total has stayed >= CB_EFFICIENT_THRESHOLD for the full window."""
        if len(self._implied_history) < 3:
            return False
        oldest = self._implied_history[0]
        window_seconds = time.time() - oldest["ts"]
        if window_seconds < CB_EFFICIENT_MINUTES * 60:
            return False
        all_efficient = all(
            r["implied_total"] >= CB_EFFICIENT_THRESHOLD
            for r in self._implied_history
        )
        return all_efficient

    @staticmethod
    def _skip(reason: str, implied_total: float = 0.0) -> Dict[str, Any]:
        return {
            "direction":        None,
            "token_price":      0.0,
            "yes_ask":          0.0,
            "no_ask":           0.0,
            "implied_total":    implied_total,
            "edge_pct":         0.0,
            "confidence":       0.0,
            "force_min_bet":    False,
            "skip_reason":      reason,
            "strategy_details": {
                "momentum":       None,
                "mean_reversion": None,
                "macd_cross":     None,
            },
            "agreement":        0,
            "strategies_voted": 0,
        }
