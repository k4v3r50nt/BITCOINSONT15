"""
BITCOINSONT15 — Signal Engine v7: Gamma Mid Sweet-Spot

Strategy: buy the dominant side ONLY when it trades in the sweet spot
  0.54 ≤ mid ≤ 0.62  →  market is biased but not yet fully priced-in.

Rationale:
  - mid < 0.54  → market neutral, no edge
  - mid 0.54–0.62 → market is leaning but may be over-correcting → follow
  - mid > 0.62  → market has already priced the outcome in; paying too much
                  (e.g. $0.705 means you need to be right >70.5% to profit,
                   but the market already says 70.5% — no free edge left)

  YES mid ∈ [0.54, 0.62] → FIRE YES
  NO  mid ∈ [0.54, 0.62] → FIRE NO
  anything else           → SKIP

  Edge  = token_mid − 0.50   (e.g. mid=0.60 → edge=0.10 → 10 %)
  EV/10 = edge × 10          (e.g. edge=0.10 → +$1.00 per $10 bet)

Price source: Gamma API /markets?clob_token_ids=
  outcomePrices[0] = YES mid, outcomePrices[1] = NO mid
  (CLOB /books and /midpoints return HTTP 400/401 on cloud IPs)
"""

import json
import logging
import time
from typing import Optional, Dict, Any, List, Tuple

import aiohttp

logger = logging.getLogger(__name__)

# ── Strategy parameters ───────────────────────────────────────────────────────

# Sweet-spot range: fire only when dominant side is between these two values
SWEET_SPOT_LOW    = 0.54   # minimum mid to enter  (edge ≥ 4 %)
SWEET_SPOT_HIGH   = 0.62   # maximum mid to enter  (above this market is saturated)
MIN_EDGE_PCT      = 0.04   # 4 % minimum edge (= SWEET_SPOT_LOW - 0.50)

# Window timing
TRADE_MIN_START   = 1.5    # don't trade before minute 1.5
TRADE_MIN_END     = 13.5   # don't trade after minute 13.5 (too close to resolve)
URGENT_AFTER      = 12.0   # flag signal as urgent after this minute

# Circuit breaker: market stays "efficient" this long → min-bet mode
CB_MIN_START_EDGE = 0.02   # edge below this = "efficient"
CB_WINDOW_MIN     = 30     # minutes of efficiency before CB fires
CB_AUTO_RESET_MIN = 45     # minutes since last loss → auto-reset CB

# Gamma endpoint
GAMMA_BASE = "https://gamma-api.polymarket.com"


