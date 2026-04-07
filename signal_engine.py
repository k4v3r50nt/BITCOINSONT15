"""
BITCOINSONT15 — Signal Engine v4: Mispricing Hunter + Biased Market

Two conditions generate a trade:
  A) MISPRICING:  implied_ask  = yes_ask + no_ask  < 0.98
                  (market maker left edge on the table)
  B) BIASED MKT:  any mid price <= 0.44
                  (market prices one side at ≤44% → buying it has +EV)

Price fetch chain (per token):
  1. CLOB /books?token_id=  → best ask from asks[0]["price"]
     (two requests: one for YES, one for NO)
  2. Gamma /markets?clob_token_ids= → outcomePrices (mid prices)
     ask estimate = mid + 0.02  (conservative spread)
  3. Default 0.50 / 0.50
"""

import json
import logging
import time
from typing import Optional, Dict, Any, List, Tuple

import aiohttp

logger = logging.getLogger(__name__)

# ── Thresholds ────────────────────────────────────────────────────────────────

IMPLIED_ASK_FLOOR      = 0.98   # implied_ask below this → MISPRICING trade
MAX_TOKEN_ASK          = 0.49   # don't buy a side above this price
MIN_EDGE_PCT           = 0.01   # minimum edge 1%

BIASED_MID_THRESHOLD   = 0.44   # mid ≤ this → BIASED MARKET trade
GAMMA_SPREAD_ESTIMATE  = 0.02   # estimated ask spread added to gamma mid prices

TRADE_WINDOW_MIN_START = 1.0    # earliest minute in window to trade
TRADE_WINDOW_MIN_END   = 14.0   # latest minute

# Circuit breaker: market stays efficient for this long → min-bet mode
CB_EFFICIENT_THRESHOLD = 0.98
CB_EFFICIENT_MINUTES   = 30
CB_AUTO_RESET_MINUTES  = 45     # auto-reset CB N minutes after last loss

CONFIDENCE_SCALE_GAP   = 0.10   # 10¢ gap below floor → confidence = 1.0

# Endpoints
CLOB_BASE  = "https://clob.polymarket.com"
GAMMA_BASE = "https://gamma-api.polymarket.com"


