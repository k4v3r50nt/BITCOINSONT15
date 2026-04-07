"""
BITCOINSONT15 — Risk Manager

Position sizing uses FLAT sizing (% of bankroll) instead of Kelly Criterion.
Kelly is incompatible with the "follow the market" strategy because in an
efficient market token_price ≈ true_probability → Kelly f* ≈ 0 always.

Flat sizing:
  base_bet = bankroll × BET_PCT_NORMAL   (default 3 %)
  cap      = bankroll × BET_PCT_MAX      (default 5 %)
  minimum  = MIN_BET_USD                 ($2.50)
  if force_min_bet (CB active): bet = MIN_BET_USD exactly
"""

import logging
import time
from typing import Optional, Dict, Any

import database

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────

MIN_BET_USD           = 2.50
MIN_BANKROLL_TO_TRADE = 10.00   # stop trading below this balance
BET_PCT_NORMAL        = 0.03    # 3 % of bankroll per trade
BET_PCT_MAX           = 0.05    # hard cap at 5 %

# Circuit breaker: N consecutive losses → pause M windows
CB_LOSS_STREAK        = 5
CB_PAUSE_WINDOWS      = 3
CB_PAUSE_SECONDS      = CB_PAUSE_WINDOWS * 900    # 2700 s = 45 min
CB_AUTO_RESET_SECONDS = 2700    # force-reset if somehow stuck longer than this