class SignalEngine:
    def __init__(self, scanner, min_confidence: float = 0.0):
        """
        scanner        — MarketScanner; provides scanner.tokens {"YES": id, "NO": id}
        min_confidence — minimum confidence threshold (0.0 = any signal passes)
        """
        self.scanner        = scanner
        self.min_confidence = min_confidence

        self._edge_history: List[Dict] = []         # for circuit breaker
        self._wins_with_signal: int    = 0
        self._last_loss_ts: float      = 0.0
        self._session: Optional[aiohttp.ClientSession] = None

    # ── HTTP session ──────────────────────────────────────────────────────────

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"User-Agent": "BITCOINSONT15/1.0"}
            )
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    # ── Gamma price fetch ─────────────────────────────────────────────────────

    async def _gamma_mids(self, yes_id: str) -> Optional[Tuple[float, float]]:
        """
        GET /markets?clob_token_ids={yes_id}
        Returns (yes_mid, no_mid) from outcomePrices, or None on failure.
        """
        session = await self._get_session()
        try:
            url = f"{GAMMA_BASE}/markets"
            async with session.get(
                url,
                params={"clob_token_ids": yes_id},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    logger.warning(f"[Signal] Gamma HTTP {resp.status}")
                    return None

                data    = await resp.json()
                markets = data if isinstance(data, list) else data.get("markets", [])
                if not markets:
                    logger.warning("[Signal] Gamma: no markets returned")
                    return None

                prices = markets[0].get("outcomePrices", "[]")
                if isinstance(prices, str):
                    prices = json.loads(prices)

                if isinstance(prices, list) and len(prices) >= 2:
                    y, n = float(prices[0]), float(prices[1])
                    logger.debug(
                        f"[Signal] Gamma mids: YES={y:.4f} NO={n:.4f} "
                        f"sum={y+n:.4f}"
                    )
                    return y, n

                logger.warning(f"[Signal] Gamma: unexpected outcomePrices: {prices}")
                return None

        except Exception as e:
            logger.warning(f"[Signal] Gamma fetch error: {e}")
            return None

    # ── Main evaluation ───────────────────────────────────────────────────────

    async def evaluate(
        self, snapshot: Dict[str, Any], minutes_elapsed: float
    ) -> Dict[str, Any]:
        """
        Called every 30s by main.py.
        Returns signal dict — key field is "direction" (None = skip).
        """
        price = snapshot.get("price", 0)
        delta = snapshot.get("delta_pct", 0.0)

        logger.info(
            f"[Signal] T+{minutes_elapsed:.1f}min | "
            f"BTC=${price:,.2f} delta={delta:+.3f}%"
        )

        # ── Resolve token IDs ─────────────────────────────────────────────────
        tokens = getattr(self.scanner, "tokens", None)
        if not tokens:
            print("[SIGNAL] SKIP — scanner sin tokens aún")
            return self._skip("no_tokens")

        yes_id = tokens.get("YES", "")
        no_id  = tokens.get("NO", "")
        if not yes_id or not no_id:
            print("[SIGNAL] SKIP — falta YES o NO token ID")
            return self._skip("missing_token_ids")

        # ── Fetch Gamma mids ──────────────────────────────────────────────────
        mids = await self._gamma_mids(yes_id)

        if mids is None:
            print("[SIGNAL] SKIP — Gamma API no disponible")
            return self._skip("gamma_unavailable")

        yes_mid, no_mid = mids

        # ── Circuit breaker auto-reset ────────────────────────────────────────
        cb_active = self._circuit_breaker_active()
        if cb_active and self._last_loss_ts > 0:
            mins_since_loss = (time.time() - self._last_loss_ts) / 60.0
            if mins_since_loss >= CB_AUTO_RESET_MIN:
                logger.info(
                    f"[Signal] Auto-resetting CB "
                    f"({mins_since_loss:.0f}min desde último loss)"
                )
                self._edge_history.clear()
                cb_active = False

        # ── Diagnostic print ──────────────────────────────────────────────────
        print(
            f"[SIGNAL] YES mid={yes_mid:.4f} | NO mid={no_mid:.4f} | "
            f"sum={yes_mid + no_mid:.4f}"
        )
        print(
            f"[SIGNAL] Minuto ventana: {minutes_elapsed:.1f} | "
            f"Circuit breaker: {cb_active}"
        )

        # ── Timing gate ───────────────────────────────────────────────────────
        if minutes_elapsed < TRADE_MIN_START:
            reason = f"too_early_{minutes_elapsed:.1f}min<{TRADE_MIN_START}"
            print(f"[SIGNAL] Decision: {reason}")
            return self._skip(reason, yes_mid=yes_mid, no_mid=no_mid)

        if minutes_elapsed > TRADE_MIN_END:
            reason = f"too_late_{minutes_elapsed:.1f}min>{TRADE_MIN_END}"
            print(f"[SIGNAL] Decision: {reason}")
            return self._skip(reason, yes_mid=yes_mid, no_mid=no_mid)

        is_urgent = minutes_elapsed >= URGENT_AFTER

        # ── Sweet-spot check ──────────────────────────────────────────────────
        #
        #  YES mid ∈ [SWEET_SPOT_LOW, SWEET_SPOT_HIGH] → FIRE YES
        #  NO  mid ∈ [SWEET_SPOT_LOW, SWEET_SPOT_HIGH] → FIRE NO
        #  If both qualify, pick the higher mid (stronger market signal)
        #  Outside range on both sides                 → SKIP
        #
        direction = None
        token_mid = None

        yes_in_range = SWEET_SPOT_LOW <= yes_mid <= SWEET_SPOT_HIGH
        no_in_range  = SWEET_SPOT_LOW <= no_mid  <= SWEET_SPOT_HIGH

        if yes_in_range and no_in_range:
            # Both in range — follow the stronger signal
            if yes_mid >= no_mid:
                direction, token_mid = "YES", yes_mid
            else:
                direction, token_mid = "NO",  no_mid
        elif yes_in_range:
            direction, token_mid = "YES", yes_mid
        elif no_in_range:
            direction, token_mid = "NO",  no_mid

        # Record edge for circuit breaker (strength of dominant side)
        best_edge = max(yes_mid - 0.50, no_mid - 0.50)
        self._record_edge(best_edge)

        if direction is None:
            # Determine why we skipped for a clear log message
            yes_reason = (
                "neutral"     if yes_mid < SWEET_SPOT_LOW  else
                "saturated"   if yes_mid > SWEET_SPOT_HIGH else
                "in-range"
            )
            no_reason = (
                "neutral"     if no_mid  < SWEET_SPOT_LOW  else
                "saturated"   if no_mid  > SWEET_SPOT_HIGH else
                "in-range"
            )
            reason = (
                f"out_of_range yes={yes_mid:.4f}({yes_reason}) "
                f"no={no_mid:.4f}({no_reason}) "
                f"range=[{SWEET_SPOT_LOW},{SWEET_SPOT_HIGH}]"
            )
            print(
                f"[SIGNAL] SKIP: yes={yes_mid:.4f}({yes_reason}) "
                f"no={no_mid:.4f}({no_reason}) "
                f"rango=[{SWEET_SPOT_LOW},{SWEET_SPOT_HIGH}]"
            )
            return self._skip(reason, yes_mid=yes_mid, no_mid=no_mid)

        edge_pct   = round(token_mid - 0.50, 4)            # how strongly market favors this side
        confidence = round(min(1.0, edge_pct / 0.10), 4)   # 10% edge → conf=1.0

        if edge_pct < MIN_EDGE_PCT:
            reason = (
                f"signal_weak_{edge_pct*100:.1f}%<{MIN_EDGE_PCT*100:.0f}%"
            )
            print(f"[SIGNAL] Edge: {edge_pct*100:.1f}% | Decision: {reason}")
            return self._skip(reason, yes_mid=yes_mid, no_mid=no_mid)

        if confidence < self.min_confidence:
            reason = (
                f"low_confidence_{confidence:.2f}<{self.min_confidence:.2f}"
            )
            print(f"[SIGNAL] Edge: {edge_pct*100:.1f}% | Decision: {reason}")
            return self._skip(reason, yes_mid=yes_mid, no_mid=no_mid)

        force_min_bet = cb_active
        ev_per_10     = round(edge_pct * 10, 2)   # edge × $10 bet

        print(
            f"[SIGNAL] Edge: {edge_pct*100:.1f}% | "
            f"EV=+${ev_per_10:.2f}/10$ | "
            f"Decision: FIRE {direction} @ mid={token_mid:.4f}"
            + (" [URGENT]" if is_urgent else "")
        )
        logger.info(
            f"[Signal] FIRE {direction} | mid={token_mid:.4f} "
            f"edge={edge_pct*100:.1f}% conf={confidence:.2f} "
            f"urgent={is_urgent}"
        )

        return {
            "direction":        direction,
            "token_price":      token_mid,     # mid used as entry price estimate
            "yes_ask":          yes_mid,        # dashboard compat (show mids as asks)
            "no_ask":           no_mid,
            "implied_total":    round(yes_mid + no_mid, 4),
            "edge_pct":         edge_pct,
            "confidence":       confidence,
            "force_min_bet":    force_min_bet,
            "skip_reason":      None,
            "urgent":           is_urgent,
            "strategy_details": {
                "momentum":       direction,
                "mean_reversion": None,
                "macd_cross":     None,
            },
            "agreement":        1,
            "strategies_voted": 1,
        }

    # ── Win/loss tracking ─────────────────────────────────────────────────────

    def record_win(self, had_signal: bool):
        if had_signal:
            self._wins_with_signal += 1
            logger.info(
                f"[Signal] Racha wins con señal: {self._wins_with_signal}"
            )
        else:
            self._wins_with_signal = 0

    def record_loss(self):
        self._wins_with_signal = 0
        self._last_loss_ts     = time.time()

    def mispricing_win_streak(self) -> int:
        return self._wins_with_signal

    # ── Circuit breaker internals ─────────────────────────────────────────────

    def _record_edge(self, edge: float):
        now = time.time()
        self._edge_history.append({"ts": now, "edge": edge})
        cutoff = now - CB_WINDOW_MIN * 60
        self._edge_history = [r for r in self._edge_history if r["ts"] >= cutoff]

    def _circuit_breaker_active(self) -> bool:
        """
        True when the market has shown < CB_MIN_START_EDGE for the full CB window.
        This means the market has been stubbornly neutral — reduce bet sizes.
        """
        if len(self._edge_history) < 5:
            return False
        oldest = self._edge_history[0]
        if time.time() - oldest["ts"] < CB_WINDOW_MIN * 60:
            return False
        return all(r["edge"] < CB_MIN_START_EDGE for r in self._edge_history)

    # ── Skip factory ─────────────────────────────────────────────────────────

    @staticmethod
    def _skip(
        reason:  str,
        yes_mid: float = 0.0,
        no_mid:  float = 0.0,
    ) -> Dict[str, Any]:
        return {
            "direction":        None,
            "token_price":      0.0,
            "yes_ask":          yes_mid,
            "no_ask":           no_mid,
            "implied_total":    round(yes_mid + no_mid, 4),
            "edge_pct":         0.0,
            "confidence":       0.0,
            "force_min_bet":    False,
            "skip_reason":      reason,
            "urgent":           False,
            "strategy_details": {
                "momentum":       None,
                "mean_reversion": None,
                "macd_cross":     None,
            },
            "agreement":        0,
            "strategies_voted": 0,
        }
