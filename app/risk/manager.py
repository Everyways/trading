"""Risk management — gates every order submission.

RiskManager maintains in-memory state (daily PnL, order rate, day-trade count)
refreshed from DB at startup. It is intentionally synchronous so it can be
called on the hot path without async overhead.

Hierarchy of checks (first failure wins):
  1. Global kill switch
  2. Strategy kill switch
  3. Monthly hard stop (50€ — permanent)
  4. Global daily loss limit
  5. Strategy daily loss limit  → pauses strategy for the day
  6. Max concurrent positions (per strategy)
  7. PDT compliance (3 day-trades per 5 rolling days)
  8. Order rate limit (per strategy per minute)
"""

from __future__ import annotations

import logging
from collections import deque
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlmodel import Session, select

from app.core.domain import OrderRequest, Position
from app.core.enums import OrderSide, RiskEventType, RiskSeverity
from app.data.models import KillSwitch as KillSwitchModel
from app.data.models import RiskEvent
from app.risk.earnings_calendar import EarningsCalendar

log = logging.getLogger(__name__)

# Approximate EUR/USD conversion. Override via global_config if needed.
_FALLBACK_EUR_PER_USD = Decimal("0.92")


class _StrategyState:
    """Per-strategy mutable risk state."""

    __slots__ = (
        "daily_pnl",
        "day_trade_times",
        "order_times",
        "paused_today",
    )

    def __init__(self) -> None:
        self.daily_pnl: Decimal = Decimal("0")
        self.day_trade_times: deque[datetime] = deque()  # rolling 5-day window
        self.order_times: deque[datetime] = deque()  # rolling 60-second window
        self.paused_today: bool = False  # strategy daily stop hit


