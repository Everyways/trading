"""TradingRunner — main paper/live trading loop.

At each 15-minute bar close the runner:
  1. Checks market hours (Alpaca clock API)
  2. Fetches fresh candles for every strategy/symbol pair
  3. Calls strategy.generate_signal()
  4. Gates the signal through RiskManager
  5. Sizes the position and submits a market order

Scheduling uses APScheduler (AsyncIOScheduler) with a cron trigger at
minutes 0, 15, 30, 45 in the America/New_York timezone.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import pandas as pd
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.core.domain import Instrument, OrderRequest, Signal
from app.core.enums import AssetClass, OrderSide, OrderType, SignalSide, StrategyMode
from app.core.registry import strategy_registry
from app.risk.position_sizer import size_position

if TYPE_CHECKING:
    from sqlmodel import Session

    from app.execution.strategy_loader import StrategyConfig
    from app.notifications.telegram import TelegramNotifier
    from app.providers.base import BrokerProvider
    from app.risk.manager import RiskManager
    from app.strategies.base import Strategy

log = logging.getLogger(__name__)

# Minutes to look back beyond the required lookback to account for gaps/weekends
_CANDLE_BUFFER_MULTIPLIER = 2

# Timeframe string → minutes
_TF_MINUTES: dict[str, int] = {
    "1m": 1,
    "5m": 5,
    "15m": 15,
    "30m": 30,
    "1h": 60,
    "4h": 240,
    "1d": 1440,
}


def _candles_to_df(candles: list[Any]) -> pd.DataFrame:
    """Convert a list of domain Candle objects to a float DataFrame."""
    if not candles:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
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


class TradingRunner:
    """Orchestrates the paper/live trading loop for all enabled strategies.

    Args:
        provider:         Connected BrokerProvider instance.
        strategy_configs: List of parsed StrategyConfig objects.
        risk_manager:     Initialised RiskManager.
        session:          SQLModel Session for the risk manager's DB writes.
        global_config:    Parsed risk_global.yaml (used for scheduling timezone).
    """

    def __init__(
        self,
        provider: BrokerProvider,
        strategy_configs: list[StrategyConfig],
        risk_manager: RiskManager,
        session: Session,
        global_config: dict[str, Any] | None = None,
        notifier: TelegramNotifier | None = None,
    ) -> None:
        self._provider = provider
        self._cfgs = strategy_configs
        self._risk = risk_manager
        self._session = session
        self._global_config = global_config or {}
        self._notifier = notifier

        # Instantiate strategy objects once (they are stateless)
        self._strategies: dict[str, Strategy] = {}
        for cfg in self._cfgs:
            cls = strategy_registry.get(cfg.name)
            if cls is None:
                log.error("Strategy '%s' not registered — skipping", cfg.name)
                continue
            instance = cls()
            try:
                instance.validate_params(cfg.params)
            except ValueError as exc:
                log.error("Strategy '%s' invalid params: %s — skipping", cfg.name, exc)
                continue
            self._strategies[cfg.name] = instance
            log.info("Strategy ready: %s v%s", cfg.name, cfg.version)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Start the scheduler and run until interrupted."""
        if not self._strategies:
            log.error("No strategies loaded — nothing to run")
            return

        await self._provider.connect()
        strategy_names = [c.name for c in self._cfgs if c.name in self._strategies]
        log.info(
            "Connected to %s. Starting scheduler with %d strategy/symbol pair(s).",
            self._provider.name,
            sum(len(c.universe) for c in self._cfgs if c.name in self._strategies),
        )
        if self._notifier:
            await self._notifier.notify_startup(strategy_names)

        scheduler = AsyncIOScheduler(timezone="America/New_York")
        scheduler.add_job(self._tick, "cron", minute="0,15,30,45", id="main_tick")
        scheduler.add_job(
            self._risk.reset_daily_state, "cron", hour=9, minute=25, id="daily_reset"
        )
        # Reset monthly loss counter on the 1st of each month at 00:01 UTC.
        # Without this the in-memory accumulator crosses month boundaries and
        # permanently blocks trading after the first month with losses.
        scheduler.add_job(
            self._risk.reset_monthly_state,
            "cron",
            day=1,
            hour=0,
            minute=1,
            id="monthly_reset",
        )
        scheduler.start()

        try:
            await asyncio.Event().wait()   # run forever
        except (KeyboardInterrupt, SystemExit):
            log.info("Shutdown requested")
        finally:
            scheduler.shutdown(wait=False)
            await self._provider.disconnect()

    async def run_once(self) -> None:
        """Run a single evaluation tick (useful for testing / --once CLI flag)."""
        await self._provider.connect()
        try:
            await self._tick()
        finally:
            await self._provider.disconnect()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _tick(self) -> None:
        """One evaluation cycle: called at each 15-minute bar close."""
        if self._risk.is_halted():
            log.warning("Global kill switch engaged — skipping tick")
            return

        if not await self._market_is_open():
            log.debug("Market closed — skipping tick")
            return

        log.info("--- Tick %s ---", datetime.now(tz=UTC).strftime("%Y-%m-%d %H:%M UTC"))

        tasks = [
            self._evaluate(cfg, self._strategies[cfg.name], entry.symbol, entry.asset_class)
            for cfg in self._cfgs
            if cfg.name in self._strategies and not self._risk.is_halted(cfg.name)
            for entry in cfg.universe
        ]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _market_is_open(self) -> bool:
        """Check Alpaca market clock. Returns True if the market is open."""
        try:
            return await self._provider.healthcheck()
        except Exception:
            log.exception("Market hours check failed — assuming closed")
            return False

    async def _evaluate(
        self,
        cfg: StrategyConfig,
        strategy: Strategy,
        symbol: str,
        asset_class_str: str,
    ) -> None:
        """Core evaluation: candles → signal → risk gate → order."""
        try:
            # 1. Fetch candles
            tf_minutes = _TF_MINUTES.get(cfg.timeframe, 15)
            lookback_minutes = cfg.lookback * tf_minutes * _CANDLE_BUFFER_MULTIPLIER
            end = datetime.now(tz=UTC)
            start = end - timedelta(minutes=lookback_minutes)

            raw_candles = await self._provider.get_historical_candles(
                symbol, cfg.timeframe, start, end
            )
            df = _candles_to_df(raw_candles)

            if len(df) < cfg.lookback:
                log.debug(
                    "%s/%s: only %d candles (need %d) — skipping",
                    cfg.name, symbol, len(df), cfg.lookback,
                )
                return

            # 2. Get account state
            account = await self._provider.get_account()
            all_positions = await self._provider.get_positions()
            symbol_positions = [p for p in all_positions if p.symbol == symbol and not p.is_flat]

            # 3. Build context
            asset_class = AssetClass(asset_class_str)
            instrument = Instrument(
                symbol=symbol,
                asset_class=asset_class,
                provider_name=self._provider.name,
            )
            from app.strategies.base import StrategyContext  # local to avoid circular

            ctx = StrategyContext(
                strategy_name=cfg.name,
                strategy_version=cfg.version,
                mode=StrategyMode(cfg.mode),
                params=cfg.params,
                instrument=instrument,
                current_position=symbol_positions[0] if symbol_positions else None,
                account_equity=account.equity,
                current_time=end,
            )

            # 4. Generate signal
            signal = strategy.generate_signal(df, ctx)
            if signal is None:
                return

            log.info("Signal: %s %s %s — %s", cfg.name, symbol, signal.side.value, signal.reason)

            # 5. Risk gate
            placeholder_order = OrderRequest(
                symbol=symbol,
                side=OrderSide.BUY if signal.side == SignalSide.BUY else OrderSide.SELL,
                type=OrderType.MARKET,
                qty=Decimal("1"),   # placeholder qty for gate check
                strategy_name=cfg.name,
            )
            allowed, rejection_reason = self._risk.check_order(
                placeholder_order,
                account.equity,
                symbol_positions,
                cfg.name,
                cfg.risk,
            )
            if not allowed:
                log.info("Blocked: %s", rejection_reason)
                # Notify only for critical stops (kill switch / monthly limit)
                if self._notifier and any(
                    kw in rejection_reason for kw in ("kill switch", "monthly hard stop")
                ):
                    await self._notifier.notify_risk_blocked(rejection_reason, cfg.name)
                return

            # 6. Submit order
            if signal.side == SignalSide.BUY:
                await self._submit_buy(cfg, signal, df, account.equity)
            elif signal.side in (SignalSide.SELL, SignalSide.CLOSE):
                await self._submit_sell(cfg, symbol, symbol_positions)

        except Exception as exc:
            log.exception("Error evaluating %s/%s", cfg.name, symbol)
            if self._notifier:
                await self._notifier.notify_error(f"{cfg.name}/{symbol}", exc)

    async def _submit_buy(
        self,
        cfg: StrategyConfig,
        signal: Signal,
        df: pd.DataFrame,
        account_equity: Decimal,
    ) -> None:
        """Size and submit a market BUY order."""
        entry_price = Decimal(str(df["close"].iloc[-1]))
        stop_loss_pct = float(cfg.params.get("stop_loss_pct", 2.0))
        risk_pct = float(cfg.risk.get("max_risk_per_trade_pct", 1.0))

        qty = size_position(
            account_equity=account_equity,
            entry_price=entry_price,
            stop_loss_pct=stop_loss_pct,
            risk_pct=risk_pct,
        )

        order = OrderRequest(
            symbol=signal.instrument.symbol,
            side=OrderSide.BUY,
            type=OrderType.MARKET,
            qty=qty,
            strategy_name=cfg.name,
        )

        try:
            ack = await self._provider.submit_order(order)
            self._risk.record_order_submitted(cfg.name)
            log.info(
                "BUY submitted: %s qty=%s @ ~%s → %s [%s]",
                signal.instrument.symbol, qty, entry_price,
                ack.status.value, ack.broker_order_id,
            )
            if self._notifier:
                await self._notifier.notify_order(
                    "BUY", signal.instrument.symbol, qty, entry_price, cfg.name
                )
        except Exception:
            log.exception("Failed to submit BUY for %s", signal.instrument.symbol)

    async def _submit_sell(
        self,
        cfg: StrategyConfig,
        symbol: str,
        positions: list[Any],
    ) -> None:
        """Close the current position via a market SELL."""
        if not positions:
            log.debug("No open position to close for %s/%s", cfg.name, symbol)
            return

        pos = positions[0]
        qty = abs(pos.qty)

        order = OrderRequest(
            symbol=symbol,
            side=OrderSide.SELL,
            type=OrderType.MARKET,
            qty=qty,
            strategy_name=cfg.name,
        )

        try:
            ack = await self._provider.submit_order(order)
            self._risk.record_order_submitted(cfg.name)

            # PDT tracking: if the position was opened today, this counts as a day trade.
            # avg_entry_price is set when the position was opened; we approximate
            # "today" by checking if the position's unrealised PnL implies same-session.
            # A more robust check would require storing entry timestamps in DB —
            # for now we detect same-day via the position side (always long in paper mode).
            if hasattr(pos, "entry_time") and pos.entry_time is not None:
                from datetime import UTC, datetime
                if pos.entry_time.date() == datetime.now(tz=UTC).date():
                    self._risk.record_day_trade(cfg.name)

            log.info(
                "SELL submitted: %s qty=%s → %s [%s]",
                symbol, qty, ack.status.value, ack.broker_order_id,
            )
            if self._notifier:
                await self._notifier.notify_order("SELL", symbol, qty, "market", cfg.name)
        except Exception:
            log.exception("Failed to submit SELL for %s/%s", cfg.name, symbol)
