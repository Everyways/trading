"""SQLModel database models — mapped 1-to-1 with §8.1 schema.

Rules:
- All monetary columns use NUMERIC(20, 10) via sa_column=Column(Numeric(20, 10))
- Primary keys use Field(default=None, primary_key=True) for SQLite/PG compatibility.
  The migration sets BIGINT for large tables in PostgreSQL.
- Append-only except kill_switches, strategies.enabled/mode, instruments.active
- client_order_id is always generated BEFORE the broker call (idempotence)
- JSON (portable) is used for JSONB columns; the migration casts to JSONB on PG
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlmodel import Field, SQLModel

# ---------------------------------------------------------------------------
# instruments
# ---------------------------------------------------------------------------

class Instrument(SQLModel, table=True):
    """Tradable instruments known to the bot."""

    __tablename__ = "instruments"
    __table_args__ = (
        UniqueConstraint("symbol", "provider_name", name="uq_instruments_symbol_provider"),
    )

    id: int | None = Field(default=None, primary_key=True)
    symbol: str = Field(sa_column=Column(String(32), nullable=False))
    asset_class: str = Field(sa_column=Column(String(32), nullable=False))
    provider_name: str = Field(sa_column=Column(String(64), nullable=False))
    tick_size: Decimal | None = Field(
        default=None, sa_column=Column(Numeric(20, 10))
    )
    min_qty: Decimal | None = Field(
        default=None, sa_column=Column(Numeric(20, 10))
    )
    active: bool = Field(default=True, sa_column=Column(Boolean, nullable=False))


# ---------------------------------------------------------------------------
# ohlcv  (TimescaleDB hypertable — created via migration)
# ---------------------------------------------------------------------------

class OHLCV(SQLModel, table=True):
    """OHLCV price bars. Converted to a TimescaleDB hypertable in migration."""

    __tablename__ = "ohlcv"
    __table_args__ = (
        Index("ix_ohlcv_instrument_timeframe_time", "instrument_id", "timeframe", "time"),
    )

    # Composite PK
    time: datetime = Field(
        sa_column=Column(DateTime(timezone=True), primary_key=True, nullable=False)
    )
    instrument_id: int = Field(
        sa_column=Column(
            Integer, ForeignKey("instruments.id"), primary_key=True, nullable=False
        )
    )
    timeframe: str = Field(
        sa_column=Column(String(8), primary_key=True, nullable=False)
    )
    open: Decimal = Field(sa_column=Column(Numeric(20, 10), nullable=False))
    high: Decimal = Field(sa_column=Column(Numeric(20, 10), nullable=False))
    low: Decimal = Field(sa_column=Column(Numeric(20, 10), nullable=False))
    close: Decimal = Field(sa_column=Column(Numeric(20, 10), nullable=False))
    volume: Decimal = Field(sa_column=Column(Numeric(20, 10), nullable=False))
    source: str | None = Field(
        default=None, sa_column=Column(String(8))
    )  # 'rest' | 'ws'


# ---------------------------------------------------------------------------
# strategies
# ---------------------------------------------------------------------------

class Strategy(SQLModel, table=True):
    """Registry of known strategies and their current configuration state."""

    __tablename__ = "strategies"

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(sa_column=Column(String(128), unique=True, nullable=False))
    version: str = Field(sa_column=Column(String(32), nullable=False))
    enabled: bool = Field(default=False, sa_column=Column(Boolean, nullable=False))
    mode: str = Field(sa_column=Column(String(8), nullable=False))  # 'paper' | 'live'
    config_hash: str | None = Field(default=None, sa_column=Column(String(64)))
    last_loaded_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )


# ---------------------------------------------------------------------------
# strategy_instruments  (join table)
# ---------------------------------------------------------------------------

class StrategyInstrument(SQLModel, table=True):
    """Many-to-many join between strategies and instruments."""

    __tablename__ = "strategy_instruments"

    strategy_id: int = Field(
        sa_column=Column(
            Integer, ForeignKey("strategies.id"), primary_key=True, nullable=False
        )
    )
    instrument_id: int = Field(
        sa_column=Column(
            Integer, ForeignKey("instruments.id"), primary_key=True, nullable=False
        )
    )


# ---------------------------------------------------------------------------
# signals
# ---------------------------------------------------------------------------

class Signal(SQLModel, table=True):
    """Trading signals generated by strategies."""

    __tablename__ = "signals"

    id: int | None = Field(default=None, primary_key=True)
    time: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False)
    )
    strategy_id: int | None = Field(
        default=None, sa_column=Column(Integer, ForeignKey("strategies.id"))
    )
    instrument_id: int | None = Field(
        default=None, sa_column=Column(Integer, ForeignKey("instruments.id"))
    )
    side: str = Field(sa_column=Column(String(8), nullable=False))  # 'buy'|'sell'|'close'
    reason: str | None = Field(default=None, sa_column=Column(Text))
    context: dict[str, Any] | None = Field(
        default=None, sa_column=Column(JSON)
    )
    executed: bool = Field(default=False, sa_column=Column(Boolean, nullable=False))
    rejected_reason: str | None = Field(default=None, sa_column=Column(Text))


# ---------------------------------------------------------------------------
# orders
# ---------------------------------------------------------------------------

class Order(SQLModel, table=True):
    """All orders submitted to a broker."""

    __tablename__ = "orders"

    id: int | None = Field(default=None, primary_key=True)
    client_order_id: str = Field(
        sa_column=Column(String(64), unique=True, nullable=False)
    )
    broker_order_id: str | None = Field(
        default=None, sa_column=Column(String(128), unique=True)
    )
    provider_name: str = Field(sa_column=Column(String(64), nullable=False))
    signal_id: int | None = Field(
        default=None, sa_column=Column(Integer, ForeignKey("signals.id"))
    )
    strategy_id: int | None = Field(
        default=None, sa_column=Column(Integer, ForeignKey("strategies.id"))
    )
    instrument_id: int | None = Field(
        default=None, sa_column=Column(Integer, ForeignKey("instruments.id"))
    )
    side: str = Field(sa_column=Column(String(8), nullable=False))
    type: str = Field(sa_column=Column(String(16), nullable=False))
    qty: Decimal = Field(sa_column=Column(Numeric(20, 10), nullable=False))
    limit_price: Decimal | None = Field(
        default=None, sa_column=Column(Numeric(20, 10))
    )
    stop_price: Decimal | None = Field(
        default=None, sa_column=Column(Numeric(20, 10))
    )
    time_in_force: str | None = Field(default=None, sa_column=Column(String(8)))
    status: str = Field(sa_column=Column(String(32), nullable=False))
    filled_qty: Decimal = Field(
        default=Decimal("0"), sa_column=Column(Numeric(20, 10), nullable=False)
    )
    avg_fill_price: Decimal | None = Field(
        default=None, sa_column=Column(Numeric(20, 10))
    )
    submitted_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )
    filled_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )
    error_message: str | None = Field(default=None, sa_column=Column(Text))
    mode: str = Field(sa_column=Column(String(8), nullable=False))  # 'paper' | 'live'


# ---------------------------------------------------------------------------
# trades
# ---------------------------------------------------------------------------

class Trade(SQLModel, table=True):
    """Completed round-trip trades (entry + exit)."""

    __tablename__ = "trades"

    id: int | None = Field(default=None, primary_key=True)
    strategy_id: int | None = Field(
        default=None, sa_column=Column(Integer, ForeignKey("strategies.id"))
    )
    instrument_id: int | None = Field(
        default=None, sa_column=Column(Integer, ForeignKey("instruments.id"))
    )
    entry_order_id: int | None = Field(
        default=None, sa_column=Column(Integer, ForeignKey("orders.id"))
    )
    exit_order_id: int | None = Field(
        default=None, sa_column=Column(Integer, ForeignKey("orders.id"))
    )
    entry_time: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )
    exit_time: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )
    entry_price: Decimal | None = Field(
        default=None, sa_column=Column(Numeric(20, 10))
    )
    exit_price: Decimal | None = Field(
        default=None, sa_column=Column(Numeric(20, 10))
    )
    qty: Decimal | None = Field(default=None, sa_column=Column(Numeric(20, 10)))
    side: str | None = Field(default=None, sa_column=Column(String(8)))
    pnl_gross: Decimal | None = Field(
        default=None, sa_column=Column(Numeric(20, 10))
    )
    pnl_net: Decimal | None = Field(
        default=None, sa_column=Column(Numeric(20, 10))
    )
    fees: Decimal | None = Field(default=None, sa_column=Column(Numeric(20, 10)))
    duration_seconds: int | None = Field(
        default=None, sa_column=Column(Integer)
    )
    mode: str = Field(sa_column=Column(String(8), nullable=False))  # 'paper' | 'live'


# ---------------------------------------------------------------------------
# positions_snapshot  (written every 60s)
# ---------------------------------------------------------------------------

class PositionSnapshot(SQLModel, table=True):
    """Periodic snapshot of open positions, written every 60 seconds."""

    __tablename__ = "positions_snapshot"

    # Composite PK: (time, strategy_id, instrument_id)
    time: datetime = Field(
        sa_column=Column(DateTime(timezone=True), primary_key=True, nullable=False)
    )
    strategy_id: int = Field(
        sa_column=Column(
            Integer, ForeignKey("strategies.id"), primary_key=True, nullable=False
        )
    )
    instrument_id: int = Field(
        sa_column=Column(
            Integer, ForeignKey("instruments.id"), primary_key=True, nullable=False
        )
    )
    qty: Decimal | None = Field(default=None, sa_column=Column(Numeric(20, 10)))
    avg_entry: Decimal | None = Field(
        default=None, sa_column=Column(Numeric(20, 10))
    )
    unrealized_pnl: Decimal | None = Field(
        default=None, sa_column=Column(Numeric(20, 10))
    )
    mode: str = Field(sa_column=Column(String(8), nullable=False))


# ---------------------------------------------------------------------------
# risk_events
# ---------------------------------------------------------------------------

class RiskEvent(SQLModel, table=True):
    """Risk management events — drawdowns, kill switches, limit breaches."""

    __tablename__ = "risk_events"

    id: int | None = Field(default=None, primary_key=True)
    time: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False)
    )
    scope: str = Field(sa_column=Column(String(16), nullable=False))  # 'global'|'strategy'
    strategy_id: int | None = Field(
        default=None, sa_column=Column(Integer, ForeignKey("strategies.id"))
    )
    event_type: str = Field(sa_column=Column(String(64), nullable=False))
    severity: str = Field(sa_column=Column(String(16), nullable=False))  # info|warn|critical
    message: str | None = Field(default=None, sa_column=Column(Text))
    payload: dict[str, Any] | None = Field(
        default=None, sa_column=Column(JSON)
    )


# ---------------------------------------------------------------------------
# kill_switches
# ---------------------------------------------------------------------------

class KillSwitch(SQLModel, table=True):
    """Current kill switch state — global or per-strategy."""

    __tablename__ = "kill_switches"
    __table_args__ = (
        UniqueConstraint("scope", "strategy_id", name="uq_kill_switches_scope_strategy"),
    )

    id: int | None = Field(default=None, primary_key=True)
    scope: str = Field(sa_column=Column(String(16), nullable=False))  # 'global'|'strategy'
    strategy_id: int | None = Field(
        default=None, sa_column=Column(Integer, ForeignKey("strategies.id"))
    )
    engaged: bool = Field(default=False, sa_column=Column(Boolean, nullable=False))
    engaged_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )
    engaged_by: str | None = Field(default=None, sa_column=Column(String(128)))
    reason: str | None = Field(default=None, sa_column=Column(Text))


# ---------------------------------------------------------------------------
# audit_log
# ---------------------------------------------------------------------------

class AuditLog(SQLModel, table=True):
    """Immutable audit trail for all significant actions."""

    __tablename__ = "audit_log"

    id: int | None = Field(default=None, primary_key=True)
    time: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False)
    )
    actor: str = Field(sa_column=Column(String(128), nullable=False))  # 'system'|'user:x'
    action: str = Field(sa_column=Column(String(128), nullable=False))  # 'strategy.enable'
    target: str | None = Field(default=None, sa_column=Column(String(256)))
    payload: dict[str, Any] | None = Field(
        default=None, sa_column=Column(JSON)
    )


# ---------------------------------------------------------------------------
# api_keys_metadata  (key rotation tracking — §22.6)
# ---------------------------------------------------------------------------

class APIKeyMetadata(SQLModel, table=True):
    """Tracks API key lifecycle for rotation reminders."""

    __tablename__ = "api_keys_metadata"

    id: int | None = Field(default=None, primary_key=True)
    provider: str = Field(sa_column=Column(String(64), nullable=False))
    created_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )
    rotated_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )
    expires_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )
    notes: str | None = Field(default=None, sa_column=Column(Text))
