"""
BITCOINSONT15 — BTC Up/Down 15-minute paper trading bot for Polymarket.

Trading loop timing:
  - Signal evaluated every 30 seconds (was 5s — reduced to avoid Gamma rate limits)
  - On FIRE: execute trade immediately (do NOT wait for end-of-window)
  - Safety net: skip execution if < 30 seconds remain in window
  - Once trade placed this window → stop evaluating until next window
"""

import asyncio
import logging
import signal
import sys
import time
from datetime import datetime

import database
from config import INITIAL_BANKROLL, MIN_CONFIDENCE, MAX_POSITION_PCT, WINDOW_SECONDS
from market_scanner import MarketScanner
from market_data import MarketData
from signal_engine import SignalEngine
from risk_manager import RiskManager
from paper_trader import PaperTrader
from dashboard import Dashboard
from telegram_alerts import TelegramAlerter
from shared_state import SharedState
from web_dashboard import start_web_dashboard

# ── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler("bot.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
logging.getLogger("signal_engine").setLevel(logging.DEBUG)
logger = logging.getLogger("main")

# Seconds before window end below which we refuse to place a new trade
MIN_SECONDS_REMAINING = 30

# How often the trading loop evaluates the signal (seconds)
SIGNAL_EVAL_INTERVAL = 30


# ── Bot state ────────────────────────────────────────────────────────────────

class BotState:
    def __init__(self):
        self.running                  = True
        self.current_window_ts        = 0
        self.window_open_price        = 0.0
        self.trade_placed_this_window = False
        self.active_trade             = None
        # last_signal: holds the most recent FIRE (never overwritten by a SKIP)
        self.last_signal              = None
        # tracks when we last ran signal evaluation
        self._last_eval_ts: float     = 0.0


# ── Dashboard / shared-state refresh (1 Hz) ───────────────────────────────────

async def dashboard_loop(
    dashboard:    Dashboard,
    market_data:  MarketData,
    scanner:      MarketScanner,
    risk_manager: RiskManager,
    bot_state:    BotState,
    shared_state: SharedState,
):
    while bot_state.running:
        try:
            snap        = market_data.snapshot()
            risk_status = risk_manager.status()

            dashboard.update_from_market(snap)
            dashboard.update_from_scanner(scanner)
            dashboard.update_from_risk(risk_status)
            if bot_state.last_signal:
                dashboard.update_from_signal(bot_state.last_signal)
            dashboard.update(active_trade=bot_state.active_trade)

            shared_state.update_price(
                price             = snap.get("price", 0),
                window_open_price = snap.get("window_open_price", 0),
                delta_pct         = snap.get("delta_pct", 0),
                delta_1min        = snap.get("delta_1min", 0),
                window_high       = snap.get("window_high", 0),
                window_low        = snap.get("window_low", 0),
                volume            = snap.get("volume", 0),
                rsi               = snap.get("rsi"),
                vwap              = snap.get("vwap"),
            )
            shared_state.update_window(
                window_ts      = scanner.current_window_ts,
                slug           = scanner.current_slug,
                time_remaining = scanner.time_remaining(),
                progress       = scanner.window_progress(),
            )
            shared_state.update_risk(
                bankroll       = risk_status["bankroll"],
                cb_active      = risk_status["circuit_breaker_active"],
                cb_remaining   = risk_status["circuit_breaker_remaining"],
            )
            shared_state.update_active_trade(bot_state.active_trade)
            if bot_state.last_signal:
                shared_state.update_signal(bot_state.last_signal)
            with shared_state._lock:
                shared_state.data_source = snap.get("source", "unknown")

        except Exception as e:
            logger.warning(f"Dashboard update error: {e}")

        await asyncio.sleep(1)


# ── Stats refresh (every 10 s) ────────────────────────────────────────────────

async def stats_refresh_loop(
    dashboard:    Dashboard,
    bot_state:    BotState,
    shared_state: SharedState,
):
    while bot_state.running:
        try:
            stats  = database.get_stats()
            recent = database.get_last_n_trades(10)
            dashboard.update_trades(stats, recent)
            shared_state.update_stats(stats, recent)
        except Exception as e:
            logger.warning(f"Stats refresh error: {e}")
        await asyncio.sleep(10)


