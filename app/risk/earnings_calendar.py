"""Earnings blackout calendar — blocks BUY orders near earnings dates.

Fetches upcoming earnings dates from yfinance (free, Yahoo Finance scrape).
Results are cached in ``data/earnings_cache.json`` for 24 hours to avoid
hammering Yahoo on every tick.

Manual override file: ``data/earnings_overrides.yaml``
Format::

    AAPL:
      - "2025-01-30"
      - "2025-04-24"
    TSLA:
      - "2025-01-29"

Blackout rule: block BUY signals on the earnings date itself and the
2 preceding trading days.  SELL signals are never blocked.

Graceful degradation: if yfinance is unavailable (import error, network
failure, Yahoo throttle), the method returns False (no blackout imposed).
The kill switch and monthly loss cap remain the real safety nets.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

_CACHE_FILE = Path("data/earnings_cache.json")
_OVERRIDE_FILE = Path("config/earnings_overrides.yaml")
_CACHE_TTL_HOURS = 24
_BLACKOUT_TRADING_DAYS = 2


def _is_trading_day(d: date) -> bool:
    """Return True for weekdays (Mon–Fri). Does not account for holidays."""
    return d.weekday() < 5


def _trading_days_until(target: date, from_date: date) -> int:
    """Count trading days between *from_date* (exclusive) and *target* (inclusive)."""
    days = 0
    current = from_date
    while current < target:
        current += timedelta(days=1)
        if _is_trading_day(current):
            days += 1
    return days


class EarningsCalendar:
    """Maintain and query upcoming earnings dates for a set of symbols.

    Args:
        cache_file:   Path for the 24-hour JSON cache (created on first use).
        override_file: Path to the YAML override file (optional, no-op if absent).
        blackout_days: Number of *trading* days before earnings to block BUY orders.
    """

    def __init__(
        self,
        cache_file: Path = _CACHE_FILE,
        override_file: Path = _OVERRIDE_FILE,
        blackout_days: int = _BLACKOUT_TRADING_DAYS,
    ) -> None:
        self._cache_file = cache_file
        self._override_file = override_file
        self._blackout_days = blackout_days
        self._cache: dict[str, Any] = self._load_cache()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def is_blackout(self, symbol: str, check_date: date | None = None) -> bool:
        """Return True if *symbol* has an earnings within the blackout window.

        Args:
            symbol:     Stock ticker (case-insensitive).
            check_date: Date to check (defaults to today UTC).

        Returns:
            True if a BUY order for *symbol* should be blocked.
        """
        if check_date is None:
            check_date = datetime.now(tz=UTC).date()

        symbol = symbol.upper()
        try:
            earnings_dates = self._get_earnings_dates(symbol)
        except Exception:
            log.warning("Earnings check failed for %s — skipping blackout", symbol, exc_info=True)
            return False

        for ed in earnings_dates:
            # Block if check_date == earnings date OR within blackout_days before it
            if ed >= check_date:
                trading_days_away = _trading_days_until(ed, check_date)
                if trading_days_away <= self._blackout_days:
                    log.info(
                        "Earnings blackout: %s earnings on %s (%d trading days away — blocked)",
                        symbol,
                        ed,
                        trading_days_away,
                    )
                    return True
        return False

    def prefetch(self, symbols: list[str]) -> None:
        """Pre-warm the cache for a list of symbols (call at startup)."""
        for symbol in symbols:
            self._get_earnings_dates(symbol.upper())

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _get_earnings_dates(self, symbol: str) -> list[date]:
        """Return upcoming earnings dates for *symbol* from cache or yfinance."""
        overrides = self._load_overrides()
        if symbol in overrides:
            return [date.fromisoformat(d) for d in overrides[symbol]]

        cached = self._cache.get(symbol)
        if cached and self._cache_is_fresh(cached.get("fetched_at", "")):
            return [date.fromisoformat(d) for d in cached.get("dates", [])]

        dates = self._fetch_from_yfinance(symbol)
        self._cache[symbol] = {
            "dates": [str(d) for d in dates],
            "fetched_at": datetime.now(tz=UTC).isoformat(),
        }
        self._save_cache()
        return dates

    def _fetch_from_yfinance(self, symbol: str) -> list[date]:
        """Fetch next earnings date from Yahoo Finance via yfinance."""
        try:
            import yfinance as yf  # optional dependency

            ticker = yf.Ticker(symbol)
            cal = ticker.calendar
            if cal is None:
                return []

            # yfinance >= 0.2 returns a dict with "Earnings Date" key
            if isinstance(cal, dict):
                raw = cal.get("Earnings Date") or cal.get("Earnings Date (Start)")
                if raw is None:
                    return []
                if hasattr(raw, "date"):  # Timestamp / datetime
                    return [raw.date()]
                if isinstance(raw, list):
                    return [
                        r.date() if hasattr(r, "date") else date.fromisoformat(str(r)) for r in raw
                    ]
                return []

            # Older yfinance: returns a DataFrame with index = field name
            if hasattr(cal, "loc"):
                try:
                    raw = cal.loc["Earnings Date"]
                    if hasattr(raw, "iloc"):
                        return [
                            v.date() if hasattr(v, "date") else date.fromisoformat(str(v))
                            for v in raw.iloc[:2]
                        ]
                except KeyError:
                    return []

            return []

        except ImportError:
            log.warning("yfinance not installed — earnings blackout disabled for %s", symbol)
            return []
        except Exception:
            log.warning(
                "yfinance fetch failed for %s — skipping earnings blackout",
                symbol,
                exc_info=True,
            )
            return []

    def _load_overrides(self) -> dict[str, list[str]]:
        if not self._override_file.exists():
            return {}
        try:
            raw = yaml.safe_load(self._override_file.read_text()) or {}
            return {k.upper(): [str(d) for d in v] for k, v in raw.items()}
        except Exception:
            log.warning("Could not load earnings overrides from %s", self._override_file)
            return {}

    def _load_cache(self) -> dict[str, Any]:
        if not self._cache_file.exists():
            return {}
        try:
            return json.loads(self._cache_file.read_text()) or {}
        except Exception:
            return {}

    def _save_cache(self) -> None:
        try:
            self._cache_file.parent.mkdir(parents=True, exist_ok=True)
            self._cache_file.write_text(json.dumps(self._cache, indent=2))
        except Exception:
            log.warning("Could not write earnings cache to %s", self._cache_file)

    def _cache_is_fresh(self, fetched_at: str) -> bool:
        if not fetched_at:
            return False
        try:
            fetched = datetime.fromisoformat(fetched_at)
            if fetched.tzinfo is None:
                fetched = fetched.replace(tzinfo=UTC)
            age_hours = (datetime.now(tz=UTC) - fetched).total_seconds() / 3600
            return age_hours < _CACHE_TTL_HOURS
        except ValueError:
            return False
