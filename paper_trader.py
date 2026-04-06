import asyncio
import logging
import time
from typing import Optional, Dict, Any

import aiohttp

import database
from config import PAPER_MODE

logger = logging.getLogger(__name__)

TAKER_FEE_PCT = 0.015  # 1.5%
BINANCE_PRICE_URL = "https://api.binance.com/api/v3/ticker/price"


async def fetch_btc_price(session: aiohttp.ClientSession) -> Optional[float]:
    try:
        async with session.get(
            BINANCE_PRICE_URL,
            params={"symbol": "BTCUSDT"},
            timeout=aiohttp.ClientTimeout(total=8),
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                return float(data["price"])
    except Exception as e:
        logger.warning(f"fetch_btc_price error: {e}")
    return None


class PaperTrader:
    def __init__(self, risk_manager, market_scanner):
        self.risk_manager = risk_manager
        self.market_scanner = market_scanner
        self.active_trade_id: Optional[int] = None
        self.active_trade: Optional[Dict[str, Any]] = None
        self._session: Optional[aiohttp.ClientSession] = None

    async def start(self):
        self._session = aiohttp.ClientSession()

    async def stop(self):
        if self._session:
            await self._session.close()

    async def execute_trade(
        self,
        direction: str,
        confidence: float,
        strategy_details: Dict[str, Any],
        open_price: float,
    ) -> Optional[Dict[str, Any]]:
        """
        Place a paper trade. Returns trade info dict or None if skipped.
        """
        if not PAPER_MODE:
            logger.warning("Not in paper mode — real trading not implemented")
            return None

        # Get realistic token price from orderbook
        if direction == "UP":
            token_price = await self.market_scanner.get_yes_price()
        else:
            token_price = await self.market_scanner.get_no_price()

        if token_price <= 0 or token_price >= 1:
            token_price = 0.5

        # Size the bet
        bet_usd = self.risk_manager.size_bet(confidence, token_price)
        if bet_usd is None:
            logger.info("Risk manager blocked trade")
            return None

        # Calculate shares and fee
        fee = round(bet_usd * TAKER_FEE_PCT, 4)
        effective_cost = bet_usd  # The bet already includes fee in paper mode
        shares = round(bet_usd / token_price, 4)

        strategies_str = ", ".join(
            k for k, v in strategy_details.items() if v is not None
        )

        bankroll_before = self.risk_manager.bankroll

        trade_id = database.save_trade(
            window_ts=self.market_scanner.current_window_ts,
            direction=direction,
            token_price=token_price,
            shares=shares,
            cost_usd=effective_cost,
            fee_usd=fee,
            bankroll_before=bankroll_before,
            confidence=confidence,
            strategies=strategies_str,
            open_price=open_price,
        )

        trade_info = {
            "id": trade_id,
            "direction": direction,
            "token_price": token_price,
            "shares": shares,
            "cost_usd": effective_cost,
            "fee_usd": fee,
            "bankroll_before": bankroll_before,
            "confidence": confidence,
            "open_price": open_price,
            "window_ts": self.market_scanner.current_window_ts,
        }

        self.active_trade_id = trade_id
        self.active_trade = trade_info

        # Deduct from bankroll immediately
        new_bankroll = round(bankroll_before - effective_cost, 4)
        self.risk_manager.update_bankroll(new_bankroll)

        logger.info(
            f"[PAPER] Trade #{trade_id}: {direction} @ {token_price:.4f} | "
            f"${effective_cost:.2f} ({shares:.4f} shares) | conf={confidence:.2f}"
        )
        return trade_info

    async def resolve_trade(self, open_price: float) -> Optional[Dict[str, Any]]:
        """
        Resolve the active trade. Wait 5 extra seconds, then fetch final BTC price.
        Returns result dict.
        """
        if self.active_trade_id is None or self.active_trade is None:
            return None

        logger.info("Waiting 5s before resolving trade...")
        await asyncio.sleep(5)

        close_price = await fetch_btc_price(self._session)
        if close_price is None:
            # Fallback: use current price from market data (passed via open_price param as current)
            close_price = open_price
            logger.warning("Could not fetch close price, using open as fallback")

        direction = self.active_trade["direction"]
        win = (
            (close_price > open_price and direction == "UP") or
            (close_price < open_price and direction == "DOWN")
        )

        trade = self.active_trade
        cost = trade["cost_usd"]
        fee = trade["fee_usd"]

        if win:
            # Payout: shares * 1.0 (binary market pays $1 per share) minus fee
            gross_payout = trade["shares"] * 1.0
            payout = round(gross_payout - fee, 4)
            pnl = round(payout - cost, 4)
            bankroll_after = round(self.risk_manager.bankroll + payout, 4)
        else:
            pnl = round(-cost, 4)
            bankroll_after = self.risk_manager.bankroll  # already deducted

        database.update_trade_result(
            trade_id=self.active_trade_id,
            win=win,
            pnl=pnl,
            bankroll_after=bankroll_after,
            close_price=close_price,
        )

        self.risk_manager.update_bankroll(bankroll_after)

        result = {
            "id": self.active_trade_id,
            "direction": direction,
            "win": win,
            "pnl": pnl,
            "bankroll_after": bankroll_after,
            "open_price": open_price,
            "close_price": close_price,
            "cost_usd": cost,
        }

        logger.info(
            f"[PAPER] Trade #{self.active_trade_id} resolved: "
            f"{'WIN' if win else 'LOSS'} | PnL=${pnl:+.2f} | "
            f"BTC {open_price:.2f} -> {close_price:.2f}"
        )

        self.active_trade_id = None
        self.active_trade = None

        return result
