"""Tests for position_sizer — pure arithmetic, no fixtures needed."""

from __future__ import annotations

from decimal import Decimal

from app.risk.position_sizer import size_position

_E = Decimal("500")    # $500 account (target Raspberry Pi setup)
_P = Decimal("400")    # $400 entry price (e.g. SPY share)


class TestSizePosition:
    def test_basic_calculation(self):
        # risk = 500 * 1% = $5
        # stop = 400 * 2% = $8
        # qty  = 5 / 8 = 0.625 → rounded to 0.625
        qty = size_position(_E, _P, stop_loss_pct=2.0, risk_pct=1.0)
        assert qty == Decimal("0.625")

    def test_larger_account(self):
        # equity=$10000, risk=1%, stop=2%, price=$400
        # qty = 100 / 8 = 12.5
        qty = size_position(Decimal("10000"), _P, stop_loss_pct=2.0, risk_pct=1.0)
        assert qty == Decimal("12.500")

    def test_min_qty_enforced_for_tiny_account(self):
        # Very small account → formula yields sub-minimum
        qty = size_position(Decimal("1"), _P, stop_loss_pct=50.0, risk_pct=0.01)
        assert qty == Decimal("0.001")   # min_qty default

    def test_custom_min_qty(self):
        qty = size_position(
            Decimal("1"), _P, stop_loss_pct=50.0, risk_pct=0.01,
            min_qty=Decimal("0.010"),
        )
        assert qty == Decimal("0.010")

    def test_max_qty_cap(self):
        # Without cap: 12.5 shares; cap at 5
        qty = size_position(
            Decimal("10000"), _P, stop_loss_pct=2.0, risk_pct=1.0,
            max_qty=Decimal("5"),
        )
        assert qty == Decimal("5")

    def test_max_qty_does_not_apply_below_cap(self):
        qty = size_position(
            _E, _P, stop_loss_pct=2.0, risk_pct=1.0,
            max_qty=Decimal("10"),
        )
        assert qty == Decimal("0.625")   # 0.625 < 10, cap not applied

    def test_zero_stop_loss_returns_min_qty(self):
        qty = size_position(_E, _P, stop_loss_pct=0.0, risk_pct=1.0)
        assert qty == Decimal("0.001")

    def test_negative_stop_loss_returns_min_qty(self):
        qty = size_position(_E, _P, stop_loss_pct=-1.0, risk_pct=1.0)
        assert qty == Decimal("0.001")

    def test_zero_entry_price_returns_min_qty(self):
        qty = size_position(_E, Decimal("0"), stop_loss_pct=2.0, risk_pct=1.0)
        assert qty == Decimal("0.001")

    def test_zero_equity_returns_min_qty(self):
        qty = size_position(Decimal("0"), _P, stop_loss_pct=2.0, risk_pct=1.0)
        assert qty == Decimal("0.001")

    def test_precision_is_three_decimal_places(self):
        # 500 * 0.5% = $2.50 risk; 400 * 1.5% = $6 stop; 2.50/6 = 0.41666...
        qty = size_position(_E, _P, stop_loss_pct=1.5, risk_pct=0.5)
        # 0.41666... → quantize to 0.001 → 0.417
        assert qty == Decimal("0.417")
