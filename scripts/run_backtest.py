"""Backtest runner — entry point.

Fetches historical candles from Alpaca and runs a backtest for a single
strategy/symbol pair, printing performance metrics to stdout.

Usage:
    python scripts/run_backtest.py \\
        --strategy rsi_mean_reversion \\
        --symbol   SPY \\
        --start    2024-01-01 \\
        --end      2024-12-31

    python scripts/run_backtest.py \\
        --strategy breakout \\
        --symbol   QQQ \\
        --start    2023-01-01 \\
        --end      2023-12-31 \\
        --equity   5000 \\
        --output   results/breakout_qqq.csv

Requires environment variables (or .env file):
    ALPACA_API_KEY, ALPACA_API_SECRET
    DATABASE_URL_SYNC  (only needed if the strategy loader reads from DB)
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import logging
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.strategies  # noqa: F401, E402 — register all strategies
from app.backtest.engine import BacktestEngine  # noqa: E402
from app.core.domain import Instrument  # noqa: E402
from app.core.enums import AssetClass  # noqa: E402
from app.core.registry import broker_registry, strategy_registry  # noqa: E402
from app.execution.strategy_loader import load_strategy_configs  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("run_backtest")

_TF_MINUTES: dict[str, int] = {
    "1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440,
}


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run a backtest for a strategy/symbol pair")
    p.add_argument("--strategy", required=True, metavar="NAME", help="Strategy name")
    p.add_argument("--symbol", required=True, metavar="TICKER", help="Symbol to backtest")
    p.add_argument(
        "--start",
        required=True,
        metavar="YYYY-MM-DD",
        help="Backtest start date (inclusive)",
    )
    p.add_argument(
        "--end",
        required=True,
        metavar="YYYY-MM-DD",
        help="Backtest end date (inclusive)",
    )
    p.add_argument(
        "--equity",
        type=float,
        default=10_000.0,
        metavar="USD",
        help="Starting equity in USD (default: 10000)",
    )
    p.add_argument(
        "--commission",
        type=float,
        default=0.001,
        metavar="PCT",
        help="One-way commission fraction (default: 0.001 = 0.1%%)",
    )
    p.add_argument(
        "--config-dir",
        default="config/strategies",
        metavar="DIR",
        help="Strategy YAML directory (default: config/strategies)",
    )
    p.add_argument(
        "--risk-config",
        default="config/risk_global.yaml",
        metavar="FILE",
        help="Global risk config path (default: config/risk_global.yaml)",
    )
    p.add_argument(
        "--output",
        metavar="CSV",
        help="Write trades to this CSV file (optional)",
    )
    return p.parse_args()


def _load_global_risk(path: str) -> dict[str, Any]:
    try:
        return yaml.safe_load(Path(path).read_text()) or {}
    except FileNotFoundError:
        log.warning("Risk config not found: %s — using defaults", path)
        return {}


def _candles_to_df(candles: list[Any]) -> pd.DataFrame:
    """Convert Candle domain objects → float OHLCV DataFrame."""
    if not candles:
        return pd.DataFrame(columns=["time", "open", "high", "low", "close", "volume"])
    rows = [
        {
            "time": c.time,
            "open": float(c.open),
            "high": float(c.high),
            "low": float(c.low),
            "close": float(c.close),
            "volume": float(c.volume),
        }
        for c in candles
        if c.is_closed
    ]
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values("time").reset_index(drop=True)
    return df


async def _run(args: argparse.Namespace) -> None:
    # Resolve strategy config
    configs = load_strategy_configs(args.config_dir)
    cfg = next((c for c in configs if c.name == args.strategy), None)
    if cfg is None:
        log.error(
            "Strategy '%s' not found in %s (or not enabled)", args.strategy, args.config_dir
        )
        sys.exit(1)

    # Resolve strategy class
    strategy_cls = strategy_registry.get(cfg.name)
    if strategy_cls is None:
        log.error("Strategy '%s' is not registered", args.strategy)
        sys.exit(1)
    strategy = strategy_cls()

    # Connect to broker for historical data
    import app.providers.alpaca  # noqa: F401 — trigger registration

    provider_cls = broker_registry.get("alpaca")
    if provider_cls is None:
        log.error("AlpacaProvider not registered — check app/providers/alpaca/provider.py")
        sys.exit(1)

    provider = provider_cls()
    await provider.connect()

    try:
        start_dt = datetime.fromisoformat(args.start).replace(tzinfo=UTC)
        end_dt = datetime.fromisoformat(args.end).replace(hour=23, minute=59, tzinfo=UTC)
        log.info(
            "Fetching %s %s candles from %s to %s",
            args.symbol, cfg.timeframe, args.start, args.end,
        )
        candles = await provider.get_historical_candles(
            args.symbol, cfg.timeframe, start_dt, end_dt
        )
    finally:
        await provider.disconnect()

    df = _candles_to_df(candles)
    if df.empty:
        log.error("No candles returned for %s — check symbol and date range", args.symbol)
        sys.exit(1)

    log.info("Fetched %d bars. Running backtest…", len(df))

    # Build Instrument
    symbol_entry = next(
        (e for e in cfg.universe if e.symbol.upper() == args.symbol.upper()),
        None,
    )
    asset_class_str = symbol_entry.asset_class if symbol_entry else "equity"
    try:
        asset_class = AssetClass(asset_class_str)
    except ValueError:
        asset_class = AssetClass.EQUITY

    instrument = Instrument(
        symbol=args.symbol.upper(),
        asset_class=asset_class,
        provider_name=cfg.provider,
    )

    # Run engine
    engine = BacktestEngine(
        strategy=strategy,
        initial_equity=Decimal(str(args.equity)),
        commission_pct=args.commission,
    )
    result = engine.run(df, cfg.params, instrument)

    # Print results
    print(str(result))
    print(f"\nTrades: {len(result.trades)}")
    if result.trades:
        wins = sum(1 for t in result.trades if t["pnl_net"] > 0)
        print(f"Win rate: {wins}/{len(result.trades)} = {wins/len(result.trades)*100:.1f}%")

    # Optionally write trades to CSV
    if args.output and result.trades:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=result.trades[0].keys())
            writer.writeheader()
            writer.writerows(result.trades)
        log.info("Trades written to %s", out_path)


def main() -> None:
    args = _parse_args()
    try:
        asyncio.run(_run(args))
    except KeyboardInterrupt:
        log.info("Backtest stopped by user")


if __name__ == "__main__":
    main()
