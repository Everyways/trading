"""Initial schema — all tables from §8.1

Revision ID: 0001
Revises:
Create Date: 2026-04-09
"""

from __future__ import annotations

import contextlib

import sqlalchemy as sa
import sqlmodel  # noqa: F401
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    # --- instruments ---
    op.create_table(
        "instruments",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("asset_class", sa.String(32), nullable=False),
        sa.Column("provider_name", sa.String(64), nullable=False),
        sa.Column("tick_size", sa.Numeric(20, 10), nullable=True),
        sa.Column("min_qty", sa.Numeric(20, 10), nullable=True),
        sa.Column("active", sa.Boolean, nullable=False, server_default="true"),
        sa.UniqueConstraint("symbol", "provider_name", name="uq_instruments_symbol_provider"),
    )

    # --- strategies ---
    op.create_table(
        "strategies",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(128), nullable=False, unique=True),
        sa.Column("version", sa.String(32), nullable=False),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("mode", sa.String(8), nullable=False),
        sa.Column("config_hash", sa.String(64), nullable=True),
        sa.Column("last_loaded_at", sa.DateTime(timezone=True), nullable=True),
    )

    # --- strategy_instruments ---
    op.create_table(
        "strategy_instruments",
        sa.Column("strategy_id", sa.Integer, sa.ForeignKey("strategies.id"), primary_key=True),
        sa.Column("instrument_id", sa.Integer, sa.ForeignKey("instruments.id"), primary_key=True),
    )

    # --- ohlcv ---
    op.create_table(
        "ohlcv",
        sa.Column("time", sa.DateTime(timezone=True), primary_key=True, nullable=False),
        sa.Column("instrument_id", sa.Integer, sa.ForeignKey("instruments.id"), primary_key=True),
        sa.Column("timeframe", sa.String(8), primary_key=True, nullable=False),
        sa.Column("open", sa.Numeric(20, 10), nullable=False),
        sa.Column("high", sa.Numeric(20, 10), nullable=False),
        sa.Column("low", sa.Numeric(20, 10), nullable=False),
        sa.Column("close", sa.Numeric(20, 10), nullable=False),
        sa.Column("volume", sa.Numeric(20, 10), nullable=False),
        sa.Column("source", sa.String(8), nullable=True),
    )
    op.create_index(
        "ix_ohlcv_instrument_timeframe_time",
        "ohlcv",
        ["instrument_id", "timeframe", "time"],
    )

    # Convert ohlcv to TimescaleDB hypertable (no-op on non-TimescaleDB DBs)
    with contextlib.suppress(Exception):
        # TimescaleDB not available (e.g. plain PostgreSQL or SQLite in tests)
        op.execute(
            "SELECT create_hypertable('ohlcv', 'time', if_not_exists => TRUE, "
            "migrate_data => TRUE)"
        )

    # --- signals ---
    op.create_table(
        "signals",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("strategy_id", sa.Integer, sa.ForeignKey("strategies.id"), nullable=True),
        sa.Column("instrument_id", sa.Integer, sa.ForeignKey("instruments.id"), nullable=True),
        sa.Column("side", sa.String(8), nullable=False),
        sa.Column("reason", sa.Text, nullable=True),
        sa.Column("context", sa.JSON, nullable=True),
        sa.Column("executed", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("rejected_reason", sa.Text, nullable=True),
    )

    # --- orders ---
    op.create_table(
        "orders",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("client_order_id", sa.String(64), nullable=False, unique=True),
        sa.Column("broker_order_id", sa.String(128), nullable=True, unique=True),
        sa.Column("provider_name", sa.String(64), nullable=False),
        sa.Column("signal_id", sa.BigInteger, sa.ForeignKey("signals.id"), nullable=True),
        sa.Column("strategy_id", sa.Integer, sa.ForeignKey("strategies.id"), nullable=True),
        sa.Column("instrument_id", sa.Integer, sa.ForeignKey("instruments.id"), nullable=True),
        sa.Column("side", sa.String(8), nullable=False),
        sa.Column("type", sa.String(16), nullable=False),
        sa.Column("qty", sa.Numeric(20, 10), nullable=False),
        sa.Column("limit_price", sa.Numeric(20, 10), nullable=True),
        sa.Column("stop_price", sa.Numeric(20, 10), nullable=True),
        sa.Column("time_in_force", sa.String(8), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("filled_qty", sa.Numeric(20, 10), nullable=False, server_default="0"),
        sa.Column("avg_fill_price", sa.Numeric(20, 10), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("filled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column("mode", sa.String(8), nullable=False),
    )

    # --- trades ---
    op.create_table(
        "trades",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("strategy_id", sa.Integer, sa.ForeignKey("strategies.id"), nullable=True),
        sa.Column("instrument_id", sa.Integer, sa.ForeignKey("instruments.id"), nullable=True),
        sa.Column("entry_order_id", sa.BigInteger, sa.ForeignKey("orders.id"), nullable=True),
        sa.Column("exit_order_id", sa.BigInteger, sa.ForeignKey("orders.id"), nullable=True),
        sa.Column("entry_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("exit_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("entry_price", sa.Numeric(20, 10), nullable=True),
        sa.Column("exit_price", sa.Numeric(20, 10), nullable=True),
        sa.Column("qty", sa.Numeric(20, 10), nullable=True),
        sa.Column("side", sa.String(8), nullable=True),
        sa.Column("pnl_gross", sa.Numeric(20, 10), nullable=True),
        sa.Column("pnl_net", sa.Numeric(20, 10), nullable=True),
        sa.Column("fees", sa.Numeric(20, 10), nullable=True),
        sa.Column("duration_seconds", sa.Integer, nullable=True),
        sa.Column("mode", sa.String(8), nullable=False),
    )

    # --- positions_snapshot ---
    op.create_table(
        "positions_snapshot",
        sa.Column("time", sa.DateTime(timezone=True), primary_key=True, nullable=False),
        sa.Column(
            "strategy_id", sa.Integer, sa.ForeignKey("strategies.id"),
            primary_key=True, nullable=False
        ),
        sa.Column(
            "instrument_id", sa.Integer, sa.ForeignKey("instruments.id"),
            primary_key=True, nullable=False
        ),
        sa.Column("qty", sa.Numeric(20, 10), nullable=True),
        sa.Column("avg_entry", sa.Numeric(20, 10), nullable=True),
        sa.Column("unrealized_pnl", sa.Numeric(20, 10), nullable=True),
        sa.Column("mode", sa.String(8), nullable=False),
    )

    # --- risk_events ---
    op.create_table(
        "risk_events",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("scope", sa.String(16), nullable=False),
        sa.Column("strategy_id", sa.Integer, sa.ForeignKey("strategies.id"), nullable=True),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("message", sa.Text, nullable=True),
        sa.Column("payload", sa.JSON, nullable=True),
    )

    # --- kill_switches ---
    op.create_table(
        "kill_switches",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("scope", sa.String(16), nullable=False),
        sa.Column("strategy_id", sa.Integer, sa.ForeignKey("strategies.id"), nullable=True),
        sa.Column("engaged", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("engaged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("engaged_by", sa.String(128), nullable=True),
        sa.Column("reason", sa.Text, nullable=True),
        sa.UniqueConstraint("scope", "strategy_id", name="uq_kill_switches_scope_strategy"),
    )

    # --- audit_log ---
    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor", sa.String(128), nullable=False),
        sa.Column("action", sa.String(128), nullable=False),
        sa.Column("target", sa.String(256), nullable=True),
        sa.Column("payload", sa.JSON, nullable=True),
    )

    # --- api_keys_metadata ---
    op.create_table(
        "api_keys_metadata",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rotated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notes", sa.Text, nullable=True),
    )


def downgrade() -> None:
    op.drop_table("api_keys_metadata")
    op.drop_table("audit_log")
    op.drop_table("kill_switches")
    op.drop_table("risk_events")
    op.drop_table("positions_snapshot")
    op.drop_table("trades")
    op.drop_table("orders")
    op.drop_table("signals")
    op.drop_index("ix_ohlcv_instrument_timeframe_time", "ohlcv")
    op.drop_table("ohlcv")
    op.drop_table("strategy_instruments")
    op.drop_table("strategies")
    op.drop_table("instruments")
