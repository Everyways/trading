"""Telegram command handler for emergency stop/resume/status.

Runs as a background asyncio task alongside the main trading runner.
Uses python-telegram-bot v20+ async polling API.

Commands accepted:
    /stop [reason]   — engage global kill switch immediately + create KILL file
    /resume          — reset kill switch + delete KILL file
    /status          — show kill switch state + monthly loss
    /help            — list commands

Security: only commands from the configured allowed_chat_id are processed.
Any other sender receives a silent drop (no response, to avoid fingerprinting).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger(__name__)


class TelegramCommandBot:
    """Polls for Telegram commands and routes them to trading-bot callbacks.

    Designed to run as a long-lived asyncio task via ``await bot.run()``.

    Args:
        token:          Telegram bot token (from @BotFather).
        allowed_chat_id: Only messages from this chat ID are acted upon.
        on_stop:        Callable(reason) — called when /stop is received.
        on_resume:      Callable(reset_by) — called when /resume is received.
        on_status:      Callable() → str — returns a status string for /status.
        kill_file:      Path to the KILL sentinel file.
    """

    def __init__(
        self,
        token: str,
        allowed_chat_id: str,
        on_stop: Callable[[str], None],
        on_resume: Callable[[str], None],
        on_status: Callable[[], str],
        kill_file: Path,
    ) -> None:
        self._token = token
        self._allowed_chat_id = str(allowed_chat_id)
        self._on_stop = on_stop
        self._on_resume = on_resume
        self._on_status = on_status
        self._kill_file = kill_file

    async def run(self) -> None:
        """Start polling loop. Runs until cancelled."""
        try:
            from telegram.ext import Application, CommandHandler, filters

            app = Application.builder().token(self._token).build()

            # Only handle commands from the authorised chat
            chat_filter = filters.Chat(chat_id=int(self._allowed_chat_id))

            app.add_handler(CommandHandler("stop", self._cmd_stop, filters=chat_filter))
            app.add_handler(CommandHandler("resume", self._cmd_resume, filters=chat_filter))
            app.add_handler(CommandHandler("status", self._cmd_status, filters=chat_filter))
            app.add_handler(CommandHandler("help", self._cmd_help, filters=chat_filter))

            log.info("Telegram command bot starting (polling)")
            async with app:
                await app.start()
                await app.updater.start_polling(
                    poll_interval=2.0,
                    drop_pending_updates=True,  # ignore commands sent while bot was offline
                )
                await asyncio.Event().wait()   # run until task is cancelled
        except asyncio.CancelledError:
            log.info("Telegram command bot stopped")
        except Exception:
            log.exception("Telegram command bot error")

    # ------------------------------------------------------------------
    # Command handlers
    # ------------------------------------------------------------------

    async def _cmd_stop(self, update: object, context: object) -> None:
        from telegram import Update

        upd: Update = update  # type: ignore[assignment]
        args = getattr(context, "args", None) or []
        reason = " ".join(args) if args else "manual Telegram /stop"

        log.critical("EMERGENCY STOP via Telegram: %s", reason)
        self._kill_file.touch()
        self._on_stop(reason)

        await upd.message.reply_text(
            f"🚨 *EMERGENCY STOP ENGAGED*\nReason: `{reason}`\n"
            "All new trades blocked. Open positions will be liquidated.",
            parse_mode="Markdown",
        )

    async def _cmd_resume(self, update: object, context: object) -> None:
        from telegram import Update

        upd: Update = update  # type: ignore[assignment]

        if self._kill_file.exists():
            self._kill_file.unlink()

        self._on_resume("Telegram /resume")

        await upd.message.reply_text(
            "✅ *Kill switch RESET*\nTrading resumed on next tick.",
            parse_mode="Markdown",
        )
        log.warning("Kill switch reset via Telegram /resume")

    async def _cmd_status(self, update: object, context: object) -> None:
        from telegram import Update

        upd: Update = update  # type: ignore[assignment]
        status_text = self._on_status()
        kill_flag = "🔴 ENGAGED" if self._kill_file.exists() else "🟢 clear"
        await upd.message.reply_text(
            f"📊 *Bot status*\nKill switch: {kill_flag}\n\n{status_text}",
            parse_mode="Markdown",
        )

    async def _cmd_help(self, update: object, context: object) -> None:
        from telegram import Update

        upd: Update = update  # type: ignore[assignment]
        text = (
            "📖 *Available commands*\n\n"
            "/stop `[reason]` — engage emergency stop\n"
            "/resume — reset kill switch and resume trading\n"
            "/status — current bot state\n"
            "/help — this message"
        )
        await upd.message.reply_text(text, parse_mode="Markdown")
