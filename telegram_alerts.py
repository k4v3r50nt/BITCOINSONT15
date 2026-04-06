import asyncio
import logging
import time
from typing import Optional, Dict, Any

import aiohttp

from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"


class TelegramAlerter:
    def __init__(self):
        self.enabled = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)
        self._session: Optional[aiohttp.ClientSession] = None
        self._last_daily_report: float = 0.0

        if not self.enabled:
            logger.info("Telegram not configured — alerts disabled")

    async def start(self):
        self._session = aiohttp.ClientSession()

    async def stop(self):
        if self._session:
            await self._session.close()

    async def send(self, text: str):
        if not self.enabled or not self._session:
            return
        url = f"{TELEGRAM_API}/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        try:
            async with self._session.post(
                url,
                json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning(f"Telegram send failed: {resp.status} {body}")
        except Exception as e:
            logger.warning(f"Telegram error: {e}")

    async def trade_executed(self, trade: Dict[str, Any]):
        direction_icon = "⬆️" if trade["direction"] == "UP" else "⬇️"
        text = (
            f"<b>🤖 BITCOINSONT15 — Trade Ejecutado</b>\n\n"
            f"{direction_icon} <b>Dirección:</b> {trade['direction']}\n"
            f"💰 <b>Monto:</b> ${trade['cost_usd']:.2f}\n"
            f"📊 <b>Confianza:</b> {trade['confidence'] * 100:.0f}%\n"
            f"🎯 <b>Precio token:</b> {trade['token_price']:.4f}\n"
            f"📈 <b>BTC open:</b> ${trade['open_price']:,.2f}\n"
            f"💼 <b>Bankroll:</b> ${trade['bankroll_before']:.2f}"
        )
        await self.send(text)

    async def trade_resolved(self, result: Dict[str, Any], bankroll: float):
        icon = "✅" if result["win"] else "❌"
        pnl_str = f"+${result['pnl']:.2f}" if result["pnl"] >= 0 else f"-${abs(result['pnl']):.2f}"
        direction_icon = "⬆️" if result["direction"] == "UP" else "⬇️"
        text = (
            f"<b>🤖 BITCOINSONT15 — Trade Resuelto {icon}</b>\n\n"
            f"{direction_icon} <b>Dirección:</b> {result['direction']}\n"
            f"{'✅ WIN' if result['win'] else '❌ LOSS'}\n"
            f"💸 <b>P&L:</b> {pnl_str}\n"
            f"📈 <b>BTC:</b> ${result['open_price']:,.2f} → ${result['close_price']:,.2f}\n"
            f"💼 <b>Bankroll:</b> ${bankroll:.2f}"
        )
        await self.send(text)

    async def circuit_breaker_alert(self, consecutive_losses: int, pause_minutes: int):
        text = (
            f"<b>⚠️ BITCOINSONT15 — Circuit Breaker Activado</b>\n\n"
            f"🚫 {consecutive_losses} pérdidas consecutivas detectadas\n"
            f"⏸️ Bot pausado por {pause_minutes} minutos\n"
            f"🔄 Reanudará automáticamente"
        )
        await self.send(text)

    async def daily_summary(self, stats: Dict[str, Any], bankroll: float):
        now = time.time()
        # Only send once per 24h
        if now - self._last_daily_report < 86400:
            return
        self._last_daily_report = now

        win_rate = stats.get("win_rate", 0)
        total_pnl = stats.get("total_pnl", 0)
        pnl_str = f"+${total_pnl:.2f}" if total_pnl >= 0 else f"-${abs(total_pnl):.2f}"

        text = (
            f"<b>📊 BITCOINSONT15 — Resumen Diario</b>\n\n"
            f"📈 <b>Trades hoy:</b> {stats.get('total', 0)}\n"
            f"✅ <b>Wins:</b> {stats.get('wins', 0)}\n"
            f"❌ <b>Losses:</b> {stats.get('losses', 0)}\n"
            f"🎯 <b>Win rate:</b> {win_rate:.1f}%\n"
            f"💸 <b>P&L total:</b> {pnl_str}\n"
            f"💼 <b>Bankroll actual:</b> ${bankroll:.2f}\n\n"
            f"🏆 Mejor trade: +${stats.get('best_trade', 0):.2f}\n"
            f"💔 Peor trade: ${stats.get('worst_trade', 0):.2f}"
        )
        await self.send(text)
