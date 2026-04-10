#!/usr/bin/env python3
"""Backfill historical OHLCV data from a broker into the local database.

Usage:
    python scripts/backfill_data.py \\
        --provider alpaca \\
        --symbols SPY,QQQ,IWM,AAPL,MSFT,TSLA,NVDA,COIN \\
        --timeframe 15m \\
        --years 2
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

# Trigger provider registration
import app.providers  # noqa: F401

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backfill OHLCV data from a broker provider into the database.",
    )
    parser.add_argument("--provider", required=True, help="Broker provider name (e.g. alpaca)")
    parser.add_argument(
        "--symbols", required=True, help="Comma-separated list of symbols (e.g. SPY,QQQ)"
    )
    parser.add_argument("--timeframe", default="15m", help="Bar interval (default: 15m)")
    parser.add_argument("--years", type=int, default=2, help="Years of history to fetch")
    parser.add_argument(
        "--chunk-days",
        type=int,
        default=30,
        help="Days per API request chunk (default: 30)",
    )
    return parser.parse_args()


async def run(args: argparse.Namespace) -> int:
    from app.core.registry import broker_registry
    from app.data.database import get_session
    from app.data.ingestion import DataIngestionService

    # Resolve provider
    if args.provider not in broker_registry:
        log.error("Unknown provider %r. Registered: %s", args.provider, list(broker_registry.all()))
        return 1

    provider_cls = broker_registry.get(args.provider)
    provider = provider_cls()

    log.info("Connecting to provider: %s", args.provider)
    await provider.connect()

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    log.info("Symbols: %s", symbols)
    log.info(
        "Timeframe: %s | Years: %d | Chunk: %d days",
        args.timeframe, args.years, args.chunk_days,
    )

    total_inserted = 0
    try:
        with get_session() as session:
            service = DataIngestionService(provider, session)
            results = await service.backfill(
                symbols=symbols,
                timeframe=args.timeframe,
                years=args.years,
                chunk_days=args.chunk_days,
            )
        for symbol, count in results.items():
            print(f"  {symbol:12s}  {count:>8,d} rows inserted")
            total_inserted += count
    finally:
        await provider.disconnect()

    print(f"\nTotal: {total_inserted:,} rows inserted across {len(symbols)} symbols.")
    return 0


def main() -> None:
    args = parse_args()
    exit_code = asyncio.run(run(args))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
