"""
BITCOINSONT15 — Paper Trader v2: Mispricing Hunter

P&L model for binary Polymarket tokens:
  - You buy N shares of YES (or NO) at token_price per share
  - If you win: each share pays out $1.00 → gross = N * 1.00
  - If you lose: shares expire worthless → gross = 0
  - Net PnL = gross - cost_usd - fee

EV example: buy YES at $0.44, fair win_rate = 50%
  EV = 0.50 * (1.00 - 0.44) - 0.50 * 0.44 = +$0.06 per share
"""

import asyncio
import logging
import time
from typing import Optional, Dict, Any

import aiohttp

import database
import state_manager
from config import PAPER_MODE

logger = logging.getLogger(__name__)

TAKER_FEE_PCT = 0.015   # 1.5% Polymarket taker fee

# BTC close price sources (same stack as market_data.py — cloud-safe)
KRAKEN_URL  = "https://api.kraken.com/0/public/Ticker"
MEMPOOL_URL = "https://mempool.space/api/v1/prices"

MIN_BET_USD = 2.50


async def fetch_btc_close(session: aiohttp.ClientSession) -> Optional[float]:
    """Fetch BTC/USD close price at window resolution time."""
    # Primary: Kraken
    try:
        async with session.get(
            KRAKEN_URL,
            params={"pair": "XBTUSD"},
            timeout=aiohttp.ClientTimeout(total=8),
            headers={"User-Agent": "BITCOINSONT15/1.0"},
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                if not data.get("error"):
                    return float(data["result"]["XXBTZUSD"]["c"][0])
    except Exception as e:
        logger.warning(f"Kraken close price error: {e}")

    # Fallback: Mempool.space
    try:
        async with session.get(
            MEMPOOL_URL,
            timeout=aiohttp.ClientTimeout(total=8),
            headers={"User-Agent": "BITCOINSONT15/1.0"},
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                return float(data["USD"])
    except Exception as e:
        logger.warning(f"Mempool close price error: {e}")

    return None


class PaperTrader:
    def __init__(self, risk_manager, market_scanner):
        self.risk_manager    = risk_manager
        self.market_scanner  = market_scanner
        self.active_trade_id: Optional[int]       = None
        self.active_trade:   Optional[Dict[str, Any]] = None
        self._session:       Optional[aiohttp.ClientSession] = None

    async def start(self):
        self._session = aiohttp.ClientSession()

    async def stop(self):
        if self._session:
            await self._session.close()

    # ── Execute ───────────────────────────────────────────────────────────────

    async def execute_trade(
        self,
        direction: str,           # "YES" or "NO"
        confidence: float,
        strategy_details: Dict[str, Any],
        open_price: float,        # BTC price at window open
        token_price: Optional[float] = None,   # pre-fetched from signal
        force_min_bet: bool = False,
        edge_pct: float = 0.0,    # signal edge % (token_mid - 0.50)
    ) -> Optional[Dict[str, Any]]:

        if not PAPER_MODE:
            logger.warning("Not in paper mode — skipping")
            return None

        # ── Token price ───────────────────────────────────────────────────────
        if token_price is None or token_price <= 0 or token_price >= 1:
            # Re-fetch if signal didn't provide it
            if direction == "YES":
                token_price = await self.market_scanner.get_yes_price()
            else:
                token_price = await self.market_scanner.get_no_price()

        if token_price <= 0 or token_price >= 1:
            logger.warning(f"Invalid token price {token_price} — skipping")
            return None

        # ── Position sizing (delegates all CB/floor logic to RiskManager) ─────
        bet_usd = self.risk_manager.size_bet(
            confidence    = confidence,
            token_price   = token_price,
            force_min_bet = force_min_bet,
        )
        if bet_usd is None:
            logger.info("[PaperTrader] RiskManager blocked trade — ver logs [RISK]")
            return None

        bet_usd = max(MIN_BET_USD, round(bet_usd, 2))

        if bet_usd > self.risk_manager.bankroll:
            logger.warning("[PaperTrader] Insufficient bankroll")
            return None

        # ── Cost breakdown ────────────────────────────────────────────────────
        fee        = round(bet_usd * TAKER_FEE_PCT, 4)
        cost_usd   = bet_usd                          # total cash out of pocket
        shares     = round(bet_usd / token_price, 4)  # binary shares purchased
        # Max possible payout: shares * $1.00 - fee
        max_payout = round(shares * 1.0 - fee, 4)
        max_profit = round(max_payout - cost_usd, 4)

        strategies_str = ", ".join(
            k for k, v in strategy_details.items() if v is not None
        ) or "mispricing_hunter"

        bankroll_before = self.risk_manager.bankroll

        trade_id = database.save_trade(
            window_ts    = self.market_scanner.current_window_ts,
            direction    = direction,
            token_price  = token_price,
            shares       = shares,
            cost_usd     = cost_usd,
            fee_usd      = fee,
            bankroll_before = bankroll_before,
            confidence   = confidence,
            strategies   = strategies_str,
            open_price   = open_price,
        )

        trade_info = {
            "id":              trade_id,
            "direction":       direction,
            "token_price":     token_price,
            "shares":          shares,
            "cost_usd":        cost_usd,
            "fee_usd":         fee,
            "max_payout":      max_payout,
            "max_profit":      max_profit,
            "bankroll_before": bankroll_before,
            "confidence":      confidence,
            "edge_pct":        edge_pct,           # ← real edge for display
            "open_price":      open_price,
            "window_ts":       self.market_scanner.current_window_ts,
        }

        self.active_trade_id = trade_id
        self.active_trade    = trade_info

        # Deduct cost from bankroll immediately
        new_bankroll = round(bankroll_before - cost_usd, 4)
        self.risk_manager.update_bankroll(new_bankroll)

        # Persist bankroll so restarts resume from here
        stats = database.get_stats()
        state_manager.save_state(
            bankroll     = new_bankroll,
            total_trades = stats.get("total", 0),
            wins         = stats.get("wins", 0),
            losses       = stats.get("losses", 0),
        )

        logger.info(
            f"[PAPER] Trade #{trade_id}: {direction} token @ {token_price:.4f} | "
            f"${cost_usd:.2f} → {shares:.4f} shares | "
            f"max_profit=${max_profit:+.4f} | conf={confidence:.2f}"
        )
        return trade_info

    # ── Resolve ───────────────────────────────────────────────────────────────

    async def resolve_trade(self, open_price: float) -> Optional[Dict[str, Any]]:
        """
        Resolve the active trade 5s after window close.
        Win condition: direction matches actual BTC move (open vs close).
          YES = BTC went up  (close > open)
          NO  = BTC went down (close < open)
        """
        if self.active_trade_id is None or self.active_trade is None:
            return None

        logger.info("[PAPER] Waiting 5s for final BTC price...")
        await asyncio.sleep(5)

        close_price = await fetch_btc_close(self._session)
        if close_price is None:
            close_price = open_price
            logger.warning("[PAPER] Close price unavailable — using open as fallback")

        direction = self.active_trade["direction"]

        # Determine outcome
        if close_price > open_price:
            winner = "YES"
        elif close_price < open_price:
            winner = "NO"
        else:
            # Exact tie is extremely rare — treat as loss (conservative)
            winner = "PUSH"

        win = direction == winner

        trade   = self.active_trade
        shares  = trade["shares"]
        cost    = trade["cost_usd"]
        fee     = trade["fee_usd"]

        if win:
            # Binary payout: each share redeems for $1.00
            gross_payout  = round(shares * 1.0, 4)
            net_payout    = round(gross_payout - fee, 4)
            pnl           = round(net_payout - cost, 4)
            bankroll_after = round(self.risk_manager.bankroll + net_payout, 4)
        else:
            # Shares expire worthless
            pnl            = round(-cost, 4)
            bankroll_after = self.risk_manager.bankroll   # cost already deducted

        database.update_trade_result(
            trade_id       = self.active_trade_id,
            win            = win,
            pnl            = pnl,
            bankroll_after = bankroll_after,
            close_price    = close_price,
        )

        self.risk_manager.update_bankroll(bankroll_after)

        # Persist resolved bankroll
        stats = database.get_stats()
        state_manager.save_state(
            bankroll     = bankroll_after,
            total_trades = stats.get("total", 0),
            wins         = stats.get("wins", 0),
            losses       = stats.get("losses", 0),
        )

        result = {
            "id":           self.active_trade_id,
            "direction":    direction,
            "winner":       winner,
            "win":          win,
            "pnl":          pnl,
            "bankroll_after": bankroll_after,
            "open_price":   open_price,
            "close_price":  close_price,
            "cost_usd":     cost,
            "shares":       shares,
            "token_price":  trade["token_price"],
        }

        logger.info(
            f"[PAPER] Trade #{self.active_trade_id} resolved: "
            f"{'WIN ✓' if win else 'LOSS ✗'} | "
            f"Bought {direction} @ {trade['token_price']:.4f} | "
            f"Winner={winner} | "
            f"BTC {open_price:,.2f} → {close_price:,.2f} | "
            f"PnL=${pnl:+.4f} | Bankroll=${bankroll_after:.2f}"
        )

        self.active_trade_id = None
        self.active_trade    = None
        return result
