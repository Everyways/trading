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
import contextlib
import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pandas as pd
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.core.domain import Instrument, OrderRequest, Signal
from app.core.enums import AssetClass, OrderSide, OrderType, SignalSide, StrategyMode
from app.core.registry import strategy_registry
from app.risk.position_sizer import size_position
from app.risk.regime import MarketRegime, RegimeDetector

if TYPE_CHECKING:
    from sqlmodel import Session

    from app.execution.strategy_loader import StrategyConfig
    from app.notifications.telegram import TelegramNotifier
    from app.notifications.telegram_commands import TelegramCommandBot
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
        command_bot: TelegramCommandBot | None = None,
        kill_file: Path = Path("KILL"),
        resume_file: Path = Path("RESUME"),
    ) -> None:
        self._provider = provider
        self._cfgs = strategy_configs
        self._risk = risk_manager
        self._session = session
        self._global_config = global_config or {}
        self._notifier = notifier
        self._command_bot = command_bot
        self._kill_file = kill_file
        self._resume_file = resume_file

        self._regime_detector = RegimeDetector()

        # In-memory entry-time registry: symbol → UTC datetime of last BUY fill.
        # Used to detect same-session round-trips (day trades) for PDT tracking.
        self._position_entry_times: dict[str, datetime] = {}

        # Tracks whether we already liquidated after the kill switch engaged,
        # so we do not re-submit SELLs on every subsequent halted tick.
        self._positions_liquidated: bool = False

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
        await self._seed_position_entry_times()
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
        scheduler.add_job(
            self._daily_report,
            "cron",
            hour=16,
            minute=30,
            id="daily_report",
        )
        scheduler.start()

        command_bot_task: asyncio.Task | None = None
        if self._command_bot:
            command_bot_task = asyncio.create_task(
                self._command_bot.run(), name="telegram-cmd-bot"
            )
            log.info("Telegram command bot task started")

        try:
            await asyncio.Event().wait()   # run forever
        except (KeyboardInterrupt, SystemExit):
            log.info("Shutdown requested")
        finally:
            scheduler.shutdown(wait=False)
            if command_bot_task is not None:
                command_bot_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await command_bot_task
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
        # RESUME sentinel: reset the in-memory kill switch if requested by dashboard.
        # Checked BEFORE the KILL file so a stale KILL doesn't immediately re-engage.
        if self._resume_file.exists():
            self._risk.reset_kill_switch(reset_by="dashboard")
            self._positions_liquidated = False
            try:
                self._resume_file.unlink()
            except OSError:
                log.exception("Failed to remove RESUME sentinel %s", self._resume_file)
            # Also remove any lingering KILL file so the next check doesn't re-engage
            if self._kill_file.exists():
                try:
                    self._kill_file.unlink()
                except OSError:
                    log.exception("Failed to remove stale KILL file %s", self._kill_file)

        # File-based kill switch: check the sentinel file every tick.
        # Most reliable emergency stop — works even if the in-memory flag was reset.
        if self._kill_file.exists():
            self._risk.engage_kill_switch("global", reason="KILL file detected")

        if self._risk.is_halted():
            if not self._positions_liquidated:
                log.warning("Kill switch engaged — liquidating all open positions")
                await self._liquidate_all_positions()
                self._positions_liquidated = True
            else:
                log.debug("Kill switch engaged — positions already liquidated, skipping tick")
            return
        # Kill switch may have been reset externally (e.g. via dashboard)
        self._positions_liquidated = False

        if not await self._market_is_open():
            log.info("Market closed — skipping tick")
            return

        log.info("--- Tick %s ---", datetime.now(tz=UTC).strftime("%Y-%m-%d %H:%M UTC"))

        regime = await self._detect_regime()
        log.info("Market regime: %s", regime.value)

        tasks = []
        for cfg in self._cfgs:
            if cfg.name not in self._strategies or self._risk.is_halted(cfg.name):
                continue
            if cfg.favourable_regimes and regime.value not in cfg.favourable_regimes:
                log.info(
                    "Regime gate: skipping %s (regime=%s, allowed=%s)",
                    cfg.name, regime.value, cfg.favourable_regimes,
                )
                continue
            for entry in cfg.universe:
                tasks.append(
                    self._evaluate(
                        cfg, self._strategies[cfg.name], entry.symbol, entry.asset_class
                    )
                )
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        await self._snapshot_positions()
        await self._check_bracket_health()

    async def _snapshot_positions(self) -> None:
        """Write a PositionSnapshot record for every non-flat broker position."""
        try:
            from sqlmodel import select as sqlselect

            from app.data.models import Instrument as InstrumentModel
            from app.data.models import PositionSnapshot
            from app.data.models import Strategy as StrategyModel

            positions = await self._provider.get_positions()
            if not positions:
                return

            now = datetime.now(tz=UTC)
            # Build symbol → strategy-name map from loaded configs
            symbol_to_cfg = {
                entry.symbol: cfg
                for cfg in self._cfgs
                if cfg.name in self._strategies
                for entry in cfg.universe
            }

            for pos in positions:
                if pos.is_flat:
                    continue
                cfg = symbol_to_cfg.get(pos.symbol)
                if cfg is None:
                    continue

                instr_row = self._session.exec(
                    sqlselect(InstrumentModel).where(
                        InstrumentModel.symbol == pos.symbol
                    )
                ).first()
                strat_row = self._session.exec(
                    sqlselect(StrategyModel).where(StrategyModel.name == cfg.name)
                ).first()
                if instr_row is None or strat_row is None:
                    continue

                snap = PositionSnapshot(
                    time=now,
                    strategy_id=strat_row.id,
                    instrument_id=instr_row.id,
                    qty=pos.qty,
                    avg_entry=pos.avg_entry_price,
                    unrealized_pnl=pos.unrealized_pnl,
                    mode=cfg.mode,
                )
                self._session.add(snap)
            self._session.commit()
        except Exception:
            log.exception("Failed to write position snapshots")

    async def _seed_position_entry_times(self) -> None:
        """Populate the entry-time registry from live broker positions at startup.

        Without this, PDT detection would miss round-trips opened before the
        current process started (e.g. after a restart mid-session).
        We don't know the exact fill time, so we use today's midnight UTC as a
        conservative proxy — any same-day sell will still be flagged as a day trade.
        """
        try:
            positions = await self._provider.get_positions()
            now = datetime.now(tz=UTC)
            today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            seeded = 0
            for pos in positions:
                if not pos.is_flat and pos.symbol not in self._position_entry_times:
                    self._position_entry_times[pos.symbol] = today_start
                    seeded += 1
            if seeded:
                log.info("Seeded entry times for %d open position(s) from broker", seeded)
        except Exception:
            log.exception("Failed to seed position entry times — PDT tracking may be incomplete")

    async def _check_bracket_health(self) -> None:
        """Verify that every open position has a protective stop order.

        Bracket legs (stop-loss sell) can be silently cancelled by Alpaca after
        certain events (e.g. partial fills, market halts). This watchdog re-places
        an emergency stop-loss when the protective leg is missing.

        If the strategy has ``trailing_stop_atr_multiplier`` set, the stop is
        ratcheted upward (never downward) to ``current_price − ATR × multiplier``
        whenever the proposed stop exceeds the existing stop price.
        """
        try:
            positions = await self._provider.get_positions()
            open_pos = [p for p in positions if not p.is_flat]
            if not open_pos:
                return

            for pos in open_pos:
                cfg = next(
                    (
                        c for c in self._cfgs
                        if any(e.symbol == pos.symbol for e in c.universe)
                    ),
                    None,
                )
                if cfg is None:
                    continue

                open_orders = await self._provider.list_open_orders(symbol=pos.symbol)
                stop_orders = [
                    o for o in open_orders
                    if o.side == OrderSide.SELL
                    and o.type in (OrderType.STOP, OrderType.STOP_LIMIT)
                ]
                has_stop = bool(stop_orders)

                if not has_stop:
                    avg_entry = pos.avg_entry_price
                    if avg_entry <= 0:
                        log.warning(
                            "Bracket watchdog: %s has no valid avg entry price", pos.symbol
                        )
                        continue

                    stop_loss_pct = float(cfg.params.get("stop_loss_pct", 2.0))
                    sl_price = (
                        avg_entry * (1 - Decimal(str(stop_loss_pct)) / 100)
                    ).quantize(Decimal("0.01"))

                    log.warning(
                        "Bracket watchdog: no stop order for %s — placing emergency stop @ %s",
                        pos.symbol, sl_price,
                    )
                    emergency_order = OrderRequest(
                        symbol=pos.symbol,
                        side=OrderSide.SELL,
                        type=OrderType.STOP,
                        qty=abs(pos.qty),
                        strategy_name="bracket_watchdog",
                        stop_price=sl_price,
                    )
                    try:
                        ack = await self._provider.submit_order(emergency_order)
                        log.warning(
                            "Emergency stop placed: %s qty=%s stop=%s → %s [%s]",
                            pos.symbol, abs(pos.qty), sl_price,
                            ack.status.value, ack.broker_order_id,
                        )
                    except Exception:
                        log.exception(
                            "Bracket watchdog: failed to place stop for %s", pos.symbol
                        )
                    continue

                # Trailing stop ratchet — only when configured and position has current price
                atr_mult = float(cfg.params.get("trailing_stop_atr_multiplier", 0.0))
                if atr_mult <= 0 or pos.current_price is None:
                    continue

                atr = await self._compute_atr(pos.symbol, cfg.timeframe)
                if atr is None or atr <= 0:
                    continue

                current_price = float(pos.current_price)
                proposed_stop = Decimal(
                    str(round(current_price - float(atr) * atr_mult, 2))
                )

                # Find the highest existing stop price among all stop-sell orders
                existing_stop = max(
                    (o.stop_price for o in stop_orders if o.stop_price is not None),
                    default=None,
                )
                if existing_stop is None or proposed_stop <= existing_stop:
                    continue

                log.info(
                    "Trailing stop raised: %s %s → %s (ATR=%.4f × %.1f)",
                    pos.symbol, existing_stop, proposed_stop, float(atr), atr_mult,
                )
                for stop_order in stop_orders:
                    try:
                        await self._provider.cancel_order(stop_order.broker_order_id)
                    except Exception:
                        log.exception(
                            "Trailing stop: failed to cancel old stop %s for %s",
                            stop_order.broker_order_id, pos.symbol,
                        )
                        break
                else:
                    new_stop = OrderRequest(
                        symbol=pos.symbol,
                        side=OrderSide.SELL,
                        type=OrderType.STOP,
                        qty=abs(pos.qty),
                        strategy_name="trailing_stop",
                        stop_price=proposed_stop,
                    )
                    try:
                        ack = await self._provider.submit_order(new_stop)
                        log.info(
                            "Trailing stop placed: %s qty=%s stop=%s → %s [%s]",
                            pos.symbol, abs(pos.qty), proposed_stop,
                            ack.status.value, ack.broker_order_id,
                        )
                    except Exception:
                        log.exception(
                            "Trailing stop: failed to place new stop for %s", pos.symbol
                        )
        except Exception:
            log.exception("Bracket health check failed")

    async def _compute_atr(self, symbol: str, timeframe: str, period: int = 14) -> Decimal | None:
        """Compute ATR for *symbol* on *timeframe* using the last ``period`` bars."""
        try:
            tf_minutes = _TF_MINUTES.get(timeframe, 15)
            lookback_minutes = period * tf_minutes * _CANDLE_BUFFER_MULTIPLIER
            end = datetime.now(tz=UTC)
            start = end - timedelta(minutes=lookback_minutes)
            candles = await self._provider.get_historical_candles(symbol, timeframe, start, end)
            df = _candles_to_df(candles)
            if len(df) < period + 1:
                return None
            prev_close = df["close"].shift(1)
            tr = pd.concat(
                [
                    df["high"] - df["low"],
                    (df["high"] - prev_close).abs(),
                    (df["low"] - prev_close).abs(),
                ],
                axis=1,
            ).max(axis=1)
            atr = tr.ewm(span=period, adjust=False).mean().iloc[-1]
            return Decimal(str(round(float(atr), 4)))
        except Exception:
            log.exception("ATR computation failed for %s/%s", symbol, timeframe)
            return None

    async def _liquidate_all_positions(self) -> None:
        """Emergency market-sell of every non-flat position (kill switch path)."""
        try:
            positions = await self._provider.get_positions()
            open_pos = [p for p in positions if not p.is_flat]
            if not open_pos:
                log.info("Kill switch: no open positions to liquidate")
                return
            log.warning("Kill switch: liquidating %d open position(s)", len(open_pos))
            for pos in open_pos:
                order = OrderRequest(
                    symbol=pos.symbol,
                    side=OrderSide.SELL,
                    type=OrderType.MARKET,
                    qty=abs(pos.qty),
                    strategy_name="kill_switch",
                )
                try:
                    ack = await self._provider.submit_order(order)
                    log.warning(
                        "Kill switch SELL: %s qty=%s → %s [%s]",
                        pos.symbol, abs(pos.qty), ack.status.value, ack.broker_order_id,
                    )
                    if self._notifier:
                        await self._notifier.notify_kill_switch(
                            scope="global",
                            reason=f"auto-liquidated {pos.symbol} qty={abs(pos.qty)}",
                        )
                except Exception:
                    log.exception("Kill switch: failed to liquidate %s", pos.symbol)
        except Exception:
            log.exception("Kill switch: failed to fetch positions for liquidation")

    async def _market_is_open(self) -> bool:
        """Check Alpaca market clock. Returns True if the market is open."""
        try:
            return await self._provider.healthcheck()
        except Exception:
            log.exception("Market hours check failed — assuming closed")
            return False

    async def _detect_regime(self) -> MarketRegime:
        """Fetch SPY daily bars and classify the current market regime."""
        try:
            end = datetime.now(tz=UTC)
            start = end - timedelta(days=260)
            candles = await self._provider.get_historical_candles("SPY", "1d", start, end)
            df = _candles_to_df(candles)
            if df.empty:
                log.warning("Regime detector: no SPY daily candles — defaulting to CHOP")
                return MarketRegime.CHOP
            return self._regime_detector.detect(df)
        except Exception:
            log.exception("Regime detection failed — defaulting to CHOP")
            return MarketRegime.CHOP

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
                log.info(
                    "%s/%s: only %d candles (need %d) — skipping",
                    cfg.name, symbol, len(df), cfg.lookback,
                )
                return

            log.info("%s/%s: %d candles fetched", cfg.name, symbol, len(df))

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
                log.debug("%s/%s: no signal this bar", cfg.name, symbol)
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
        """Size and submit a market BUY order.

        If the strategy config contains ``take_profit_pct`` the order is
        submitted as a bracket order: Alpaca attaches a stop-loss sell leg
        (at ``stop_loss_pct`` below entry) and a take-profit limit sell leg
        (at ``take_profit_pct`` above entry) as child orders automatically.
        """
        entry_price = Decimal(str(df["close"].iloc[-1]))
        stop_loss_pct = float(cfg.params.get("stop_loss_pct", 2.0))
        take_profit_pct = float(cfg.params.get("take_profit_pct", 0.0))
        risk_pct = float(cfg.risk.get("max_risk_per_trade_pct", 1.0))

        qty = size_position(
            account_equity=account_equity,
            entry_price=entry_price,
            stop_loss_pct=stop_loss_pct,
            risk_pct=risk_pct,
        )

        # Bracket prices (both required for Alpaca bracket order class)
        sl_price = (entry_price * (1 - Decimal(str(stop_loss_pct)) / 100)).quantize(
            Decimal("0.01")
        )
        tp_price: Decimal | None = None
        if take_profit_pct > 0:
            tp_price = (
                entry_price * (1 + Decimal(str(take_profit_pct)) / 100)
            ).quantize(Decimal("0.01"))

        order = OrderRequest(
            symbol=signal.instrument.symbol,
            side=OrderSide.BUY,
            type=OrderType.MARKET,
            qty=qty,
            strategy_name=cfg.name,
            stop_loss_price=sl_price,
            take_profit_price=tp_price,   # None → plain market order
        )

        try:
            ack = await self._provider.submit_order(order)
            self._risk.record_order_submitted(cfg.name)
            bracket_info = (
                f" [bracket SL={sl_price} TP={tp_price}]" if tp_price else f" [SL={sl_price}]"
            )
            log.info(
                "BUY submitted: %s qty=%s @ ~%s%s → %s [%s]",
                signal.instrument.symbol, qty, entry_price, bracket_info,
                ack.status.value, ack.broker_order_id,
            )
            self._position_entry_times[signal.instrument.symbol] = datetime.now(tz=UTC)
            if self._notifier:
                await self._notifier.notify_order(
                    "BUY", signal.instrument.symbol, qty, entry_price, cfg.name
                )
        except Exception:
            log.exception("Failed to submit BUY for %s", signal.instrument.symbol)

    async def _daily_report(self) -> None:
        """Compile and send the end-of-day Telegram report at 16:30 ET.

        Queries today's closed trades from the DB, groups them by strategy,
        then calls notify_daily_report() on the notifier.
        """
        if not self._notifier:
            return
        try:
            from sqlmodel import select as sqlselect

            from app.data.models import Strategy as StrategyModel
            from app.data.models import Trade as TradeModel

            now = datetime.now(tz=UTC)
            today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            today_end = today_start + timedelta(days=1)

            trades_today = self._session.exec(
                sqlselect(TradeModel).where(
                    TradeModel.exit_time >= today_start,
                    TradeModel.exit_time < today_end,
                )
            ).all()

            total = len(trades_today)
            wins = sum(1 for t in trades_today if (t.pnl_net or Decimal("0")) > 0)
            win_rate = wins / total * 100 if total > 0 else 0.0
            net_pnl = sum((t.pnl_net or Decimal("0")) for t in trades_today)

            # Group by strategy_id
            by_strategy: dict[int, list] = {}
            for t in trades_today:
                if t.strategy_id:
                    by_strategy.setdefault(t.strategy_id, []).append(t)

            strat_summary: list[tuple[str, int, Decimal]] = []
            for strat_id, strat_trades in sorted(by_strategy.items()):
                strat_row = self._session.exec(
                    sqlselect(StrategyModel).where(StrategyModel.id == strat_id)
                ).first()
                name = strat_row.name if strat_row else f"strategy_{strat_id}"
                strat_pnl = sum((t.pnl_net or Decimal("0")) for t in strat_trades)
                strat_summary.append((name, len(strat_trades), strat_pnl))

            # Open positions from broker (best-effort)
            open_positions = 0
            try:
                positions = await self._provider.get_positions()
                open_positions = sum(1 for p in positions if not p.is_flat)
            except Exception:
                log.warning("Daily report: could not fetch open positions count")

            date_str = now.strftime("%d/%m/%Y")
            await self._notifier.notify_daily_report(
                date_str=date_str,
                net_pnl=net_pnl,
                trades=total,
                wins=wins,
                win_rate_pct=win_rate,
                monthly_loss_eur=self._risk.monthly_loss_eur,
                open_positions=open_positions,
                strategy_summary=strat_summary,
            )
            log.info("Daily report sent for %s", date_str)
        except Exception:
            log.exception("Failed to send daily report")

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

            # PDT tracking: check our in-memory registry for the entry time.
            # If the BUY was placed today, this SELL is a same-session round-trip → day trade.
            entry_dt = self._position_entry_times.pop(symbol, None)
            if entry_dt is not None and entry_dt.date() == datetime.now(tz=UTC).date():
                self._risk.record_day_trade(cfg.name)

            log.info(
                "SELL submitted: %s qty=%s → %s [%s]",
                symbol, qty, ack.status.value, ack.broker_order_id,
            )
            if self._notifier:
                await self._notifier.notify_order("SELL", symbol, qty, "market", cfg.name)
        except Exception:
            log.exception("Failed to submit SELL for %s/%s", cfg.name, symbol)