class RiskManager:
    """Central risk gate for order submission.

    Args:
        session:        SQLModel Session for DB audit writes.
        global_config:  Parsed content of config/risk_global.yaml.
        eur_per_usd:    FX rate used to convert USD PnL → EUR for monthly stop.
    """

    def __init__(
        self,
        session: Session,
        global_config: dict[str, Any],
        eur_per_usd: Decimal = _FALLBACK_EUR_PER_USD,
    ) -> None:
        self._session = session
        self._g = global_config.get("global_limits", {})
        self._pdt_cfg = global_config.get("pdt_compliance", {})
        self._eur_per_usd = eur_per_usd

        self._global_kill: bool = False
        self._monthly_loss_eur: Decimal = Decimal("0")
        self._daily_global_pnl: Decimal = Decimal("0")
        self._today: date = datetime.now(tz=UTC).date()
        self._states: dict[str, _StrategyState] = {}
        self._earnings = EarningsCalendar()

        self._load_from_db()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def check_order(
        self,
        order: OrderRequest,  # noqa: ARG002 — reserved for future symbol-level checks
        account_equity: Decimal,
        open_positions: list[Position],
        strategy_name: str,
        strategy_risk: dict[str, Any],
    ) -> tuple[bool, str]:
        """Gate an order through all risk checks.

        Returns ``(True, "")`` when allowed, ``(False, reason)`` when blocked.
        Thread-safe (no shared mutable state outside this object).
        """
        state = self._state(strategy_name)

        # 1. Global kill switch
        if self._global_kill:
            return False, "global kill switch engaged"

        # 2. Strategy kill switch (paused for the day)
        if state.paused_today:
            return False, f"{strategy_name}: paused for today (daily loss limit hit)"

        # 3. Monthly hard stop (EUR)
        max_monthly = Decimal(str(self._g.get("max_monthly_loss_eur", 50)))
        if self._monthly_loss_eur >= max_monthly:
            self._engage_global_kill(
                reason=f"monthly loss {self._monthly_loss_eur:.2f}€ ≥ {max_monthly}€",
                event_type=RiskEventType.MONTHLY_LOSS_LIMIT,
            )
            return False, f"monthly hard stop: {self._monthly_loss_eur:.2f}€ ≥ {max_monthly}€"

        # 4. Global daily loss
        max_daily_pct = Decimal(str(self._g.get("max_daily_loss_pct", 3.0)))
        if account_equity > 0:
            global_daily_pct = -self._daily_global_pnl / account_equity * 100
            if global_daily_pct >= max_daily_pct:
                return False, (f"global daily loss {global_daily_pct:.2f}% ≥ {max_daily_pct}%")

        # 5. Strategy daily loss → pause strategy
        max_strat_daily_pct = Decimal(str(strategy_risk.get("max_daily_loss_pct", 2.0)))
        if account_equity > 0:
            strat_daily_pct = -state.daily_pnl / account_equity * 100
            if strat_daily_pct >= max_strat_daily_pct:
                state.paused_today = True
                self._log_risk_event(
                    event_type=RiskEventType.DAILY_LOSS_LIMIT,
                    severity=RiskSeverity.WARN,
                    scope="strategy",
                    message=(
                        f"{strategy_name}: daily loss "
                        f"{strat_daily_pct:.2f}% ≥ {max_strat_daily_pct}%"
                    ),
                )
                return False, (
                    f"{strategy_name}: daily loss {strat_daily_pct:.2f}% ≥ {max_strat_daily_pct}%"
                )

        # 6. Max concurrent positions (per strategy)
        live_count = sum(1 for p in open_positions if not p.is_flat)
        max_pos = int(strategy_risk.get("max_concurrent_positions", 2))
        if live_count >= max_pos:
            return False, f"{strategy_name}: at max positions ({live_count}/{max_pos})"

        # 7. PDT compliance
        if self._pdt_cfg.get("enabled", True):
            max_dt = int(self._pdt_cfg.get("max_day_trades_per_5d", 3))
            recent = self._count_recent_day_trades(state)
            if recent >= max_dt:
                return False, f"PDT: {recent} day trades in 5 days (max {max_dt})"

        # 8. Order rate limit (per strategy)
        max_rate = int(strategy_risk.get("max_orders_per_minute", 5))
        if not self._under_rate_limit(state, max_rate):
            return False, f"{strategy_name}: order rate limit {max_rate}/min exceeded"

        # 9. Earnings blackout — block BUY orders within 2 trading days of earnings.
        #    SELL signals always pass so existing positions can be closed normally.
        if order.side == OrderSide.BUY and self._earnings.is_blackout(order.symbol):
            reason = f"{order.symbol}: earnings blackout active"
            self._log_risk_event(
                event_type=RiskEventType.DAILY_LOSS_LIMIT,
                severity=RiskSeverity.INFO,
                scope="strategy",
                message=reason,
            )
            return False, reason

        return True, ""

    def record_order_submitted(self, strategy_name: str) -> None:
        """Call immediately after submitting an order (for rate limiting)."""
        self._state(strategy_name).order_times.append(datetime.now(tz=UTC))

    def record_fill(
        self,
        realised_pnl: Decimal,
        strategy_name: str,
        is_closing: bool = False,
    ) -> None:
        """Update daily PnL after a fill.

        Only closing fills (sell on a long, buy on a short) count as realised PnL.
        """
        if not is_closing:
            return
        state = self._state(strategy_name)
        state.daily_pnl += realised_pnl
        self._daily_global_pnl += realised_pnl
        if realised_pnl < 0:
            self._monthly_loss_eur += -realised_pnl * self._eur_per_usd

    def record_day_trade(self, strategy_name: str) -> None:
        """Record a day trade (open + close same session) for PDT tracking."""
        self._state(strategy_name).day_trade_times.append(datetime.now(tz=UTC))

    def engage_kill_switch(
        self,
        scope: str,
        strategy_name: str | None = None,
        reason: str = "",
    ) -> None:
        """Manually engage a kill switch (e.g. from CLI or dashboard)."""
        if scope == "global":
            self._engage_global_kill(reason=reason, event_type=RiskEventType.KILL_SWITCH_ENGAGED)
        elif scope == "strategy" and strategy_name:
            self._state(strategy_name).paused_today = True
            log.warning("Strategy kill switch: %s — %s", strategy_name, reason)

    def reset_kill_switch(self, reason: str = "", reset_by: str = "operator") -> None:
        """Reset the global kill switch. Clears in-memory flag and persists to DB.

        IMPORTANT: After reset, the bot can process ticks again immediately.
        The caller is responsible for verifying it is safe to resume trading.
        """
        if not self._global_kill:
            log.info("Kill switch reset requested but switch is not engaged — no-op")
            return
        self._global_kill = False
        full_reason = f"RESET by {reset_by}: {reason}" if reason else f"RESET by {reset_by}"
        log.critical("GLOBAL KILL SWITCH RESET — %s", full_reason)
        self._log_risk_event(
            event_type=RiskEventType.KILL_SWITCH_ENGAGED,
            severity=RiskSeverity.WARNING,
            scope="global",
            message=full_reason,
        )
        try:
            stmt = select(KillSwitchModel).where(
                KillSwitchModel.scope == "global",
                KillSwitchModel.strategy_id == None,  # noqa: E711
            )
            existing = self._session.exec(stmt).first()
            if existing:
                existing.engaged = False
                existing.reason = full_reason
                self._session.add(existing)
                self._session.commit()
        except Exception:
            log.exception("Failed to persist kill switch reset to DB")

    def is_halted(self, strategy_name: str | None = None) -> bool:
        """True if global kill or (strategy_name provided) strategy is paused."""
        if self._global_kill:
            return True
        if strategy_name:
            return self._state(strategy_name).paused_today
        return False

    def reset_daily_state(self) -> None:
        """Reset intra-day counters. Call at each market open."""
        self._today = datetime.now(tz=UTC).date()
        self._daily_global_pnl = Decimal("0")
        for state in self._states.values():
            state.daily_pnl = Decimal("0")
            state.paused_today = False
            state.order_times.clear()
        log.info("Daily risk state reset for %s", self._today)

    def reset_monthly_state(self) -> None:
        """Reset monthly loss counter. Call on the 1st of each month at 00:00 UTC.

        Without this the in-memory _monthly_loss_eur accumulates across calendar
        months and would permanently block trading after the first losing month.
        """
        self._monthly_loss_eur = Decimal("0")
        # Also re-read from DB to pick up any trades that landed just before midnight
        self._load_from_db()
        log.info("Monthly risk state reset (month boundary)")

    @property
    def monthly_loss_eur(self) -> Decimal:
        return self._monthly_loss_eur

    @property
    def global_kill_engaged(self) -> bool:
        return self._global_kill

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _state(self, strategy_name: str) -> _StrategyState:
        if strategy_name not in self._states:
            self._states[strategy_name] = _StrategyState()
        return self._states[strategy_name]

    def _count_recent_day_trades(self, state: _StrategyState) -> int:
        cutoff = datetime.now(tz=UTC) - timedelta(days=5)
        while state.day_trade_times and state.day_trade_times[0] < cutoff:
            state.day_trade_times.popleft()
        return len(state.day_trade_times)

    def _under_rate_limit(self, state: _StrategyState, max_per_minute: int) -> bool:
        cutoff = datetime.now(tz=UTC) - timedelta(seconds=60)
        while state.order_times and state.order_times[0] < cutoff:
            state.order_times.popleft()
        return len(state.order_times) < max_per_minute

    def _engage_global_kill(
        self,
        reason: str,
        event_type: RiskEventType = RiskEventType.KILL_SWITCH_ENGAGED,
    ) -> None:
        if self._global_kill:
            return  # already engaged
        self._global_kill = True
        log.critical("GLOBAL KILL SWITCH ENGAGED: %s", reason)
        self._log_risk_event(
            event_type=event_type,
            severity=RiskSeverity.CRITICAL,
            scope="global",
            message=reason,
        )
        # Upsert kill switch record in DB
        try:
            stmt = select(KillSwitchModel).where(
                KillSwitchModel.scope == "global",
                KillSwitchModel.strategy_id == None,  # noqa: E711
            )
            existing = self._session.exec(stmt).first()
            if existing:
                existing.engaged = True
                existing.engaged_at = datetime.now(tz=UTC)
                existing.engaged_by = "risk_manager"
                existing.reason = reason
                self._session.add(existing)
            else:
                sw = KillSwitchModel(
                    scope="global",
                    engaged=True,
                    engaged_at=datetime.now(tz=UTC),
                    engaged_by="risk_manager",
                    reason=reason,
                )
                self._session.add(sw)
            self._session.commit()
        except Exception:
            log.exception("Failed to persist kill switch to DB")

    def _log_risk_event(
        self,
        event_type: RiskEventType,
        severity: RiskSeverity,
        scope: str,
        message: str,
    ) -> None:
        try:
            event = RiskEvent(
                time=datetime.now(tz=UTC),
                scope=scope,
                event_type=event_type.value,
                severity=severity.value,
                message=message,
            )
            self._session.add(event)
            self._session.commit()
        except Exception:
            log.exception("Failed to persist risk event to DB")

    def _load_from_db(self) -> None:
        """Load global kill switch state and monthly PnL from DB at startup."""
        try:
            # Global kill switch
            stmt = select(KillSwitchModel).where(
                KillSwitchModel.scope == "global",
                KillSwitchModel.engaged == True,  # noqa: E712
            )
            if self._session.exec(stmt).first():
                self._global_kill = True
                log.warning("Global kill switch is ENGAGED (loaded from DB)")

            # Monthly PnL from trades table
            from app.data.models import Trade  # local import to avoid circular

            now = datetime.now(tz=UTC)
            month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            stmt_trades = select(Trade).where(Trade.entry_time >= month_start)
            trades = self._session.exec(stmt_trades).all()
            monthly_pnl_usd = sum((t.pnl_net or Decimal("0")) for t in trades)
            if monthly_pnl_usd < 0:
                self._monthly_loss_eur = -monthly_pnl_usd * self._eur_per_usd
            log.info(
                "RiskManager: monthly_loss_eur=%.2f, global_kill=%s",
                self._monthly_loss_eur,
                self._global_kill,
            )
        except Exception:
            log.exception("Failed to load risk state from DB — starting with defaults")
