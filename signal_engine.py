"""
BITCOINSONT15 — Signal Engine v3: Mispricing Hunter

Core insight: YES + NO tokens always resolve to $1.00.
When implied_total (yes_ask + no_ask) < 0.98, one side is underpriced
and buying it has positive expected value.

Edge:  0.50 - token_price  (positive = we have edge)
EV per $10:  (0.50 - token_price) / token_price * 10
"""

import asyncio
import json
import logging
import time
from typing import Optional, Dict, Any, List

import aiohttp

logger = logging.getLogger(__name__)

# ── Thresholds ────────────────────────────────────────────────────────────────

IMPLIED_TOTAL_FLOOR    = 0.98   # below this → eligible (was 0.94)
IMPLIED_TOTAL_CEILING  = 1.06   # above this → overvalued, skip
MAX_TOKEN_PRICE        = 0.49   # don't buy a side above this
MIN_EDGE_PCT           = 0.01   # minimum edge 1% (was 3%)

TRADE_WINDOW_MIN_START = 1.0    # operate from minute 1 (was 3)
TRADE_WINDOW_MIN_END   = 14.0   # operate until minute 14 (was 13)

# Circuit breaker: implied_total stays ≥ this for this many minutes → min bets
CB_EFFICIENT_THRESHOLD = 0.98
CB_EFFICIENT_MINUTES   = 30
CB_AUTO_RESET_MINUTES  = 45     # auto-reset CB this many minutes after last loss

# Kelly confidence scaling
CONFIDENCE_SCALE_GAP   = 0.10   # 10-cent gap below floor → confidence 1.0

# Price endpoints (no auth needed)
CLOB_BASE  = "https://clob.polymarket.com"
GAMMA_BASE = "https://gamma-api.polymarket.com"


