"""Walk-forward optimisation — rolling IS / OOS window parameter grid search.

Fetches historical candles from Alpaca, then runs a rolling grid search:
  • In-sample  (IS) window → exhaustive grid search → best params by metric
  • Out-of-sample (OOS) window → forward-test with best IS params

The process repeats, advancing by ``--step`` days each iteration.
Results are printed as a table and optionally written to CSV.

Usage::

    python scripts/run_walk_forward.py \\
        --strategy rsi_mean_reversion \\
        --symbol   SPY \\
        --start    2022-01-01 \\
        --end      2024-12-31 \\
        --in-sample  252 \\
        --out-of-sample 63 \\
        --step       63 \\
        --param-grid config/param_grids/rsi_mean_reversion.yaml \\
        --metric     sharpe \\
        --output     results/wf_rsi_spy.csv

Requires environment variables (or .env file):
    ALPACA_API_KEY, ALPACA_API_SECRET
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import itertools
import logging
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.backtest.metrics import BacktestMetrics

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
    level=logging.WARNING,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("run_walk_forward")


# ---------------------------------------------------------------------------
# Metric extractors
# ---------------------------------------------------------------------------

def _metric_sharpe(m: BacktestMetrics) -> float:
    return m.sharpe_ratio


def _metric_return(m: BacktestMetrics) -> float:
    return m.total_return_pct


def _metric_profit_factor(m: BacktestMetrics) -> float:
    return m.profit_factor if m.profit_factor != float("inf") else 999.0


def _metric_win_rate(m: BacktestMetrics) -> float:
    return m.win_rate_pct


_METRIC_FN: dict[str, Any] = {
    "sharpe": _metric_sharpe,
    "return": _metric_return,
    "profit_factor": _metric_profit_factor,
    "win_rate": _metric_win_rate,
}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Walk-forward parameter optimisation for a trading strategy"
    )
    p.add_argument("--strategy", required=True, metavar="NAME", help="Strategy name")
    p.add_argument("--symbol", required=True, metavar="TICKER", help="Symbol to test")
    p.add_argument("--start", required=True, metavar="YYYY-MM-DD", help="Full dataset start")
    p.add_argument("--end", required=True, metavar="YYYY-MM-DD", help="Full dataset end")
    p.add_argument(
        "--in-sample",
        type=int,
        default=252,
        metavar="DAYS",
        dest="in_sample",
        help="In-sample window in calendar days (default: 252)",
    )
    p.add_argument(
        "--out-of-sample",
        type=int,
        default=63,
        metavar="DAYS",
        dest="out_of_sample",
        help="Out-of-sample window in calendar days (default: 63)",
    )
    p.add_argument(
        "--step",
        type=int,
        default=63,
        metavar="DAYS",
        help="Window advancement step in calendar days (default: 63)",
    )
    p.add_argument(
        "--param-grid",
        required=True,
        metavar="YAML",
        dest="param_grid",
        help="Path to param_grid YAML file (e.g. config/param_grids/rsi_mean_reversion.yaml)",
    )
    p.add_argument(
        "--metric",
        default="sharpe",
        choices=list(_METRIC_FN.keys()),
        help="Optimisation target metric (default: sharpe)",
    )
    p.add_argument(
        "--equity",
        type=float,
        default=10_000.0,
        metavar="USD",
        help="Starting equity per fold in USD (default: 10000)",
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
        dest="config_dir",
        help="Strategy YAML directory (default: config/strategies)",
    )
    p.add_argument(
        "--output",
        metavar="CSV",
        help="Write per-fold results to this CSV file (optional)",
    )
    return p.parse_args()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_param_grid(path: str) -> dict[str, list[Any]]:
    """Load ``param_grid`` from a YAML file.  Each value must be a list."""
    raw = yaml.safe_load(Path(path).read_text()) or {}
    grid: dict[str, list[Any]] = raw.get("param_grid", {})
    for key, val in grid.items():
        if not isinstance(val, list):
            raise ValueError(f"param_grid.{key} must be a list, got {type(val).__name__}")
    if not grid:
        raise ValueError(f"No 'param_grid' section found in {path}")
    return grid


def _all_combinations(grid: dict[str, list[Any]]) -> list[dict[str, Any]]:
    """Cartesian product of all param values."""
    keys = list(grid)
    return [
        dict(zip(keys, combo, strict=True))
        for combo in itertools.product(*[grid[k] for k in keys])
    ]


def _build_windows(
    start: datetime,
    end: datetime,
    is_days: int,
    oos_days: int,
    step_days: int,
) -> list[tuple[datetime, datetime, datetime, datetime]]:
    """Return list of ``(is_start, is_end, oos_start, oos_end)`` tuples.

    Stops adding windows when the OOS window would exceed *end*.
    """
    windows: list[tuple[datetime, datetime, datetime, datetime]] = []
    is_delta = timedelta(days=is_days)
    oos_delta = timedelta(days=oos_days)
    step = timedelta(days=step_days)
    cursor = start
    while True:
        is_end = cursor + is_delta
        oos_end = is_end + oos_delta
        if oos_end > end:
            break
        windows.append((cursor, is_end, is_end, oos_end))
        cursor += step
    return windows


def _slice_df(df: pd.DataFrame, start: datetime, end: datetime) -> pd.DataFrame:
    """Return rows where ``time >= start`` and ``time < end``."""
    mask = (df["time"] >= start) & (df["time"] < end)
    return df[mask].reset_index(drop=True)


def _candles_to_df(candles: list[Any]) -> pd.DataFrame:
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


# ---------------------------------------------------------------------------
# Core async entry point
# ---------------------------------------------------------------------------

async def _run(args: argparse.Namespace) -> None:
    # 1. Load strategy config
    configs = load_strategy_configs(args.config_dir)
    cfg = next((c for c in configs if c.name == args.strategy), None)
    if cfg is None:
        log.error(
            "Strategy '%s' not found in %s (or not enabled)", args.strategy, args.config_dir
        )
        sys.exit(1)

    strategy_cls = strategy_registry.get(cfg.name)
    if strategy_cls is None:
        log.error("Strategy '%s' is not registered", args.strategy)
        sys.exit(1)
    strategy = strategy_cls()

    # 2. Load and validate param grid
    try:
        grid = _load_param_grid(args.param_grid)
    except (FileNotFoundError, ValueError) as exc:
        log.error("Param grid error: %s", exc)
        sys.exit(1)

    combos = _all_combinations(grid)

    print(f"\nStrategy : {args.strategy}  |  Symbol : {args.symbol}")
    print(f"Metric   : {args.metric}  |  Combinations : {len(combos)}")
    print(f"IS={args.in_sample}d  OOS={args.out_of_sample}d  step={args.step}d\n")

    # 3. Fetch OHLCV data from Alpaca
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
        print(f"Fetching {args.symbol} {cfg.timeframe} candles {args.start} → {args.end} …")
        candles = await provider.get_historical_candles(
            args.symbol, cfg.timeframe, start_dt, end_dt
        )
    finally:
        await provider.disconnect()

    df = _candles_to_df(candles)
    if df.empty:
        log.error("No candles returned for %s — check symbol and date range", args.symbol)
        sys.exit(1)
    print(f"Fetched {len(df)} bars.\n")

    # 4. Build walk-forward windows
    windows = _build_windows(
        start_dt, end_dt, args.in_sample, args.out_of_sample, args.step
    )
    if not windows:
        log.error(
            "No complete IS+OOS windows fit in the date range "
            "(%s → %s). Use a longer range or smaller windows.",
            args.start,
            args.end,
        )
        sys.exit(1)
    print(f"Walk-forward windows : {len(windows)}\n")

    # 5. Build Instrument domain object
    symbol_entry = next(
        (e for e in cfg.universe if e.symbol.upper() == args.symbol.upper()), None
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

    base_params = {**cfg.params, "timeframe": cfg.timeframe}
    metric_fn = _METRIC_FN[args.metric]

    engine = BacktestEngine(
        strategy=strategy,
        initial_equity=Decimal(str(args.equity)),
        commission_pct=args.commission,
    )

    # 6. Walk-forward loop
    fold_results: list[dict[str, Any]] = []
    col_w = 13

    hdr = (
        f"{'#':>3}  "
        f"{'IS-start':10}  {'IS-end':10}  "
        f"{'OOS-start':10}  {'OOS-end':10}  "
        f"{'IS-' + args.metric:>{col_w}}  "
        f"{'OOS-' + args.metric:>{col_w}}  "
        f"{'Trades':>6}  "
        f"{'OOS-PnL ($)':>11}  "
        f"Best params"
    )
    sep = "-" * len(hdr)
    print(hdr)
    print(sep)

    for fold_idx, (is_start, is_end, oos_start, oos_end) in enumerate(windows, start=1):
        df_is = _slice_df(df, is_start, is_end)
        df_oos = _slice_df(df, oos_start, oos_end)

        # In-sample grid search
        best_is_val = float("-inf")
        best_combo: dict[str, Any] = {}

        for combo in combos:
            params = {**base_params, **combo}
            result_is = engine.run(df_is, params, instrument)
            val = metric_fn(result_is.metrics)
            if val > best_is_val:
                best_is_val = val
                best_combo = combo

        # Out-of-sample evaluation with best IS params
        best_params_full = {**base_params, **best_combo}
        result_oos = engine.run(df_oos, best_params_full, instrument)
        oos_val = metric_fn(result_oos.metrics)

        combo_str = "  ".join(f"{k}={v}" for k, v in best_combo.items())
        print(
            f"{fold_idx:>3}  "
            f"{is_start.date()!s:10}  {is_end.date()!s:10}  "
            f"{oos_start.date()!s:10}  {oos_end.date()!s:10}  "
            f"{best_is_val:>{col_w}.4f}  "
            f"{oos_val:>{col_w}.4f}  "
            f"{result_oos.metrics.total_trades:>6}  "
            f"{float(result_oos.metrics.net_pnl):>11.2f}  "
            f"{combo_str}"
        )

        fold_row: dict[str, Any] = {
            "fold": fold_idx,
            "is_start": is_start.date().isoformat(),
            "is_end": is_end.date().isoformat(),
            "oos_start": oos_start.date().isoformat(),
            "oos_end": oos_end.date().isoformat(),
            f"is_{args.metric}": round(best_is_val, 4),
            f"oos_{args.metric}": round(oos_val, 4),
            "oos_trades": result_oos.metrics.total_trades,
            "oos_pnl": round(float(result_oos.metrics.net_pnl), 2),
            "oos_win_rate_pct": round(result_oos.metrics.win_rate_pct, 1),
            **{f"best_{k}": v for k, v in best_combo.items()},
        }
        fold_results.append(fold_row)

    # 7. Aggregate summary
    print("\n" + "=" * 60)
    print(f"RÉSUMÉ — {len(fold_results)} fold(s)")
    if fold_results:
        oos_vals = [r[f"oos_{args.metric}"] for r in fold_results]
        oos_pnls = [r["oos_pnl"] for r in fold_results]
        total_trades = sum(r["oos_trades"] for r in fold_results)
        pos_folds = sum(1 for p in oos_pnls if p > 0)
        print(f"  OOS {args.metric} moyen  : {sum(oos_vals) / len(oos_vals):.4f}")
        print(f"  OOS PnL total      : ${sum(oos_pnls):.2f}")
        print(f"  OOS trades total   : {total_trades}")
        print(f"  Folds profitables  : {pos_folds}/{len(fold_results)}")
    print("=" * 60)

    # 8. Optional CSV output
    if args.output and fold_results:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=fold_results[0].keys())
            writer.writeheader()
            writer.writerows(fold_results)
        print(f"\nRésultats écrits dans : {out_path}")


def main() -> None:
    args = _parse_args()
    try:
        asyncio.run(_run(args))
    except KeyboardInterrupt:
        log.info("Walk-forward stopped by user")


if __name__ == "__main__":
    main()
