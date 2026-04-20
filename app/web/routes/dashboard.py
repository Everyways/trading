"""HTML dashboard routes."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import select

from app.web.auth import require_auth

router = APIRouter()
_templates = Jinja2Templates(directory=Path(__file__).parent.parent / "templates")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt(value: object, prefix: str = "$", decimals: int = 2) -> str:
    """Format a Decimal / float as a currency string."""
    if value is None:
        return "—"
    try:
        v = float(value)
        sign = "+" if v > 0 else ""
        return f"{sign}{prefix}{v:,.{decimals}f}"
    except (TypeError, ValueError):
        return str(value)


def _load_dashboard_data() -> dict[str, Any]:
    """Synchronous DB read — returns all data needed by the template."""
    from app.data.database import get_session
    from app.data.models import (
        Instrument,
        KillSwitch,
        Order,
        PositionSnapshot,
        RiskEvent,
        Strategy,
        Trade,
    )

    data: dict[str, Any] = {
        "now": datetime.now(tz=UTC).strftime("%Y-%m-%d %H:%M UTC"),
        "kill_switch": False,
        "kill_switch_reason": "",
        "monthly_loss_eur": 0.0,
        "today_pnl": 0.0,
        "today_trades": 0,
        "strategies": [],
        "positions": [],
        "recent_trades": [],
        "risk_events": [],
        "recent_orders": [],
        "error": None,
    }

    try:
        with get_session() as session:
            # ── Lookup dicts ─────────────────────────────────────────────
            strategies_db = session.exec(select(Strategy)).all()
            strategy_map = {s.id: s.name for s in strategies_db}
            instruments_db = session.exec(select(Instrument)).all()
            instrument_map = {i.id: i.symbol for i in instruments_db}

            # ── Kill switch ───────────────────────────────────────────────
            ks = session.exec(
                select(KillSwitch).where(KillSwitch.engaged == True)  # noqa: E712
            ).first()
            if ks:
                data["kill_switch"] = True
                data["kill_switch_reason"] = ks.reason or ""

            # ── Today's PnL (trades closed today) ────────────────────────
            today_start = datetime.now(tz=UTC).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            today_closed = session.exec(
                select(Trade).where(Trade.exit_time >= today_start)
            ).all()
            data["today_pnl"] = sum(float(t.pnl_net or 0) for t in today_closed)
            data["today_trades"] = len(today_closed)

            # ── Monthly loss (EUR) — trades closed this month ─────────────
            month_start = today_start.replace(day=1)
            monthly_trades = session.exec(
                select(Trade).where(Trade.exit_time >= month_start)
            ).all()
            monthly_pnl = sum(float(t.pnl_net or 0) for t in monthly_trades)
            eur_per_usd = 0.92
            if monthly_pnl < 0:
                data["monthly_loss_eur"] = abs(monthly_pnl) * eur_per_usd

            # ── Strategies ───────────────────────────────────────────────
            data["strategies"] = [
                {
                    "name": s.name,
                    "version": s.version,
                    "enabled": s.enabled,
                    "mode": s.mode,
                    "last_loaded": s.last_loaded_at.strftime("%Y-%m-%d %H:%M")
                    if s.last_loaded_at
                    else "—",
                }
                for s in strategies_db
            ]

            # ── Latest positions (dedup by strategy+instrument) ────────────
            recent_snaps = session.exec(
                select(PositionSnapshot)
                .order_by(PositionSnapshot.time.desc())
                .limit(200)
            ).all()
            seen: set[tuple[int, int]] = set()
            positions = []
            for snap in recent_snaps:
                key = (snap.strategy_id, snap.instrument_id)
                if key in seen:
                    continue
                seen.add(key)
                qty = float(snap.qty or 0)
                if abs(qty) < 1e-9:
                    continue   # skip flat positions
                pnl = float(snap.unrealized_pnl or 0)
                positions.append({
                    "symbol": instrument_map.get(snap.instrument_id, f"#{snap.instrument_id}"),
                    "strategy": strategy_map.get(snap.strategy_id, f"#{snap.strategy_id}"),
                    "qty": f"{qty:.3f}",
                    "avg_entry": f"{float(snap.avg_entry or 0):.2f}",
                    "unrealized_pnl": pnl,
                    "unrealized_pnl_fmt": _fmt(pnl),
                    "pnl_class": "pos" if pnl >= 0 else "neg",
                    "time": snap.time.strftime("%H:%M"),
                })
            data["positions"] = positions

            # ── Recent trades (with per-row cumulative PnL) ───────────────
            trades_db = session.exec(
                select(Trade).order_by(Trade.exit_time.desc()).limit(50)
            ).all()
            # Compute running cumulative PnL oldest→newest, then reverse for display
            pnl_values = [float(t.pnl_net or 0) for t in reversed(trades_db)]
            cumulative: list[float] = []
            running = 0.0
            for v in pnl_values:
                running += v
                cumulative.append(running)
            cumulative.reverse()   # align with newest-first order
            data["recent_trades"] = [
                {
                    "strategy": strategy_map.get(t.strategy_id, "—"),
                    "symbol": instrument_map.get(t.instrument_id, "—"),
                    "side": (t.side or "").upper(),
                    "qty": f"{float(t.qty or 0):.3f}",
                    "entry_price": f"{float(t.entry_price or 0):.2f}",
                    "exit_price": f"{float(t.exit_price or 0):.2f}",
                    "pnl": float(t.pnl_net or 0),
                    "pnl_fmt": _fmt(t.pnl_net),
                    "pnl_class": "pos" if (t.pnl_net or 0) > 0 else "neg",
                    "cumulative_pnl_fmt": _fmt(cumulative[i]),
                    "cumulative_pnl_class": "pos" if cumulative[i] >= 0 else "neg",
                    "duration": f"{(t.duration_seconds or 0) // 60}m",
                    "time": t.exit_time.strftime("%m-%d %H:%M") if t.exit_time else "—",
                }
                for i, t in enumerate(trades_db)
            ]

            # ── Risk events ───────────────────────────────────────────────
            events = session.exec(
                select(RiskEvent).order_by(RiskEvent.time.desc()).limit(15)
            ).all()
            data["risk_events"] = [
                {
                    "time": e.time.strftime("%m-%d %H:%M"),
                    "severity": e.severity,
                    "type": (e.event_type or "").replace("_", " "),
                    "message": e.message or "",
                }
                for e in events
            ]

            # ── Recent orders ──────────────────────────────────────────────
            orders = session.exec(
                select(Order).order_by(Order.submitted_at.desc()).limit(20)
            ).all()
            data["recent_orders"] = [
                {
                    "time": o.submitted_at.strftime("%m-%d %H:%M") if o.submitted_at else "—",
                    "symbol": instrument_map.get(o.instrument_id, "—"),
                    "strategy": strategy_map.get(o.strategy_id, "—"),
                    "side": (o.side or "").upper(),
                    "qty": f"{float(o.qty or 0):.3f}",
                    "status": o.status,
                    "fill_price": f"{float(o.avg_fill_price or 0):.2f}"
                    if o.avg_fill_price
                    else "—",
                }
                for o in orders
            ]

    except Exception as exc:
        data["error"] = str(exc)

    return data


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, _: str = Depends(require_auth)) -> HTMLResponse:
    data = _load_dashboard_data()
    return _templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context=data
    )
