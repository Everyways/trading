"""Telegram notification service.

Sends alerts to a configured Telegram chat when significant trading events occur.
All public methods are async and safe to call with ``await``; failures are logged
but never propagate to the caller.

Configuration (via .env or environment variables):
    TELEGRAM_BOT_TOKEN        — token from @BotFather
    TELEGRAM_CHAT_ID_GLOBAL   — numeric chat ID (get via @userinfobot)

If either variable is absent the notifier becomes a no-op.
"""

from __future__ import annotations

import asyncio
import logging
from decimal import Decimal

log = logging.getLogger(__name__)

# Telegram message length limit
_MAX_LEN = 4096


class TelegramNotifier:
    """Async Telegram notifier.  No-op when token / chat_id not configured.

    Usage::

        notifier = TelegramNotifier(token, chat_id)
        await notifier.notify_order("BUY", "SPY", qty=0.5, price=450.0, strategy="rsi")
    """

    def __init__(self, token: str | None, chat_id: str | None) -> None:
        self._token = token or ""
        self._chat_id = chat_id or ""
        self._enabled = bool(self._token and self._chat_id)
        if self._enabled:
            log.info("Telegram notifications enabled (chat_id=%s)", self._chat_id)
        else:
            log.debug("Telegram notifications disabled (token/chat_id not set)")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def notify_order(
        self,
        side: str,
        symbol: str,
        qty: object,
        price: object,
        strategy: str,
    ) -> None:
        """Alert when an order is submitted."""
        emoji = "\U0001f7e2" if side.upper() == "BUY" else "\U0001f534"   # 🟢 / 🔴
        text = (
            f"{emoji} *{side.upper()}* `{symbol}`\n"
            f"qty=`{qty}`  ~price=`{price}`\n"
            f"strategy: `{strategy}`"
        )
        await self._send(text)

    async def notify_risk_blocked(self, reason: str, strategy: str) -> None:
        """Alert when a risk check blocks an order (only for critical blocks)."""
        text = f"\u26a0\ufe0f *Risk blocked* `{strategy}`\n`{reason}`"   # ⚠️
        await self._send(text)

    async def notify_kill_switch(self, scope: str, reason: str) -> None:
        """Critical alert when a kill switch engages."""
        text = (
            f"\U0001f6a8 *KILL SWITCH ENGAGED*\n"   # 🚨
            f"scope: `{scope}`\n"
            f"reason: `{reason}`"
        )
        await self._send(text)

    async def notify_error(self, context: str, exc: Exception) -> None:
        """Alert on an unhandled exception in the trading loop."""
        text = (
            f"\u274c *Error* in `{context}`\n"   # ❌
            f"`{type(exc).__name__}: {exc}`"
        )
        await self._send(text)

    async def notify_daily_summary(
        self,
        date_str: str,
        net_pnl: Decimal,
        trades: int,
        monthly_loss_eur: Decimal,
    ) -> None:
        """End-of-day performance summary."""
        sign = "+" if net_pnl >= 0 else ""
        text = (
            f"\U0001f4ca *Daily summary* {date_str}\n"   # 📊
            f"PnL: `{sign}${net_pnl:.2f}`  Trades: `{trades}`\n"
            f"Monthly loss: `\u20ac{monthly_loss_eur:.2f}`"
        )
        await self._send(text)

    async def notify_startup(self, strategies: list[str]) -> None:
        """Sent once when the paper trading runner starts."""
        names = ", ".join(f"`{s}`" for s in strategies)
        text = f"\U0001f916 *Bot started*\nStrategies: {names}"   # 🤖
        await self._send(text)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _send(self, text: str) -> None:
        """Send a Markdown message; silently drops if disabled or on error."""
        if not self._enabled:
            return
        # Truncate to Telegram's limit
        if len(text) > _MAX_LEN:
            text = text[:_MAX_LEN - 3] + "..."
        try:
            from telegram import Bot  # python-telegram-bot

            async with Bot(self._token) as bot:
                await asyncio.wait_for(
                    bot.send_message(
                        chat_id=self._chat_id,
                        text=text,
                        parse_mode="Markdown",
                    ),
                    timeout=10.0,
                )
        except TimeoutError:
            log.warning("Telegram send timed out")
        except Exception as exc:
            log.warning("Telegram send failed: %s", exc)