class SignalEngine:
    def __init__(self, scanner, min_confidence: float = 0.0):
        """
        scanner: MarketScanner — used for token IDs (tokens["YES"/"NO"]).
        min_confidence: minimum confidence to fire. Default 0.0 = any positive edge.
        """
        self.scanner = scanner
        self.min_confidence = min_confidence

        self._implied_history: List[Dict] = []
        self._consecutive_wins_with_mispricing: int = 0
        self._last_loss_ts: float = 0.0
        self._session: Optional[aiohttp.ClientSession] = None

    # ── Session lifecycle (lazy, reused across calls) ─────────────────────────

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"User-Agent": "BITCOINSONT15/1.0"}
            )
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    # ── Price fetch: 3-endpoint fallback chain ────────────────────────────────

    async def _fetch_token_prices(
        self, yes_id: str, no_id: str
    ) -> Dict[str, Optional[float]]:
        """
        Returns {"yes_ask": float, "no_ask": float} or defaults 0.50/0.50.
        Tries endpoints in order:
          1. CLOB /midpoints
          2. Gamma API /markets?clob_token_ids=
          3. Default 0.50/0.50 (logs warning)
        """
        session = await self._get_session()

        # ── 1. CLOB midpoints ─────────────────────────────────────────────────
        try:
            url = f"{CLOB_BASE}/midpoints"
            async with session.get(
                url,
                params={"token_ids": f"{yes_id},{no_id}"},
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    mids = data.get("midpoints", {})
                    yes_mid = mids.get(yes_id)
                    no_mid  = mids.get(no_id)
                    if yes_mid is not None and no_mid is not None:
                        y, n = float(yes_mid), float(no_mid)
                        logger.debug(
                            f"[Signal] CLOB midpoints: YES={y:.4f} NO={n:.4f}"
                        )
                        return {"yes_ask": y, "no_ask": n}
                else:
                    logger.debug(f"[Signal] CLOB midpoints HTTP {resp.status}")
        except Exception as e:
            logger.warning(f"[Signal] CLOB midpoints error: {e}")

        # ── 2. Gamma API outcomePrices ────────────────────────────────────────
        try:
            url = f"{GAMMA_BASE}/markets"
            async with session.get(
                url,
                params={"clob_token_ids": yes_id},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    markets = data if isinstance(data, list) else data.get("markets", [])
                    if markets:
                        prices = markets[0].get("outcomePrices", "[]")
                        if isinstance(prices, str):
                            prices = json.loads(prices)
                        if isinstance(prices, list) and len(prices) >= 2:
                            y, n = float(prices[0]), float(prices[1])
                            logger.debug(
                                f"[Signal] Gamma API prices: YES={y:.4f} NO={n:.4f}"
                            )
                            return {"yes_ask": y, "no_ask": n}
                else:
                    logger.debug(f"[Signal] Gamma API HTTP {resp.status}")
        except Exception as e:
            logger.warning(f"[Signal] Gamma API error: {e}")

        # ── 3. Fallback default ───────────────────────────────────────────────
        print("[SIGNAL] PRECIO DEFAULT — todos los endpoints fallaron, usando 0.50/0.50")
        logger.warning("[Signal] All price endpoints failed — using 0.50/0.50 default")
        return {"yes_ask": 0.50, "no_ask": 0.50}

    # ── Main evaluation ───────────────────────────────────────────────────────

    async def evaluate(
        self, snapshot: Dict[str, Any], minutes_elapsed: float
    ) -> Dict[str, Any]:
        """
        Fetches live token prices, checks for mispricing, returns signal dict.
        """
        price = snapshot.get("price", 0)
        delta = snapshot.get("delta_pct", 0.0)
        rsi   = snapshot.get("rsi")

        logger.info(
            f"[Signal] T+{minutes_elapsed:.1f}min | BTC=${price:,.2f} "
            f"delta={delta:+.3f}% rsi={rsi!r}"
        )

        # ── Resolve token IDs from scanner ────────────────────────────────────
        tokens = getattr(self.scanner, "tokens", None)
        if not tokens:
            logger.warning("[Signal] SKIP — no tokens from scanner")
            print(f"[SIGNAL] SKIP — scanner has no tokens yet")
            return self._skip("no_tokens")

        yes_id = tokens.get("YES", "")
        no_id  = tokens.get("NO", "")

        if not yes_id or not no_id:
            logger.warning("[Signal] SKIP — missing YES or NO token ID")
            return self._skip("missing_token_ids")

        # ── Fetch prices ──────────────────────────────────────────────────────
        prices  = await self._fetch_token_prices(yes_id, no_id)
        yes_ask = prices["yes_ask"]
        no_ask  = prices["no_ask"]

        implied_total = round(yes_ask + no_ask, 4)

        # ── Circuit breaker auto-reset ────────────────────────────────────────
        cb_active = self._market_too_efficient()
        if cb_active and self._last_loss_ts > 0:
            mins_since_loss = (time.time() - self._last_loss_ts) / 60.0
            if mins_since_loss >= CB_AUTO_RESET_MINUTES:
                logger.info(
                    f"[Signal] Auto-resetting circuit breaker "
                    f"({mins_since_loss:.0f}min since last loss)"
                )
                self._implied_history.clear()
                cb_active = False

        # ── Diagnostic print ──────────────────────────────────────────────────
        print(
            f"[SIGNAL] YES ask: {yes_ask:.4f} | NO ask: {no_ask:.4f} | "
            f"implied: {implied_total:.4f}"
        )
        print(
            f"[SIGNAL] Minuto ventana: {minutes_elapsed:.1f} | "
            f"Circuit breaker: {cb_active}"
        )

        # ── Timing gate ───────────────────────────────────────────────────────
        if minutes_elapsed < TRADE_WINDOW_MIN_START:
            reason = f"too_early_{minutes_elapsed:.1f}min<{TRADE_WINDOW_MIN_START}"
            print(f"[SIGNAL] Edge: — | Decision: {reason}")
            logger.info(f"[Signal] SKIP — {reason}")
            return self._skip(reason, yes_ask=yes_ask, no_ask=no_ask,
                              implied_total=implied_total)

        if minutes_elapsed > TRADE_WINDOW_MIN_END:
            reason = f"too_late_{minutes_elapsed:.1f}min>{TRADE_WINDOW_MIN_END}"
            print(f"[SIGNAL] Edge: — | Decision: {reason}")
            logger.info(f"[Signal] SKIP — {reason}")
            return self._skip(reason, yes_ask=yes_ask, no_ask=no_ask,
                              implied_total=implied_total)

        # Record for circuit breaker history
        self._record_implied(implied_total)

        # ── Overvalued check ──────────────────────────────────────────────────
        if implied_total > IMPLIED_TOTAL_CEILING:
            reason = f"overvalued_total={implied_total:.4f}>{IMPLIED_TOTAL_CEILING}"
            print(f"[SIGNAL] Edge: — | Decision: {reason}")
            logger.info(f"[Signal] SKIP — {reason}")
            return self._skip(reason, yes_ask=yes_ask, no_ask=no_ask,
                              implied_total=implied_total)

        # ── Efficiency check ──────────────────────────────────────────────────
        if implied_total >= IMPLIED_TOTAL_FLOOR:
            reason = f"efficient_total={implied_total:.4f}>={IMPLIED_TOTAL_FLOOR}"
            print(f"[SIGNAL] Edge: — | Decision: {reason}")
            logger.info(f"[Signal] SKIP — {reason}")
            return self._skip(reason, yes_ask=yes_ask, no_ask=no_ask,
                              implied_total=implied_total)

        # ── Pick cheaper side ─────────────────────────────────────────────────
        direction, token_price = self._pick_side(yes_ask, no_ask)

        if direction is None:
            reason = f"no_edge yes={yes_ask:.4f} no={no_ask:.4f} max={MAX_TOKEN_PRICE}"
            print(f"[SIGNAL] Edge: — | Decision: {reason}")
            logger.info(f"[Signal] SKIP — {reason}")
            return self._skip(reason, yes_ask=yes_ask, no_ask=no_ask,
                              implied_total=implied_total)

        # ── Edge and confidence ───────────────────────────────────────────────
        edge_pct   = round(0.50 - token_price, 4)
        gap        = round(IMPLIED_TOTAL_FLOOR - implied_total, 4)
        confidence = round(min(1.0, gap / CONFIDENCE_SCALE_GAP), 4)

        if edge_pct < MIN_EDGE_PCT:
            reason = f"edge_too_small_{edge_pct*100:.1f}%<{MIN_EDGE_PCT*100:.0f}%"
            print(f"[SIGNAL] Edge: {edge_pct*100:.2f}% | Decision: {reason}")
            logger.info(f"[Signal] SKIP — {reason}")
            return self._skip(reason, yes_ask=yes_ask, no_ask=no_ask,
                              implied_total=implied_total, edge_pct=edge_pct)

        if confidence < self.min_confidence:
            reason = f"low_confidence_{confidence:.2f}<{self.min_confidence:.2f}"
            print(f"[SIGNAL] Edge: {edge_pct*100:.2f}% | Decision: {reason}")
            logger.info(f"[Signal] SKIP — {reason}")
            return self._skip(reason, yes_ask=yes_ask, no_ask=no_ask,
                              implied_total=implied_total, edge_pct=edge_pct)

        # ── Force min bet if CB active ────────────────────────────────────────
        force_min_bet = cb_active
        if force_min_bet:
            logger.info("[Signal] Circuit breaker active → forcing min bet")

        ev_per_10 = round((edge_pct / token_price) * 10, 2) if token_price > 0 else 0.0

        print(
            f"[SIGNAL] Edge: {edge_pct*100:.2f}% | "
            f"EV=$+{ev_per_10:.2f}/10$ | Decision: FIRE {direction}"
        )
        logger.info(
            f"[Signal] FIRE {direction} | token={token_price:.4f} "
            f"edge={edge_pct:+.4f} ({edge_pct*100:.1f}%) "
            f"conf={confidence:.2f} implied={implied_total:.4f} "
            f"EV=+${ev_per_10:.2f} per $10"
        )

        return {
            "direction":        direction,      # "YES" or "NO"
            "token_price":      token_price,
            "yes_ask":          yes_ask,
            "no_ask":           no_ask,
            "implied_total":    implied_total,
            "edge_pct":         edge_pct,
            "confidence":       confidence,
            "force_min_bet":    force_min_bet,
            "skip_reason":      None,
            # compat fields expected by dashboard / paper_trader
            "strategy_details": {
                "momentum":       direction,
                "mean_reversion": None,
                "macd_cross":     None,
            },
            "agreement":        1,
            "strategies_voted": 1,
        }

    # ── Win/loss tracking ─────────────────────────────────────────────────────

    def record_win(self, had_mispricing: bool):
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
        self._last_loss_ts = time.time()

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

        logger.debug(
            f"[Signal] Neither side cheap enough: "
            f"YES={yes_ask:.4f} NO={no_ask:.4f} max={MAX_TOKEN_PRICE}"
        )
        return None, None

    def _record_implied(self, implied_total: float):
        now = time.time()
        self._implied_history.append({"ts": now, "implied_total": implied_total})
        cutoff = now - CB_EFFICIENT_MINUTES * 60
        self._implied_history = [
            r for r in self._implied_history if r["ts"] >= cutoff
        ]

    def _market_too_efficient(self) -> bool:
        """True if implied_total has stayed >= CB_EFFICIENT_THRESHOLD for the full CB window."""
        if len(self._implied_history) < 3:
            return False
        oldest = self._implied_history[0]
        window_seconds = time.time() - oldest["ts"]
        if window_seconds < CB_EFFICIENT_MINUTES * 60:
            return False
        return all(
            r["implied_total"] >= CB_EFFICIENT_THRESHOLD
            for r in self._implied_history
        )

    @staticmethod
    def _skip(
        reason: str,
        yes_ask: float = 0.0,
        no_ask: float = 0.0,
        implied_total: float = 0.0,
        edge_pct: float = 0.0,
    ) -> Dict[str, Any]:
        return {
            "direction":        None,
            "token_price":      0.0,
            "yes_ask":          yes_ask,
            "no_ask":           no_ask,
            "implied_total":    implied_total,
            "edge_pct":         edge_pct,
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