# ── Main trading loop ─────────────────────────────────────────────────────────

async def main_trading_loop(
    scanner:       MarketScanner,
    market_data:   MarketData,
    signal_engine: SignalEngine,
    risk_manager:  RiskManager,
    paper_trader:  PaperTrader,
    telegram:      TelegramAlerter,
    bot_state:     BotState,
    shared_state:  SharedState = None,
):
    logger.info("Starting main trading loop")

    while bot_state.running:
        try:
            now = time.time()

            # ── Window detection ──────────────────────────────────────────────
            from market_scanner import current_window_ts
            wts = current_window_ts()

            if wts != bot_state.current_window_ts:
                logger.info(
                    f"=== New window: {wts} "
                    f"({datetime.fromtimestamp(wts).strftime('%H:%M:%S')}) ==="
                )

                # Resolve previous window trade
                if bot_state.active_trade:
                    logger.info("Resolving previous window trade…")
                    result = await paper_trader.resolve_trade(
                        bot_state.window_open_price
                    )
                    if result:
                        had_signal = True
                        if result["win"]:
                            signal_engine.record_win(had_signal)
                        else:
                            signal_engine.record_loss()
                        bot_state.active_trade = None
                        await telegram.trade_resolved(result, risk_manager.bankroll)
                        stats = database.get_stats()
                        await telegram.daily_summary(stats, risk_manager.bankroll)

                # Reset window state
                bot_state.current_window_ts        = wts
                bot_state.trade_placed_this_window = False
                bot_state.last_signal              = None
                bot_state._last_eval_ts            = 0.0   # force eval on first loop

                await scanner.refresh()

                # Wait up to 15 s for market data price
                for _ in range(15):
                    if market_data.current_price > 0:
                        break
                    await asyncio.sleep(1)

                bot_state.window_open_price = market_data.current_price
                market_data.set_window_open(wts)
                if shared_state is not None:
                    shared_state.new_window(wts, bot_state.window_open_price)
                logger.info(
                    f"Window open price: ${bot_state.window_open_price:,.2f}"
                )

            # ── Compute timing ────────────────────────────────────────────────
            elapsed         = int(now) - wts
            time_remaining  = max(0, WINDOW_SECONDS - elapsed)
            minutes_elapsed = elapsed / 60.0

            # ── Signal evaluation every SIGNAL_EVAL_INTERVAL seconds ──────────
            # Only evaluate:
            #   • after minute 3 (signal_engine has its own gate anyway)
            #   • when no trade has been placed yet this window
            #   • when the eval interval has elapsed
            secs_since_eval = now - bot_state._last_eval_ts
            should_eval = (
                elapsed >= 60                           # minimum T+1min
                and not bot_state.trade_placed_this_window
                and secs_since_eval >= SIGNAL_EVAL_INTERVAL
            )

            if should_eval:
                bot_state._last_eval_ts = now
                snap   = market_data.snapshot()
                signal = await signal_engine.evaluate(snap, minutes_elapsed)

                # ── Preserve FIRE, never overwrite with SKIP ──────────────────
                # This was the original bug: re-evaluation at T+14.0 would
                # overwrite the T+13.9 FIRE with a "too_late" SKIP, so the
                # trade was never placed.
                if signal.get("direction") is not None:
                    bot_state.last_signal = signal          # FIRE → always save
                elif bot_state.last_signal is None:
                    bot_state.last_signal = signal          # first result for dashboard

                # ── Execute immediately on FIRE ───────────────────────────────
                if (
                    signal.get("direction") is not None
                    and time_remaining > MIN_SECONDS_REMAINING
                    and not bot_state.trade_placed_this_window
                ):
                    urgency = " [URGENT]" if signal.get("urgent") else ""
                    logger.info(
                        f"FIRE @ T+{minutes_elapsed:.1f}min "
                        f"({time_remaining}s remaining){urgency} → "
                        f"executing {signal['direction']} immediately"
                    )

                    trade = await paper_trader.execute_trade(
                        direction        = signal["direction"],
                        confidence       = signal["confidence"],
                        strategy_details = signal.get("strategy_details", {}),
                        open_price       = bot_state.window_open_price,
                        token_price      = signal.get("token_price"),
                        force_min_bet    = signal.get("force_min_bet", False),
                        edge_pct         = signal.get("edge_pct", 0.0),
                    )

                    if trade:
                        bot_state.active_trade             = trade
                        bot_state.trade_placed_this_window = True
                        await telegram.trade_executed(trade)
                        logger.info(
                            f"Trade placed: #{trade['id']} "
                            f"dir={trade['direction']} "
                            f"token_mid=${signal.get('token_price', 0):.4f} "
                            f"edge={signal.get('edge_pct', 0)*100:.1f}% "
                            f"cost=${trade.get('cost_usd', 0):.2f} "
                            f"@ T+{minutes_elapsed:.1f}min"
                        )

                elif (
                    signal.get("direction") is not None
                    and time_remaining <= MIN_SECONDS_REMAINING
                ):
                    logger.warning(
                        f"FIRE @ T+{minutes_elapsed:.1f}min but only "
                        f"{time_remaining}s left — too late to execute"
                    )

            # Sleep 5 s between loop ticks (dashboard / window detection still fast)
            await asyncio.sleep(5)

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.exception(f"Trading loop error: {e}")
            await asyncio.sleep(10)


