"""JSON API endpoints for programmatic monitoring."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import select

from app.web.auth import require_auth

router = APIRouter()


@router.get("/status")
def status(_: str = Depends(require_auth)) -> dict[str, Any]:
    """Quick health / state snapshot — useful for Uptime Kuma, scripts, etc."""
    from app.data.database import get_session
    from app.data.models import KillSwitch, Strategy, Trade

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

            # Open positions from latest PositionSnapshot (one row per strategy/instrument)
            from app.data.models import PositionSnapshot
            recent_snaps = session.exec(
                select(PositionSnapshot).order_by(PositionSnapshot.time.desc()).limit(200)
            ).all()
            seen_pos: set[tuple[int, int]] = set()
            open_count = 0
            for snap in recent_snaps:
                key = (snap.strategy_id, snap.instrument_id)
                if key in seen_pos:
                    continue
                seen_pos.add(key)
                if abs(float(snap.qty or 0)) > 1e-9:
                    open_count += 1
            result["open_positions"] = open_count

    except Exception as exc:
        result["ok"] = False
        result["error"] = str(exc)

    return result


@router.post("/stop")
def emergency_stop(reason: str = "API /stop", _: str = Depends(require_auth)) -> dict[str, str]:
    """Engage the emergency stop: creates the KILL sentinel file.

    The trading runner checks for this file on every tick and engages the
    global kill switch immediately. Positions are liquidated on next tick.
    """
    from app.config import get_settings
    kill_file = Path(get_settings().kill_switch_file)
    kill_file.touch()
    return {"status": "stopped", "kill_file": str(kill_file), "reason": reason}


@router.post("/resume")
def emergency_resume(_: str = Depends(require_auth)) -> dict[str, str]:
    """Reset the emergency stop: deletes the KILL sentinel file.

    The in-memory kill switch is NOT reset here — only the file is removed.
    Use the Telegram /resume command or restart the process to clear the
    in-memory flag after verifying it is safe to resume trading.
    """
    from app.config import get_settings
    kill_file = Path(get_settings().kill_switch_file)
    if not kill_file.exists():
        raise HTTPException(
            status_code=409,
            detail="KILL file does not exist — bot is not stopped via file",
        )
    kill_file.unlink()
    return {
        "status": "file_removed",
        "kill_file": str(kill_file),
        "note": "Send /resume via Telegram to also reset the in-memory kill switch",
    }
