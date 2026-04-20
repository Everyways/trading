"""Docker entry point.

Starts the paper trading runner (same behaviour as ``scripts/run_paper.py``
but without argparse, for use with ``CMD ["python", "-m", "app.main"]``).

All configuration is read from environment variables / .env file.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

import yaml

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("app.main")

_CONFIG_DIR = Path("config/strategies")
_RISK_CONFIG = Path("config/risk_global.yaml")


def _load_global_risk() -> dict:
    try:
        return yaml.safe_load(_RISK_CONFIG.read_text()) or {}
    except FileNotFoundError:
        log.warning("Risk config not found: %s — using defaults", _RISK_CONFIG)
        return {}


async def _run() -> None:
    import app.providers.alpaca  # noqa: F401 — register AlpacaProvider
    import app.strategies  # noqa: F401 — register all strategies
    from app.config import get_settings
    from app.core.registry import broker_registry
    from app.data.database import get_session
    from app.execution.runner import TradingRunner
    from app.execution.strategy_loader import load_strategy_configs
    from app.notifications.telegram import TelegramNotifier
    from app.notifications.telegram_commands import TelegramCommandBot
    from app.risk.manager import RiskManager
    s = get_settings()

    # ------------------------------------------------------------------ #
    # PANIC / live-mode guard                                              #
    # ------------------------------------------------------------------ #
    if s.panic:
        log.critical("PANIC env var set — engaging kill switch and exiting")
        sys.exit(1)

    kill_file = Path(s.kill_switch_file)
    resume_file = Path(s.resume_switch_file)
    if kill_file.exists():
        log.critical("KILL file detected at startup: %s — remove it to start", kill_file)
        sys.exit(1)
    # Clean up any leftover RESUME file from a previous session
    if resume_file.exists():
        try:
            resume_file.unlink()
        except OSError:
            log.warning("Could not remove leftover RESUME file %s", resume_file)

    global_config = _load_global_risk()

    # Respect mode_filter: allow live only when TRADING_BOT_LIVE_ENABLED=true
    mode_filter = "live" if s.live_trading_enabled else "paper"
    configs = load_strategy_configs(str(_CONFIG_DIR), mode_filter=mode_filter)
    if not configs:
        log.error("No enabled %s strategies found in %s", mode_filter, _CONFIG_DIR)
        sys.exit(1)

    # Guard: refuse to start live strategies without explicit opt-in
    if not s.live_trading_enabled:
        live_cfgs = [c for c in configs if c.mode == "live"]
        if live_cfgs:
            log.error(
                "Live strategies found but TRADING_BOT_LIVE_ENABLED is not set: %s",
                [c.name for c in live_cfgs],
            )
            sys.exit(1)

    log.info("Loaded %d strategy config(s): %s", len(configs), [c.name for c in configs])

    provider_cls = broker_registry.get("alpaca")
    if provider_cls is None:
        log.error("AlpacaProvider not registered")
        sys.exit(1)

    notifier = TelegramNotifier(s.telegram_bot_token, s.telegram_chat_id_global)

    with get_session() as session:
        risk_manager = RiskManager(session, global_config)
        if risk_manager.global_kill_engaged:
            log.critical("Global kill switch engaged — reset before starting")
            sys.exit(1)

        # Optional Telegram command bot (requires TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID)
        command_bot: TelegramCommandBot | None = None
        if s.telegram_bot_token and s.telegram_chat_id_global:
            command_bot = TelegramCommandBot(
                token=s.telegram_bot_token,
                allowed_chat_id=s.telegram_chat_id_global,
                on_stop=lambda reason: risk_manager.engage_kill_switch("global", reason=reason),
                on_resume=lambda reset_by: risk_manager.reset_kill_switch(reset_by=reset_by),
                on_status=lambda: (
                    f"Monthly loss: {risk_manager.monthly_loss_eur:.2f}€\n"
                    f"Kill switch: {'ENGAGED' if risk_manager.global_kill_engaged else 'clear'}"
                ),
                kill_file=kill_file,
            )

        runner = TradingRunner(
            provider=provider_cls(),
            strategy_configs=configs,
            risk_manager=risk_manager,
            session=session,
            global_config=global_config,
            notifier=notifier,
            command_bot=command_bot,
            kill_file=kill_file,
            resume_file=resume_file,
        )
        log.info("Starting continuous %s trading loop", mode_filter)
        await runner.run()


def main() -> None:
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        log.info("Runner stopped")


if __name__ == "__main__":
    main()
