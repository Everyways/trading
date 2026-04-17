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
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pandas as pd
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.core.domain import Fill, Instrument, OrderRequest, Signal
from app.core.enums import AssetClass, OrderSide, OrderType, SignalSide, StrategyMode
from app.core.registry import strategy_registry
from app.risk.position_sizer import size_position

if TYPE_CHECKING:
    from sqlmodel import Session

    from app.execution.strategy_loader import StrategyConfig
    from app.notifications.telegram import TelegramNotifier
    from app.notifications.telegram_commands import TelegramCommandBot
    from app.providers.base import BrokerProvider
    from app.risk.manager import RiskManager
    from app.strategies.base import Strategy

log = logging.getLogger(__name__)

# Calendar-time multiplier to ensure we fetch enough closed candles.
# Markets are open ~6.5 h/day (390 min) vs 1440 min/day calendar time, plus
# weekends take out 2/7 of days.  A multiplier of 8 gives ~3–4× the required
# trading-time window so the lookback is satisfied even after a long weekend.
_CANDLE_BUFFER_MULTIPLIER = 8

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


@dataclass
class _TickEvent:
    """Records the outcome of one strategy/symbol evaluation within a tick."""
    strategy: str
    symbol: str
    # kind: "skipped" | "no_signal" | "blocked" | "order_buy" | "order_sell"
    kind: str
    signal_reason: str = ""   # strategy's own explanation of why it fired
    block_reason: str = ""    # risk manager rejection message


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
    ) -> None:
        self._provider = provider
        self._cfgs = strategy_configs
        self._risk = risk_manager
        self._session = session
        self._global_config = global_config or {}
        self._notifier = notifier
        self._command_bot = command_bot
        self._kill_file = kill_file

        # In-memory entry-time registry: symbol → UTC datetime of last BUY fill.
        # Used to detect same-session round-trips (day trades) for PDT tracking.
        self._position_entry_times: dict[str, datetime] = {}

        # Entry price registry: symbol → Decimal price at which we entered.
        # Populated on BUY submit / startup seed; consumed on SELL fill to compute PnL.
        self._position_entry_prices: dict[str, Decimal] = {}

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
        self._sync_db_references()
        await self._seed_position_entry_times()
        await self._seed_position_entry_prices()
        # Backfill any orders/trades that were missed while the process was down
        await self._reconcile_on_startup()
        # Immediately resolve any orders still marked pending from a prior run
        await self._sync_order_statuses()
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
        # Periodically sync stale pending orders with broker truth
        scheduler.add_job(
            self._sync_order_statuses,
            "interval",
            minutes=5,
            id="order_status_sync",
        )
        scheduler.start()

        command_bot_task: asyncio.Task | None = None
        if self._command_bot:
            command_bot_task = asyncio.create_task(
                self._command_bot.run(), name="telegram-cmd-bot"
            )
            log.info("Telegram command bot task started")

        fill_listener_task = asyncio.create_task(
            self._run_fill_listener(), name="fill-listener"
        )
        log.info("Fill listener task started")

        try:
            await asyncio.Event().wait()   # run forever
        except (KeyboardInterrupt, SystemExit):
            log.info("Shutdown requested")
        finally:
            scheduler.shutdown(wait=False)
            fill_listener_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await fill_listener_task
            if command_bot_task is not None:
                command_bot_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await command_bot_task
            await self._provider.disconnect()

    async def run_once(self) -> None:
        """Run a single evaluation tick (useful for testing / --once CLI flag)."""
        await self._provider.connect()
        self._sync_db_references()
        try:
            await self._tick()
        finally:
            await self._provider.disconnect()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _tick(self) -> None:
        """One evaluation cycle: called at each 15-minute bar close."""
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

        now = datetime.now(tz=UTC)
        log.info("--- Tick %s ---", now.strftime("%Y-%m-%d %H:%M UTC"))

        events: list[_TickEvent] = []
        tasks = [
            self._evaluate(cfg, self._strategies[cfg.name], entry.symbol, entry.asset_class, events)
            for cfg in self._cfgs
            if cfg.name in self._strategies and not self._risk.is_halted(cfg.name)
            for entry in cfg.universe
        ]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        await self._snapshot_positions()
        await self._check_bracket_health()

        if self._notifier:
            await self._send_tick_summary(now, events)

    def _sync_db_references(self) -> None:
        """Upsert Strategy and Instrument rows for every loaded config.

        Called at startup so that _snapshot_positions always finds the FK rows
        it needs. Without this, a fresh DB produces silent skips and the
        dashboard shows no positions even when the broker has open ones.
        Also keeps version/mode/last_loaded_at in sync with the YAML configs.
        """
        try:
            from sqlmodel import select as sqlselect

            from app.data.models import Instrument as InstrumentModel
            from app.data.models import Strategy as StrategyModel

            now = datetime.now(tz=UTC)
            for cfg in self._cfgs:
                if cfg.name not in self._strategies:
                    continue

                strat = self._session.exec(
                    sqlselect(StrategyModel).where(StrategyModel.name == cfg.name)
                ).first()
                if strat is None:
                    strat = StrategyModel(
                        name=cfg.name,
                        version=cfg.version,
                        enabled=True,
                        mode=cfg.mode,
                        last_loaded_at=now,
                    )
                    self._session.add(strat)
                else:
                    strat.version = cfg.version
                    strat.enabled = True
                    strat.mode = cfg.mode
                    strat.last_loaded_at = now
                    self._session.add(strat)

                for entry in cfg.universe:
                    instr = self._session.exec(
                        sqlselect(InstrumentModel).where(
                            InstrumentModel.symbol == entry.symbol,
                            InstrumentModel.provider_name == cfg.provider,
                        )
                    ).first()
                    if instr is None:
                        self._session.add(InstrumentModel(
                            symbol=entry.symbol,
                            asset_class=entry.asset_class,
                            provider_name=cfg.provider,
                            active=True,
                        ))

            self._session.commit()
            log.info("DB references synced (%d strategies)", len(self._cfgs))
        except Exception:
            log.exception("Failed to sync DB references — snapshot writes may fail")

    async def _snapshot_positions(self) -> None:
        """Write a PositionSnapshot for every configured strategy/symbol pair.

        Non-flat positions get their live qty/pnl.  Flat (or missing) positions
        get qty=0 so the dashboard's abs(qty) filter removes stale entries.
        Iterates all (strategy, symbol) pairs rather than only broker positions,
        fixing two prior bugs: stale closed positions stayed visible forever,
        and two strategies sharing a symbol caused one to be silently dropped.
        """
        try:
            from sqlmodel import select as sqlselect

            from app.data.models import Instrument as InstrumentModel
            from app.data.models import PositionSnapshot
            from app.data.models import Strategy as StrategyModel

            positions = await self._provider.get_positions()
            pos_by_symbol = {p.symbol: p for p in positions if not p.is_flat}
            now = datetime.now(tz=UTC)

            for cfg in self._cfgs:
                if cfg.name not in self._strategies:
                    continue

                strat_row = self._session.exec(
                    sqlselect(StrategyModel).where(StrategyModel.name == cfg.name)
                ).first()
                if strat_row is None:
                    continue

                for entry in cfg.universe:
                    instr_row = self._session.exec(
                        sqlselect(InstrumentModel).where(
                            InstrumentModel.symbol == entry.symbol,
                            InstrumentModel.provider_name == cfg.provider,
                        )
                    ).first()
                    if instr_row is None:
                        continue

                    pos = pos_by_symbol.get(entry.symbol)
                    snap = PositionSnapshot(
                        time=now,
                        strategy_id=strat_row.id,
                        instrument_id=instr_row.id,
                        qty=pos.qty if pos else Decimal("0"),
                        avg_entry=pos.avg_entry_price if pos else None,
                        unrealized_pnl=pos.unrealized_pnl if pos else Decimal("0"),
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

    async def _seed_position_entry_prices(self) -> None:
        """Seed entry-price registry from live broker positions at startup."""
        try:
            positions = await self._provider.get_positions()
            seeded = 0
            for pos in positions:
                if not pos.is_flat and pos.symbol not in self._position_entry_prices:
                    self._position_entry_prices[pos.symbol] = pos.avg_entry_price
                    seeded += 1
            if seeded:
                log.info("Seeded entry prices for %d open position(s) from broker", seeded)
        except Exception:
            log.exception("Failed to seed position entry prices — PnL tracking may be incomplete")

    async def _reconcile_on_startup(self) -> None:
        """Backfill orders and trades missed while the process was down.

        Queries the broker for all closed orders since the last order timestamp
        in the DB (or 24 h ago as fallback), inserts missing Order rows, and
        derives Trade rows for SELL fills whose entry price can be recovered.
        """
        try:
            from sqlmodel import select as sqlselect

            from app.data.models import Instrument as InstrumentModel
            from app.data.models import Order as OrderModel
            from app.data.models import Trade as TradeModel

            # Watermark: last submitted_at we have in DB
            last_order = self._session.exec(
                sqlselect(OrderModel).order_by(OrderModel.submitted_at.desc()).limit(1)
            ).first()
            since = (
                last_order.submitted_at
                if last_order and last_order.submitted_at
                else datetime.now(tz=UTC) - timedelta(hours=24)
            )

            log.info("Reconciliation: fetching broker orders since %s", since.strftime("%Y-%m-%d %H:%M UTC"))
            closed_orders = await self._provider.list_closed_orders(since)
            if not closed_orders:
                log.info("Reconciliation: no closed orders since watermark")
                return

            # Index existing broker_order_ids to skip duplicates
            existing_ids: set[str] = set()
            for ack in closed_orders:
                row = self._session.exec(
                    sqlselect(OrderModel).where(
                        OrderModel.broker_order_id == ack.broker_order_id
                    )
                ).first()
                if row:
                    existing_ids.add(ack.broker_order_id)

            new_acks = [a for a in closed_orders if a.broker_order_id not in existing_ids]
            if not new_acks:
                log.info("Reconciliation: all %d closed order(s) already in DB", len(closed_orders))
                return

            log.info("Reconciliation: backfilling %d missing order(s)", len(new_acks))

            for ack in new_acks:
                instr_row = self._session.exec(
                    sqlselect(InstrumentModel).where(
                        InstrumentModel.symbol == ack.symbol,
                        InstrumentModel.provider_name == self._provider.name,
                    )
                ).first()
                db_order = OrderModel(
                    client_order_id=ack.client_order_id,
                    broker_order_id=ack.broker_order_id,
                    provider_name=self._provider.name,
                    strategy_id=None,   # unknown after restart
                    instrument_id=instr_row.id if instr_row else None,
                    side=str(ack.side),
                    type=str(ack.type),
                    qty=ack.qty,
                    status=str(ack.status),
                    filled_qty=ack.filled_qty,
                    avg_fill_price=ack.avg_fill_price,
                    submitted_at=ack.submitted_at,
                    filled_at=ack.filled_at,
                    mode="paper",   # safe fallback
                )
                self._session.add(db_order)

            self._session.commit()

            # Derive Trade records for SELL fills that closed a position
            sell_acks = [
                a for a in new_acks
                if a.side == OrderSide.SELL
                and a.avg_fill_price
                and a.filled_at
            ]
            trades_created = 0
            for sell in sell_acks:
                symbol = sell.symbol
                exit_time = sell.filled_at

                # Skip if a trade with this approximate exit time already exists
                existing_trade = self._session.exec(
                    sqlselect(TradeModel).where(
                        TradeModel.exit_time >= exit_time - timedelta(seconds=30),
                        TradeModel.exit_time <= exit_time + timedelta(seconds=30),
                    )
                ).first()
                if existing_trade:
                    continue

                # Entry price: prefer in-memory (seeded from open position), then
                # look for a matching BUY in the closed orders we just fetched.
                entry_price = self._position_entry_prices.get(symbol)
                entry_time = self._position_entry_times.get(symbol)

                if entry_price is None:
                    buy_candidates = [
                        a for a in closed_orders
                        if a.side == OrderSide.BUY
                        and a.symbol == symbol
                        and a.avg_fill_price
                        and a.filled_at
                        and a.filled_at < exit_time
                    ]
                    if buy_candidates:
                        best_buy = max(buy_candidates, key=lambda a: a.filled_at)  # type: ignore[arg-type]
                        entry_price = best_buy.avg_fill_price
                        entry_time = best_buy.filled_at

                if entry_price is None:
                    log.debug("Reconciliation: no entry price for %s — trade skipped", symbol)
                    continue

                exit_price = sell.avg_fill_price
                qty = sell.filled_qty
                pnl_gross = (exit_price - entry_price) * qty
                pnl_net = pnl_gross
                duration_s = int((exit_time - entry_time).total_seconds()) if entry_time else 0

                instr_row = self._session.exec(
                    sqlselect(InstrumentModel).where(
                        InstrumentModel.symbol == symbol,
                        InstrumentModel.provider_name == self._provider.name,
                    )
                ).first()

                trade = TradeModel(
                    strategy_id=None,
                    instrument_id=instr_row.id if instr_row else None,
                    entry_time=entry_time,
                    exit_time=exit_time,
                    entry_price=entry_price,
                    exit_price=exit_price,
                    qty=qty,
                    side="buy",
                    pnl_gross=pnl_gross,
                    pnl_net=pnl_net,
                    fees=Decimal("0"),
                    duration_seconds=duration_s,
                    mode="paper",
                )
                self._session.add(trade)
                # Remove from in-memory tracking — position is now closed
                self._position_entry_prices.pop(symbol, None)
                self._position_entry_times.pop(symbol, None)
                trades_created += 1

            if trades_created:
                self._session.commit()

            log.info(
                "Reconciliation complete: %d new order(s), %d new trade(s)",
                len(new_acks), trades_created,
            )
        except Exception:
            log.exception("Startup reconciliation failed — some orders/trades may be missing")

    async def _sync_order_statuses(self) -> None:
        """Pull current status from broker for any orders still 'pending' in the DB.

        Prevents phantom open orders on the dashboard after a freeze+restart.
        Called at startup and scheduled every 5 minutes.
        """
        try:
            from sqlmodel import select as sqlselect

            from app.data.models import Order as OrderModel

            stale = self._session.exec(
                sqlselect(OrderModel).where(
                    OrderModel.status.in_(["pending", "submitted", "partially_filled"])
                )
            ).all()

            if not stale:
                return

            updated = 0
            for db_order in stale:
                if not db_order.broker_order_id:
                    continue
                try:
                    ack = await self._provider.get_order(db_order.broker_order_id)
                    new_status = str(ack.status)
                    if new_status != db_order.status:
                        db_order.status = new_status
                        db_order.filled_qty = ack.filled_qty
                        db_order.avg_fill_price = ack.avg_fill_price
                        db_order.filled_at = ack.filled_at
                        self._session.add(db_order)
                        updated += 1
                except Exception:
                    log.debug("Could not fetch order status for %s", db_order.broker_order_id)

            if updated:
                self._session.commit()
                log.info("Order status sync: updated %d stale order(s)", updated)
        except Exception:
            log.exception("Order status sync failed")

    def _persist_order(
        self,
        order: OrderRequest,
        ack: "OrderAck",
        cfg: "StrategyConfig",
    ) -> int | None:
        """Write an Order row to the DB. Returns the row id or None on failure."""
        try:
            from sqlmodel import select as sqlselect

            from app.data.models import Instrument as InstrumentModel
            from app.data.models import Order as OrderModel
            from app.data.models import Strategy as StrategyModel

            strat_row = self._session.exec(
                sqlselect(StrategyModel).where(
                    StrategyModel.name == (order.strategy_name or cfg.name)
                )
            ).first()
            instr_row = self._session.exec(
                sqlselect(InstrumentModel).where(
                    InstrumentModel.symbol == order.symbol,
                    InstrumentModel.provider_name == self._provider.name,
                )
            ).first()

            db_order = OrderModel(
                client_order_id=ack.client_order_id,
                broker_order_id=ack.broker_order_id,
                provider_name=self._provider.name,
                strategy_id=strat_row.id if strat_row else None,
                instrument_id=instr_row.id if instr_row else None,
                side=str(order.side),
                type=str(order.type),
                qty=order.qty,
                limit_price=order.limit_price,
                stop_price=order.stop_price,
                time_in_force=str(order.time_in_force) if order.time_in_force else None,
                status=str(ack.status),
                filled_qty=ack.filled_qty,
                avg_fill_price=ack.avg_fill_price,
                submitted_at=ack.submitted_at,
                filled_at=ack.filled_at,
                mode=cfg.mode,
            )
            self._session.add(db_order)
            self._session.commit()
            self._session.refresh(db_order)
            return db_order.id
        except Exception:
            log.exception("Failed to persist order %s", ack.broker_order_id)
            return None

    async def _run_fill_listener(self) -> None:
        """Consume fill events with exponential-backoff reconnect on any failure.

        The WebSocket connection to Alpaca can drop silently (network hiccup,
        Raspberry Pi Wi-Fi stutter, broker restart).  This loop ensures the
        stream is always re-established so no fill is permanently missed.
        """
        backoff = 5.0
        while True:
            try:
                log.info("Fill listener: connecting to broker stream")
                async for fill in self._provider.stream_fills():
                    backoff = 5.0   # reset backoff on any successful message
                    await self._on_fill(fill)
                # stream_fills() returned normally — broker closed the stream
                log.warning("Fill stream ended — reconnecting in %.0fs", backoff)
            except asyncio.CancelledError:
                log.info("Fill listener: shutdown")
                raise
            except Exception:
                log.exception("Fill listener error — reconnecting in %.0fs", backoff)
            try:
                await asyncio.sleep(backoff)
            except asyncio.CancelledError:
                raise
            backoff = min(backoff * 2, 300.0)   # cap at 5 min

    async def _on_fill(self, fill: Fill) -> None:
        """Handle a fill event.

        BUY fill  → record the entry price for future PnL calculation.
        SELL fill → compute PnL from tracked entry, write a Trade row to the DB.
        """
        try:
            symbol = fill.symbol
            log.info(
                "Fill: %s %s qty=%s @ %s",
                fill.side.value.upper(), symbol, fill.qty, fill.price,
            )

            if fill.side == OrderSide.BUY:
                if symbol not in self._position_entry_prices:
                    self._position_entry_prices[symbol] = fill.price
                return

            # SELL fill — close the tracked position and record the round-trip
            entry_price = self._position_entry_prices.pop(symbol, None)
            entry_time = self._position_entry_times.pop(symbol, None)
            if entry_price is None:
                log.debug("No entry price tracked for %s — cannot record trade PnL", symbol)
                return

            exit_price = fill.price
            exit_time = fill.timestamp
            qty = fill.qty
            pnl_gross = (exit_price - entry_price) * qty
            fees = fill.fee
            pnl_net = pnl_gross - fees
            duration_s = int((exit_time - entry_time).total_seconds()) if entry_time else 0

            cfg = next(
                (c for c in self._cfgs if any(e.symbol == symbol for e in c.universe)),
                None,
            )

            try:
                from sqlmodel import select as sqlselect

                from app.data.models import Instrument as InstrumentModel
                from app.data.models import Strategy as StrategyModel
                from app.data.models import Trade as TradeModel

                strat_row = None
                if cfg:
                    strat_row = self._session.exec(
                        sqlselect(StrategyModel).where(StrategyModel.name == cfg.name)
                    ).first()
                instr_row = self._session.exec(
                    sqlselect(InstrumentModel).where(
                        InstrumentModel.symbol == symbol,
                        InstrumentModel.provider_name == self._provider.name,
                    )
                ).first()

                trade = TradeModel(
                    strategy_id=strat_row.id if strat_row else None,
                    instrument_id=instr_row.id if instr_row else None,
                    entry_time=entry_time,
                    exit_time=exit_time,
                    entry_price=entry_price,
                    exit_price=exit_price,
                    qty=qty,
                    side="buy",
                    pnl_gross=pnl_gross,
                    pnl_net=pnl_net,
                    fees=fees,
                    duration_seconds=duration_s,
                    mode=cfg.mode if cfg else "paper",
                )
                self._session.add(trade)
                self._session.commit()

                sign = "+" if pnl_net >= 0 else ""
                log.info(
                    "Trade recorded: LONG %s qty=%s entry=%s exit=%s PnL=%s%s",
                    symbol, qty, entry_price, exit_price, sign, pnl_net,
                )
            except Exception:
                log.exception("Failed to persist Trade record for %s", symbol)

            if self._notifier:
                await self._notifier.notify_trade_closed(
                    symbol=symbol,
                    qty=qty,
                    entry_price=entry_price,
                    exit_price=exit_price,
                    pnl_net=pnl_net,
                    strategy=cfg.name if cfg else "unknown",
                )
        except Exception:
            log.exception("Error processing fill")

    async def _check_bracket_health(self) -> None:
        """Verify that every open position has a protective stop order.

        Bracket legs (stop-loss sell) can be silently cancelled by Alpaca after
        certain events (e.g. partial fills, market halts). This watchdog re-places
        an emergency stop-loss when the protective leg is missing.
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
                has_stop = any(
                    o.side == OrderSide.SELL
                    and o.type in (OrderType.STOP, OrderType.STOP_LIMIT)
                    for o in open_orders
                )
                if has_stop:
                    continue

                avg_entry = pos.avg_entry_price
                if avg_entry <= 0:
                    log.warning("Bracket watchdog: %s has no valid avg entry price", pos.symbol)
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
                    log.exception("Bracket watchdog: failed to place stop for %s", pos.symbol)
        except Exception:
            log.exception("Bracket health check failed")

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

    async def _send_tick_summary(
        self,
        tick_time: datetime,
        events: list[_TickEvent],
    ) -> None:
        """Build and send the per-tick Telegram digest."""
        from zoneinfo import ZoneInfo

        et = tick_time.astimezone(ZoneInfo("America/New_York"))
        time_str = et.strftime("%d %b %H:%M ET")

        total = len(events)
        skipped = sum(1 for e in events if e.kind == "skipped")
        no_signal = sum(1 for e in events if e.kind == "no_signal")

        notable: list[str] = []
        for ev in events:
            if ev.kind == "blocked":
                line = (
                    f"\u26d4 *Blocked* `{ev.strategy}/{ev.symbol}`"   # ⛔
                    f" \u2014 {ev.block_reason}"
                )
                if ev.signal_reason:
                    line += f"\n  _Signal: {ev.signal_reason}_"
                notable.append(line)
            elif ev.kind in ("order_buy", "order_sell"):
                side_label = "BUY \U0001f7e2" if ev.kind == "order_buy" else "SELL \U0001f534"
                line = f"{side_label} `{ev.strategy}/{ev.symbol}`"
                if ev.signal_reason:
                    line += f"\n  _Signal: {ev.signal_reason}_"
                notable.append(line)

        await self._notifier.notify_tick_summary(
            time_str=time_str,
            total_pairs=total,
            skipped_pairs=skipped,
            no_signal_pairs=no_signal,
            notable_lines=notable,
        )

    async def _evaluate(
        self,
        cfg: StrategyConfig,
        strategy: Strategy,
        symbol: str,
        asset_class_str: str,
        events: list[_TickEvent],
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
                events.append(_TickEvent(cfg.name, symbol, "skipped"))
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
                events.append(_TickEvent(cfg.name, symbol, "no_signal"))
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
                events.append(_TickEvent(
                    cfg.name, symbol, "blocked",
                    signal_reason=signal.reason,
                    block_reason=rejection_reason,
                ))
                # Critical stops also get an immediate standalone alert
                if self._notifier and any(
                    kw in rejection_reason for kw in ("kill switch", "monthly hard stop")
                ):
                    await self._notifier.notify_risk_blocked(rejection_reason, cfg.name)
                return

            # 6. Submit order
            if signal.side == SignalSide.BUY:
                await self._submit_buy(cfg, signal, df, account.equity)
                events.append(_TickEvent(cfg.name, symbol, "order_buy", signal_reason=signal.reason))
            elif signal.side in (SignalSide.SELL, SignalSide.CLOSE):
                await self._submit_sell(cfg, symbol, symbol_positions)
                events.append(_TickEvent(cfg.name, symbol, "order_sell", signal_reason=signal.reason))

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

        # Alpaca rejects bracket orders with fractional quantities.
        # Round up to the nearest whole share when bracket legs are attached.
        if tp_price is not None and qty != qty.to_integral_value():
            import math
            qty = Decimal(math.ceil(qty))

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
            # Track entry price so the SELL fill handler can compute PnL
            self._position_entry_prices[signal.instrument.symbol] = entry_price
            self._persist_order(order, ack, cfg)
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

        # Ensure entry price is tracked before we lose the position.
        # Covers the case where the bot restarted after the BUY fill and
        # _position_entry_prices was seeded from avg_entry_price but _on_fill
        # never ran for that BUY.
        if symbol not in self._position_entry_prices and pos.avg_entry_price > 0:
            self._position_entry_prices[symbol] = pos.avg_entry_price
            log.debug("Captured entry price for %s from open position: %s", symbol, pos.avg_entry_price)

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
            # Note: do NOT pop _position_entry_times/_prices here — the fill listener
            # will consume those when the SELL fill actually arrives.
            entry_dt = self._position_entry_times.get(symbol)
            if entry_dt is not None and entry_dt.date() == datetime.now(tz=UTC).date():
                self._risk.record_day_trade(cfg.name)

            log.info(
                "SELL submitted: %s qty=%s → %s [%s]",
                symbol, qty, ack.status.value, ack.broker_order_id,
            )
            self._persist_order(order, ack, cfg)
            if self._notifier:
                await self._notifier.notify_order("SELL", symbol, qty, "market", cfg.name)
        except Exception:
            log.exception("Failed to submit SELL for %s/%s", cfg.name, symbol)
