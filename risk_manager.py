import logging
import time
from typing import Optional, Dict, Any

import database

logger = logging.getLogger(__name__)

KELLY_FRACTION = 0.25
MAX_POSITION_PCT = 0.05
MIN_BET_USD = 2.50
CIRCUIT_BREAKER_LOSSES = 5
CIRCUIT_BREAKER_WINDOWS = 3
WINDOW_SECONDS = 900


class RiskManager:
    def __init__(self, initial_bankroll: float, max_position_pct: float = MAX_POSITION_PCT):
        self.bankroll = initial_bankroll
        self.max_position_pct = max_position_pct
        self.circuit_breaker_until: int = 0  # unix timestamp
        self.circuit_breaker_active = False

    def check_circuit_breaker(self) -> bool:
        """Returns True if trading is allowed, False if blocked."""
        if self.circuit_breaker_active:
            if int(time.time()) >= self.circuit_breaker_until:
                logger.info("Circuit breaker expired — resuming trading")
                self.circuit_breaker_active = False
                return True
            return False

        consecutive_losses = database.get_consecutive_losses()
        if consecutive_losses >= CIRCUIT_BREAKER_LOSSES:
            self.circuit_breaker_until = int(time.time()) + (CIRCUIT_BREAKER_WINDOWS * WINDOW_SECONDS)
            self.circuit_breaker_active = True
            logger.warning(f"Circuit breaker activated! {consecutive_losses} consecutive losses. Pausing {CIRCUIT_BREAKER_WINDOWS} windows.")
            return False

        return True

    def circuit_breaker_remaining(self) -> int:
        if not self.circuit_breaker_active:
            return 0
        remaining = self.circuit_breaker_until - int(time.time())
        return max(0, remaining)

    def size_bet(self, confidence: float, token_price: float) -> Optional[float]:
        """
        Kelly Criterion at 25% fraction.
        Kelly f* = (p * b - q) / b
        where b = (1 - token_price) / token_price (odds), p = win_prob, q = 1 - p
        """
        if not self.check_circuit_breaker():
            return None

        if token_price <= 0 or token_price >= 1:
            token_price = 0.5

        # Use confidence as win probability estimate
        p = confidence
        q = 1 - p
        b = (1 - token_price) / token_price  # net odds per dollar

        if b <= 0:
            return None

        kelly_f = (p * b - q) / b
        if kelly_f <= 0:
            return None

        # Apply fraction
        kelly_f_conservative = kelly_f * KELLY_FRACTION

        # Dollar amount
        bet_usd = self.bankroll * kelly_f_conservative

        # Hard cap
        max_bet = self.bankroll * self.max_position_pct
        bet_usd = min(bet_usd, max_bet)

        # Minimum
        if bet_usd < MIN_BET_USD:
            # Still place if we can afford minimum
            if self.bankroll >= MIN_BET_USD:
                bet_usd = MIN_BET_USD
            else:
                return None

        # Don't bet more than we have
        bet_usd = min(bet_usd, self.bankroll)

        return round(bet_usd, 2)

    def update_bankroll(self, new_bankroll: float):
        self.bankroll = new_bankroll

    def status(self) -> Dict[str, Any]:
        return {
            "bankroll": self.bankroll,
            "circuit_breaker_active": self.circuit_breaker_active,
            "circuit_breaker_remaining": self.circuit_breaker_remaining(),
            "max_bet": round(self.bankroll * self.max_position_pct, 2),
            "min_bet": MIN_BET_USD,
        }