class RiskManager:
    def __init__(self, initial_bankroll: float, max_position_pct: float = BET_PCT_MAX):
        self.bankroll           = initial_bankroll
        self.max_position_pct   = max_position_pct

        self._cb_active         = False
        self._cb_until: int     = 0    # unix timestamp when CB expires
        self._cb_activated_at: int = 0 # unix timestamp when CB was set

    # ── Circuit breaker ───────────────────────────────────────────────────────

    def _check_and_update_cb(self) -> bool:
        """
        Returns True if trading is ALLOWED, False if blocked.
        Auto-resets if CB has been active longer than CB_AUTO_RESET_SECONDS.
        """
        now = int(time.time())

        if self._cb_active:
            time_active = now - self._cb_activated_at
            time_remaining = max(0, self._cb_until - now)

            # Auto-reset: CB has been running too long (e.g. survived a restart)
            if now >= self._cb_until or time_active >= CB_AUTO_RESET_SECONDS:
                logger.info(
                    f"[RISK] Circuit breaker EXPIRED "
                    f"(active for {time_active}s, limit={CB_AUTO_RESET_SECONDS}s) "
                    "— resuming trading"
                )
                print(
                    f"[RISK] Circuit breaker reset automático "
                    f"(llevaba {time_active}s activo)"
                )
                self._cb_active       = False
                self._cb_until        = 0
                self._cb_activated_at = 0
                return True

            print(
                f"[RISK] Circuit breaker ACTIVO | "
                f"activo desde {time_active}s | "
                f"faltan {time_remaining}s ({time_remaining//60}min)"
            )
            logger.warning(
                f"[RISK] CB active — {time_remaining}s remaining "
                f"(activated {time_active}s ago)"
            )
            return False

        # CB not active — check consecutive losses
        consecutive_losses = database.get_consecutive_losses()
        if consecutive_losses >= CB_LOSS_STREAK:
            self._cb_active       = True
            self._cb_until        = now + CB_PAUSE_SECONDS
            self._cb_activated_at = now
            logger.warning(
                f"[RISK] Circuit breaker ACTIVATED — "
                f"{consecutive_losses} consecutive losses. "
                f"Pausing {CB_PAUSE_WINDOWS} windows ({CB_PAUSE_SECONDS}s)."
            )
            print(
                f"[RISK] ⚠ Circuit breaker activado: {consecutive_losses} "
                f"losses seguidas → pausa de {CB_PAUSE_SECONDS}s"
            )
            return False

        return True

    @property
    def circuit_breaker_active(self) -> bool:
        return self._cb_active

    def circuit_breaker_remaining(self) -> int:
        if not self._cb_active:
            return 0
        return max(0, self._cb_until - int(time.time()))

    # ── Position sizing ───────────────────────────────────────────────────────

    def size_bet(
        self,
        confidence: float,
        token_price: float,
        force_min_bet: bool = False,
    ) -> Optional[float]:
        """
        Return bet size in USD, or None if trade should be blocked.

        Sizing uses flat % of bankroll (NOT Kelly — Kelly returns near-zero
        for momentum/follow-market strategies where price ≈ probability).
        """
        now = int(time.time())
        consecutive_losses = database.get_consecutive_losses() if not self._cb_active else "—"
        time_since_cb = (now - self._cb_activated_at) if self._cb_active else 0

        print(
            f"[RISK] Bankroll: ${self.bankroll:.2f} | "
            f"Min bet: ${MIN_BET_USD:.2f} | "
            f"CB active: {self._cb_active} | "
            f"Streak losses: {consecutive_losses} | "
            f"CB activo desde: {time_since_cb}s"
        )
        logger.info(
            f"[RISK] size_bet called — bankroll=${self.bankroll:.2f} "
            f"conf={confidence:.2f} token={token_price:.4f} "
            f"force_min={force_min_bet} cb={self._cb_active}"
        )

        # ── Bankroll floor ────────────────────────────────────────────────────
        if self.bankroll < MIN_BANKROLL_TO_TRADE:
            print(
                f"[RISK] BLOCKED: bankroll ${self.bankroll:.2f} < "
                f"mínimo ${MIN_BANKROLL_TO_TRADE:.2f}"
            )
            logger.warning(
                f"[RISK] Bankroll ${self.bankroll:.2f} below minimum "
                f"${MIN_BANKROLL_TO_TRADE:.2f} — blocking trade"
            )
            return None

        # ── Circuit breaker ───────────────────────────────────────────────────
        cb_allows = self._check_and_update_cb()

        if force_min_bet:
            # CB is active but we still place the minimum as a probe bet
            bet = MIN_BET_USD
            print(f"[RISK] force_min_bet → apuesta mínima ${bet:.2f}")
            logger.info(f"[RISK] Force min bet: ${bet:.2f}")
            return bet

        if not cb_allows:
            print(f"[RISK] BLOCKED por circuit breaker")
            return None

        # ── Flat sizing ───────────────────────────────────────────────────────
        base_bet = self.bankroll * BET_PCT_NORMAL            # 3 % of bankroll
        max_bet  = self.bankroll * self.max_position_pct     # 5 % hard cap
        bet      = min(base_bet, max_bet)

        # Enforce minimum
        if bet < MIN_BET_USD:
            if self.bankroll >= MIN_BET_USD:
                bet = MIN_BET_USD
                print(f"[RISK] Bet redondeado al mínimo: ${bet:.2f}")
            else:
                print(
                    f"[RISK] BLOCKED: apuesta calculada ${bet:.2f} < "
                    f"mínimo ${MIN_BET_USD:.2f} y bankroll insuficiente"
                )
                return None

        bet = min(round(bet, 2), self.bankroll)

        print(
            f"[RISK] ALLOW | bet=${bet:.2f} "
            f"({BET_PCT_NORMAL*100:.0f}% de ${self.bankroll:.2f}) | "
            f"max_cap=${max_bet:.2f}"
        )
        logger.info(f"[RISK] Approved bet: ${bet:.2f}")
        return bet

    # ── Bankroll update ───────────────────────────────────────────────────────

    def update_bankroll(self, new_bankroll: float):
        old = self.bankroll
        self.bankroll = round(new_bankroll, 4)
        logger.debug(
            f"[RISK] Bankroll updated: ${old:.2f} → ${self.bankroll:.2f} "
            f"({self.bankroll - old:+.2f})"
        )

    # ── Status snapshot (for dashboard) ──────────────────────────────────────

    def status(self) -> Dict[str, Any]:
        return {
            "bankroll":                 self.bankroll,
            "circuit_breaker_active":   self._cb_active,
            "circuit_breaker_remaining":self.circuit_breaker_remaining(),
            "max_bet":                  round(self.bankroll * self.max_position_pct, 2),
            "min_bet":                  MIN_BET_USD,
        }
