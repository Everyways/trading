"""JSON API endpoints for programmatic monitoring."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends
from sqlmodel import select

from app.web.auth import require_auth

router = APIRouter()


@router.get("/status")
def status(_: str = Depends(require_auth)) -> dict[str, Any]:
    """Quick health / state snapshot — useful for Uptime Kuma, scripts, etc."""
    from app.data.database import get_session
    from app.data.models import KillSwitch, Order, Strategy, Trade

    result: dict[str, Any] = {
        "ok": True,
        "timestamp": datetime.now(tz=UTC).isoformat(),
        "kill_switch_engaged": False,
        "kill_switch_reason": None,
        "monthly_loss_eur": 0.0,
        "today_pnl_usd": 0.0,
        "today_trades": 0,
        "open_positions": 0,
        "strategies": [],
    }

    try:
        with get_session() as session:
            # Kill switch
            ks = session.exec(
                select(KillSwitch).where(KillSwitch.engaged == True)  # noqa: E712
            ).first()
            if ks:
                result["kill_switch_engaged"] = True
                result["kill_switch_reason"] = ks.reason

            # Monthly PnL
            month_start = datetime.now(tz=UTC).replace(
                day=1, hour=0, minute=0, second=0, microsecond=0
            )
            monthly = session.exec(
                select(Trade).where(Trade.entry_time >= month_start)
            ).all()
            monthly_pnl = sum(float(t.pnl_net or 0) for t in monthly)
            if monthly_pnl < 0:
                result["monthly_loss_eur"] = round(abs(monthly_pnl) * 0.92, 2)

            # Today PnL
            today_start = datetime.now(tz=UTC).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            today = [t for t in monthly if t.entry_time and t.entry_time >= today_start]
            result["today_pnl_usd"] = round(sum(float(t.pnl_net or 0) for t in today), 2)
            result["today_trades"] = len(today)

            # Strategies
            strategies = session.exec(select(Strategy)).all()
            result["strategies"] = [
                {"name": s.name, "enabled": s.enabled, "mode": s.mode}
                for s in strategies
            ]

            # Recent filled orders → open positions approximation
            filled = session.exec(
                select(Order).where(Order.status == "filled").order_by(
                    Order.filled_at.desc()  # type: ignore[arg-type]
                ).limit(100)
            ).all()
            result["open_positions"] = sum(
                1 for o in filled if o.side == "buy"
            ) - sum(1 for o in filled if o.side == "sell")

    except Exception as exc:
        result["ok"] = False
        result["error"] = str(exc)

    return result
