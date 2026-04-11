"""Backtest performance metrics.

All functions are pure (no I/O). Inputs are pandas Series / plain Python lists.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import Decimal

import pandas as pd


@dataclass
class BacktestMetrics:
    """Aggregated performance metrics for one backtest run."""

    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0

    gross_profit: Decimal = field(default_factory=lambda: Decimal("0"))
    gross_loss: Decimal = field(default_factory=lambda: Decimal("0"))
    net_pnl: Decimal = field(default_factory=lambda: Decimal("0"))

    total_return_pct: float = 0.0
    max_drawdown_pct: float = 0.0
    sharpe_ratio: float = 0.0
    profit_factor: float = 0.0
    win_rate_pct: float = 0.0

    avg_win: Decimal = field(default_factory=lambda: Decimal("0"))
    avg_loss: Decimal = field(default_factory=lambda: Decimal("0"))
    avg_holding_bars: float = 0.0

    def __str__(self) -> str:
        return (
            f"Trades: {self.total_trades}  "
            f"Win rate: {self.win_rate_pct:.1f}%  "
            f"Net PnL: ${self.net_pnl:.2f}  "
            f"Return: {self.total_return_pct:.2f}%  "
            f"Max DD: {self.max_drawdown_pct:.2f}%  "
            f"Sharpe: {self.sharpe_ratio:.2f}  "
            f"PF: {self.profit_factor:.2f}"
        )


def compute_metrics(
    trade_pnls: list[Decimal],
    equity_curve: pd.Series,
    initial_equity: Decimal,
    holding_bars: list[int] | None = None,
    bars_per_year: int = 252 * 26,   # 252 trading days × 26 bars/day for 15m
) -> BacktestMetrics:
    """Compute performance metrics from a completed backtest.

    Args:
        trade_pnls:     Net PnL per closed trade (positive = win, negative = loss).
        equity_curve:   Equity value at each bar (indexed 0..N).
        initial_equity: Starting equity (used for return % calculation).
        holding_bars:   Number of bars each trade was held (optional).
        bars_per_year:  Annualisation factor for Sharpe (default = 15m bars/year).

    Returns:
        Populated BacktestMetrics dataclass.
    """
    m = BacktestMetrics()
    m.total_trades = len(trade_pnls)

    if m.total_trades == 0:
        return m

    wins = [p for p in trade_pnls if p > 0]
    losses = [p for p in trade_pnls if p <= 0]

    m.winning_trades = len(wins)
    m.losing_trades = len(losses)
    m.gross_profit = sum(wins, Decimal("0"))
    m.gross_loss = sum(losses, Decimal("0"))
    m.net_pnl = m.gross_profit + m.gross_loss

    m.win_rate_pct = m.winning_trades / m.total_trades * 100

    if m.gross_loss != 0:
        m.profit_factor = float(m.gross_profit / abs(m.gross_loss))
    else:
        m.profit_factor = float("inf") if m.gross_profit > 0 else 0.0

    if m.winning_trades > 0:
        m.avg_win = m.gross_profit / m.winning_trades
    if m.losing_trades > 0:
        m.avg_loss = m.gross_loss / m.losing_trades

    if holding_bars:
        m.avg_holding_bars = sum(holding_bars) / len(holding_bars)

    if initial_equity > 0:
        final = equity_curve.iloc[-1] if not equity_curve.empty else float(initial_equity)
        m.total_return_pct = (final - float(initial_equity)) / float(initial_equity) * 100

    m.max_drawdown_pct = _max_drawdown(equity_curve)
    m.sharpe_ratio = _sharpe(equity_curve, bars_per_year)

    return m


def _max_drawdown(equity: pd.Series) -> float:
    """Maximum peak-to-trough drawdown as a percentage."""
    if equity.empty or len(equity) < 2:
        return 0.0
    running_max = equity.cummax()
    drawdown = (equity - running_max) / running_max * 100
    return float(drawdown.min())   # most negative value = worst drawdown


def _sharpe(equity: pd.Series, bars_per_year: int) -> float:
    """Annualised Sharpe ratio (risk-free rate = 0)."""
    if equity.empty or len(equity) < 2:
        return 0.0
    returns = equity.pct_change().dropna()
    if returns.std() == 0:
        return 0.0
    return float(returns.mean() / returns.std() * math.sqrt(bars_per_year))
