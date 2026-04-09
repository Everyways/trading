"""Tests for SQLModel database models — using in-memory SQLite."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlmodel import Session, select

from app.data.models import (
    AuditLog,
    Instrument,
    KillSwitch,
    OHLCV,
    Order,
    RiskEvent,
    Signal,
    Strategy,
    StrategyInstrument,
    Trade,
)


def _ts(hour: int = 12) -> datetime:
    return datetime(2026, 4, 9, hour, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Instrument
# ---------------------------------------------------------------------------

class TestInstrumentModel:
    def test_create_and_retrieve(self, db_session: Session) -> None:
        inst = Instrument(symbol="SPY", asset_class="equity", provider_name="alpaca")
        db_session.add(inst)
        db_session.commit()
        db_session.refresh(inst)

        assert inst.id is not None
        fetched = db_session.get(Instrument, inst.id)
        assert fetched is not None
        assert fetched.symbol == "SPY"

    def test_default_active_true(self, db_session: Session) -> None:
        inst = Instrument(symbol="QQQ", asset_class="equity", provider_name="alpaca")
        db_session.add(inst)
        db_session.commit()
        db_session.refresh(inst)
        assert inst.active is True

    def test_tick_size_is_decimal(self, db_session: Session) -> None:
        inst = Instrument(
            symbol="IWM",
            asset_class="equity",
            provider_name="alpaca",
            tick_size=Decimal("0.01"),
            min_qty=Decimal("0.001"),
        )
        db_session.add(inst)
        db_session.commit()
        db_session.refresh(inst)

        assert inst.tick_size == Decimal("0.01")
        assert inst.min_qty == Decimal("0.001")

    def test_unique_constraint_symbol_provider(self, db_session: Session) -> None:
        inst1 = Instrument(symbol="AAPL", asset_class="equity", provider_name="alpaca")
        inst2 = Instrument(symbol="AAPL", asset_class="equity", provider_name="alpaca")
        db_session.add(inst1)
        db_session.commit()
        db_session.add(inst2)

        with pytest.raises(Exception):  # IntegrityError
            db_session.commit()


# ---------------------------------------------------------------------------
# Strategy
# ---------------------------------------------------------------------------

class TestStrategyModel:
    def test_create_strategy(self, db_session: Session) -> None:
        strat = Strategy(name="rsi_mean_reversion", version="1.0.0", mode="paper")
        db_session.add(strat)
        db_session.commit()
        db_session.refresh(strat)

        assert strat.id is not None
        assert strat.enabled is False  # default


# ---------------------------------------------------------------------------
# OHLCV
# ---------------------------------------------------------------------------

class TestOHLCVModel:
    def test_create_ohlcv(self, db_session: Session) -> None:
        inst = Instrument(symbol="SPY", asset_class="equity", provider_name="alpaca")
        db_session.add(inst)
        db_session.commit()
        db_session.refresh(inst)

        candle = OHLCV(
            time=_ts(9),
            instrument_id=inst.id,  # type: ignore[arg-type]
            timeframe="15m",
            open=Decimal("400.00"),
            high=Decimal("405.00"),
            low=Decimal("398.00"),
            close=Decimal("402.00"),
            volume=Decimal("1000000"),
            source="rest",
        )
        db_session.add(candle)
        db_session.commit()

        fetched = db_session.exec(
            select(OHLCV).where(OHLCV.instrument_id == inst.id)
        ).first()
        assert fetched is not None
        assert fetched.close == Decimal("402.00")


# ---------------------------------------------------------------------------
# Order
# ---------------------------------------------------------------------------

class TestOrderModel:
    def test_create_order(self, db_session: Session) -> None:
        order = Order(
            client_order_id="test-uuid-001",
            provider_name="alpaca",
            side="buy",
            type="market",
            qty=Decimal("10"),
            status="pending",
            mode="paper",
        )
        db_session.add(order)
        db_session.commit()
        db_session.refresh(order)

        assert order.id is not None
        assert order.filled_qty == Decimal("0")

    def test_client_order_id_unique(self, db_session: Session) -> None:
        o1 = Order(
            client_order_id="same-uuid",
            provider_name="alpaca",
            side="buy",
            type="market",
            qty=Decimal("1"),
            status="pending",
            mode="paper",
        )
        o2 = Order(
            client_order_id="same-uuid",
            provider_name="alpaca",
            side="sell",
            type="market",
            qty=Decimal("1"),
            status="pending",
            mode="paper",
        )
        db_session.add(o1)
        db_session.commit()
        db_session.add(o2)

        with pytest.raises(Exception):  # IntegrityError
            db_session.commit()


# ---------------------------------------------------------------------------
# KillSwitch
# ---------------------------------------------------------------------------

class TestKillSwitchModel:
    def test_create_global_kill_switch(self, db_session: Session) -> None:
        ks = KillSwitch(scope="global", engaged=False)
        db_session.add(ks)
        db_session.commit()
        db_session.refresh(ks)

        assert ks.id is not None
        assert not ks.engaged

    def test_engage_kill_switch(self, db_session: Session) -> None:
        ks = KillSwitch(
            scope="global",
            engaged=True,
            engaged_at=_ts(),
            engaged_by="system",
            reason="drawdown limit",
        )
        db_session.add(ks)
        db_session.commit()
        db_session.refresh(ks)

        assert ks.engaged
        assert ks.reason == "drawdown limit"


# ---------------------------------------------------------------------------
# AuditLog
# ---------------------------------------------------------------------------

class TestAuditLogModel:
    def test_create_audit_entry(self, db_session: Session) -> None:
        entry = AuditLog(
            time=_ts(),
            actor="system",
            action="strategy.enable",
            target="rsi_mean_reversion",
            payload={"version": "1.0.0"},
        )
        db_session.add(entry)
        db_session.commit()
        db_session.refresh(entry)

        assert entry.id is not None
        assert entry.payload == {"version": "1.0.0"}  # type: ignore[comparison-overlap]


# ---------------------------------------------------------------------------
# RiskEvent
# ---------------------------------------------------------------------------

class TestRiskEventModel:
    def test_create_risk_event(self, db_session: Session) -> None:
        evt = RiskEvent(
            time=_ts(),
            scope="global",
            event_type="drawdown_limit",
            severity="critical",
            message="Global drawdown exceeded 10%",
        )
        db_session.add(evt)
        db_session.commit()
        db_session.refresh(evt)

        assert evt.id is not None
        assert evt.severity == "critical"
