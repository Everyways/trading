"""Backtest engine — bar-by-bar historical replay.

No look-ahead: at bar i the strategy only sees df.iloc[:i+1].
Market-order fills execute at the NEXT bar's open price.
Stop-loss and take-profit are checked against the bar's intra-bar high/low.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import pandas as pd

from app.backtest.metrics import BacktestMetrics, compute_metrics
from app.core.domain import Instrument, Position
from app.core.enums import SignalSide, StrategyMode
from app.risk.position_sizer import size_position

if TYPE_CHECKING:
    from app.strategies.base import Strategy

log = logging.getLogger(__name__)


@dataclass
class BacktestResult:
    """Output of a single BacktestEngine.run() call."""

    strategy_name: str
    symbol: str
    start: datetime
    end: datetime
    metrics: BacktestMetrics
    equity_curve: pd.Series          # float equity value at each evaluated bar
    trades: list[dict[str, Any]] = field(default_factory=list)

    def __str__(self) -> str:
        return (
            f"[{self.strategy_name}/{self.symbol}]  "
            f"{self.start.date()} → {self.end.date()}  {self.metrics}"
        )


class BacktestEngine:
    """Bar-by-bar backtest engine (long-only).

    Fills orders at the open of the bar *after* the signal bar, simulating a
    market order placed at the previous close. Stop-loss and take-profit levels
    are checked against the current bar's intra-bar high/low.

    Args:
        strategy:        Instantiated Strategy to evaluate.
        initial_equity:  Starting capital in USD.
        commission_pct:  One-way commission as a fraction (0.001 = 0.1%).
    """

    def __init__(
        self,
        strategy: Strategy,
        initial_equity: Decimal = Decimal("10000"),
        commission_pct: float = 0.001,
    ) -> None:
        self._strategy = strategy
        self._initial_equity = initial_equity
        self._commission_pct = commission_pct

    def run(
        self,
        df: pd.DataFrame,
        params: dict[str, Any],
        instrument: Instrument,
    ) -> BacktestResult:
        """Replay *df* bar-by-bar through the strategy.

        Args:
            df:         OHLCV DataFrame in ascending time order with float columns
                        open, high, low, close, volume.  Optionally a 'time'
                        column (datetime / Timestamp) for trade timestamps.
            params:     Strategy parameter dict forwarded to StrategyContext.
                        Recognised keys: lookback, stop_loss_pct, take_profit_pct,
                        max_holding_bars, risk_pct.
            instrument: Domain Instrument passed to StrategyContext.

        Returns:
            BacktestResult with metrics, equity curve, and trade list.
        """
        from app.strategies.base import StrategyContext  # local import avoids circular dep

        lookback = int(params.get("lookback", 20))
        stop_loss_pct = float(params.get("stop_loss_pct", 2.0))
        take_profit_pct = float(params.get("take_profit_pct", 0.0))
        max_holding_bars = int(params.get("max_holding_bars", 0))
        risk_pct = float(params.get("risk_pct", 1.0))

        # Compute annualisation factor from the strategy timeframe so the Sharpe
        # ratio is correct regardless of bar size (5m, 15m, 1h, 1d, …).
        _tf_minutes: dict[str, int] = {
            "1m": 1, "5m": 5, "15m": 15, "30m": 30,
            "1h": 60, "4h": 240, "1d": 1440,
        }
        tf = str(params.get("timeframe", "15m"))
        tf_minutes = _tf_minutes.get(tf, 15)
        # US equity session = 390 minutes/day; daily bars use 252 days directly
        bars_per_year = 252 if tf_minutes >= 1440 else int(252 * (390 / tf_minutes))

        # Need at least lookback + 1 signal bar + 1 fill bar
        if len(df) < lookback + 2:
            return BacktestResult(
                strategy_name=self._strategy.name,
                symbol=instrument.symbol,
                start=datetime.now(tz=UTC),
                end=datetime.now(tz=UTC),
                metrics=BacktestMetrics(),
                equity_curve=pd.Series(dtype=float),
            )

        has_time_col = "time" in df.columns

        def _bar_time(idx: int) -> datetime:
            if has_time_col:
                ts = df["time"].iloc[idx]
                if isinstance(ts, pd.Timestamp):
                    return ts.to_pydatetime()
                if isinstance(ts, datetime):
                    return ts
            raw = df.index[idx]
            if isinstance(raw, pd.Timestamp):
                return raw.to_pydatetime()
            return datetime.now(tz=UTC)

        equity: float = float(self._initial_equity)
        position: dict[str, Any] | None = None
        pending_entry: bool = False
        pending_exit: bool = False
        equity_values: list[float] = []
        trades_list: list[dict[str, Any]] = []

        def _open_position(open_price: float, bar_idx: int) -> None:
            nonlocal position
            qty = float(
                size_position(
                    account_equity=Decimal(str(equity)),
                    entry_price=Decimal(str(open_price)),
                    stop_loss_pct=stop_loss_pct,
                    risk_pct=risk_pct,
                )
            )
            # entry_fee is stored here but charged at close (all fees realised together)
            entry_fee = open_price * qty * self._commission_pct
            stop_price = open_price * (1.0 - stop_loss_pct / 100.0)
            tp_price: float | None = (
                open_price * (1.0 + take_profit_pct / 100.0)
                if take_profit_pct > 0.0
                else None
            )
            position = {
                "entry_price": open_price,
                "qty": qty,
                "stop": stop_price,
                "take_profit": tp_price,
                "entry_bar": bar_idx,
                "entry_time": _bar_time(bar_idx),
                "entry_fee": entry_fee,
            }

        def _close_position(exit_price: float, bar_idx: int, reason: str) -> None:
            nonlocal equity, position
            if position is None:
                return
            qty = position["qty"]
            pnl_gross = (exit_price - position["entry_price"]) * qty
            exit_fee = exit_price * qty * self._commission_pct
            pnl_net = pnl_gross - position["entry_fee"] - exit_fee
            equity += pnl_net
            trades_list.append(
                {
                    "entry_time": position["entry_time"],
                    "exit_time": _bar_time(bar_idx),
                    "entry_price": position["entry_price"],
                    "exit_price": exit_price,
                    "qty": qty,
                    "pnl_gross": pnl_gross,
                    "pnl_net": pnl_net,
                    "exit_reason": reason,
                    "entry_bar": position["entry_bar"],
                    "exit_bar": bar_idx,
                }
            )
            position = None

        for i in range(lookback, len(df)):
            row = df.iloc[i]
            bar_open = float(row["open"])
            bar_high = float(row["high"])
            bar_low = float(row["low"])
            bar_close = float(row["close"])

            # 1. Fill pending entry at this bar's open
            if pending_entry and position is None:
                _open_position(bar_open, i)
                pending_entry = False
                pending_exit = False  # clear stale exit from any prior cycle

            # 2. Fill pending exit (signal-driven close) at this bar's open
            if pending_exit and position is not None:
                _close_position(bar_open, i, "signal")
                pending_exit = False

            # 3. Check stop-loss / take-profit / max-holding on current bar
            forced_close = False
            if position is not None:
                if bar_low <= position["stop"]:
                    _close_position(position["stop"], i, "stop_loss")
                    forced_close = True
                elif position["take_profit"] is not None and bar_high >= position["take_profit"]:
                    _close_position(position["take_profit"], i, "take_profit")
                    forced_close = True
                elif max_holding_bars > 0 and i - position["entry_bar"] >= max_holding_bars:
                    _close_position(bar_close, i, "max_bars")
                    forced_close = True
            if forced_close:
                pending_exit = False  # discard pending signal exit — position already closed

            # 4. Generate signal on all candles visible up to bar i (no look-ahead)
            visible = df.iloc[: i + 1]
            current_pos: Position | None = None
            if position is not None:
                current_pos = Position(
                    symbol=instrument.symbol,
                    qty=Decimal(str(position["qty"])),
                    avg_entry_price=Decimal(str(position["entry_price"])),
                    current_price=Decimal(str(bar_close)),
                )
            ctx = StrategyContext(
                strategy_name=self._strategy.name,
                strategy_version=getattr(self._strategy, "version", "1.0"),
                mode=StrategyMode.PAPER,
                params=params,
                instrument=instrument,
                current_position=current_pos,
                account_equity=Decimal(str(equity)),
                current_time=_bar_time(i),
            )
            signal = self._strategy.generate_signal(visible, ctx)

            if position is None and signal and signal.side == SignalSide.BUY:
                pending_entry = True
            elif position is not None and signal and signal.side in (
                SignalSide.SELL,
                SignalSide.CLOSE,
            ):
                pending_exit = True

            # 5. Record equity including unrealised mark-to-market on open position
            if position is not None:
                unrealised = (bar_close - position["entry_price"]) * position["qty"]
                equity_values.append(equity + unrealised)
            else:
                equity_values.append(equity)

        # Close any position still open at end of data
        if position is not None:
            last_close = float(df.iloc[-1]["close"])
            _close_position(last_close, len(df) - 1, "end_of_data")
            if equity_values:
                equity_values[-1] = equity

        equity_curve = pd.Series(equity_values, dtype=float)
        trade_pnls = [Decimal(str(round(t["pnl_net"], 8))) for t in trades_list]
        holding_bars = [t["exit_bar"] - t["entry_bar"] for t in trades_list] or None
        metrics = compute_metrics(
            trade_pnls,
            equity_curve,
            self._initial_equity,
            holding_bars,
            bars_per_year=bars_per_year,
        )

        return BacktestResult(
            strategy_name=self._strategy.name,
            symbol=instrument.symbol,
            start=_bar_time(lookback),
            end=_bar_time(len(df) - 1),
            metrics=metrics,
            equity_curve=equity_curve,
            trades=trades_list,
        )
