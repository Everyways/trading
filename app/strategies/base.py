"""Strategy ABC + StrategyContext — contracts all strategies must satisfy.

Rules (enforced by tests):
- generate_signal() must never do I/O (no DB, no HTTP)
- generate_signal() must be deterministic: same input → same output
- candles DataFrame must only contain closed bars (validated by the runner)
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from decimal import Decimal
from typing import Any

import pandas as pd
from pydantic import BaseModel

from app.core.domain import Instrument, Position, Signal
from app.core.enums import StrategyMode


class StrategyContext(BaseModel):
    """Immutable context passed to every generate_signal() call."""

    strategy_name: str
    strategy_version: str
    mode: StrategyMode
    params: dict[str, Any]
    instrument: Instrument
    current_position: Position | None
    account_equity: Decimal
    current_time: datetime          # timestamp of the last closed candle

    model_config = {"frozen": True}


class Strategy(ABC):
    """Abstract base class for all trading strategies.

    Implementations must be registered with @strategy_registry.register("<name>")
    and live in app/strategies/<name>.py.
    """

    name: str
    version: str
    description: str
    required_timeframe: str
    required_lookback: int

    @abstractmethod
    def generate_signal(
        self,
        candles: pd.DataFrame,
        ctx: StrategyContext,
    ) -> Signal | None:
        """Generate a trading signal from closed candles and context.

        Args:
            candles: DataFrame sorted ascending by time. The LAST row is the most
                     recently closed bar. Must never contain the currently forming bar.
            ctx: Immutable context snapshot for this evaluation cycle.

        Returns:
            A Signal if an actionable event is detected, else None.

        Contract:
            - No I/O (DB, HTTP, file system)
            - Deterministic: same input → same output
            - Must not modify candles in-place
        """

    def validate_params(self, params: dict[str, Any]) -> None:  # noqa: B027
        """Validate strategy parameters at boot time.

        Raise ValueError with a descriptive message if any parameter is invalid.
        Default implementation is a no-op (subclasses override as needed).
        """
