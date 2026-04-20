"""Unit tests for EarningsCalendar — mock yfinance, verify cache TTL + override precedence."""

from __future__ import annotations

import json
import tempfile
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from app.risk.earnings_calendar import EarningsCalendar, _trading_days_until


# ---------------------------------------------------------------------------
# Helper tests
# ---------------------------------------------------------------------------

class TestTradingDaysUntil:
    def test_same_day_is_zero(self) -> None:
        d = date(2024, 1, 15)  # Monday
        assert _trading_days_until(d, d) == 0

    def test_one_trading_day_ahead(self) -> None:
        # Monday to Tuesday = 1 trading day
        assert _trading_days_until(date(2024, 1, 16), date(2024, 1, 15)) == 1

    def test_weekend_skipped(self) -> None:
        # Friday to next Monday = 1 trading day (Saturday + Sunday skipped)
        assert _trading_days_until(date(2024, 1, 22), date(2024, 1, 19)) == 1

    def test_five_day_span(self) -> None:
        # Monday to Friday of the same week = 4 trading days
        assert _trading_days_until(date(2024, 1, 19), date(2024, 1, 15)) == 4


# ---------------------------------------------------------------------------
# EarningsCalendar tests (mocked yfinance)
# ---------------------------------------------------------------------------

def _make_calendar(
    tmp_path: Path,
    *,
    overrides: dict | None = None,
    cache: dict | None = None,
    blackout_days: int = 2,
) -> EarningsCalendar:
    cache_file = tmp_path / "earnings_cache.json"
    override_file = tmp_path / "earnings_overrides.yaml"
    if overrides is not None:
        override_file.write_text(yaml.dump(overrides))
    if cache is not None:
        cache_file.write_text(json.dumps(cache))
    return EarningsCalendar(
        cache_file=cache_file,
        override_file=override_file,
        blackout_days=blackout_days,
    )


class TestOverridePrecedence:
    def test_override_takes_priority_over_yfinance(self, tmp_path: Path) -> None:
        future_date = (datetime.now(tz=UTC).date() + timedelta(days=1)).isoformat()
        cal = _make_calendar(tmp_path, overrides={"AAPL": [future_date]})
        # Should be blocked regardless of what yfinance would return
        with patch("app.risk.earnings_calendar.EarningsCalendar._fetch_from_yfinance") as m:
            assert cal.is_blackout("AAPL") is True
            m.assert_not_called()

    def test_symbol_case_insensitive_in_overrides(self, tmp_path: Path) -> None:
        future_date = (datetime.now(tz=UTC).date() + timedelta(days=1)).isoformat()
        cal = _make_calendar(tmp_path, overrides={"aapl": [future_date]})
        assert cal.is_blackout("AAPL") is True

    def test_past_override_date_not_blocked(self, tmp_path: Path) -> None:
        past_date = (datetime.now(tz=UTC).date() - timedelta(days=10)).isoformat()
        cal = _make_calendar(tmp_path, overrides={"AAPL": [past_date]})
        assert cal.is_blackout("AAPL") is False


class TestCacheTTL:
    def test_fresh_cache_not_refetched(self, tmp_path: Path) -> None:
        future = (datetime.now(tz=UTC).date() + timedelta(days=1)).isoformat()
        now_iso = datetime.now(tz=UTC).isoformat()
        cache = {"TSLA": {"dates": [future], "fetched_at": now_iso}}
        cal = _make_calendar(tmp_path, cache=cache)
        with patch("app.risk.earnings_calendar.EarningsCalendar._fetch_from_yfinance") as m:
            assert cal.is_blackout("TSLA") is True
            m.assert_not_called()

    def test_stale_cache_triggers_refetch(self, tmp_path: Path) -> None:
        future = (datetime.now(tz=UTC).date() + timedelta(days=1)).isoformat()
        stale_iso = (datetime.now(tz=UTC) - timedelta(hours=25)).isoformat()
        cache = {"TSLA": {"dates": [future], "fetched_at": stale_iso}}
        cal = _make_calendar(tmp_path, cache=cache)
        with patch("app.risk.earnings_calendar.EarningsCalendar._fetch_from_yfinance") as m:
            m.return_value = []
            cal.is_blackout("TSLA")
            m.assert_called_once_with("TSLA")

    def test_empty_cache_triggers_fetch(self, tmp_path: Path) -> None:
        cal = _make_calendar(tmp_path)
        with patch("app.risk.earnings_calendar.EarningsCalendar._fetch_from_yfinance") as m:
            m.return_value = []
            cal.is_blackout("NVDA")
            m.assert_called_once_with("NVDA")


class TestBlackoutWindow:
    @pytest.mark.parametrize("days_ahead", [0, 1, 2])
    def test_blocked_within_blackout_window(self, tmp_path: Path, days_ahead: int) -> None:
        today = datetime.now(tz=UTC).date()
        # Find a weekday `days_ahead` trading days from today
        target = today
        count = 0
        while count < days_ahead:
            target += timedelta(days=1)
            if target.weekday() < 5:
                count += 1
        future_iso = target.isoformat()
        cal = _make_calendar(tmp_path, overrides={"SPY": [future_iso]})
        assert cal.is_blackout("SPY", check_date=today) is True

    def test_not_blocked_outside_blackout_window(self, tmp_path: Path) -> None:
        today = datetime.now(tz=UTC).date()
        far_future = today + timedelta(days=30)
        cal = _make_calendar(tmp_path, overrides={"SPY": [far_future.isoformat()]})
        assert cal.is_blackout("SPY", check_date=today) is False


class TestYfinanceFetch:
    def test_yfinance_import_error_returns_empty(self, tmp_path: Path) -> None:
        cal = _make_calendar(tmp_path)
        with patch.dict("sys.modules", {"yfinance": None}):
            result = cal._fetch_from_yfinance("AAPL")
        assert result == []

    def test_yfinance_exception_returns_empty(self, tmp_path: Path) -> None:
        cal = _make_calendar(tmp_path)
        with patch("app.risk.earnings_calendar.EarningsCalendar._fetch_from_yfinance") as m:
            m.side_effect = Exception("network error")
            # is_blackout should not raise, just return False
            assert cal.is_blackout("AAPL") is False

    def test_yfinance_returns_dict_with_timestamp(self, tmp_path: Path) -> None:
        future_date = datetime.now(tz=UTC).date() + timedelta(days=1)
        mock_ts = MagicMock()
        mock_ts.date.return_value = future_date

        mock_yf = MagicMock()
        mock_yf.Ticker.return_value.calendar = {"Earnings Date": mock_ts}

        cal = _make_calendar(tmp_path)
        with patch.dict("sys.modules", {"yfinance": mock_yf}):
            result = cal._fetch_from_yfinance("AAPL")

        assert future_date in result

    def test_result_written_to_cache(self, tmp_path: Path) -> None:
        future_date = datetime.now(tz=UTC).date() + timedelta(days=5)
        cal = _make_calendar(tmp_path)
        with patch("app.risk.earnings_calendar.EarningsCalendar._fetch_from_yfinance") as m:
            m.return_value = [future_date]
            cal.is_blackout("COIN")
        assert cal._cache_file.exists()
        cached = json.loads(cal._cache_file.read_text())
        assert "COIN" in cached
        assert future_date.isoformat() in cached["COIN"]["dates"]
