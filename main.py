"""
BITCOINSONT15 — BTC Up/Down 15-minute paper trading bot for Polymarket.

Trade execution flow (fixed):
  - Every 5 seconds: evaluate signal
  - If FIRE and time_remaining > 30s → execute trade IMMEDIATELY
  - Do NOT wait until T+890s; that caused late signals to be overwritten by SKIPs
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
# signal_engine logs at DEBUG so every decision is visible in bot.log
logging.getLogger("signal_engine").setLevel(logging.DEBUG)
logger = logging.getLogger("main")

# Safety buffer: don't place trades in the last N seconds of a window
MIN_SECONDS_REMAINING = 30


# ── Global state ─────────────────────────────────────────────────────────────

class BotState:
    def __init__(self):
        self.running                  = True
        self.current_window_ts        = 0
        self.window_open_price        = 0.0
        self.trade_placed_this_window = False
        self.active_trade             = None
        # last_signal: stores the most recent FIRE signal (never overwritten by SKIP)
        self.last_signal              = None


# ── Dashboard / shared-state refresh loop ────────────────────────────────────

async def dashboard_loop(
    dashboard:    Dashboard,
    market_data:  MarketData,
    scanner:      MarketScanner,
    risk_manager: RiskManager,
    bot_state:    BotState,
    shared_state: SharedState,
):
    """Updates terminal dashboard and web shared state every second."""
    while bot_state.running:
        try:
            snap         = market_data.snapshot()
            risk_status  = risk_manager.status()

            dashboard.update_from_market(snap)
            dashboard.update_from_scanner(scanner)
            dashboard.update_from_risk(risk_status)

            if bot_state.last_signal:
                dashboard.update_from_signal(bot_state.last_signal)
            dashboard.update(active_trade=bot_state.active_trade)

            # ── Push to web shared state ──────────────────────────────────
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
                window_ts     = scanner.current_window_ts,
                slug          = scanner.current_slug,
                time_remaining= scanner.time_remaining(),
                progress      = scanner.window_progress(),
            )
            shared_state.update_risk(
                bankroll   = risk_status["bankroll"],
                cb_active  = risk_status["circuit_breaker_active"],
                cb_remaining = risk_status["circuit_breaker_remaining"],
            )
            shared_state.update_active_trade(bot_state.active_trade)
            if bot_state.last_signal:
                shared_state.update_signal(bot_state.last_signal)
            with shared_state._lock:
                shared_state.data_source = snap.get("source", "unknown")

        except Exception as e:
            logger.warning(f"Dashboard update error: {e}")

        await asyncio.sleep(1)


# ── Stats refresh loop ────────────────────────────────────────────────────────

async def stats_refresh_loop(
    dashboard:    Dashboard,
    bot_state:    BotState,
    shared_state: SharedState,
):
    """Refreshes DB stats every 10 seconds (terminal + web)."""
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
            # ── Window detection ──────────────────────────────────────────────
            from market_scanner import current_window_ts
            wts = current_window_ts()

            if wts != bot_state.current_window_ts:
                logger.info(
                    f"=== New window: {wts} "
                    f"({datetime.fromtimestamp(wts).strftime('%H:%M:%S')}) ==="
                )

                # ── Resolve previous window trade ─────────────────────────────
                if bot_state.active_trade:
                    logger.info("Resolving previous window trade…")
                    result = await paper_trader.resolve_trade(
                        bot_state.window_open_price
                    )
                    if result:
                        had_mispricing = bot_state.window_open_price > 0
                        if result["win"]:
                            signal_engine.record_win(had_mispricing)
                        else:
                            signal_engine.record_loss()
                        bot_state.active_trade = None
                        await telegram.trade_resolved(result, risk_manager.bankroll)
                        stats = database.get_stats()
                        await telegram.daily_summary(stats, risk_manager.bankroll)

                # ── Reset window state ────────────────────────────────────────
                bot_state.current_window_ts        = wts
                bot_state.trade_placed_this_window = False
                bot_state.last_signal              = None

                await scanner.refresh()

                # Wait up to 15s for market data to have a price
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

            # ── Timing within window ──────────────────────────────────────────
            elapsed         = int(time.time()) - wts
            time_remaining  = max(0, WINDOW_SECONDS - elapsed)
            minutes_elapsed = elapsed / 60.0

            # ── Signal evaluation ─────────────────────────────────────────────
            # Start from T+1min (signal_engine has its own internal timing gate).
            # Evaluate every loop (every 5s) until a trade is placed.
            if elapsed >= 60 and not bot_state.trade_placed_this_window:
                snap   = market_data.snapshot()
                signal = await signal_engine.evaluate(snap, minutes_elapsed)

                # Only update last_signal when we have an actual FIRE.
                # Never overwrite a FIRE with a SKIP — that was the original bug.
                if signal.get("direction") is not None:
                    bot_state.last_signal = signal
                elif bot_state.last_signal is None:
                    # Store SKIPs only when there's no prior signal (for dashboard)
                    bot_state.last_signal = signal

                # ── Execute immediately on FIRE ───────────────────────────────
                # Condition: signal fired AND enough time left in window
                if (
                    signal.get("direction") is not None
                    and time_remaining > MIN_SECONDS_REMAINING
                    and not bot_state.trade_placed_this_window
                ):
                    urgency_tag = " [URGENT]" if signal.get("urgent") else ""
                    logger.info(
                        f"FIRE @ T+{minutes_elapsed:.1f}min "
                        f"({time_remaining}s remaining){urgency_tag} → "
                        f"executing {signal['direction']} trade now"
                    )

                    trade = await paper_trader.execute_trade(
                        direction       = signal["direction"],
                        confidence      = signal["confidence"],
                        strategy_details= signal.get("strategy_details", {}),
                        open_price      = bot_state.window_open_price,
                        token_price     = signal.get("token_price"),
                        force_min_bet   = signal.get("force_min_bet", False),
                    )

                    if trade:
                        bot_state.active_trade             = trade
                        bot_state.trade_placed_this_window = True
                        await telegram.trade_executed(trade)
                        logger.info(
                            f"Trade placed: #{trade['id']} "
                            f"dir={trade['direction']} "
                            f"token=${signal.get('token_price', 0):.4f} "
                            f"cost=${trade.get('cost_usd', 0):.2f} "
                            f"@ T+{minutes_elapsed:.1f}min"
                        )

                elif (
                    signal.get("direction") is not None
                    and time_remaining <= MIN_SECONDS_REMAINING
                ):
                    logger.warning(
                        f"FIRE signal @ T+{minutes_elapsed:.1f}min but only "
                        f"{time_remaining}s remaining — skipping (too late to execute)"
                    )

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

    # ── Init DB ───────────────────────────────────────────────────────────────
    database.init_db()

    # Resume bankroll from last session if available
    last_trades = database.get_last_n_trades(1)
    if last_trades and last_trades[0].get("bankroll_after"):
        current_bankroll = last_trades[0]["bankroll_after"]
        logger.info(f"Resuming with bankroll: ${current_bankroll:.2f}")
    else:
        current_bankroll = INITIAL_BANKROLL
        logger.info(f"Starting fresh with bankroll: ${current_bankroll:.2f}")

    # ── Init components ───────────────────────────────────────────────────────
    shared_state   = SharedState(initial_bankroll=INITIAL_BANKROLL)
    scanner        = MarketScanner()
    market_data    = MarketData()
    signal_engine  = SignalEngine(scanner=scanner, min_confidence=MIN_CONFIDENCE)
    risk_manager   = RiskManager(current_bankroll, MAX_POSITION_PCT)
    paper_trader   = PaperTrader(risk_manager, scanner)
    dashboard      = Dashboard(INITIAL_BANKROLL)
    telegram       = TelegramAlerter()
    bot_state      = BotState()

    # Start web dashboard (background thread, non-blocking)
    start_web_dashboard(shared_state)

    # Start async components
    await scanner.start()
    await market_data.start()
    await paper_trader.start()
    await telegram.start()

    # Seed dashboard with initial DB data
    initial_stats  = database.get_stats()
    recent_trades  = database.get_last_n_trades(10)
    dashboard.update_trades(initial_stats, recent_trades)
    dashboard.update(bankroll=current_bankroll)
    shared_state.update_stats(initial_stats, recent_trades)
    shared_state.update_risk(current_bankroll, False, 0)

    dashboard.start()

    # ── Graceful shutdown ─────────────────────────────────────────────────────
    def shutdown(sig, frame):
        logger.info("Shutdown signal received")
        bot_state.running = False

    signal.signal(signal.SIGINT,  shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    # ── Create asyncio tasks ──────────────────────────────────────────────────
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