class SignalEngine:
    def __init__(self, scanner, min_confidence: float = 0.0):
        """
        scanner      — MarketScanner; used only for scanner.tokens {"YES": id, "NO": id}
        min_confidence — minimum confidence to fire a trade (0.0 = any positive edge)
        """
        self.scanner        = scanner
        self.min_confidence = min_confidence

        self._implied_history: List[Dict] = []
        self._consecutive_wins_with_mispricing: int = 0
        self._last_loss_ts: float = 0.0
        self._session: Optional[aiohttp.ClientSession] = None

        # Track last source used (for logging / debugging)
        self._last_price_source: str = "none"

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

    # ── Price fetch layer ─────────────────────────────────────────────────────

    async def _clob_best_ask(self, token_id: str) -> Optional[float]:
        """
        GET /books?token_id={token_id}
        Asks are sorted ascending; asks[0] is the cheapest (best ask for buyer).
        Returns None on failure.
        """
        session = await self._get_session()
        try:
            url = f"{CLOB_BASE}/books"
            async with session.get(
                url,
                params={"token_id": token_id},
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                if resp.status != 200:
                    logger.debug(f"[Signal] CLOB /books HTTP {resp.status} for {token_id[:8]}…")
                    return None
                data = await resp.json()
                asks = data.get("asks", [])
                if not asks:
                    logger.debug(f"[Signal] CLOB /books: empty asks for {token_id[:8]}…")
                    return None
                # asks sorted ascending → first element = best ask
                best = min(asks, key=lambda a: float(a.get("price", 1)))
                price = float(best["price"])
                logger.debug(f"[Signal] CLOB best ask for {token_id[:8]}… = {price:.4f}")
                return price
        except Exception as e:
            logger.warning(f"[Signal] CLOB /books error for {token_id[:8]}…: {e}")
            return None

    async def _gamma_prices(
        self, yes_id: str
    ) -> Optional[Tuple[float, float]]:
        """
        GET /markets?clob_token_ids={yes_id}
        Returns (yes_mid, no_mid) or None.
        These are MID prices — callers must add GAMMA_SPREAD_ESTIMATE.
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
                    logger.debug(f"[Signal] Gamma HTTP {resp.status}")
                    return None
                data = await resp.json()
                markets = data if isinstance(data, list) else data.get("markets", [])
                if not markets:
                    logger.debug("[Signal] Gamma: no markets returned")
                    return None
                prices = markets[0].get("outcomePrices", "[]")
                if isinstance(prices, str):
                    prices = json.loads(prices)
                if isinstance(prices, list) and len(prices) >= 2:
                    y, n = float(prices[0]), float(prices[1])
                    logger.debug(f"[Signal] Gamma mids: YES={y:.4f} NO={n:.4f}")
                    return y, n
                return None
        except Exception as e:
            logger.warning(f"[Signal] Gamma error: {e}")
            return None

    async def _fetch_prices(
        self, yes_id: str, no_id: str
    ) -> Dict[str, Any]:
        """
        3-level fallback chain.
        Returns dict with keys: yes_ask, no_ask, yes_mid, no_mid, source
          source ∈ {"clob", "gamma", "default"}
          yes_mid / no_mid are None when source == "clob"
        """
        # ── 1. CLOB /books (real ask prices, two requests) ────────────────────
        yes_ask = await self._clob_best_ask(yes_id)
        no_ask  = await self._clob_best_ask(no_id)

        if yes_ask is not None and no_ask is not None:
            self._last_price_source = "clob"
            logger.info(
                f"[Signal] CLOB asks — YES={yes_ask:.4f} NO={no_ask:.4f} "
                f"implied={yes_ask+no_ask:.4f}"
            )
            return {
                "yes_ask":  yes_ask,
                "no_ask":   no_ask,
                "yes_mid":  None,
                "no_mid":   None,
                "source":   "clob",
            }

        # ── 2. Gamma mid prices + spread estimate ─────────────────────────────
        gamma = await self._gamma_prices(yes_id)
        if gamma is not None:
            yes_mid, no_mid = gamma
            yes_ask_est = round(yes_mid + GAMMA_SPREAD_ESTIMATE, 4)
            no_ask_est  = round(no_mid  + GAMMA_SPREAD_ESTIMATE, 4)
            self._last_price_source = "gamma"
            logger.info(
                f"[Signal] Gamma mids={yes_mid:.4f}/{no_mid:.4f} "
                f"→ ask_est={yes_ask_est:.4f}/{no_ask_est:.4f} "
                f"implied_ask={yes_ask_est+no_ask_est:.4f}"
            )
            return {
                "yes_ask":  yes_ask_est,
                "no_ask":   no_ask_est,
                "yes_mid":  yes_mid,
                "no_mid":   no_mid,
                "source":   "gamma",
            }

        # ── 3. Default (both endpoints down) ─────────────────────────────────
        print("[SIGNAL] PRECIO DEFAULT — todos los endpoints fallaron, usando 0.50/0.50")
        logger.warning("[Signal] All price endpoints failed — using 0.50/0.50 default")
        self._last_price_source = "default"
        return {
            "yes_ask": 0.50,
            "no_ask":  0.50,
            "yes_mid": 0.50,
            "no_mid":  0.50,
            "source":  "default",
        }

    # ── Main evaluation ───────────────────────────────────────────────────────

    async def evaluate(
        self, snapshot: Dict[str, Any], minutes_elapsed: float
    ) -> Dict[str, Any]:
        """
        Evaluates whether to trade in the current window minute.
        Returns signal dict compatible with paper_trader and shared_state.
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

        # ── Fetch prices ──────────────────────────────────────────────────────
        p = await self._fetch_prices(yes_id, no_id)
        yes_ask  = p["yes_ask"]
        no_ask   = p["no_ask"]
        yes_mid  = p["yes_mid"]   # None when source == "clob"
        no_mid   = p["no_mid"]
        source   = p["source"]

        implied_ask = round(yes_ask + no_ask, 4)

        # ── Circuit breaker auto-reset ────────────────────────────────────────
        cb_active = self._market_too_efficient()
        if cb_active and self._last_loss_ts > 0:
            mins_since_loss = (time.time() - self._last_loss_ts) / 60.0
            if mins_since_loss >= CB_AUTO_RESET_MINUTES:
                logger.info(
                    f"[Signal] Auto-resetting CB "
                    f"({mins_since_loss:.0f}min desde último loss)"
                )
                self._implied_history.clear()
                cb_active = False

        # ── Diagnostic prints ─────────────────────────────────────────────────
        print(
            f"[SIGNAL] src={source} | "
            f"YES ask={yes_ask:.4f} | NO ask={no_ask:.4f} | "
            f"implied={implied_ask:.4f}"
        )
        if yes_mid is not None:
            print(
                f"[SIGNAL] Gamma mids → YES={yes_mid:.4f} NO={no_mid:.4f}"
            )
        print(
            f"[SIGNAL] Minuto ventana: {minutes_elapsed:.1f} | "
            f"Circuit breaker: {cb_active}"
        )

        # ── Timing gate ───────────────────────────────────────────────────────
        if minutes_elapsed < TRADE_WINDOW_MIN_START:
            reason = f"too_early_{minutes_elapsed:.1f}min"
            print(f"[SIGNAL] Edge: — | Decision: {reason}")
            return self._skip(reason, yes_ask=yes_ask, no_ask=no_ask,
                              implied_total=implied_ask)

        if minutes_elapsed > TRADE_WINDOW_MIN_END:
            reason = f"too_late_{minutes_elapsed:.1f}min"
            print(f"[SIGNAL] Edge: — | Decision: {reason}")
            return self._skip(reason, yes_ask=yes_ask, no_ask=no_ask,
                              implied_total=implied_ask)

        # Record for CB history
        self._record_implied(implied_ask)

        # ═════════════════════════════════════════════════════════════════════
        # CONDITION A — MISPRICING: implied_ask < IMPLIED_ASK_FLOOR (0.98)
        # ═════════════════════════════════════════════════════════════════════
        signal_a = None
        if implied_ask < IMPLIED_ASK_FLOOR:
            direction, token_price = self._pick_cheaper_side(yes_ask, no_ask)
            if direction is not None:
                edge_pct   = round(0.50 - token_price, 4)
                gap        = round(IMPLIED_ASK_FLOOR - implied_ask, 4)
                confidence = round(min(1.0, gap / CONFIDENCE_SCALE_GAP), 4)

                if edge_pct >= MIN_EDGE_PCT and confidence >= self.min_confidence:
                    signal_a = {
                        "direction":   direction,
                        "token_price": token_price,
                        "edge_pct":    edge_pct,
                        "confidence":  confidence,
                        "reason":      "MISPRICING",
                    }
                    print(
                        f"[SIGNAL] ✓ MISPRICING | {direction} @ {token_price:.4f} "
                        f"edge={edge_pct*100:.1f}% conf={confidence:.2f}"
                    )

        # ═════════════════════════════════════════════════════════════════════
        # CONDITION B — BIASED MARKET: any mid ≤ 0.44 (only when Gamma/default)
        # ═════════════════════════════════════════════════════════════════════
        signal_b = None
        if yes_mid is not None and no_mid is not None:
            biased_dir   = None
            biased_mid   = None
            biased_ask   = None

            if yes_mid <= BIASED_MID_THRESHOLD:
                biased_dir = "YES"
                biased_mid = yes_mid
                biased_ask = yes_ask
            elif no_mid <= BIASED_MID_THRESHOLD:
                biased_dir = "NO"
                biased_mid = no_mid
                biased_ask = no_ask

            if biased_dir is not None and biased_ask < MAX_TOKEN_ASK:
                edge_pct   = round(0.50 - biased_mid, 4)
                confidence = round(min(1.0, edge_pct * 10), 4)

                if edge_pct >= MIN_EDGE_PCT and confidence >= self.min_confidence:
                    signal_b = {
                        "direction":   biased_dir,
                        "token_price": biased_ask,
                        "edge_pct":    edge_pct,
                        "confidence":  confidence,
                        "reason":      "BIASED_MKT",
                    }
                    print(
                        f"[SIGNAL] ✓ BIASED_MKT | {biased_dir} "
                        f"mid={biased_mid:.4f} ask={biased_ask:.4f} "
                        f"edge={edge_pct*100:.1f}%"
                    )

        # ── Select best signal (prefer higher confidence) ─────────────────────
        chosen = None
        if signal_a and signal_b:
            chosen = signal_a if signal_a["confidence"] >= signal_b["confidence"] else signal_b
            print(f"[SIGNAL] Both A+B fired → chose {chosen['reason']}")
        elif signal_a:
            chosen = signal_a
        elif signal_b:
            chosen = signal_b

        if chosen is None:
            reason = (
                f"no_signal implied={implied_ask:.4f} "
                f"yes_mid={yes_mid!r} no_mid={no_mid!r}"
            )
            print(f"[SIGNAL] Edge: — | Decision: SKIP ({reason})")
            return self._skip(reason, yes_ask=yes_ask, no_ask=no_ask,
                              implied_total=implied_ask)

        # ── Build final result ────────────────────────────────────────────────
        direction   = chosen["direction"]
        token_price = chosen["token_price"]
        edge_pct    = chosen["edge_pct"]
        confidence  = chosen["confidence"]
        force_min_bet = cb_active

        ev_per_10 = round((edge_pct / token_price) * 10, 2) if token_price > 0 else 0.0

        print(
            f"[SIGNAL] Edge: {edge_pct*100:.2f}% | "
            f"EV=+${ev_per_10:.2f}/10$ | "
            f"Decision: FIRE {direction} ({chosen['reason']})"
        )
        logger.info(
            f"[Signal] FIRE {direction} | token={token_price:.4f} "
            f"edge={edge_pct*100:.1f}% conf={confidence:.2f} "
            f"implied={implied_ask:.4f} reason={chosen['reason']}"
        )

        return {
            "direction":        direction,
            "token_price":      token_price,
            "yes_ask":          yes_ask,
            "no_ask":           no_ask,
            "implied_total":    implied_ask,
            "edge_pct":         edge_pct,
            "confidence":       confidence,
            "force_min_bet":    force_min_bet,
            "skip_reason":      None,
            # compat fields
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
                f"[Signal] Racha mispricing: "
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

    def _pick_cheaper_side(
        self, yes_ask: float, no_ask: float
    ) -> Tuple[Optional[str], Optional[float]]:
        """Return (direction, price) for the cheaper valid side."""
        yes_ok = yes_ask <= no_ask and yes_ask < MAX_TOKEN_ASK
        no_ok  = no_ask  <  yes_ask and no_ask  < MAX_TOKEN_ASK
        if yes_ok:
            return "YES", yes_ask
        if no_ok:
            return "NO", no_ask
        return None, None

    def _record_implied(self, implied: float):
        now = time.time()
        self._implied_history.append({"ts": now, "implied": implied})
        cutoff = now - CB_EFFICIENT_MINUTES * 60
        self._implied_history = [
            r for r in self._implied_history if r["ts"] >= cutoff
        ]

    def _market_too_efficient(self) -> bool:
        """True when implied has stayed ≥ CB_EFFICIENT_THRESHOLD for the full CB window."""
        if len(self._implied_history) < 3:
            return False
        oldest = self._implied_history[0]
        window_seconds = time.time() - oldest["ts"]
        if window_seconds < CB_EFFICIENT_MINUTES * 60:
            return False
        return all(
            r["implied"] >= CB_EFFICIENT_THRESHOLD
            for r in self._implied_history
        )

    @staticmethod
    def _skip(
        reason: str,
        yes_ask:      float = 0.0,
        no_ask:       float = 0.0,
        implied_total: float = 0.0,
        edge_pct:     float = 0.0,
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
