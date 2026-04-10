"""Paper trading runner — entry point.

Usage:
    python scripts/run_paper.py                          # run continuously
    python scripts/run_paper.py --once                   # single tick then exit
    python scripts/run_paper.py --strategy rsi_mean_reversion  # one strategy only

Requires environment variables (or .env file):
    ALPACA_API_KEY, ALPACA_API_SECRET
    DATABASE_URL_SYNC   (e.g. sqlite:///./data/paper.db  or postgresql://...)

The script uses Alpaca *paper* endpoints by default
(ALPACA_BASE_URL defaults to https://paper-api.alpaca.markets).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

import yaml

# Ensure project root is importable when run as a script
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Register all strategies so strategy_registry is populated
import app.strategies  # noqa: F401, E402
from app.core.registry import broker_registry  # noqa: E402
from app.data.database import get_session  # noqa: E402
from app.execution.runner import TradingRunner  # noqa: E402
from app.execution.strategy_loader import load_strategy_configs  # noqa: E402
from app.risk.manager import RiskManager  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("run_paper")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Paper trading runner")
    p.add_argument(
        "--once",
        action="store_true",
        help="Run a single evaluation tick then exit (useful for testing)",
    )
    p.add_argument(
        "--strategy",
        metavar="NAME",
        help="Run only the named strategy (default: all enabled paper strategies)",
    )
    p.add_argument(
        "--config-dir",
        default="config/strategies",
        metavar="DIR",
        help="Path to strategy YAML files (default: config/strategies)",
    )
    p.add_argument(
        "--risk-config",
        default="config/risk_global.yaml",
        metavar="FILE",
        help="Path to global risk config YAML (default: config/risk_global.yaml)",
    )
    return p.parse_args()


def _load_global_risk(path: str) -> dict:
    try:
        return yaml.safe_load(Path(path).read_text()) or {}
    except FileNotFoundError:
        log.warning("Risk config not found: %s — using defaults", path)
        return {}


async def _run(args: argparse.Namespace) -> None:
    global_config = _load_global_risk(args.risk_config)

    # Load strategy configs (paper mode only)
    configs = load_strategy_configs(args.config_dir, mode_filter="paper")
    if args.strategy:
        configs = [c for c in configs if c.name == args.strategy]
        if not configs:
            log.error("Strategy '%s' not found or not enabled for paper mode", args.strategy)
            sys.exit(1)

    if not configs:
        log.error("No enabled paper strategies found in %s", args.config_dir)
        sys.exit(1)

    log.info("Loaded %d strategy config(s): %s", len(configs), [c.name for c in configs])

    # Instantiate Alpaca provider (requires API keys in env / .env)
    import app.providers.alpaca  # noqa: F401 — trigger registration

    provider_cls = broker_registry.get("alpaca")
    if provider_cls is None:
        log.error("AlpacaProvider not registered — check app/providers/alpaca/provider.py")
        sys.exit(1)

    provider = provider_cls()

    # DB session — uses DATABASE_URL_SYNC from env
    # For SQLite: sqlite:///./data/paper.db
    # For PostgreSQL: postgresql+psycopg2://user:pass@host/db
    with get_session() as session:
        risk_manager = RiskManager(session, global_config)

        if risk_manager.global_kill_engaged:
            log.critical(
                "Global kill switch is engaged. "
                "Reset it in the database before starting the runner."
            )
            sys.exit(1)

        runner = TradingRunner(
            provider=provider,
            strategy_configs=configs,
            risk_manager=risk_manager,
            session=session,
            global_config=global_config,
        )

        if args.once:
            log.info("Running single tick (--once mode)")
            await runner.run_once()
            log.info("Single tick complete — exiting")
        else:
            log.info("Starting continuous paper trading loop (Ctrl+C to stop)")
            await runner.run()


def main() -> None:
    args = _parse_args()
    try:
        asyncio.run(_run(args))
    except KeyboardInterrupt:
        log.info("Runner stopped by user")


if __name__ == "__main__":
    main()
