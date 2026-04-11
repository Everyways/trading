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
    from app.core.registry import broker_registry
    from app.data.database import get_session
    from app.execution.runner import TradingRunner
    from app.execution.strategy_loader import load_strategy_configs
    from app.notifications.telegram import TelegramNotifier
    from app.risk.manager import RiskManager

    global_config = _load_global_risk()
    configs = load_strategy_configs(str(_CONFIG_DIR), mode_filter="paper")
    if not configs:
        log.error("No enabled paper strategies found in %s", _CONFIG_DIR)
        sys.exit(1)

    log.info("Loaded %d strategy config(s): %s", len(configs), [c.name for c in configs])

    provider_cls = broker_registry.get("alpaca")
    if provider_cls is None:
        log.error("AlpacaProvider not registered")
        sys.exit(1)

    from app.config import get_settings
    s = get_settings()
    notifier = TelegramNotifier(s.telegram_bot_token, s.telegram_chat_id_global)

    with get_session() as session:
        risk_manager = RiskManager(session, global_config)
        if risk_manager.global_kill_engaged:
            log.critical("Global kill switch engaged — reset before starting")
            sys.exit(1)

        runner = TradingRunner(
            provider=provider_cls(),
            strategy_configs=configs,
            risk_manager=risk_manager,
            session=session,
            global_config=global_config,
            notifier=notifier,
        )
        log.info("Starting continuous paper trading loop")
        await runner.run()


def main() -> None:
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        log.info("Runner stopped")


if __name__ == "__main__":
    main()