# ── Entry point ───────────────────────────────────────────────────────────────

async def main():
    logger.info("=" * 60)
    logger.info("  BITCOINSONT15 — BTC Paper Trading Bot")
    logger.info("=" * 60)

    database.init_db()

    # Resume bankroll from last session
    last_trades = database.get_last_n_trades(1)
    if last_trades and last_trades[0].get("bankroll_after"):
        current_bankroll = last_trades[0]["bankroll_after"]
        logger.info(f"Resuming with bankroll: ${current_bankroll:.2f}")
    else:
        current_bankroll = INITIAL_BANKROLL
        logger.info(f"Starting fresh with bankroll: ${current_bankroll:.2f}")

    # Initialise components
    shared_state  = SharedState(initial_bankroll=INITIAL_BANKROLL)
    scanner       = MarketScanner()
    market_data   = MarketData()
    signal_engine = SignalEngine(scanner=scanner, min_confidence=MIN_CONFIDENCE)
    risk_manager  = RiskManager(current_bankroll, MAX_POSITION_PCT)
    paper_trader  = PaperTrader(risk_manager, scanner)
    dashboard     = Dashboard(INITIAL_BANKROLL)
    telegram      = TelegramAlerter()
    bot_state     = BotState()

    # Web dashboard (background daemon thread)
    start_web_dashboard(shared_state)

    # Start async components
    await scanner.start()
    await market_data.start()
    await paper_trader.start()
    await telegram.start()

    # Seed dashboard
    initial_stats = database.get_stats()
    recent_trades = database.get_last_n_trades(10)
    dashboard.update_trades(initial_stats, recent_trades)
    dashboard.update(bankroll=current_bankroll)
    shared_state.update_stats(initial_stats, recent_trades)
    shared_state.update_risk(current_bankroll, False, 0)
    dashboard.start()

    # Graceful shutdown on SIGINT / SIGTERM
    def _shutdown(sig, frame):
        logger.info("Shutdown signal received")
        bot_state.running = False

    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    tasks = [
        asyncio.create_task(
            dashboard_loop(
                dashboard, market_data, scanner,
                risk_manager, bot_state, shared_state,
            )
        ),
        asyncio.create_task(
            stats_refresh_loop(dashboard, bot_state, shared_state)
        ),
        asyncio.create_task(
            main_trading_loop(
                scanner, market_data, signal_engine, risk_manager,
                paper_trader, telegram, bot_state, shared_state,
            )
        ),
    ]

    try:
        await asyncio.gather(*tasks)
    except (asyncio.CancelledError, KeyboardInterrupt):
        pass
    finally:
        logger.info("Shutting down…")
        bot_state.running = False
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await scanner.stop()
        await market_data.stop()
        await paper_trader.stop()
        await telegram.stop()
        await signal_engine.close()
        dashboard.stop()
        logger.info("BITCOINSONT15 stopped.")


if __name__ == "__main__":
    asyncio.run(main())
